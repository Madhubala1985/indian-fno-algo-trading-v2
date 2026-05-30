# FILE: platform_core/analytics/structure/liquidity_sweep_detector.py
"""
platform_core.analytics.structure.liquidity_sweep_detector
═══════════════════════════════════════════════════════════════════════
Production-grade causal liquidity sweep detector for finalized bar streams.

This module detects liquidity sweeps against precomputed liquidity levels using
only finalized bars and optional confirmation windows. The implementation is
streaming-safe, replay-safe, and does not use future information.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from statistics import fmean

from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import LiquidityLevel
from platform_core.analytics.structure.models import LiquiditySweep
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructureSide
from platform_core.analytics.structure.models import SweepOutcome


class LiquiditySweepDetectorError(RuntimeError):
    """Base exception for liquidity sweep detector failures."""


@dataclass(frozen=True, slots=True)
class LiquiditySweepDetectorConfig:
    """Configuration for causal liquidity sweep detection."""

    instrument_id: str
    timeframe: str
    max_breach_ratio: float = 0.003
    min_wick_to_range_ratio: float = 0.30
    min_rejection_body_ratio: float = 0.20
    min_volume_ratio: float = 1.25
    confirmation_window_bars: int = 3
    baseline_window_size: int = 20
    max_active_candidates: int = 256
    max_recent_sweeps: int = 256
    require_close_back_inside: bool = True
    require_volume_confirmation: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.max_breach_ratio < 0:
            raise ValueError("max_breach_ratio must be non-negative")
        if not 0.0 <= self.min_wick_to_range_ratio <= 1.0:
            raise ValueError("min_wick_to_range_ratio must lie between 0 and 1")
        if not 0.0 <= self.min_rejection_body_ratio <= 1.0:
            raise ValueError("min_rejection_body_ratio must lie between 0 and 1")
        if self.min_volume_ratio < 0:
            raise ValueError("min_volume_ratio must be non-negative")
        if self.confirmation_window_bars <= 0:
            raise ValueError("confirmation_window_bars must be positive")
        if self.baseline_window_size <= 1:
            raise ValueError("baseline_window_size must be greater than 1")
        if self.max_active_candidates <= 0:
            raise ValueError("max_active_candidates must be positive")
        if self.max_recent_sweeps <= 0:
            raise ValueError("max_recent_sweeps must be positive")


@dataclass(frozen=True, slots=True)
class SweepDetectionMetrics:
    """Per-bar liquidity interaction metrics for a specific level."""

    level_id: str
    side: StructureSide
    breach_price: float
    breach_distance: float
    breach_ratio: float
    wick_to_range_ratio: float
    rejection_body_ratio: float
    volume_ratio: float | None
    closed_back_inside: bool
    qualifies_as_candidate: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "level_id": self.level_id,
            "side": self.side.value,
            "breach_price": self.breach_price,
            "breach_distance": self.breach_distance,
            "breach_ratio": self.breach_ratio,
            "wick_to_range_ratio": self.wick_to_range_ratio,
            "rejection_body_ratio": self.rejection_body_ratio,
            "volume_ratio": self.volume_ratio,
            "closed_back_inside": self.closed_back_inside,
            "qualifies_as_candidate": self.qualifies_as_candidate,
        }


@dataclass(frozen=True, slots=True)
class SweepCandidateSnapshot:
    """Serializable candidate snapshot."""

    sweep_id: str
    level_id: str
    side: StructureSide
    trigger_bar_timestamp: datetime
    expiry_bar_index: int
    breach_price: float
    breach_distance: float
    breach_ratio: float
    confirmation_score: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "sweep_id": self.sweep_id,
            "level_id": self.level_id,
            "side": self.side.value,
            "trigger_bar_timestamp": _ensure_utc(self.trigger_bar_timestamp).isoformat(),
            "expiry_bar_index": self.expiry_bar_index,
            "breach_price": self.breach_price,
            "breach_distance": self.breach_distance,
            "breach_ratio": self.breach_ratio,
            "confirmation_score": self.confirmation_score,
        }


@dataclass(frozen=True, slots=True)
class LiquiditySweepDetectionResult:
    """Output of processing one finalized bar."""

    emitted_sweeps: tuple[LiquiditySweep, ...]
    invalidated_sweep_ids: tuple[str, ...]
    active_candidate_count: int
    processed_bar_count: int
    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "emitted_sweeps": [sweep.to_dict() for sweep in self.emitted_sweeps],
            "invalidated_sweep_ids": list(self.invalidated_sweep_ids),
            "active_candidate_count": self.active_candidate_count,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class LiquiditySweepDetectorState:
    """Serializable detector state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    baseline_bars: tuple[dict[str, JsonValue], ...]
    active_candidates: tuple[dict[str, JsonValue], ...]
    recent_sweeps: tuple[dict[str, JsonValue], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "baseline_bars": list(self.baseline_bars),
            "active_candidates": list(self.active_candidates),
            "recent_sweeps": list(self.recent_sweeps),
        }


@dataclass(slots=True)
class _SweepCandidate:
    """Internal mutable sweep candidate."""

    sweep_id: str
    level: LiquidityLevel
    trigger_bar: StructureBar
    side: StructureSide
    breach_price: float
    breach_distance: float
    breach_ratio: float
    confirmation_score: float
    expiry_bar_index: int

    def to_snapshot(self) -> SweepCandidateSnapshot:
        return SweepCandidateSnapshot(
            sweep_id=self.sweep_id,
            level_id=self.level.level_id,
            side=self.side,
            trigger_bar_timestamp=self.trigger_bar.bar_timestamp_utc,
            expiry_bar_index=self.expiry_bar_index,
            breach_price=self.breach_price,
            breach_distance=self.breach_distance,
            breach_ratio=self.breach_ratio,
            confirmation_score=self.confirmation_score,
        )


class LiquiditySweepDetector:
    """Causal finalized-bar liquidity sweep detector."""

    def __init__(self, *, config: LiquiditySweepDetectorConfig) -> None:
        self._config = config
        self._baseline_bars: deque[StructureBar] = deque(maxlen=config.baseline_window_size)
        self._active_candidates: deque[_SweepCandidate] = deque(maxlen=config.max_active_candidates)
        self._recent_sweeps: deque[LiquiditySweep] = deque(maxlen=config.max_recent_sweeps)
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None

    @property
    def processed_bar_count(self) -> int:
        return self._processed_bar_count

    @property
    def recent_sweeps(self) -> tuple[LiquiditySweep, ...]:
        return tuple(self._recent_sweeps)

    @property
    def state(self) -> LiquiditySweepDetectorState:
        return LiquiditySweepDetectorState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            baseline_bars=tuple(bar.to_dict() for bar in self._baseline_bars),
            active_candidates=tuple(candidate.to_snapshot().to_dict() for candidate in self._active_candidates),
            recent_sweeps=tuple(sweep.to_dict() for sweep in self._recent_sweeps),
        )

    def reset(self) -> None:
        self._baseline_bars.clear()
        self._active_candidates.clear()
        self._recent_sweeps.clear()
        self._processed_bar_count = 0
        self._last_bar_timestamp = None

    def process_bar(
        self,
        *,
        bar: StructureBar,
        liquidity_levels: Iterable[LiquidityLevel],
    ) -> LiquiditySweepDetectionResult:
        self._validate_bar(bar)
        emitted_sweeps: list[LiquiditySweep] = []
        invalidated_sweep_ids: list[str] = []
        levels = tuple(self._filter_levels(liquidity_levels))
        for level in levels:
            metrics = self._compute_metrics(bar=bar, level=level)
            if metrics.qualifies_as_candidate:
                candidate = self._build_candidate(bar=bar, level=level, metrics=metrics)
                self._upsert_candidate(candidate)
        self._update_candidates(bar=bar, emitted_sweeps=emitted_sweeps, invalidated_sweep_ids=invalidated_sweep_ids)
        self._baseline_bars.append(bar)
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        return LiquiditySweepDetectionResult(
            emitted_sweeps=tuple(emitted_sweeps),
            invalidated_sweep_ids=tuple(invalidated_sweep_ids),
            active_candidate_count=len(self._active_candidates),
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(
        self,
        *,
        bars: Iterable[StructureBar],
        liquidity_levels_provider: callable,
    ) -> tuple[LiquiditySweepDetectionResult, ...]:
        results: list[LiquiditySweepDetectionResult] = []
        for bar in bars:
            levels = liquidity_levels_provider(bar)
            results.append(self.process_bar(bar=bar, liquidity_levels=levels))
        return tuple(results)

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise LiquiditySweepDetectorError("bar instrument_id does not match detector configuration")
        if bar.timeframe != self._config.timeframe:
            raise LiquiditySweepDetectorError("bar timeframe does not match detector configuration")
        current_timestamp = bar.bar_timestamp_utc
        if self._last_bar_timestamp is not None and current_timestamp <= self._last_bar_timestamp:
            raise LiquiditySweepDetectorError("bars must arrive in strictly increasing finalized timestamp order")

    def _filter_levels(self, liquidity_levels: Iterable[LiquidityLevel]) -> tuple[LiquidityLevel, ...]:
        filtered: list[LiquidityLevel] = []
        for level in liquidity_levels:
            if level.instrument_id != self._config.instrument_id:
                continue
            if level.timeframe != self._config.timeframe:
                continue
            filtered.append(level)
        return tuple(filtered)

    def _compute_metrics(self, *, bar: StructureBar, level: LiquidityLevel) -> SweepDetectionMetrics:
        side = level.side
        bar_range = max(bar.range_size, 1e-12)
        volume_ratio = self._volume_ratio(bar)
        if side == StructureSide.BEARISH:
            breach_price = bar.high_price
            breach_distance = max(0.0, bar.high_price - level.upper_bound_price)
            breach_ratio = breach_distance / level.reference_price if level.reference_price > 0 else 0.0
            wick_to_range_ratio = bar.upper_wick_size / bar_range
            rejection_body_ratio = max(0.0, (level.upper_bound_price - bar.close_price)) / bar_range
            closed_back_inside = bar.close_price <= level.upper_bound_price
        elif side == StructureSide.BULLISH:
            breach_price = bar.low_price
            breach_distance = max(0.0, level.lower_bound_price - bar.low_price)
            breach_ratio = breach_distance / level.reference_price if level.reference_price > 0 else 0.0
            wick_to_range_ratio = bar.lower_wick_size / bar_range
            rejection_body_ratio = max(0.0, (bar.close_price - level.lower_bound_price)) / bar_range
            closed_back_inside = bar.close_price >= level.lower_bound_price
        else:
            breach_price = level.reference_price
            breach_distance = 0.0
            breach_ratio = 0.0
            wick_to_range_ratio = 0.0
            rejection_body_ratio = 0.0
            closed_back_inside = False
        qualifies = (
            side in (StructureSide.BEARISH, StructureSide.BULLISH)
            and breach_distance > 0
            and breach_ratio <= self._config.max_breach_ratio
            and wick_to_range_ratio >= self._config.min_wick_to_range_ratio
            and rejection_body_ratio >= self._config.min_rejection_body_ratio
            and (not self._config.require_close_back_inside or closed_back_inside)
            and (
                not self._config.require_volume_confirmation
                or volume_ratio is not None and volume_ratio >= self._config.min_volume_ratio
            )
        )
        return SweepDetectionMetrics(
            level_id=level.level_id,
            side=side,
            breach_price=breach_price,
            breach_distance=breach_distance,
            breach_ratio=breach_ratio,
            wick_to_range_ratio=wick_to_range_ratio,
            rejection_body_ratio=rejection_body_ratio,
            volume_ratio=volume_ratio,
            closed_back_inside=closed_back_inside,
            qualifies_as_candidate=qualifies,
        )

    def _build_candidate(
        self,
        *,
        bar: StructureBar,
        level: LiquidityLevel,
        metrics: SweepDetectionMetrics,
    ) -> _SweepCandidate:
        confirmation_score = self._compute_confirmation_score(metrics=metrics)
        sweep_id = f"{bar.instrument_id}:{bar.timeframe}:sweep:{level.level_id}:{bar.bar_timestamp_utc.isoformat()}"
        return _SweepCandidate(
            sweep_id=sweep_id,
            level=level,
            trigger_bar=bar,
            side=metrics.side,
            breach_price=metrics.breach_price,
            breach_distance=metrics.breach_distance,
            breach_ratio=metrics.breach_ratio,
            confirmation_score=confirmation_score,
            expiry_bar_index=self._processed_bar_count + self._config.confirmation_window_bars,
        )

    def _compute_confirmation_score(self, *, metrics: SweepDetectionMetrics) -> float:
        volume_component = 0.0
        if metrics.volume_ratio is not None and self._config.min_volume_ratio > 0:
            volume_component = min(metrics.volume_ratio / self._config.min_volume_ratio, 2.0)
        return metrics.wick_to_range_ratio + metrics.rejection_body_ratio + volume_component

    def _upsert_candidate(self, candidate: _SweepCandidate) -> None:
        remaining: deque[_SweepCandidate] = deque(maxlen=self._config.max_active_candidates)
        replaced = False
        for existing in self._active_candidates:
            same_level = existing.level.level_id == candidate.level.level_id
            same_side = existing.side == candidate.side
            if same_level and same_side:
                remaining.append(candidate if candidate.confirmation_score >= existing.confirmation_score else existing)
                replaced = True
            else:
                remaining.append(existing)
        if not replaced:
            remaining.append(candidate)
        self._active_candidates = remaining

    def _update_candidates(
        self,
        *,
        bar: StructureBar,
        emitted_sweeps: list[LiquiditySweep],
        invalidated_sweep_ids: list[str],
    ) -> None:
        retained: deque[_SweepCandidate] = deque(maxlen=self._config.max_active_candidates)
        current_bar_index = self._processed_bar_count
        for candidate in self._active_candidates:
            outcome = self._evaluate_candidate(bar=bar, candidate=candidate)
            if outcome is None:
                if current_bar_index >= candidate.expiry_bar_index:
                    invalidated_sweep_ids.append(candidate.sweep_id)
                else:
                    retained.append(candidate)
                continue
            if outcome.outcome == SweepOutcome.CONFIRMED:
                self._recent_sweeps.append(outcome)
                emitted_sweeps.append(outcome)
            else:
                invalidated_sweep_ids.append(candidate.sweep_id)
        self._active_candidates = retained

    def _evaluate_candidate(self, *, bar: StructureBar, candidate: _SweepCandidate) -> LiquiditySweep | None:
        if candidate.side == StructureSide.BEARISH:
            confirmed = bar.close_price < candidate.level.lower_bound_price or bar.close_price < candidate.trigger_bar.low_price
            failed = bar.close_price > candidate.level.upper_bound_price and bar.high_price > candidate.breach_price
        elif candidate.side == StructureSide.BULLISH:
            confirmed = bar.close_price > candidate.level.upper_bound_price or bar.close_price > candidate.trigger_bar.high_price
            failed = bar.close_price < candidate.level.lower_bound_price and bar.low_price < candidate.breach_price
        else:
            return None
        if confirmed:
            return LiquiditySweep(
                sweep_id=candidate.sweep_id,
                instrument_id=self._config.instrument_id,
                timeframe=self._config.timeframe,
                side=candidate.side,
                level=candidate.level,
                trigger_bar=candidate.trigger_bar,
                breach_price=candidate.breach_price,
                breach_distance=candidate.breach_distance,
                breach_ratio=candidate.breach_ratio,
                reversal_close_price=bar.close_price,
                reversal_bar_timestamp=bar.bar_timestamp_utc,
                outcome=SweepOutcome.CONFIRMED,
                confirmation_score=candidate.confirmation_score,
                metadata={
                    "confirmation_bar_timestamp": bar.bar_timestamp_utc.isoformat(),
                    "confirmation_bar_close": bar.close_price,
                    "confirmation_bar_high": bar.high_price,
                    "confirmation_bar_low": bar.low_price,
                },
            )
        if failed:
            return LiquiditySweep(
                sweep_id=candidate.sweep_id,
                instrument_id=self._config.instrument_id,
                timeframe=self._config.timeframe,
                side=candidate.side,
                level=candidate.level,
                trigger_bar=candidate.trigger_bar,
                breach_price=candidate.breach_price,
                breach_distance=candidate.breach_distance,
                breach_ratio=candidate.breach_ratio,
                reversal_close_price=bar.close_price,
                reversal_bar_timestamp=bar.bar_timestamp_utc,
                outcome=SweepOutcome.FAILED,
                confirmation_score=candidate.confirmation_score,
                metadata={
                    "failure_bar_timestamp": bar.bar_timestamp_utc.isoformat(),
                    "failure_bar_close": bar.close_price,
                },
            )
        return None

    def _volume_ratio(self, bar: StructureBar) -> float | None:
        baseline_volumes = [history_bar.volume for history_bar in self._baseline_bars if history_bar.volume > 0]
        if not baseline_volumes:
            return None
        baseline_volume = fmean(baseline_volumes)
        if baseline_volume <= 0:
            return None
        return bar.volume / baseline_volume



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "LiquiditySweepDetectionResult",
    "LiquiditySweepDetector",
    "LiquiditySweepDetectorConfig",
    "LiquiditySweepDetectorError",
    "LiquiditySweepDetectorState",
    "SweepCandidateSnapshot",
    "SweepDetectionMetrics",
]
