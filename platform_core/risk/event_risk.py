# FILE: platform_core/risk/event_risk.py
"""
platform_core.risk.event_risk
═══════════════════════════════════════════════════════════════════════
Production-grade event-risk engine specialized for Indian index futures and
options workflows.

The engine evaluates finalized scheduled and unscheduled market-risk events and
converts them into deterministic trading gates, sizing reductions, and hedging
urgency directives. It is replay-safe, JSON-serializable, and suitable for
integration with execution, orchestration, and broader institutional risk
controls.

Core controls:
- Pre-event and post-event risk windows.
- Event severity weighting with category-specific multipliers.
- Position reduction and new-entry gating.
- Options-expiry and scheduled macro-event stress integration.
- Gap-risk aware escalation for overnight or imminent events.
- Deterministic aggregation across multiple simultaneous events.

This module does not fetch calendars, place hedges, or alter positions. It
emits structured event-risk decisions for downstream risk, sizing, and
execution layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class EventRiskError(RuntimeError):
    """Base exception for event-risk failures."""


class EventRiskInputError(EventRiskError):
    """Raised when event-risk inputs are invalid or causally unsafe."""


class EventRiskComputationError(EventRiskError):
    """Raised when an event-risk computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class EventRiskConfig:
    """Configuration for deterministic event-risk aggregation."""

    default_pre_event_minutes: int = 30
    default_post_event_minutes: int = 15
    high_severity_reduction_fraction: float = 0.75
    medium_severity_reduction_fraction: float = 0.45
    low_severity_reduction_fraction: float = 0.20
    block_score_threshold: float = 0.80
    reduce_score_threshold: float = 0.45
    caution_score_threshold: float = 0.20
    overnight_gap_multiplier: float = 1.20
    expiry_event_multiplier: float = 1.15
    unscheduled_event_multiplier: float = 1.25
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_ints = {
            "default_pre_event_minutes": self.default_pre_event_minutes,
            "default_post_event_minutes": self.default_post_event_minutes,
        }
        for field_name, field_value in positive_ints.items():
            if field_value <= 0:
                raise ValueError(f"{field_name} must be positive")
        bounded_fields = {
            "high_severity_reduction_fraction": self.high_severity_reduction_fraction,
            "medium_severity_reduction_fraction": self.medium_severity_reduction_fraction,
            "low_severity_reduction_fraction": self.low_severity_reduction_fraction,
            "block_score_threshold": self.block_score_threshold,
            "reduce_score_threshold": self.reduce_score_threshold,
            "caution_score_threshold": self.caution_score_threshold,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        positive_floats = {
            "overnight_gap_multiplier": self.overnight_gap_multiplier,
            "expiry_event_multiplier": self.expiry_event_multiplier,
            "unscheduled_event_multiplier": self.unscheduled_event_multiplier,
        }
        for field_name, field_value in positive_floats.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not self.low_severity_reduction_fraction <= self.medium_severity_reduction_fraction <= self.high_severity_reduction_fraction:
            raise ValueError("severity reduction fractions must satisfy low <= medium <= high")
        if not self.caution_score_threshold <= self.reduce_score_threshold <= self.block_score_threshold:
            raise ValueError("score thresholds must satisfy caution <= reduce <= block")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """Serializable event definition for risk aggregation."""

    event_id: str
    category: str
    severity: str
    scheduled_timestamp: datetime
    is_unscheduled: bool
    affects_overnight_gap: bool
    affects_expiry_flow: bool
    pre_event_minutes: int | None = None
    post_event_minutes: int | None = None
    scope_symbol: str | None = None

    def __post_init__(self) -> None:
        string_fields = {
            "event_id": self.event_id,
            "category": self.category,
            "severity": self.severity,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.pre_event_minutes is not None and self.pre_event_minutes <= 0:
            raise ValueError("pre_event_minutes must be positive when provided")
        if self.post_event_minutes is not None and self.post_event_minutes <= 0:
            raise ValueError("post_event_minutes must be positive when provided")
        if self.scope_symbol is not None and not self.scope_symbol.strip():
            raise ValueError("scope_symbol must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "category": self.category,
            "severity": self.severity,
            "scheduled_timestamp": _ensure_utc(self.scheduled_timestamp).isoformat(),
            "is_unscheduled": self.is_unscheduled,
            "affects_overnight_gap": self.affects_overnight_gap,
            "affects_expiry_flow": self.affects_expiry_flow,
            "pre_event_minutes": self.pre_event_minutes,
            "post_event_minutes": self.post_event_minutes,
            "scope_symbol": self.scope_symbol,
        }


@dataclass(frozen=True, slots=True)
class EventRiskSnapshot:
    """Serializable finalized snapshot for event-risk evaluation."""

    timestamp: datetime
    underlying_symbol: str
    instrument_type: str
    holding_overnight: bool
    has_open_risk: bool
    current_position_notional: float
    realized_volatility_fraction: float
    implied_volatility_fraction: float | None
    events: tuple[RiskEvent, ...]

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip() or not self.instrument_type.strip():
            raise ValueError("underlying_symbol and instrument_type must be non-empty")
        if not isfinite(self.current_position_notional):
            raise ValueError("current_position_notional must be finite")
        if self.realized_volatility_fraction <= 0.0 or not isfinite(self.realized_volatility_fraction):
            raise ValueError("realized_volatility_fraction must be positive and finite")
        if self.implied_volatility_fraction is not None:
            if self.implied_volatility_fraction <= 0.0 or not isfinite(self.implied_volatility_fraction):
                raise ValueError("implied_volatility_fraction must be positive and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "underlying_symbol": self.underlying_symbol,
            "instrument_type": self.instrument_type,
            "holding_overnight": self.holding_overnight,
            "has_open_risk": self.has_open_risk,
            "current_position_notional": self.current_position_notional,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "implied_volatility_fraction": self.implied_volatility_fraction,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class EventRiskBreakdown:
    """Interpretable decomposition of aggregated event risk."""

    active_event_count: int
    max_single_event_score: float
    aggregate_event_score: float
    reduction_fraction: float
    nearest_event_id: str | None
    nearest_event_minutes: float | None
    overnight_risk_triggered: bool
    expiry_risk_triggered: bool
    unscheduled_risk_triggered: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "active_event_count": self.active_event_count,
            "max_single_event_score": self.max_single_event_score,
            "aggregate_event_score": self.aggregate_event_score,
            "reduction_fraction": self.reduction_fraction,
            "nearest_event_id": self.nearest_event_id,
            "nearest_event_minutes": self.nearest_event_minutes,
            "overnight_risk_triggered": self.overnight_risk_triggered,
            "expiry_risk_triggered": self.expiry_risk_triggered,
            "unscheduled_risk_triggered": self.unscheduled_risk_triggered,
        }


@dataclass(frozen=True, slots=True)
class EventRiskDecision:
    """Serializable event-risk decision for downstream risk gating."""

    timestamp: datetime
    level: str
    allow_new_entries: bool
    require_position_reduction: bool
    recommended_reduction_fraction: float
    hedge_urgency: str
    reason: str
    breakdown: EventRiskBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "level": self.level,
            "allow_new_entries": self.allow_new_entries,
            "require_position_reduction": self.require_position_reduction,
            "recommended_reduction_fraction": self.recommended_reduction_fraction,
            "hedge_urgency": self.hedge_urgency,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


class EventRiskEngine:
    """Deterministic event-risk engine."""

    def __init__(self, *, config: EventRiskConfig | None = None) -> None:
        self._config = config or EventRiskConfig()
        self._last_timestamp: datetime | None = None
        self._processed_snapshots = 0
        self._last_decision: EventRiskDecision | None = None

    def reset(self) -> None:
        self._last_timestamp = None
        self._processed_snapshots = 0
        self._last_decision = None

    def evaluate(
        self,
        *,
        snapshot: EventRiskSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> EventRiskDecision:
        timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        active_event_scores: list[tuple[RiskEvent, float, float]] = []
        nearest_event_id: str | None = None
        nearest_event_minutes: float | None = None
        overnight_risk_triggered = False
        expiry_risk_triggered = False
        unscheduled_risk_triggered = False

        for event in snapshot.events:
            event_score, signed_minutes = self._event_score(snapshot=snapshot, event=event, timestamp=timestamp)
            if event_score <= 0.0:
                continue
            active_event_scores.append((event, event_score, signed_minutes))
            abs_minutes = abs(signed_minutes)
            if nearest_event_minutes is None or abs_minutes < nearest_event_minutes:
                nearest_event_minutes = abs_minutes
                nearest_event_id = event.event_id
            overnight_risk_triggered = overnight_risk_triggered or event.affects_overnight_gap
            expiry_risk_triggered = expiry_risk_triggered or event.affects_expiry_flow
            unscheduled_risk_triggered = unscheduled_risk_triggered or event.is_unscheduled

        aggregate_event_score = 0.0
        max_single_event_score = 0.0
        for _, event_score, _ in active_event_scores:
            aggregate_event_score += event_score
            if event_score > max_single_event_score:
                max_single_event_score = event_score
        aggregate_event_score = min(1.0, aggregate_event_score)

        reduction_fraction = self._compute_reduction_fraction(
            aggregate_event_score=aggregate_event_score,
            max_single_event_score=max_single_event_score,
        )
        breakdown = EventRiskBreakdown(
            active_event_count=len(active_event_scores),
            max_single_event_score=max_single_event_score,
            aggregate_event_score=aggregate_event_score,
            reduction_fraction=reduction_fraction,
            nearest_event_id=nearest_event_id,
            nearest_event_minutes=nearest_event_minutes,
            overnight_risk_triggered=overnight_risk_triggered,
            expiry_risk_triggered=expiry_risk_triggered,
            unscheduled_risk_triggered=unscheduled_risk_triggered,
        )
        decision = self._decide(timestamp=timestamp, snapshot=snapshot, breakdown=breakdown)
        self._last_timestamp = timestamp
        self._processed_snapshots += 1
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> EventRiskDecision:
        if self._last_decision is None:
            raise EventRiskComputationError("no event-risk decision is available")
        return self._last_decision

    def _validate_snapshot(self, *, snapshot: EventRiskSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise EventRiskInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise EventRiskInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise EventRiskInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _event_score(
        self,
        *,
        snapshot: EventRiskSnapshot,
        event: RiskEvent,
        timestamp: datetime,
    ) -> tuple[float, float]:
        event_timestamp = _ensure_utc(event.scheduled_timestamp)
        signed_minutes = (event_timestamp - timestamp).total_seconds() / 60.0
        pre_window_minutes = event.pre_event_minutes or self._config.default_pre_event_minutes
        post_window_minutes = event.post_event_minutes or self._config.default_post_event_minutes

        in_scope = event.scope_symbol is None or event.scope_symbol.strip().upper() == snapshot.underlying_symbol.strip().upper()
        if not in_scope:
            return 0.0, signed_minutes
        if signed_minutes >= 0.0:
            if signed_minutes > pre_window_minutes:
                return 0.0, signed_minutes
            time_weight = 1.0 - (signed_minutes / pre_window_minutes)
        else:
            abs_minutes = abs(signed_minutes)
            if abs_minutes > post_window_minutes:
                return 0.0, signed_minutes
            time_weight = 1.0 - (abs_minutes / post_window_minutes)

        severity_weight = self._severity_weight(event.severity)
        category_weight = self._category_weight(event.category)
        event_score = time_weight * severity_weight * category_weight
        if event.is_unscheduled:
            event_score *= self._config.unscheduled_event_multiplier
        if event.affects_expiry_flow:
            event_score *= self._config.expiry_event_multiplier
        if event.affects_overnight_gap and snapshot.holding_overnight:
            event_score *= self._config.overnight_gap_multiplier
        if snapshot.implied_volatility_fraction is not None:
            iv_scalar = _clamp(
                snapshot.implied_volatility_fraction / snapshot.realized_volatility_fraction,
                lower=0.75,
                upper=1.50,
            )
            event_score *= iv_scalar
        if not snapshot.has_open_risk:
            event_score *= 0.50
        return min(1.0, event_score), signed_minutes

    def _severity_weight(self, severity: str) -> float:
        normalized = severity.strip().upper()
        mapping = {
            "LOW": 0.25,
            "MEDIUM": 0.55,
            "HIGH": 0.85,
            "CRITICAL": 1.00,
        }
        return mapping.get(normalized, 0.50)

    def _category_weight(self, category: str) -> float:
        normalized = category.strip().upper()
        mapping = {
            "MACRO": 1.00,
            "CENTRAL_BANK": 1.00,
            "EXPIRY": 0.85,
            "EARNINGS": 0.70,
            "POLICY": 0.95,
            "GEOPOLITICAL": 0.90,
            "EXCHANGE": 0.80,
            "UNSCHEDULED": 1.00,
        }
        return mapping.get(normalized, 0.70)

    def _compute_reduction_fraction(self, *, aggregate_event_score: float, max_single_event_score: float) -> float:
        if aggregate_event_score >= self._config.block_score_threshold or max_single_event_score >= self._config.block_score_threshold:
            return self._config.high_severity_reduction_fraction
        if aggregate_event_score >= self._config.reduce_score_threshold or max_single_event_score >= self._config.reduce_score_threshold:
            return self._config.medium_severity_reduction_fraction
        if aggregate_event_score >= self._config.caution_score_threshold or max_single_event_score >= self._config.caution_score_threshold:
            return self._config.low_severity_reduction_fraction
        return 0.0

    def _decide(
        self,
        *,
        timestamp: datetime,
        snapshot: EventRiskSnapshot,
        breakdown: EventRiskBreakdown,
    ) -> EventRiskDecision:
        if breakdown.aggregate_event_score >= self._config.block_score_threshold:
            return self._build_decision(
                timestamp=timestamp,
                level="BLOCKED",
                allow_new_entries=False,
                require_position_reduction=snapshot.has_open_risk,
                recommended_reduction_fraction=breakdown.reduction_fraction,
                hedge_urgency="IMMEDIATE",
                reason="EVENT_BLOCK_THRESHOLD",
                breakdown=breakdown,
                snapshot=snapshot,
            )
        if breakdown.aggregate_event_score >= self._config.reduce_score_threshold:
            return self._build_decision(
                timestamp=timestamp,
                level="REDUCED",
                allow_new_entries=False,
                require_position_reduction=snapshot.has_open_risk,
                recommended_reduction_fraction=breakdown.reduction_fraction,
                hedge_urgency="HIGH",
                reason="EVENT_REDUCTION_THRESHOLD",
                breakdown=breakdown,
                snapshot=snapshot,
            )
        if breakdown.aggregate_event_score >= self._config.caution_score_threshold:
            return self._build_decision(
                timestamp=timestamp,
                level="CAUTION",
                allow_new_entries=True,
                require_position_reduction=False,
                recommended_reduction_fraction=breakdown.reduction_fraction,
                hedge_urgency="ELEVATED",
                reason="EVENT_CAUTION_THRESHOLD",
                breakdown=breakdown,
                snapshot=snapshot,
            )
        return self._build_decision(
            timestamp=timestamp,
            level="NORMAL",
            allow_new_entries=True,
            require_position_reduction=False,
            recommended_reduction_fraction=0.0,
            hedge_urgency="NORMAL",
            reason="NO_ACTIVE_EVENT_RISK",
            breakdown=breakdown,
            snapshot=snapshot,
        )

    def _build_decision(
        self,
        *,
        timestamp: datetime,
        level: str,
        allow_new_entries: bool,
        require_position_reduction: bool,
        recommended_reduction_fraction: float,
        hedge_urgency: str,
        reason: str,
        breakdown: EventRiskBreakdown,
        snapshot: EventRiskSnapshot,
    ) -> EventRiskDecision:
        return EventRiskDecision(
            timestamp=timestamp,
            level=level,
            allow_new_entries=allow_new_entries,
            require_position_reduction=require_position_reduction,
            recommended_reduction_fraction=recommended_reduction_fraction,
            hedge_urgency=hedge_urgency,
            reason=reason,
            breakdown=breakdown,
            metadata={
                "underlying_symbol": snapshot.underlying_symbol,
                "instrument_type": snapshot.instrument_type,
                "processed_snapshots": self._processed_snapshots + 1,
            },
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _clamp(value: float, *, lower: float, upper: float) -> float:
    if lower > upper:
        raise EventRiskComputationError("invalid clamp bounds")
    return max(lower, min(value, upper))


__all__ = [
    "EventRiskComputationError",
    "EventRiskConfig",
    "EventRiskBreakdown",
    "EventRiskDecision",
    "EventRiskEngine",
    "EventRiskError",
    "EventRiskInputError",
    "EventRiskSnapshot",
    "RiskEvent",
]
