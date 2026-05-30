# FILE: platform_core/analytics/structure/choch_detector.py
"""
platform_core.analytics.structure.choch_detector
═══════════════════════════════════════════════════════════════════════
Production-grade causal Change of Character (CHOCH) detector for finalized bars.

This module detects the first structural break against the currently inferred
market structure direction using confirmed swing points and finalized bars only.
It is incremental, replay-safe, and compatible with BOS-driven state updates.
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


class CHOCHDetectorError(RuntimeError):
    """Base exception for CHOCH detector failures."""


@dataclass(frozen=True, slots=True)
class CHOCHDetectorConfig:
    """Configuration for causal CHOCH detection."""

    instrument_id: str
    timeframe: str
    require_close_break: bool = True
    minimum_break_distance: float = 0.0
    minimum_break_ratio: float = 0.0
    one_signal_per_directional_flip: bool = True
    infer_direction_from_latest_bos: bool = True
    use_body_extremes_for_confirmation: bool = True
    max_recent_events: int = 256

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
class CHOCHTrendState:
    """Serializable directional memory for CHOCH detection."""

    inferred_structure_side: str | None
    latest_swing_high_id: str | None
    latest_swing_high_price: float | None
    latest_swing_high_timestamp: datetime | None
    latest_swing_low_id: str | None
    latest_swing_low_price: float | None
    latest_swing_low_timestamp: datetime | None
    last_bos_side: str | None
    last_bos_point_id: str | None
    last_choch_side: str | None
    last_flip_anchor_id: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "inferred_structure_side": self.inferred_structure_side,
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
            "last_bos_side": self.last_bos_side,
            "last_bos_point_id": self.last_bos_point_id,
            "last_choch_side": self.last_choch_side,
            "last_flip_anchor_id": self.last_flip_anchor_id,
        }


@dataclass(frozen=True, slots=True)
class CHOCHDetectionResult:
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
class CHOCHDetectorState:
    """Serializable detector state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    trend_state: dict[str, JsonValue]
    recent_events: tuple[dict[str, JsonValue], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "trend_state": self.trend_state,
            "recent_events": list(self.recent_events),
        }


class CHOCHDetector:
    """Causal CHOCH detector driven by confirmed swings and BOS-informed trend state."""

    def __init__(self, *, config: CHOCHDetectorConfig) -> None:
        self._config = config
        self._latest_swing_high: StructurePoint | None = None
        self._latest_swing_low: StructurePoint | None = None
        self._inferred_structure_side: StructureSide = StructureSide.NEUTRAL
        self._last_bos_side: StructureSide | None = None
        self._last_bos_point_id: str | None = None
        self._last_choch_side: StructureSide | None = None
        self._last_flip_anchor_id: str | None = None
        self._recent_events: deque[StructureEvent] = deque(maxlen=config.max_recent_events)
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None

    @property
    def recent_events(self) -> tuple[StructureEvent, ...]:
        return tuple(self._recent_events)

    @property
    def state(self) -> CHOCHDetectorState:
        return CHOCHDetectorState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            trend_state=self._trend_state().to_dict(),
            recent_events=tuple(event.to_dict() for event in self._recent_events),
        )

    def reset(self) -> None:
        self._latest_swing_high = None
        self._latest_swing_low = None
        self._inferred_structure_side = StructureSide.NEUTRAL
        self._last_bos_side = None
        self._last_bos_point_id = None
        self._last_choch_side = None
        self._last_flip_anchor_id = None
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

    def update_bos_events(self, bos_events: Iterable[StructureEvent]) -> None:
        for event in bos_events:
            self._validate_bos_event(event)
            self._last_bos_side = event.side
            self._last_bos_point_id = event.point_id
            if self._config.infer_direction_from_latest_bos:
                self._inferred_structure_side = event.side

    def process_bar(
        self,
        *,
        bar: StructureBar,
        swing_points: Iterable[StructurePoint] = (),
        bos_events: Iterable[StructureEvent] = (),
    ) -> CHOCHDetectionResult:
        self._validate_bar(bar)
        self.update_swings(swing_points)
        self.update_bos_events(bos_events)
        emitted_events: list[StructureEvent] = []
        bullish_choch = self._detect_bullish_choch(bar)
        if bullish_choch is not None:
            emitted_events.append(bullish_choch)
            self._recent_events.append(bullish_choch)
            self._last_choch_side = StructureSide.BULLISH
            self._last_flip_anchor_id = bullish_choch.point_id
            self._inferred_structure_side = StructureSide.BULLISH
        bearish_choch = self._detect_bearish_choch(bar)
        if bearish_choch is not None:
            emitted_events.append(bearish_choch)
            self._recent_events.append(bearish_choch)
            self._last_choch_side = StructureSide.BEARISH
            self._last_flip_anchor_id = bearish_choch.point_id
            self._inferred_structure_side = StructureSide.BEARISH
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        return CHOCHDetectionResult(
            emitted_events=tuple(emitted_events),
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(
        self,
        *,
        bars: Iterable[StructureBar],
        swing_points_provider: callable,
        bos_events_provider: callable,
    ) -> tuple[CHOCHDetectionResult, ...]:
        results: list[CHOCHDetectionResult] = []
        for bar in bars:
            swings = swing_points_provider(bar)
            bos_events = bos_events_provider(bar)
            results.append(self.process_bar(bar=bar, swing_points=swings, bos_events=bos_events))
        return tuple(results)

    def _detect_bullish_choch(self, bar: StructureBar) -> StructureEvent | None:
        if self._inferred_structure_side != StructureSide.BEARISH:
            return None
        point = self._latest_swing_high
        if point is None:
            return None
        if point.bar_timestamp_utc >= bar.bar_timestamp_utc:
            return None
        if self._config.one_signal_per_directional_flip and self._last_flip_anchor_id == point.point_id:
            return None
        confirmation_price = self._bullish_confirmation_price(bar)
        break_distance = confirmation_price - point.price
        break_ratio = break_distance / point.price if point.price > 0 else 0.0
        if break_distance < self._config.minimum_break_distance:
            return None
        if break_ratio < self._config.minimum_break_ratio:
            return None
        if confirmation_price <= point.price:
            return None
        return StructureEvent(
            event_id=self._event_id(bar=bar, side=StructureSide.BULLISH, point=point),
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            event_type=StructureEventType.CHOCH,
            side=StructureSide.BULLISH,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=confirmation_price,
            reference_price=point.price,
            point_id=point.point_id,
            related_point_ids=(point.point_id,),
            confidence_score=self._compute_confidence_score(
                break_distance=break_distance,
                level_price=point.price,
                point_strength_score=point.strength_score,
            ),
            causal_bar_index=self._processed_bar_count,
            metadata={
                "previous_structure_side": StructureSide.BEARISH.value,
                "last_bos_side": self._last_bos_side.value if self._last_bos_side else None,
                "last_bos_point_id": self._last_bos_point_id,
                "break_distance": break_distance,
                "break_ratio": break_ratio,
                "confirmation_mode": "body_close" if self._config.require_close_break else "price_extreme",
                "swing_timestamp": point.bar_timestamp_utc.isoformat(),
            },
        )

    def _detect_bearish_choch(self, bar: StructureBar) -> StructureEvent | None:
        if self._inferred_structure_side != StructureSide.BULLISH:
            return None
        point = self._latest_swing_low
        if point is None:
            return None
        if point.bar_timestamp_utc >= bar.bar_timestamp_utc:
            return None
        if self._config.one_signal_per_directional_flip and self._last_flip_anchor_id == point.point_id:
            return None
        confirmation_price = self._bearish_confirmation_price(bar)
        break_distance = point.price - confirmation_price
        break_ratio = break_distance / point.price if point.price > 0 else 0.0
        if break_distance < self._config.minimum_break_distance:
            return None
        if break_ratio < self._config.minimum_break_ratio:
            return None
        if confirmation_price >= point.price:
            return None
        return StructureEvent(
            event_id=self._event_id(bar=bar, side=StructureSide.BEARISH, point=point),
            instrument_id=bar.instrument_id,
            timeframe=bar.timeframe,
            event_type=StructureEventType.CHOCH,
            side=StructureSide.BEARISH,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=confirmation_price,
            reference_price=point.price,
            point_id=point.point_id,
            related_point_ids=(point.point_id,),
            confidence_score=self._compute_confidence_score(
                break_distance=break_distance,
                level_price=point.price,
                point_strength_score=point.strength_score,
            ),
            causal_bar_index=self._processed_bar_count,
            metadata={
                "previous_structure_side": StructureSide.BULLISH.value,
                "last_bos_side": self._last_bos_side.value if self._last_bos_side else None,
                "last_bos_point_id": self._last_bos_point_id,
                "break_distance": break_distance,
                "break_ratio": break_ratio,
                "confirmation_mode": "body_close" if self._config.require_close_break else "price_extreme",
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
            raise CHOCHDetectorError("bar instrument_id does not match detector configuration")
        if bar.timeframe != self._config.timeframe:
            raise CHOCHDetectorError("bar timeframe does not match detector configuration")
        current_timestamp = bar.bar_timestamp_utc
        if self._last_bar_timestamp is not None and current_timestamp <= self._last_bar_timestamp:
            raise CHOCHDetectorError("bars must arrive in strictly increasing finalized timestamp order")

    def _validate_point(self, point: StructurePoint) -> None:
        if point.instrument_id != self._config.instrument_id:
            raise CHOCHDetectorError("swing point instrument_id does not match detector configuration")
        if point.timeframe != self._config.timeframe:
            raise CHOCHDetectorError("swing point timeframe does not match detector configuration")
        if point.point_type not in {StructurePointType.SWING_HIGH, StructurePointType.SWING_LOW}:
            raise CHOCHDetectorError("CHOCH detector only accepts confirmed swing highs and swing lows")

    def _validate_bos_event(self, event: StructureEvent) -> None:
        if event.instrument_id != self._config.instrument_id:
            raise CHOCHDetectorError("BOS event instrument_id does not match detector configuration")
        if event.timeframe != self._config.timeframe:
            raise CHOCHDetectorError("BOS event timeframe does not match detector configuration")
        if event.event_type != StructureEventType.BOS:
            raise CHOCHDetectorError("CHOCH detector only accepts BOS events for trend-state updates")

    def _trend_state(self) -> CHOCHTrendState:
        return CHOCHTrendState(
            inferred_structure_side=None
            if self._inferred_structure_side == StructureSide.NEUTRAL
            else self._inferred_structure_side.value,
            latest_swing_high_id=self._latest_swing_high.point_id if self._latest_swing_high else None,
            latest_swing_high_price=self._latest_swing_high.price if self._latest_swing_high else None,
            latest_swing_high_timestamp=self._latest_swing_high.bar_timestamp_utc if self._latest_swing_high else None,
            latest_swing_low_id=self._latest_swing_low.point_id if self._latest_swing_low else None,
            latest_swing_low_price=self._latest_swing_low.price if self._latest_swing_low else None,
            latest_swing_low_timestamp=self._latest_swing_low.bar_timestamp_utc if self._latest_swing_low else None,
            last_bos_side=self._last_bos_side.value if self._last_bos_side else None,
            last_bos_point_id=self._last_bos_point_id,
            last_choch_side=self._last_choch_side.value if self._last_choch_side else None,
            last_flip_anchor_id=self._last_flip_anchor_id,
        )

    def _event_id(self, *, bar: StructureBar, side: StructureSide, point: StructurePoint) -> str:
        return (
            f"{bar.instrument_id}:{bar.timeframe}:choch:{side.value}:"
            f"{point.point_id}:{bar.bar_timestamp_utc.isoformat()}"
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "CHOCHDetectionResult",
    "CHOCHDetector",
    "CHOCHDetectorConfig",
    "CHOCHDetectorError",
    "CHOCHDetectorState",
    "CHOCHTrendState",
]
