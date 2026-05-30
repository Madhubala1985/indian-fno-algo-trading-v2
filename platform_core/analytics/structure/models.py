# FILE: platform_core/analytics/structure/models.py
"""
platform_core.analytics.structure.models
═══════════════════════════════════════════════════════════════════════
Production-grade domain models for market structure analytics specialized for
Indian Index Futures & Options workflows.

The models in this module are intentionally computation-neutral and preserve:
- streaming compatibility,
- replay compatibility,
- no-hindsight constraints,
- causal state transitions,
- JSON/Redis/PostgreSQL/TimescaleDB serializability,
- modular reuse across liquidity sweeps, BOS, CHOCH, FVG, displacement,
  support/resistance, and structure engines.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


class StructureSide(str, Enum):
    """Directional side for structure analytics."""
    
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    SUPPORT = "support"
    RESISTANCE = "resistance"


class StructurePointType(str, Enum):
    """Point classification for local structure pivots."""

    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    INTERNAL_HIGH = "internal_high"
    INTERNAL_LOW = "internal_low"


class StructureEventType(str, Enum):
    """Typed structure event taxonomy."""

    # Core structure
    BOS = "BOS"
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"

    CHOCH = "CHOCH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"

    STRUCTURE_BULLISH = "STRUCTURE_BULLISH"
    STRUCTURE_BEARISH = "STRUCTURE_BEARISH"
    STRUCTURE_NEUTRAL = "STRUCTURE_NEUTRAL"

    # Liquidity
    LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"

    LIQUIDITY_SWEEP_CONFIRMED = (
        "LIQUIDITY_SWEEP_CONFIRMED"
    )

    LIQUIDITY_SWEEP_INVALIDATED = (
        "LIQUIDITY_SWEEP_INVALIDATED"
    )

    # Fair Value Gaps
    FVG = "FVG"

    FVG_CREATED = "FVG_CREATED"

    FVG_UPDATED = "FVG_UPDATED"

    FVG_MITIGATED = "FVG_MITIGATED"

    # Displacement
    DISPLACEMENT = "DISPLACEMENT"

    # Generic support / resistance
    SUPPORT_TOUCH = "SUPPORT_TOUCH"

    RESISTANCE_TOUCH = "RESISTANCE_TOUCH"

    SUPPORT_BREAK = "SUPPORT_BREAK"

    RESISTANCE_BREAK = "RESISTANCE_BREAK"

    SUPPORT_CREATED = "SUPPORT_CREATED"

    RESISTANCE_CREATED = "RESISTANCE_CREATED"

    SUPPORT_UPDATED = "SUPPORT_UPDATED"

    RESISTANCE_UPDATED = "RESISTANCE_UPDATED"

    # SR zone aliases used across generated modules
    SR_ZONE_CREATED = "SR_ZONE_CREATED"

    SR_ZONE_UPDATED = "SR_ZONE_UPDATED"

    SR_ZONE_BROKEN = "SR_ZONE_BROKEN"

    SR_ZONE_TOUCHED = "SR_ZONE_TOUCHED"

    SUPPORT_ZONE_CREATED = (
        "SUPPORT_ZONE_CREATED"
    )

    RESISTANCE_ZONE_CREATED = (
        "RESISTANCE_ZONE_CREATED"
    )

    SUPPORT_ZONE_UPDATED = (
        "SUPPORT_ZONE_UPDATED"
    )

    RESISTANCE_ZONE_UPDATED = (
        "RESISTANCE_ZONE_UPDATED"
    )

    SUPPORT_ZONE_BROKEN = (
        "SUPPORT_ZONE_BROKEN"
    )

    RESISTANCE_ZONE_BROKEN = (
        "RESISTANCE_ZONE_BROKEN"
    )

    SUPPORT_ZONE_TOUCHED = (
        "SUPPORT_ZONE_TOUCHED"
    )

    RESISTANCE_ZONE_TOUCHED = (
        "RESISTANCE_ZONE_TOUCHED"
    )

    # Internal orchestration
    ENGINE_STATE_UPDATE = (
        "ENGINE_STATE_UPDATE"
    )

    PIPELINE_TRANSITION = (
        "PIPELINE_TRANSITION"
    )


class GapFillState(str, Enum):
    """Fair-value-gap fill state."""

    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    INVALIDATED = "invalidated"


class LevelStatus(str, Enum):
    """Support/resistance level lifecycle state."""

    ACTIVE = "active"
    TOUCHED = "touched"
    BROKEN = "broken"
    RETIRED = "retired"


class SweepOutcome(str, Enum):
    """Outcome classification for a liquidity sweep."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StructureBar:
    """Canonical finalized bar input for structure analytics."""

    instrument_id: str
    timeframe: str
    bar_timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    trade_count: int | None = None
    open_interest: float | None = None
    vwap: float | None = None
    event_sequence_number: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.high_price < self.low_price:
            raise ValueError("high_price cannot be less than low_price")
        if self.open_price < 0 or self.high_price < 0 or self.low_price < 0 or self.close_price < 0:
            raise ValueError("bar prices must be non-negative")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.trade_count is not None and self.trade_count < 0:
            raise ValueError("trade_count must be non-negative when provided")
        if self.open_interest is not None and self.open_interest < 0:
            raise ValueError("open_interest must be non-negative when provided")
        if self.event_sequence_number is not None and self.event_sequence_number < 0:
            raise ValueError("event_sequence_number must be non-negative when provided")

    @property
    def bar_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.bar_timestamp)

    @property
    def typical_price(self) -> float:
        return (self.high_price + self.low_price + self.close_price) / 3.0

    @property
    def range_size(self) -> float:
        return self.high_price - self.low_price

    @property
    def body_size(self) -> float:
        return abs(self.close_price - self.open_price)

    @property
    def upper_wick_size(self) -> float:
        return self.high_price - max(self.open_price, self.close_price)

    @property
    def lower_wick_size(self) -> float:
        return min(self.open_price, self.close_price) - self.low_price

    @property
    def direction(self) -> StructureSide:
        if self.close_price > self.open_price:
            return StructureSide.BULLISH
        if self.close_price < self.open_price:
            return StructureSide.BEARISH
        return StructureSide.NEUTRAL

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "bar_timestamp": self.bar_timestamp_utc.isoformat(),
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
            "trade_count": self.trade_count,
            "open_interest": self.open_interest,
            "vwap": self.vwap,
            "event_sequence_number": self.event_sequence_number,
        }


@dataclass(frozen=True, slots=True)
class StructurePoint:
    """Causal structure pivot or internal turning point."""

    point_id: str
    instrument_id: str
    timeframe: str
    point_type: StructurePointType
    side: StructureSide
    price: float
    bar_timestamp: datetime
    source_bar_index: int
    strength_score: float = 0.0
    confirmation_bar_timestamp: datetime | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.point_id.strip():
            raise ValueError("point_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.price < 0:
            raise ValueError("price must be non-negative")
        if self.source_bar_index < 0:
            raise ValueError("source_bar_index must be non-negative")
        if self.strength_score < 0:
            raise ValueError("strength_score must be non-negative")

    @property
    def bar_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.bar_timestamp)

    @property
    def confirmation_bar_timestamp_utc(self) -> datetime | None:
        return _ensure_utc(self.confirmation_bar_timestamp) if self.confirmation_bar_timestamp else None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "point_id": self.point_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "point_type": self.point_type.value,
            "side": self.side.value,
            "price": self.price,
            "bar_timestamp": self.bar_timestamp_utc.isoformat(),
            "source_bar_index": self.source_bar_index,
            "strength_score": self.strength_score,
            "confirmation_bar_timestamp": self.confirmation_bar_timestamp_utc.isoformat()
            if self.confirmation_bar_timestamp_utc
            else None,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    """Observable liquidity pool or stop cluster proxy."""

    level_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    reference_price: float
    upper_bound_price: float
    lower_bound_price: float
    first_seen_timestamp: datetime
    last_updated_timestamp: datetime
    touch_count: int = 0
    estimated_resting_liquidity_score: float = 0.0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.level_id.strip():
            raise ValueError("level_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.reference_price < 0 or self.upper_bound_price < 0 or self.lower_bound_price < 0:
            raise ValueError("liquidity level prices must be non-negative")
        if self.upper_bound_price < self.lower_bound_price:
            raise ValueError("upper_bound_price cannot be below lower_bound_price")
        if self.touch_count < 0:
            raise ValueError("touch_count must be non-negative")
        if self.estimated_resting_liquidity_score < 0:
            raise ValueError("estimated_resting_liquidity_score must be non-negative")

    @property
    def first_seen_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.first_seen_timestamp)

    @property
    def last_updated_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.last_updated_timestamp)

    @property
    def width(self) -> float:
        return self.upper_bound_price - self.lower_bound_price

    def contains_price(self, price: float) -> bool:
        return self.lower_bound_price <= price <= self.upper_bound_price

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "level_id": self.level_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "reference_price": self.reference_price,
            "upper_bound_price": self.upper_bound_price,
            "lower_bound_price": self.lower_bound_price,
            "first_seen_timestamp": self.first_seen_timestamp_utc.isoformat(),
            "last_updated_timestamp": self.last_updated_timestamp_utc.isoformat(),
            "touch_count": self.touch_count,
            "estimated_resting_liquidity_score": self.estimated_resting_liquidity_score,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    """A causal liquidity sweep candidate or confirmation."""

    sweep_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    level: LiquidityLevel
    trigger_bar: StructureBar
    breach_price: float
    breach_distance: float
    breach_ratio: float
    reversal_close_price: float | None = None
    reversal_bar_timestamp: datetime | None = None
    outcome: SweepOutcome = SweepOutcome.PENDING
    confirmation_score: float = 0.0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sweep_id.strip():
            raise ValueError("sweep_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.breach_price < 0 or self.breach_distance < 0 or self.breach_ratio < 0:
            raise ValueError("breach metrics must be non-negative")
        if self.reversal_close_price is not None and self.reversal_close_price < 0:
            raise ValueError("reversal_close_price must be non-negative when provided")
        if self.confirmation_score < 0:
            raise ValueError("confirmation_score must be non-negative")

    @property
    def reversal_bar_timestamp_utc(self) -> datetime | None:
        return _ensure_utc(self.reversal_bar_timestamp) if self.reversal_bar_timestamp else None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "sweep_id": self.sweep_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "level": self.level.to_dict(),
            "trigger_bar": self.trigger_bar.to_dict(),
            "breach_price": self.breach_price,
            "breach_distance": self.breach_distance,
            "breach_ratio": self.breach_ratio,
            "reversal_close_price": self.reversal_close_price,
            "reversal_bar_timestamp": self.reversal_bar_timestamp_utc.isoformat()
            if self.reversal_bar_timestamp_utc
            else None,
            "outcome": self.outcome.value,
            "confirmation_score": self.confirmation_score,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class DisplacementLeg:
    """Directional impulse leg used for structure confirmation."""

    leg_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    start_timestamp: datetime
    end_timestamp: datetime
    start_price: float
    end_price: float
    range_size: float
    body_to_range_ratio: float
    normalized_impulse_score: float
    volume_ratio: float | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.leg_id.strip():
            raise ValueError("leg_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.start_price < 0 or self.end_price < 0 or self.range_size < 0:
            raise ValueError("displacement prices and range must be non-negative")
        if self.body_to_range_ratio < 0:
            raise ValueError("body_to_range_ratio must be non-negative")
        if self.normalized_impulse_score < 0:
            raise ValueError("normalized_impulse_score must be non-negative")
        if self.volume_ratio is not None and self.volume_ratio < 0:
            raise ValueError("volume_ratio must be non-negative when provided")
        if _ensure_utc(self.end_timestamp) < _ensure_utc(self.start_timestamp):
            raise ValueError("end_timestamp cannot precede start_timestamp")

    @property
    def start_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.start_timestamp)

    @property
    def end_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.end_timestamp)

    @property
    def duration_seconds(self) -> float:
        return (self.end_timestamp_utc - self.start_timestamp_utc).total_seconds()

    @property
    def price_change(self) -> float:
        return self.end_price - self.start_price

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "leg_id": self.leg_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "start_timestamp": self.start_timestamp_utc.isoformat(),
            "end_timestamp": self.end_timestamp_utc.isoformat(),
            "start_price": self.start_price,
            "end_price": self.end_price,
            "range_size": self.range_size,
            "body_to_range_ratio": self.body_to_range_ratio,
            "normalized_impulse_score": self.normalized_impulse_score,
            "volume_ratio": self.volume_ratio,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """Three-candle imbalance zone for FVG analytics."""

    gap_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    start_timestamp: datetime
    end_timestamp: datetime
    upper_bound_price: float
    lower_bound_price: float
    midpoint_price: float
    fill_ratio: float = 0.0
    fill_state: GapFillState = GapFillState.OPEN
    displacement_leg_id: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.gap_id.strip():
            raise ValueError("gap_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.upper_bound_price < 0 or self.lower_bound_price < 0 or self.midpoint_price < 0:
            raise ValueError("gap prices must be non-negative")
        if self.upper_bound_price < self.lower_bound_price:
            raise ValueError("upper_bound_price cannot be below lower_bound_price")
        if not 0.0 <= self.fill_ratio <= 1.0:
            raise ValueError("fill_ratio must lie between 0 and 1")
        if _ensure_utc(self.end_timestamp) < _ensure_utc(self.start_timestamp):
            raise ValueError("end_timestamp cannot precede start_timestamp")

    @property
    def start_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.start_timestamp)

    @property
    def end_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.end_timestamp)

    @property
    def width(self) -> float:
        return self.upper_bound_price - self.lower_bound_price

    def contains_price(self, price: float) -> bool:
        return self.lower_bound_price <= price <= self.upper_bound_price

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gap_id": self.gap_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "start_timestamp": self.start_timestamp_utc.isoformat(),
            "end_timestamp": self.end_timestamp_utc.isoformat(),
            "upper_bound_price": self.upper_bound_price,
            "lower_bound_price": self.lower_bound_price,
            "midpoint_price": self.midpoint_price,
            "fill_ratio": self.fill_ratio,
            "fill_state": self.fill_state.value,
            "displacement_leg_id": self.displacement_leg_id,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class SupportResistanceLevel:
    """Support or resistance level state used by structure engines."""

    level_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide
    reference_price: float
    upper_bound_price: float
    lower_bound_price: float
    status: LevelStatus
    first_seen_timestamp: datetime
    last_updated_timestamp: datetime
    originating_point_ids: tuple[str, ...] = ()
    touch_count: int = 0
    break_count: int = 0
    strength_score: float = 0.0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.level_id.strip():
            raise ValueError("level_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.reference_price < 0 or self.upper_bound_price < 0 or self.lower_bound_price < 0:
            raise ValueError("level prices must be non-negative")
        if self.upper_bound_price < self.lower_bound_price:
            raise ValueError("upper_bound_price cannot be below lower_bound_price")
        if self.touch_count < 0 or self.break_count < 0:
            raise ValueError("touch_count and break_count must be non-negative")
        if self.strength_score < 0:
            raise ValueError("strength_score must be non-negative")

    @property
    def first_seen_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.first_seen_timestamp)

    @property
    def last_updated_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.last_updated_timestamp)

    @property
    def width(self) -> float:
        return self.upper_bound_price - self.lower_bound_price

    def contains_price(self, price: float) -> bool:
        return self.lower_bound_price <= price <= self.upper_bound_price

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "level_id": self.level_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "reference_price": self.reference_price,
            "upper_bound_price": self.upper_bound_price,
            "lower_bound_price": self.lower_bound_price,
            "status": self.status.value,
            "first_seen_timestamp": self.first_seen_timestamp_utc.isoformat(),
            "last_updated_timestamp": self.last_updated_timestamp_utc.isoformat(),
            "originating_point_ids": list(self.originating_point_ids),
            "touch_count": self.touch_count,
            "break_count": self.break_count,
            "strength_score": self.strength_score,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """Canonical structure engine output event."""

    event_id: str
    instrument_id: str
    timeframe: str
    event_type: StructureEventType
    side: StructureSide
    event_timestamp: datetime
    trigger_price: float
    reference_price: float | None = None
    point_id: str | None = None
    related_point_ids: tuple[str, ...] = ()
    liquidity_level_id: str | None = None
    gap_id: str | None = None
    displacement_leg_id: str | None = None
    support_resistance_level_id: str | None = None
    confidence_score: float = 0.0
    causal_bar_index: int | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must be non-empty")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.trigger_price < 0:
            raise ValueError("trigger_price must be non-negative")
        if self.reference_price is not None and self.reference_price < 0:
            raise ValueError("reference_price must be non-negative when provided")
        if self.confidence_score < 0:
            raise ValueError("confidence_score must be non-negative")
        if self.causal_bar_index is not None and self.causal_bar_index < 0:
            raise ValueError("causal_bar_index must be non-negative when provided")

    @property
    def event_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.event_timestamp)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "event_type": self.event_type.value,
            "side": self.side.value,
            "event_timestamp": self.event_timestamp_utc.isoformat(),
            "trigger_price": self.trigger_price,
            "reference_price": self.reference_price,
            "point_id": self.point_id,
            "related_point_ids": list(self.related_point_ids),
            "liquidity_level_id": self.liquidity_level_id,
            "gap_id": self.gap_id,
            "displacement_leg_id": self.displacement_leg_id,
            "support_resistance_level_id": self.support_resistance_level_id,
            "confidence_score": self.confidence_score,
            "causal_bar_index": self.causal_bar_index,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class StructureState:
    """Serializable causal structure state snapshot."""

    instrument_id: str
    timeframe: str
    last_bar_timestamp: datetime | None = None
    current_side: StructureSide = StructureSide.NEUTRAL
    last_confirmed_bos: StructureEvent | None = None
    last_confirmed_choch: StructureEvent | None = None
    active_points: tuple[StructurePoint, ...] = ()
    active_liquidity_levels: tuple[LiquidityLevel, ...] = ()
    active_gaps: tuple[FairValueGap, ...] = ()
    active_levels: tuple[SupportResistanceLevel, ...] = ()
    active_displacement_legs: tuple[DisplacementLeg, ...] = ()
    active_sweeps: tuple[LiquiditySweep, ...] = ()
    recent_events: tuple[StructureEvent, ...] = ()
    processed_bar_count: int = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.processed_bar_count < 0:
            raise ValueError("processed_bar_count must be non-negative")

    @property
    def last_bar_timestamp_utc(self) -> datetime | None:
        return _ensure_utc(self.last_bar_timestamp) if self.last_bar_timestamp else None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "last_bar_timestamp": self.last_bar_timestamp_utc.isoformat() if self.last_bar_timestamp_utc else None,
            "current_side": self.current_side.value,
            "last_confirmed_bos": self.last_confirmed_bos.to_dict() if self.last_confirmed_bos else None,
            "last_confirmed_choch": self.last_confirmed_choch.to_dict() if self.last_confirmed_choch else None,
            "active_points": [point.to_dict() for point in self.active_points],
            "active_liquidity_levels": [level.to_dict() for level in self.active_liquidity_levels],
            "active_gaps": [gap.to_dict() for gap in self.active_gaps],
            "active_levels": [level.to_dict() for level in self.active_levels],
            "active_displacement_legs": [leg.to_dict() for leg in self.active_displacement_legs],
            "active_sweeps": [sweep.to_dict() for sweep in self.active_sweeps],
            "recent_events": [event.to_dict() for event in self.recent_events],
            "processed_bar_count": self.processed_bar_count,
            "metadata": self.metadata,
        }

    @classmethod
    def empty(cls, *, instrument_id: str, timeframe: str) -> "StructureState":
        return cls(instrument_id=instrument_id, timeframe=timeframe)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "FairValueGap",
    "GapFillState",
    "JsonPrimitive",
    "JsonValue",
    "LevelStatus",
    "LiquidityLevel",
    "LiquiditySweep",
    "StructureBar",
    "StructureEvent",
    "StructureEventType",
    "StructurePoint",
    "StructurePointType",
    "StructureSide",
    "StructureState",
    "SupportResistanceLevel",
    "SweepOutcome",
    "DisplacementLeg",
]
