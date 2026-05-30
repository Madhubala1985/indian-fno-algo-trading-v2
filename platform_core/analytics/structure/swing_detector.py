# FILE: platform_core/analytics/structure/swing_detector.py
"""
platform_core.analytics.structure.swing_detector
═══════════════════════════════════════════════════════════════════════
Production-grade causal swing detector for finalized bar streams.

This module detects confirmed swing highs and swing lows using only finalized
bars and explicit right-side confirmation windows. It is designed for:
- no-hindsight computation,
- incremental streaming updates,
- deterministic replay,
- JSON-safe state snapshots,
- downstream BOS/CHOCH/FVG/liquidity integration.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any

from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructurePoint
from platform_core.analytics.structure.models import StructurePointType
from platform_core.analytics.structure.models import StructureSide


class SwingDetectorError(RuntimeError):
    """Base exception for swing detector failures."""


@dataclass(frozen=True, slots=True)
class SwingDetectorConfig:
    """Configuration for causal swing detection."""

    instrument_id: str
    timeframe: str
    left_window_size: int = 2
    right_window_size: int = 2
    minimum_price_distance: float = 0.0
    minimum_strength_score: float = 0.0
    include_internal_structure: bool = True
    deduplicate_equal_extremes: bool = True
    max_recent_points: int = 256

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.left_window_size <= 0:
            raise ValueError("left_window_size must be positive")
        if self.right_window_size <= 0:
            raise ValueError("right_window_size must be positive")
        if self.minimum_price_distance < 0:
            raise ValueError("minimum_price_distance must be non-negative")
        if self.minimum_strength_score < 0:
            raise ValueError("minimum_strength_score must be non-negative")
        if self.max_recent_points <= 0:
            raise ValueError("max_recent_points must be positive")


@dataclass(frozen=True, slots=True)
class SwingDetectionResult:
    """Output of processing one finalized bar."""

    confirmed_points: tuple[StructurePoint, ...]
    processed_bar_count: int
    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "confirmed_points": [point.to_dict() for point in self.confirmed_points],
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class SwingDetectorState:
    """Serializable detector state for replay and persistence."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    buffered_bars: tuple[dict[str, JsonValue], ...]
    recent_points: tuple[dict[str, JsonValue], ...]
    last_confirmed_high_price: float | None = None
    last_confirmed_low_price: float | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "buffered_bars": list(self.buffered_bars),
            "recent_points": list(self.recent_points),
            "last_confirmed_high_price": self.last_confirmed_high_price,
            "last_confirmed_low_price": self.last_confirmed_low_price,
        }


class SwingDetector:
    """Causal swing detector using finalized-bar confirmation windows."""

    def __init__(self, *, config: SwingDetectorConfig) -> None:
        self._config = config
        self._buffer_size = config.left_window_size + config.right_window_size + 1
        self._bars: deque[StructureBar] = deque(maxlen=self._buffer_size)
        self._recent_points: deque[StructurePoint] = deque(maxlen=config.max_recent_points)
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None
        self._last_confirmed_high_price: float | None = None
        self._last_confirmed_low_price: float | None = None

    @property
    def processed_bar_count(self) -> int:
        return self._processed_bar_count

    @property
    def recent_points(self) -> tuple[StructurePoint, ...]:
        return tuple(self._recent_points)

    @property
    def state(self) -> SwingDetectorState:
        return SwingDetectorState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            buffered_bars=tuple(bar.to_dict() for bar in self._bars),
            recent_points=tuple(point.to_dict() for point in self._recent_points),
            last_confirmed_high_price=self._last_confirmed_high_price,
            last_confirmed_low_price=self._last_confirmed_low_price,
        )

    def reset(self) -> None:
        self._bars.clear()
        self._recent_points.clear()
        self._processed_bar_count = 0
        self._last_bar_timestamp = None
        self._last_confirmed_high_price = None
        self._last_confirmed_low_price = None

    def process_bar(self, bar: StructureBar) -> SwingDetectionResult:
        self._validate_bar(bar)
        self._bars.append(bar)
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        confirmed_points: list[StructurePoint] = []
        if len(self._bars) == self._buffer_size:
            candidate_index = self._config.left_window_size
            candidate_bar = self._bars[candidate_index]
            if self._is_confirmed_swing_high(candidate_index):
                point = self._build_point(candidate_bar=candidate_bar, point_type=StructurePointType.SWING_HIGH)
                if self._accept_point(point):
                    self._recent_points.append(point)
                    self._last_confirmed_high_price = point.price
                    confirmed_points.append(point)
            if self._is_confirmed_swing_low(candidate_index):
                point = self._build_point(candidate_bar=candidate_bar, point_type=StructurePointType.SWING_LOW)
                if self._accept_point(point):
                    self._recent_points.append(point)
                    self._last_confirmed_low_price = point.price
                    confirmed_points.append(point)
        return SwingDetectionResult(
            confirmed_points=tuple(confirmed_points),
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(self, bars: Iterable[StructureBar]) -> tuple[SwingDetectionResult, ...]:
        return tuple(self.process_bar(bar) for bar in bars)

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise SwingDetectorError("bar instrument_id does not match detector configuration")
        if bar.timeframe != self._config.timeframe:
            raise SwingDetectorError("bar timeframe does not match detector configuration")
        current_timestamp = bar.bar_timestamp_utc
        if self._last_bar_timestamp is not None and current_timestamp <= self._last_bar_timestamp:
            raise SwingDetectorError("bars must arrive in strictly increasing finalized timestamp order")

    def _is_confirmed_swing_high(self, candidate_index: int) -> bool:
        candidate = self._bars[candidate_index]
        candidate_high = candidate.high_price
        left_bars = list(self._bars)[:candidate_index]
        right_bars = list(self._bars)[candidate_index + 1 :]
        if any(left_bar.high_price > candidate_high for left_bar in left_bars):
            return False
        if any(right_bar.high_price >= candidate_high for right_bar in right_bars):
            return False
        if self._config.deduplicate_equal_extremes:
            if any(left_bar.high_price == candidate_high for left_bar in left_bars):
                return False
        strength_score = self._compute_high_strength(candidate_index)
        if strength_score < self._config.minimum_strength_score:
            return False
        if self._config.minimum_price_distance > 0 and self._last_confirmed_high_price is not None:
            if abs(candidate_high - self._last_confirmed_high_price) < self._config.minimum_price_distance:
                return False
        return True

    def _is_confirmed_swing_low(self, candidate_index: int) -> bool:
        candidate = self._bars[candidate_index]
        candidate_low = candidate.low_price
        left_bars = list(self._bars)[:candidate_index]
        right_bars = list(self._bars)[candidate_index + 1 :]
        if any(left_bar.low_price < candidate_low for left_bar in left_bars):
            return False
        if any(right_bar.low_price <= candidate_low for right_bar in right_bars):
            return False
        if self._config.deduplicate_equal_extremes:
            if any(left_bar.low_price == candidate_low for left_bar in left_bars):
                return False
        strength_score = self._compute_low_strength(candidate_index)
        if strength_score < self._config.minimum_strength_score:
            return False
        if self._config.minimum_price_distance > 0 and self._last_confirmed_low_price is not None:
            if abs(candidate_low - self._last_confirmed_low_price) < self._config.minimum_price_distance:
                return False
        return True

    def _compute_high_strength(self, candidate_index: int) -> float:
        bars = list(self._bars)
        candidate = bars[candidate_index]
        left_high_distance = max(candidate.high_price - bar.high_price for bar in bars[:candidate_index])
        right_high_distance = max(candidate.high_price - bar.high_price for bar in bars[candidate_index + 1 :])
        baseline_range = max(candidate.range_size, 1e-12)
        return (left_high_distance + right_high_distance) / baseline_range

    def _compute_low_strength(self, candidate_index: int) -> float:
        bars = list(self._bars)
        candidate = bars[candidate_index]
        left_low_distance = max(bar.low_price - candidate.low_price for bar in bars[:candidate_index])
        right_low_distance = max(bar.low_price - candidate.low_price for bar in bars[candidate_index + 1 :])
        baseline_range = max(candidate.range_size, 1e-12)
        return (left_low_distance + right_low_distance) / baseline_range

    def _build_point(self, *, candidate_bar: StructureBar, point_type: StructurePointType) -> StructurePoint:
        source_bar_index = self._processed_bar_count - self._config.right_window_size - 1
        side = StructureSide.BEARISH if point_type == StructurePointType.SWING_HIGH else StructureSide.BULLISH
        price = candidate_bar.high_price if point_type == StructurePointType.SWING_HIGH else candidate_bar.low_price
        strength_score = (
            self._compute_high_strength(self._config.left_window_size)
            if point_type == StructurePointType.SWING_HIGH
            else self._compute_low_strength(self._config.left_window_size)
        )
        point_id = (
            f"{candidate_bar.instrument_id}:{candidate_bar.timeframe}:"
            f"{point_type.value}:{candidate_bar.bar_timestamp_utc.isoformat()}"
        )
        metadata = {
            "left_window_size": self._config.left_window_size,
            "right_window_size": self._config.right_window_size,
            "candidate_open_price": candidate_bar.open_price,
            "candidate_close_price": candidate_bar.close_price,
            "candidate_volume": candidate_bar.volume,
            "candidate_range_size": candidate_bar.range_size,
            "is_internal_structure": self._config.include_internal_structure,
        }
        return StructurePoint(
            point_id=point_id,
            instrument_id=candidate_bar.instrument_id,
            timeframe=candidate_bar.timeframe,
            point_type=point_type,
            side=side,
            price=price,
            bar_timestamp=candidate_bar.bar_timestamp_utc,
            source_bar_index=source_bar_index,
            strength_score=strength_score,
            confirmation_bar_timestamp=self._last_bar_timestamp,
            metadata=metadata,
        )

    def _accept_point(self, point: StructurePoint) -> bool:
        if not self._recent_points:
            return True
        last_point = self._recent_points[-1]
        if point.point_id == last_point.point_id:
            return False
        if point.point_type == last_point.point_type:
            if point.point_type == StructurePointType.SWING_HIGH and point.price <= last_point.price:
                if self._config.deduplicate_equal_extremes:
                    return False
            if point.point_type == StructurePointType.SWING_LOW and point.price >= last_point.price:
                if self._config.deduplicate_equal_extremes:
                    return False
        return True


@dataclass(frozen=True, slots=True)
class SwingPointPair:
    """Convenience container for the most recent confirmed swing pair."""

    latest_high: StructurePoint | None
    latest_low: StructurePoint | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "latest_high": self.latest_high.to_dict() if self.latest_high else None,
            "latest_low": self.latest_low.to_dict() if self.latest_low else None,
        }


class SwingPointAccessor:
    """Read-only helper for extracting confirmed swing points from detector state."""

    def __init__(self, detector: SwingDetector) -> None:
        self._detector = detector

    def latest_pair(self) -> SwingPointPair:
        latest_high: StructurePoint | None = None
        latest_low: StructurePoint | None = None
        for point in reversed(self._detector.recent_points):
            if latest_high is None and point.point_type == StructurePointType.SWING_HIGH:
                latest_high = point
            elif latest_low is None and point.point_type == StructurePointType.SWING_LOW:
                latest_low = point
            if latest_high is not None and latest_low is not None:
                break
        return SwingPointPair(latest_high=latest_high, latest_low=latest_low)

    def highs(self) -> tuple[StructurePoint, ...]:
        return tuple(
            point for point in self._detector.recent_points if point.point_type == StructurePointType.SWING_HIGH
        )

    def lows(self) -> tuple[StructurePoint, ...]:
        return tuple(
            point for point in self._detector.recent_points if point.point_type == StructurePointType.SWING_LOW
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "SwingDetectionResult",
    "SwingDetector",
    "SwingDetectorConfig",
    "SwingDetectorError",
    "SwingDetectorState",
    "SwingPointAccessor",
    "SwingPointPair",
]
