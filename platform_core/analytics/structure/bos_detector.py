# FILE: platform_core/analytics/structure/bos_detector.py
"""
platform_core.analytics.structure.bos_detector
═══════════════════════════════════════════════════════════════════════
Production-grade causal Break of Structure (BOS) detector for finalized bars.

This module detects bullish and bearish BOS events using confirmed swing points
and finalized bar closes only. It preserves:
- no-hindsight logic,
- incremental streaming compatibility,
- deterministic replay behavior,
- JSON-safe state snapshots,
- clean integration with swing, displacement, liquidity, and higher-order
  structure engines.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructureEvent
from platform_core.analytics.structure.models import StructureEventType
from platform_core.analytics.structure.models import StructurePoint
from platform_core.analytics.structure.models import StructurePointType
from platform_core.analytics.structure.models import StructureSide


class BOSDetectorError(RuntimeError):
    """Base exception for BOS detector failures."""


@dataclass(frozen=True, slots=True)
class BOSDetectorConfig:
    """Configuration for causal BOS detection."""

    instrument_id: str
    timeframe: str
    require_close_break: bool = True
    minimum_break_distance: float = 0.0
    minimum_break_ratio: float = 0.0
    one_signal_per_swing: bool = True
    max_recent_events: int = 256
    use_body_extremes_for_confirmation: bool = True

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.minimum_break_distance < 0:
            raise ValueError("minimum_break_distance must be non-negative")
        if self.minimum_break_ratio < 0:
            raise ValueError("minimum_break_ratio must be non-negative")
        if self.max_recent_events <= 0:
            raise ValueError("max_recent_events must be positive")


@dataclass(frozen=True, slots=True)
class BOSCandidateState:
    """Serializable BOS swing reference state."""

    latest_swing_high_id: str | None
    latest_swing_high_price: float | None
    latest_swing_high_timestamp: datetime | None
    latest_swing_low_id: str | None
    latest_swing_low_price: float | None
    latest_swing_low_timestamp: datetime | None
    last_bullish_bos_swing_id: str | None
    last_bearish_bos_swing_id: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "latest_swing_high_id": self.latest_swing_high_id,
            "latest_swing_high_price": self.latest_swing_high_price,
            "latest_swing_high_timestamp": self.latest_swing_high_timestamp.isoformat()
            if self.latest_swing_high_timestamp
            else None,
            "latest_swing_low_id": self.latest_swing_low_id,
            "latest_swing_low_price": self.latest_swing_low_price,
            "latest_swing_low_timestamp": self.latest_swing_low_timestamp.isoformat()
            if self.latest_swing_low_timestamp
            else None,
            "last_bullish_bos_swing_id": self.last_bullish_bos_swing_id,
            "last_bearish_bos_swing_id": self.last_bearish_bos_swing_id,
        }


@dataclass(frozen=True, slots=True)
class BOSDetectionResult:
    """Output of processing one finalized bar."""

    emitted_events: tuple[StructureEvent, ...]
    processed_bar_count: int
    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "emitted_events": [event.to_dict() for event in self.emitted_events],
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class BOSDetectorState:
    """Serializable detector state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    candidate_state: dict[str, JsonValue]
    recent_events: tuple[dict[str, JsonValue], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "candidate_state": self.candidate_state,
            "recent_events": list(self.recent_events),
        }


class BOSDetector:
    """Causal BOS detector driven by confirmed swing points and finalized bars."""

    def __init__(self, *, config: BOSDetectorConfig) -> None:
        self._config = config
        self._latest_swing_high: StructurePoint | None = None
        self._latest_swing_low: StructurePoint | None = None
        self._last_bullish_bos_swing_id: str | None = None
        self._last_bearish_bos_swing_id: str | None = None
        self._recent_events: deque[StructureEvent] = deque(maxlen=config.max_recent_events)
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None

    @property
    def recent_events(self) -> tuple[StructureEvent, ...]:
        return tuple(self._recent_events)

    @property
    def state(self) -> BOSDetectorState:
        return BOSDetectorState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            candidate_state=self._candidate_state().to_dict(),
            recent_events=tuple(event.to_dict() for event in self._recent_events),
        )

    def reset(self) -> None:
        self._latest_swing_high = None
        self._latest_swing_low = None
        self._last_bullish_bos_swing_id = None
        self._last_bearish_bos_swing_id = None
        self._recent_events.clear()
        self._processed_bar_count = 0
        self._last_bar_timestamp = None

    def update_swings(self, swing_points: Iterable[StructurePoint]) -> None:
        for point in swing_points:
            self._validate_point(point)
            if point.point_type == StructurePointType.SWING_HIGH:
                if self._latest_swing_high is None or point.bar_timestamp_utc >= self._latest_swing_high.bar_timestamp_utc:
                    self._latest_swing_high = point
            elif point.point_type == StructurePointType.SWING_LOW:
                if self._latest_swing_low is None or point.bar_timestamp_utc >= self._latest_swing_low.bar_timestamp_utc:
                    self._latest_swing_low = point

    def process_bar(self, *, bar: StructureBar, swing_points: Iterable[StructurePoint] = ()) -> BOSDetectionResult:
        self._validate_bar(bar)
        self.update_swings(swing_points)
        emitted_events: list[StructureEvent] = []
        bullish_event = self._detect_bullish_bos(bar)
        if bullish_event is not None:
            self._recent_events.append(bullish_event)
            emitted_events.append(bullish_event)
            self._last_bullish_bos_swing_id = bullish_event.point_id
        bearish_event = self._detect_bearish_bos(bar)
        if bearish_event is not None:
            self._recent_events.append(bearish_event)
            emitted_events.append(bearish_event)
            self._last_bearish_bos_swing_id = bearish_event.point_id
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        return BOSDetectionResult(
            emitted_events=tuple(emitted_events),
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(
        self,
        *,
        bars: Iterable[StructureBar],
        swing_points_provider: callable,
    ) -> tuple[BOSDetectionResult, ...]:
        results: list[BOSDetectionResult] = []
        for bar in bars:
            swing_points = swing_points_provider(bar)
            results.append(self.process_bar(bar=bar, swing_points=swing_points))
        return tuple(results)

    def _detect_bullish_bos(self, bar: StructureBar) -> StructureEvent | None:
        point = self._latest_swing_high
        if point is None:
            return None
        if self._config.one_signal_per_swing and point.point_id == self._last_bullish_bos_swing_id:
            return None
        if point.bar_timestamp_utc >= bar.bar_timestamp_utc:
            return None
        confirmation_price = self._bullish_confirmation_price(bar)
        break_distance = confirmation_price - point.price
        break_ratio = break_distance / point.price if point.price > 0 else 0.0
        if break_distance < self._config.minimum_break_distance:
            return None
        if break_ratio < self._config.minimum_break_ratio:
            return None
        if not self._breaks_above_level(confirmation_price=confirmation_price, level_price=point.price):
            return None
        confidence_score = self._compute_confidence_score(
            break_distance=break_distance,
            level_price=point.price,
            point_strength_score=point.strength_score,
        )
        return StructureEvent(
            event_id=self._event_id(bar=bar, side=StructureSide.BULLISH, point=point),
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            event_type=StructureEventType.BOS,
            side=StructureSide.BULLISH,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=confirmation_price,
            reference_price=point.price,
            point_id=point.point_id,
            related_point_ids=(point.point_id,),
            confidence_score=confidence_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "confirmation_mode": "body_close" if self._config.require_close_break else "price_extreme",
                "break_distance": break_distance,
                "break_ratio": break_ratio,
                "swing_timestamp": point.bar_timestamp_utc.isoformat(),
            },
        )

    def _detect_bearish_bos(self, bar: StructureBar) -> StructureEvent | None:
        point = self._latest_swing_low
        if point is None:
            return None
        if self._config.one_signal_per_swing and point.point_id == self._last_bearish_bos_swing_id:
            return None
        if point.bar_timestamp_utc >= bar.bar_timestamp_utc:
            return None
        confirmation_price = self._bearish_confirmation_price(bar)
        break_distance = point.price - confirmation_price
        break_ratio = break_distance / point.price if point.price > 0 else 0.0
        if break_distance < self._config.minimum_break_distance:
            return None
        if break_ratio < self._config.minimum_break_ratio:
            return None
        if not self._breaks_below_level(confirmation_price=confirmation_price, level_price=point.price):
            return None
        confidence_score = self._compute_confidence_score(
            break_distance=break_distance,
            level_price=point.price,
            point_strength_score=point.strength_score,
        )
        return StructureEvent(
            event_id=self._event_id(bar=bar, side=StructureSide.BEARISH, point=point),
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            event_type=StructureEventType.BOS,
            side=StructureSide.BEARISH,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=confirmation_price,
            reference_price=point.price,
            point_id=point.point_id,
            related_point_ids=(point.point_id,),
            confidence_score=confidence_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "confirmation_mode": "body_close" if self._config.require_close_break else "price_extreme",
                "break_distance": break_distance,
                "break_ratio": break_ratio,
                "swing_timestamp": point.bar_timestamp_utc.isoformat(),
            },
        )

    def _bullish_confirmation_price(self, bar: StructureBar) -> float:
        if self._config.require_close_break:
            if self._config.use_body_extremes_for_confirmation:
                return max(bar.open_price, bar.close_price)
            return bar.close_price
        return bar.high_price

    def _bearish_confirmation_price(self, bar: StructureBar) -> float:
        if self._config.require_close_break:
            if self._config.use_body_extremes_for_confirmation:
                return min(bar.open_price, bar.close_price)
            return bar.close_price
        return bar.low_price

    def _breaks_above_level(self, *, confirmation_price: float, level_price: float) -> bool:
        return confirmation_price > level_price

    def _breaks_below_level(self, *, confirmation_price: float, level_price: float) -> bool:
        return confirmation_price < level_price

    def _compute_confidence_score(
        self,
        *,
        break_distance: float,
        level_price: float,
        point_strength_score: float,
    ) -> float:
        normalized_break = break_distance / level_price if level_price > 0 else 0.0
        return normalized_break + point_strength_score

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise BOSDetectorError("bar instrument_id does not match detector configuration")
        if bar.timeframe != self._config.timeframe:
            raise BOSDetectorError("bar timeframe does not match detector configuration")
        current_timestamp = bar.bar_timestamp_utc
        if self._last_bar_timestamp is not None and current_timestamp <= self._last_bar_timestamp:
            raise BOSDetectorError("bars must arrive in strictly increasing finalized timestamp order")

    def _validate_point(self, point: StructurePoint) -> None:
        if point.instrument_id != self._config.instrument_id:
            raise BOSDetectorError("swing point instrument_id does not match detector configuration")
        if point.timeframe != self._config.timeframe:
            raise BOSDetectorError("swing point timeframe does not match detector configuration")
        if point.point_type not in {StructurePointType.SWING_HIGH, StructurePointType.SWING_LOW}:
            raise BOSDetectorError("BOS detector only accepts confirmed swing highs and swing lows")

    def _candidate_state(self) -> BOSCandidateState:
        return BOSCandidateState(
            latest_swing_high_id=self._latest_swing_high.point_id if self._latest_swing_high else None,
            latest_swing_high_price=self._latest_swing_high.price if self._latest_swing_high else None,
            latest_swing_high_timestamp=self._latest_swing_high.bar_timestamp_utc if self._latest_swing_high else None,
            latest_swing_low_id=self._latest_swing_low.point_id if self._latest_swing_low else None,
            latest_swing_low_price=self._latest_swing_low.price if self._latest_swing_low else None,
            latest_swing_low_timestamp=self._latest_swing_low.bar_timestamp_utc if self._latest_swing_low else None,
            last_bullish_bos_swing_id=self._last_bullish_bos_swing_id,
            last_bearish_bos_swing_id=self._last_bearish_bos_swing_id,
        )

    def _event_id(self, *, bar: StructureBar, side: StructureSide, point: StructurePoint) -> str:
        return (
            f"{bar.instrument_id}:{bar.timeframe}:bos:{side.value}:"
            f"{point.point_id}:{bar.bar_timestamp_utc.isoformat()}"
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "BOSCandidateState",
    "BOSDetectionResult",
    "BOSDetector",
    "BOSDetectorConfig",
    "BOSDetectorError",
    "BOSDetectorState",
]
