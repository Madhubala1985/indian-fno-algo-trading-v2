# FILE: platform_core/analytics/structure/support_resistance_engine.py
"""
platform_core.analytics.structure.support_resistance_engine
═══════════════════════════════════════════════════════════════════════
Production-grade causal support and resistance engine for finalized market structure inputs.

This module builds and maintains price interaction zones from confirmed swing
points only. Zones are clustered incrementally, updated causally, and invalidated
only by finalized-bar breaches. The engine is replay-safe, JSON-serializable,
and designed for downstream use by structure, trap, and execution logic.
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


class SupportResistanceEngineError(RuntimeError):
    """Base exception for support/resistance engine failures."""


@dataclass(frozen=True, slots=True)
class SupportResistanceEngineConfig:
    """Configuration for causal zone detection and lifecycle control."""

    instrument_id: str
    timeframe: str
    merge_distance_abs: float = 0.0
    merge_distance_ratio: float = 0.001
    touch_tolerance_abs: float = 0.0
    touch_tolerance_ratio: float = 0.0005
    break_tolerance_abs: float = 0.0
    break_tolerance_ratio: float = 0.0005
    minimum_touches_for_qualification: int = 2
    max_active_zones: int = 256
    max_recent_events: int = 512
    use_close_for_break_validation: bool = True

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.merge_distance_abs < 0 or self.merge_distance_ratio < 0:
            raise ValueError("merge distances must be non-negative")
        if self.touch_tolerance_abs < 0 or self.touch_tolerance_ratio < 0:
            raise ValueError("touch tolerances must be non-negative")
        if self.break_tolerance_abs < 0 or self.break_tolerance_ratio < 0:
            raise ValueError("break tolerances must be non-negative")
        if self.minimum_touches_for_qualification <= 0:
            raise ValueError("minimum_touches_for_qualification must be positive")
        if self.max_active_zones <= 0:
            raise ValueError("max_active_zones must be positive")
        if self.max_recent_events <= 0:
            raise ValueError("max_recent_events must be positive")


@dataclass(frozen=True, slots=True)
class PriceZone:
    """Serializable support/resistance zone state."""

    zone_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    center_price: float
    lower_price: float
    upper_price: float
    width: float
    touch_count: int
    qualified_touch_count: int
    source_point_ids: tuple[str, ...]
    first_touch_timestamp: datetime
    last_touch_timestamp: datetime
    last_touch_price: float
    last_break_timestamp: datetime | None = None
    break_count: int = 0
    is_active: bool = True
    is_broken: bool = False
    is_qualified: bool = False
    strength_score: float = 0.0

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "zone_id": self.zone_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "center_price": self.center_price,
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "width": self.width,
            "touch_count": self.touch_count,
            "qualified_touch_count": self.qualified_touch_count,
            "source_point_ids": list(self.source_point_ids),
            "first_touch_timestamp": self.first_touch_timestamp.isoformat(),
            "last_touch_timestamp": self.last_touch_timestamp.isoformat(),
            "last_touch_price": self.last_touch_price,
            "last_break_timestamp": self.last_break_timestamp.isoformat() if self.last_break_timestamp else None,
            "break_count": self.break_count,
            "is_active": self.is_active,
            "is_broken": self.is_broken,
            "is_qualified": self.is_qualified,
            "strength_score": self.strength_score,
        }


@dataclass(frozen=True, slots=True)
class SupportResistanceProcessingResult:
    """Output of processing one bar or point update."""

    created_zones: tuple[PriceZone, ...]
    updated_zones: tuple[PriceZone, ...]
    broken_zones: tuple[PriceZone, ...]
    emitted_events: tuple[StructureEvent, ...]
    processed_bar_count: int
    processed_point_count: int
    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "created_zones": [zone.to_dict() for zone in self.created_zones],
            "updated_zones": [zone.to_dict() for zone in self.updated_zones],
            "broken_zones": [zone.to_dict() for zone in self.broken_zones],
            "emitted_events": [event.to_dict() for event in self.emitted_events],
            "processed_bar_count": self.processed_bar_count,
            "processed_point_count": self.processed_point_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class SupportResistanceEngineState:
    """Serializable engine state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    processed_point_count: int
    last_bar_timestamp: datetime | None
    active_zones: tuple[dict[str, JsonValue], ...]
    recent_events: tuple[dict[str, JsonValue], ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "processed_point_count": self.processed_point_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "active_zones": list(self.active_zones),
            "recent_events": list(self.recent_events),
        }


class SupportResistanceEngine:
    """Incremental causal support and resistance zone engine."""

    def __init__(self, *, config: SupportResistanceEngineConfig) -> None:
        self._config = config
        self._zones: dict[str, PriceZone] = {}
        self._recent_events: deque[StructureEvent] = deque(maxlen=config.max_recent_events)
        self._processed_bar_count = 0
        self._processed_point_count = 0
        self._last_bar_timestamp: datetime | None = None

    @property
    def active_zones(self) -> tuple[PriceZone, ...]:
        zones = [zone for zone in self._zones.values() if zone.is_active]
        return tuple(sorted(zones, key=lambda zone: (zone.side.value, zone.center_price, zone.first_touch_timestamp)))

    @property
    def recent_events(self) -> tuple[StructureEvent, ...]:
        return tuple(self._recent_events)

    @property
    def state(self) -> SupportResistanceEngineState:
        return SupportResistanceEngineState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            processed_point_count=self._processed_point_count,
            last_bar_timestamp=self._last_bar_timestamp,
            active_zones=tuple(zone.to_dict() for zone in self.active_zones),
            recent_events=tuple(event.to_dict() for event in self._recent_events),
        )

    def reset(self) -> None:
        self._zones.clear()
        self._recent_events.clear()
        self._processed_bar_count = 0
        self._processed_point_count = 0
        self._last_bar_timestamp = None

    def process_points(self, *, points: Iterable[StructurePoint]) -> SupportResistanceProcessingResult:
        created_zones: list[PriceZone] = []
        updated_zones: list[PriceZone] = []
        emitted_events: list[StructureEvent] = []
        for point in points:
            self._validate_point(point)
            side = self._zone_side_from_point(point)
            matching_zone = self._find_matching_zone(point=point, side=side)
            if matching_zone is None:
                self._enforce_zone_capacity()
                zone = self._create_zone_from_point(point=point, side=side)
                self._zones[zone.zone_id] = zone
                created_zones.append(zone)
                event = self._build_zone_created_event(zone=zone, point=point)
                emitted_events.append(event)
                self._recent_events.append(event)
            else:
                updated_zone = self._update_zone_with_point(zone=matching_zone, point=point)
                self._zones[updated_zone.zone_id] = updated_zone
                updated_zones.append(updated_zone)
                event = self._build_zone_updated_event(zone=updated_zone, point=point)
                emitted_events.append(event)
                self._recent_events.append(event)
            self._processed_point_count += 1
        return SupportResistanceProcessingResult(
            created_zones=tuple(created_zones),
            updated_zones=tuple(updated_zones),
            broken_zones=tuple(),
            emitted_events=tuple(emitted_events),
            processed_bar_count=self._processed_bar_count,
            processed_point_count=self._processed_point_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bar(self, *, bar: StructureBar) -> SupportResistanceProcessingResult:
        self._validate_bar(bar)
        updated_zones: list[PriceZone] = []
        broken_zones: list[PriceZone] = []
        emitted_events: list[StructureEvent] = []
        for zone_id, zone in list(self._zones.items()):
            if not zone.is_active:
                continue
            if self._is_zone_touched(zone=zone, bar=bar):
                touched_zone = self._update_zone_with_bar_touch(zone=zone, bar=bar)
                self._zones[zone_id] = touched_zone
                updated_zones.append(touched_zone)
                event = self._build_zone_touch_event(zone=touched_zone, bar=bar)
                emitted_events.append(event)
                self._recent_events.append(event)
                zone = touched_zone
            if self._is_zone_broken(zone=zone, bar=bar):
                broken_zone = self._mark_zone_broken(zone=zone, bar=bar)
                self._zones[zone_id] = broken_zone
                broken_zones.append(broken_zone)
                event = self._build_zone_broken_event(zone=broken_zone, bar=bar)
                emitted_events.append(event)
                self._recent_events.append(event)
        self._processed_bar_count += 1
        self._last_bar_timestamp = bar.bar_timestamp_utc
        return SupportResistanceProcessingResult(
            created_zones=tuple(),
            updated_zones=tuple(updated_zones),
            broken_zones=tuple(broken_zones),
            emitted_events=tuple(emitted_events),
            processed_bar_count=self._processed_bar_count,
            processed_point_count=self._processed_point_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process(
        self,
        *,
        bar: StructureBar,
        points: Iterable[StructurePoint] = (),
    ) -> SupportResistanceProcessingResult:
        point_result = self.process_points(points=points)
        bar_result = self.process_bar(bar=bar)
        return SupportResistanceProcessingResult(
            created_zones=point_result.created_zones,
            updated_zones=point_result.updated_zones + bar_result.updated_zones,
            broken_zones=bar_result.broken_zones,
            emitted_events=point_result.emitted_events + bar_result.emitted_events,
            processed_bar_count=bar_result.processed_bar_count,
            processed_point_count=point_result.processed_point_count,
            last_bar_timestamp=bar_result.last_bar_timestamp,
        )

    def _find_matching_zone(self, *, point: StructurePoint, side: StructureSide) -> PriceZone | None:
        tolerance = self._merge_tolerance(point.price)
        best_zone: PriceZone | None = None
        best_distance: float | None = None
        for zone in self._zones.values():
            if not zone.is_active or zone.side != side:
                continue
            distance = abs(zone.center_price - point.price)
            if distance > tolerance:
                continue
            if best_distance is None or distance < best_distance:
                best_zone = zone
                best_distance = distance
        return best_zone

    def _create_zone_from_point(self, *, point: StructurePoint, side: StructureSide) -> PriceZone:
        half_width = self._touch_tolerance(point.price)
        lower = point.price - half_width
        upper = point.price + half_width
        zone = PriceZone(
            zone_id=self._zone_id(point=point, side=side),
            instrument_id=point.instrument_id,
            timeframe=point.timeframe,
            side=side,
            center_price=point.price,
            lower_price=lower,
            upper_price=upper,
            width=upper - lower,
            touch_count=1,
            qualified_touch_count=0,
            source_point_ids=(point.point_id,),
            first_touch_timestamp=point.bar_timestamp_utc,
            last_touch_timestamp=point.bar_timestamp_utc,
            last_touch_price=point.price,
            is_qualified=False,
            strength_score=self._strength_score(touch_count=1, qualified_touch_count=0, break_count=0),
        )
        return zone

    def _update_zone_with_point(self, *, zone: PriceZone, point: StructurePoint) -> PriceZone:
        source_point_ids = zone.source_point_ids + (point.point_id,)
        touch_count = zone.touch_count + 1
        qualified_touch_count = touch_count if touch_count >= self._config.minimum_touches_for_qualification else zone.qualified_touch_count
        center_price = self._weighted_center_price(zone=zone, new_price=point.price)
        half_width = max(self._touch_tolerance(center_price), zone.width / 2.0)
        lower = center_price - half_width
        upper = center_price + half_width
        is_qualified = touch_count >= self._config.minimum_touches_for_qualification
        return PriceZone(
            zone_id=zone.zone_id,
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            side=zone.side,
            center_price=center_price,
            lower_price=lower,
            upper_price=upper,
            width=upper - lower,
            touch_count=touch_count,
            qualified_touch_count=qualified_touch_count,
            source_point_ids=source_point_ids,
            first_touch_timestamp=zone.first_touch_timestamp,
            last_touch_timestamp=point.bar_timestamp_utc,
            last_touch_price=point.price,
            last_break_timestamp=zone.last_break_timestamp,
            break_count=zone.break_count,
            is_active=True,
            is_broken=False,
            is_qualified=is_qualified,
            strength_score=self._strength_score(
                touch_count=touch_count,
                qualified_touch_count=qualified_touch_count,
                break_count=zone.break_count,
            ),
        )

    def _update_zone_with_bar_touch(self, *, zone: PriceZone, bar: StructureBar) -> PriceZone:
        interaction_price = self._zone_interaction_price(zone=zone, bar=bar)
        touch_count = zone.touch_count + 1
        qualified_touch_count = zone.qualified_touch_count + (1 if zone.is_qualified else 0)
        center_price = self._weighted_center_price(zone=zone, new_price=interaction_price)
        half_width = max(self._touch_tolerance(center_price), zone.width / 2.0)
        lower = center_price - half_width
        upper = center_price + half_width
        return PriceZone(
            zone_id=zone.zone_id,
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            side=zone.side,
            center_price=center_price,
            lower_price=lower,
            upper_price=upper,
            width=upper - lower,
            touch_count=touch_count,
            qualified_touch_count=qualified_touch_count,
            source_point_ids=zone.source_point_ids,
            first_touch_timestamp=zone.first_touch_timestamp,
            last_touch_timestamp=bar.bar_timestamp_utc,
            last_touch_price=interaction_price,
            last_break_timestamp=zone.last_break_timestamp,
            break_count=zone.break_count,
            is_active=True,
            is_broken=False,
            is_qualified=zone.is_qualified or touch_count >= self._config.minimum_touches_for_qualification,
            strength_score=self._strength_score(
                touch_count=touch_count,
                qualified_touch_count=qualified_touch_count,
                break_count=zone.break_count,
            ),
        )

    def _mark_zone_broken(self, *, zone: PriceZone, bar: StructureBar) -> PriceZone:
        return PriceZone(
            zone_id=zone.zone_id,
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            side=zone.side,
            center_price=zone.center_price,
            lower_price=zone.lower_price,
            upper_price=zone.upper_price,
            width=zone.width,
            touch_count=zone.touch_count,
            qualified_touch_count=zone.qualified_touch_count,
            source_point_ids=zone.source_point_ids,
            first_touch_timestamp=zone.first_touch_timestamp,
            last_touch_timestamp=zone.last_touch_timestamp,
            last_touch_price=zone.last_touch_price,
            last_break_timestamp=bar.bar_timestamp_utc,
            break_count=zone.break_count + 1,
            is_active=False,
            is_broken=True,
            is_qualified=zone.is_qualified,
            strength_score=self._strength_score(
                touch_count=zone.touch_count,
                qualified_touch_count=zone.qualified_touch_count,
                break_count=zone.break_count + 1,
            ),
        )

    def _is_zone_touched(self, *, zone: PriceZone, bar: StructureBar) -> bool:
        if zone.side == StructureSide.SUPPORT:
            return bar.low_price <= zone.upper_price and bar.high_price >= zone.lower_price
        return bar.high_price >= zone.lower_price and bar.low_price <= zone.upper_price

    def _is_zone_broken(self, *, zone: PriceZone, bar: StructureBar) -> bool:
        reference_price = bar.close_price if self._config.use_close_for_break_validation else (
            bar.low_price if zone.side == StructureSide.SUPPORT else bar.high_price
        )
        tolerance = self._break_tolerance(zone.center_price)
        if zone.side == StructureSide.SUPPORT:
            return reference_price < (zone.lower_price - tolerance)
        return reference_price > (zone.upper_price + tolerance)

    def _zone_interaction_price(self, *, zone: PriceZone, bar: StructureBar) -> float:
        if zone.side == StructureSide.SUPPORT:
            candidate = bar.low_price
        else:
            candidate = bar.high_price
        return min(max(candidate, zone.lower_price), zone.upper_price)

    def _zone_side_from_point(self, point: StructurePoint) -> StructureSide:
        if point.point_type == StructurePointType.SWING_LOW:
            return StructureSide.SUPPORT
        if point.point_type == StructurePointType.SWING_HIGH:
            return StructureSide.RESISTANCE
        raise SupportResistanceEngineError("only swing highs and swing lows are valid for support/resistance zones")

    def _merge_tolerance(self, price: float) -> float:
        return max(self._config.merge_distance_abs, price * self._config.merge_distance_ratio)

    def _touch_tolerance(self, price: float) -> float:
        return max(self._config.touch_tolerance_abs, price * self._config.touch_tolerance_ratio)

    def _break_tolerance(self, price: float) -> float:
        return max(self._config.break_tolerance_abs, price * self._config.break_tolerance_ratio)

    def _weighted_center_price(self, *, zone: PriceZone, new_price: float) -> float:
        return ((zone.center_price * zone.touch_count) + new_price) / float(zone.touch_count + 1)

    def _strength_score(self, *, touch_count: int, qualified_touch_count: int, break_count: int) -> float:
        return max(0.0, float(touch_count) + 0.5 * float(qualified_touch_count) - 2.0 * float(break_count))

    def _build_zone_created_event(self, *, zone: PriceZone, point: StructurePoint) -> StructureEvent:
        return StructureEvent(
            event_id=f"{zone.zone_id}:created",
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            event_type=StructureEventType.SR_ZONE_CREATED,
            side=zone.side,
            event_timestamp=point.bar_timestamp_utc,
            trigger_price=zone.center_price,
            reference_price=point.price,
            point_id=zone.zone_id,
            related_point_ids=zone.source_point_ids,
            confidence_score=zone.strength_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "lower_price": zone.lower_price,
                "upper_price": zone.upper_price,
                "touch_count": zone.touch_count,
                "qualified_touch_count": zone.qualified_touch_count,
                "is_qualified": zone.is_qualified,
            },
        )

    def _build_zone_updated_event(self, *, zone: PriceZone, point: StructurePoint) -> StructureEvent:
        return StructureEvent(
            event_id=f"{zone.zone_id}:updated:{point.point_id}",
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            event_type=StructureEventType.SR_ZONE_UPDATED,
            side=zone.side,
            event_timestamp=point.bar_timestamp_utc,
            trigger_price=zone.center_price,
            reference_price=point.price,
            point_id=zone.zone_id,
            related_point_ids=zone.source_point_ids,
            confidence_score=zone.strength_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "lower_price": zone.lower_price,
                "upper_price": zone.upper_price,
                "touch_count": zone.touch_count,
                "qualified_touch_count": zone.qualified_touch_count,
                "is_qualified": zone.is_qualified,
            },
        )

    def _build_zone_touch_event(self, *, zone: PriceZone, bar: StructureBar) -> StructureEvent:
        return StructureEvent(
            event_id=f"{zone.zone_id}:touch:{bar.bar_timestamp_utc.isoformat()}",
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            event_type=StructureEventType.SR_ZONE_TOUCHED,
            side=zone.side,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=zone.last_touch_price,
            reference_price=zone.center_price,
            point_id=zone.zone_id,
            related_point_ids=zone.source_point_ids,
            confidence_score=zone.strength_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "touch_count": zone.touch_count,
                "qualified_touch_count": zone.qualified_touch_count,
                "last_touch_price": zone.last_touch_price,
                "is_qualified": zone.is_qualified,
            },
        )

    def _build_zone_broken_event(self, *, zone: PriceZone, bar: StructureBar) -> StructureEvent:
        return StructureEvent(
            event_id=f"{zone.zone_id}:broken:{bar.bar_timestamp_utc.isoformat()}",
            instrument_id=zone.instrument_id,
            timeframe=zone.timeframe,
            event_type=StructureEventType.SR_ZONE_BROKEN,
            side=zone.side,
            event_timestamp=bar.bar_timestamp_utc,
            trigger_price=bar.close_price,
            reference_price=zone.center_price,
            point_id=zone.zone_id,
            related_point_ids=zone.source_point_ids,
            confidence_score=zone.strength_score,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "break_count": zone.break_count,
                "last_break_timestamp": zone.last_break_timestamp.isoformat() if zone.last_break_timestamp else None,
                "was_qualified": zone.is_qualified,
            },
        )

    def _zone_id(self, *, point: StructurePoint, side: StructureSide) -> str:
        return (
            f"{point.instrument_id}:{point.timeframe}:sr:{side.value}:"
            f"{point.bar_timestamp_utc.isoformat()}:{point.point_id}"
        )

    def _enforce_zone_capacity(self) -> None:
        active_zone_ids = [zone.zone_id for zone in self._zones.values() if zone.is_active]
        if len(active_zone_ids) < self._config.max_active_zones:
            return
        oldest_zone_id = min(
            active_zone_ids,
            key=lambda zone_id: self._zones[zone_id].first_touch_timestamp,
        )
        del self._zones[oldest_zone_id]

    def _validate_point(self, point: StructurePoint) -> None:
        if point.instrument_id != self._config.instrument_id:
            raise SupportResistanceEngineError("point instrument_id does not match engine configuration")
        if point.timeframe != self._config.timeframe:
            raise SupportResistanceEngineError("point timeframe does not match engine configuration")
        if point.point_type not in {StructurePointType.SWING_HIGH, StructurePointType.SWING_LOW}:
            raise SupportResistanceEngineError("only swing highs and swing lows are valid support/resistance anchors")

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise SupportResistanceEngineError("bar instrument_id does not match engine configuration")
        if bar.timeframe != self._config.timeframe:
            raise SupportResistanceEngineError("bar timeframe does not match engine configuration")
        if self._last_bar_timestamp is not None and bar.bar_timestamp_utc <= self._last_bar_timestamp:
            raise SupportResistanceEngineError("bars must arrive in strictly increasing finalized timestamp order")



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PriceZone",
    "SupportResistanceEngine",
    "SupportResistanceEngineConfig",
    "SupportResistanceEngineError",
    "SupportResistanceEngineState",
    "SupportResistanceProcessingResult",
]
