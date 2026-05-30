# FILE: platform_core/analytics/structure/displacement_engine.py
"""
platform_core.analytics.structure.displacement_engine
═══════════════════════════════════════════════════════════════════════
Production-grade causal displacement engine for finalized bar streams.

This module detects directional displacement legs using only finalized bars and
incremental rolling baselines. It preserves:
- no-hindsight computation,
- deterministic replay compatibility,
- streaming-safe incremental state,
- JSON-safe snapshots,
- integration compatibility with structure, BOS, CHOCH, FVG, and liquidity
  analytics.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from statistics import fmean

from platform_core.analytics.structure.models import DisplacementLeg
from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructureSide


class DisplacementEngineError(RuntimeError):
    """Base exception for displacement engine failures."""


@dataclass(frozen=True, slots=True)
class DisplacementEngineConfig:
    """Configuration for causal displacement detection."""

    instrument_id: str
    timeframe: str
    baseline_window_size: int = 20
    min_body_to_range_ratio: float = 0.60
    min_normalized_impulse_score: float = 1.50
    min_range_expansion_ratio: float = 1.25
    min_volume_ratio: float = 1.50
    max_merge_gap_bars: int = 1
    max_recent_legs: int = 256
    allow_neutral_close_extension: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.baseline_window_size <= 1:
            raise ValueError("baseline_window_size must be greater than 1")
        if not 0.0 <= self.min_body_to_range_ratio <= 1.0:
            raise ValueError("min_body_to_range_ratio must lie between 0 and 1")
        if self.min_normalized_impulse_score < 0:
            raise ValueError("min_normalized_impulse_score must be non-negative")
        if self.min_range_expansion_ratio < 0:
            raise ValueError("min_range_expansion_ratio must be non-negative")
        if self.min_volume_ratio < 0:
            raise ValueError("min_volume_ratio must be non-negative")
        if self.max_merge_gap_bars < 0:
            raise ValueError("max_merge_gap_bars must be non-negative")
        if self.max_recent_legs <= 0:
            raise ValueError("max_recent_legs must be positive")


@dataclass(frozen=True, slots=True)
class DisplacementBarMetrics:
    """Normalized metrics for a single finalized bar."""

    instrument_id: str
    timeframe: str
    bar_timestamp: datetime
    side: StructureSide
    bar_range: float
    body_size: float
    body_to_range_ratio: float
    normalized_impulse_score: float
    range_expansion_ratio: float
    volume_ratio: float | None
    qualifies_as_displacement_bar: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "bar_timestamp": _ensure_utc(self.bar_timestamp).isoformat(),
            "side": self.side.value,
            "bar_range": self.bar_range,
            "body_size": self.body_size,
            "body_to_range_ratio": self.body_to_range_ratio,
            "normalized_impulse_score": self.normalized_impulse_score,
            "range_expansion_ratio": self.range_expansion_ratio,
            "volume_ratio": self.volume_ratio,
            "qualifies_as_displacement_bar": self.qualifies_as_displacement_bar,
        }


@dataclass(frozen=True, slots=True)
class DisplacementEngineResult:
    """Output of processing one finalized bar."""

    metrics: DisplacementBarMetrics
    emitted_legs: tuple[DisplacementLeg, ...]
    active_leg: DisplacementLeg | None
    processed_bar_count: int
    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "metrics": self.metrics.to_dict(),
            "emitted_legs": [leg.to_dict() for leg in self.emitted_legs],
            "active_leg": self.active_leg.to_dict() if self.active_leg else None,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class DisplacementEngineState:
    """Serializable incremental engine state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    baseline_bars: tuple[dict[str, JsonValue], ...]
    recent_legs: tuple[dict[str, JsonValue], ...]
    active_leg: dict[str, JsonValue] | None
    active_leg_gap_count: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "baseline_bars": list(self.baseline_bars),
            "recent_legs": list(self.recent_legs),
            "active_leg": self.active_leg,
            "active_leg_gap_count": self.active_leg_gap_count,
        }


@dataclass(slots=True)
class _MutableLeg:
    """Internal mutable displacement leg accumulator."""

    leg_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    start_timestamp: datetime
    end_timestamp: datetime
    start_price: float
    end_price: float
    max_range_size: float
    max_body_to_range_ratio: float
    max_normalized_impulse_score: float
    max_volume_ratio: float | None
    bar_count: int = 1
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def to_model(self) -> DisplacementLeg:
        return DisplacementLeg(
            leg_id=self.leg_id,
            instrument_id=self.instrument_id,
            timeframe=self.timeframe,
            side=self.side,
            start_timestamp=self.start_timestamp,
            end_timestamp=self.end_timestamp,
            start_price=self.start_price,
            end_price=self.end_price,
            range_size=self.max_range_size,
            body_to_range_ratio=self.max_body_to_range_ratio,
            normalized_impulse_score=self.max_normalized_impulse_score,
            volume_ratio=self.max_volume_ratio,
            metadata={**self.metadata, "bar_count": self.bar_count},
        )


class DisplacementEngine:
    """Incremental causal displacement detector for finalized bars."""

    def __init__(self, *, config: DisplacementEngineConfig) -> None:
        self._config = config
        self._baseline_bars: deque[StructureBar] = deque(maxlen=config.baseline_window_size)
        self._recent_legs: deque[DisplacementLeg] = deque(maxlen=config.max_recent_legs)
        self._active_leg: _MutableLeg | None = None
        self._active_leg_gap_count = 0
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None

    @property
    def processed_bar_count(self) -> int:
        return self._processed_bar_count

    @property
    def recent_legs(self) -> tuple[DisplacementLeg, ...]:
        return tuple(self._recent_legs)

    @property
    def active_leg(self) -> DisplacementLeg | None:
        return self._active_leg.to_model() if self._active_leg else None

    @property
    def state(self) -> DisplacementEngineState:
        return DisplacementEngineState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            baseline_bars=tuple(bar.to_dict() for bar in self._baseline_bars),
            recent_legs=tuple(leg.to_dict() for leg in self._recent_legs),
            active_leg=self._active_leg.to_model().to_dict() if self._active_leg else None,
            active_leg_gap_count=self._active_leg_gap_count,
        )

    def reset(self) -> None:
        self._baseline_bars.clear()
        self._recent_legs.clear()
        self._active_leg = None
        self._active_leg_gap_count = 0
        self._processed_bar_count = 0
        self._last_bar_timestamp = None

    def process_bar(self, bar: StructureBar) -> DisplacementEngineResult:
        self._validate_bar(bar)
        metrics = self._compute_metrics(bar)
        emitted_legs: list[DisplacementLeg] = []
        if metrics.qualifies_as_displacement_bar:
            if self._active_leg is None:
                self._active_leg = self._start_leg(bar=bar, metrics=metrics)
                self._active_leg_gap_count = 0
            elif self._can_extend_active_leg(bar=bar, metrics=metrics):
                self._extend_leg(bar=bar, metrics=metrics)
                self._active_leg_gap_count = 0
            else:
                emitted_legs.append(self._finalize_active_leg())
                self._active_leg = self._start_leg(bar=bar, metrics=metrics)
                self._active_leg_gap_count = 0
        else:
            self._handle_non_displacement_bar(bar=bar, emitted_legs=emitted_legs)
        self._baseline_bars.append(bar)
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        return DisplacementEngineResult(
            metrics=metrics,
            emitted_legs=tuple(emitted_legs),
            active_leg=self._active_leg.to_model() if self._active_leg else None,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(self, bars: Iterable[StructureBar]) -> tuple[DisplacementEngineResult, ...]:
        return tuple(self.process_bar(bar) for bar in bars)

    def flush(self) -> tuple[DisplacementLeg, ...]:
        if self._active_leg is None:
            return ()
        leg = self._finalize_active_leg()
        return (leg,)

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise DisplacementEngineError("bar instrument_id does not match engine configuration")
        if bar.timeframe != self._config.timeframe:
            raise DisplacementEngineError("bar timeframe does not match engine configuration")
        current_timestamp = bar.bar_timestamp_utc
        if self._last_bar_timestamp is not None and current_timestamp <= self._last_bar_timestamp:
            raise DisplacementEngineError("bars must arrive in strictly increasing finalized timestamp order")

    def _compute_metrics(self, bar: StructureBar) -> DisplacementBarMetrics:
        side = bar.direction
        bar_range = bar.range_size
        body_size = bar.body_size
        body_to_range_ratio = body_size / bar_range if bar_range > 0 else 0.0
        baseline_range = self._baseline_average_range()
        normalized_impulse_score = body_size / baseline_range if baseline_range > 0 else 0.0
        range_expansion_ratio = bar_range / baseline_range if baseline_range > 0 else 0.0
        volume_ratio = self._volume_ratio(bar)
        qualifies = (
            side in (StructureSide.BULLISH, StructureSide.BEARISH)
            and body_to_range_ratio >= self._config.min_body_to_range_ratio
            and normalized_impulse_score >= self._config.min_normalized_impulse_score
            and range_expansion_ratio >= self._config.min_range_expansion_ratio
            and (volume_ratio is None or volume_ratio >= self._config.min_volume_ratio)
        )
        return DisplacementBarMetrics(
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            bar_timestamp=bar.bar_timestamp_utc,
            side=side,
            bar_range=bar_range,
            body_size=body_size,
            body_to_range_ratio=body_to_range_ratio,
            normalized_impulse_score=normalized_impulse_score,
            range_expansion_ratio=range_expansion_ratio,
            volume_ratio=volume_ratio,
            qualifies_as_displacement_bar=qualifies,
        )

    def _handle_non_displacement_bar(self, *, bar: StructureBar, emitted_legs: list[DisplacementLeg]) -> None:
        if self._active_leg is None:
            return
        if self._is_permitted_gap_bar(bar):
            self._active_leg_gap_count += 1
            self._active_leg.end_timestamp = bar.bar_timestamp_utc
            self._active_leg.end_price = bar.close_price
            return
        emitted_legs.append(self._finalize_active_leg())

    def _is_permitted_gap_bar(self, bar: StructureBar) -> bool:
        if self._active_leg is None:
            return False
        if self._active_leg_gap_count >= self._config.max_merge_gap_bars:
            return False
        if bar.direction == self._active_leg.side:
            return True
        if self._config.allow_neutral_close_extension and bar.direction == StructureSide.NEUTRAL:
            return True
        return False

    def _can_extend_active_leg(self, *, bar: StructureBar, metrics: DisplacementBarMetrics) -> bool:
        if self._active_leg is None:
            return False
        if metrics.side != self._active_leg.side:
            return False
        return True

    def _start_leg(self, *, bar: StructureBar, metrics: DisplacementBarMetrics) -> _MutableLeg:
        leg_id = (
            f"{bar.instrument_id}:{bar.timeframe}:displacement:{metrics.side.value}:"
            f"{bar.bar_timestamp_utc.isoformat()}"
        )
        return _MutableLeg(
            leg_id=leg_id,
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            side=metrics.side,
            start_timestamp=bar.bar_timestamp_utc,
            end_timestamp=bar.bar_timestamp_utc,
            start_price=bar.open_price,
            end_price=bar.close_price,
            max_range_size=bar.range_size,
            max_body_to_range_ratio=metrics.body_to_range_ratio,
            max_normalized_impulse_score=metrics.normalized_impulse_score,
            max_volume_ratio=metrics.volume_ratio,
            metadata={
                "initial_high_price": bar.high_price,
                "initial_low_price": bar.low_price,
                "baseline_window_size": self._config.baseline_window_size,
            },
        )

    def _extend_leg(self, *, bar: StructureBar, metrics: DisplacementBarMetrics) -> None:
        if self._active_leg is None:
            raise DisplacementEngineError("cannot extend leg when no active leg exists")
        self._active_leg.end_timestamp = bar.bar_timestamp_utc
        self._active_leg.end_price = bar.close_price
        self._active_leg.max_range_size = max(self._active_leg.max_range_size, bar.range_size)
        self._active_leg.max_body_to_range_ratio = max(
            self._active_leg.max_body_to_range_ratio,
            metrics.body_to_range_ratio,
        )
        self._active_leg.max_normalized_impulse_score = max(
            self._active_leg.max_normalized_impulse_score,
            metrics.normalized_impulse_score,
        )
        if metrics.volume_ratio is not None:
            if self._active_leg.max_volume_ratio is None:
                self._active_leg.max_volume_ratio = metrics.volume_ratio
            else:
                self._active_leg.max_volume_ratio = max(self._active_leg.max_volume_ratio, metrics.volume_ratio)
        self._active_leg.bar_count += 1

    def _finalize_active_leg(self) -> DisplacementLeg:
        if self._active_leg is None:
            raise DisplacementEngineError("cannot finalize a missing active leg")
        leg = self._active_leg.to_model()
        self._recent_legs.append(leg)
        self._active_leg = None
        self._active_leg_gap_count = 0
        return leg

    def _baseline_average_range(self) -> float:
        if not self._baseline_bars:
            return 0.0
        return fmean(bar.range_size for bar in self._baseline_bars)

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
    "DisplacementBarMetrics",
    "DisplacementEngine",
    "DisplacementEngineConfig",
    "DisplacementEngineError",
    "DisplacementEngineResult",
    "DisplacementEngineState",
]
