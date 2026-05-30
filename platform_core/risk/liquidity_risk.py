# FILE: platform_core/risk/liquidity_risk.py
"""
platform_core.risk.liquidity_risk
═══════════════════════════════════════════════════════════════════════
Production-grade liquidity risk engine specialized for Indian index futures and
options workflows.

The engine evaluates finalized liquidity conditions and converts order intent,
spread conditions, order-book depth, participation burden, and market-impact
approximations into deterministic liquidity-risk decisions. It preserves
replay safety by using only causal snapshots and serializable internal state.

Core controls:
- Bid/ask spread stress classification.
- Participation-rate and notional-share governance.
- Order-book depth sufficiency checks.
- Square-root style market-impact approximation.
- Slippage-aware trading gates and quantity haircuts.
- Exit-aware downgrade logic that remains safety-first.

This module does not route orders or mutate positions. It emits structured
liquidity risk decisions for downstream risk gating, sizing, and execution
components.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import floor
from math import isfinite
from math import sqrt


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class LiquidityRiskError(RuntimeError):
    """Base exception for liquidity risk failures."""


class LiquidityRiskInputError(LiquidityRiskError):
    """Raised when liquidity risk inputs are invalid or causally unsafe."""


class LiquidityRiskComputationError(LiquidityRiskError):
    """Raised when liquidity risk computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class LiquidityRiskConfig:
    """Configuration for deterministic liquidity-risk evaluation."""

    warning_spread_bps: float = 8.0
    block_spread_bps: float = 18.0
    max_participation_rate_fraction: float = 0.08
    warning_participation_rate_fraction: float = 0.04
    min_top_of_book_fill_fraction: float = 0.30
    min_depth_cover_fraction: float = 1.20
    impact_volatility_scalar: float = 1.00
    warning_impact_bps: float = 10.0
    block_impact_bps: float = 22.0
    warning_slippage_bps: float = 10.0
    block_slippage_bps: float = 20.0
    max_notional_share_of_adv_fraction: float = 0.015
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "warning_spread_bps": self.warning_spread_bps,
            "block_spread_bps": self.block_spread_bps,
            "max_participation_rate_fraction": self.max_participation_rate_fraction,
            "warning_participation_rate_fraction": self.warning_participation_rate_fraction,
            "min_top_of_book_fill_fraction": self.min_top_of_book_fill_fraction,
            "min_depth_cover_fraction": self.min_depth_cover_fraction,
            "impact_volatility_scalar": self.impact_volatility_scalar,
            "warning_impact_bps": self.warning_impact_bps,
            "block_impact_bps": self.block_impact_bps,
            "warning_slippage_bps": self.warning_slippage_bps,
            "block_slippage_bps": self.block_slippage_bps,
            "max_notional_share_of_adv_fraction": self.max_notional_share_of_adv_fraction,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        bounded_fields = {
            "max_participation_rate_fraction": self.max_participation_rate_fraction,
            "warning_participation_rate_fraction": self.warning_participation_rate_fraction,
            "min_top_of_book_fill_fraction": self.min_top_of_book_fill_fraction,
            "max_notional_share_of_adv_fraction": self.max_notional_share_of_adv_fraction,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.warning_spread_bps > self.block_spread_bps:
            raise ValueError("warning_spread_bps must be less than or equal to block_spread_bps")
        if self.warning_impact_bps > self.block_impact_bps:
            raise ValueError("warning_impact_bps must be less than or equal to block_impact_bps")
        if self.warning_slippage_bps > self.block_slippage_bps:
            raise ValueError("warning_slippage_bps must be less than or equal to block_slippage_bps")
        if self.warning_participation_rate_fraction > self.max_participation_rate_fraction:
            raise ValueError("warning_participation_rate_fraction must be less than or equal to max_participation_rate_fraction")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class LiquidityRiskSnapshot:
    """Serializable finalized snapshot for liquidity-risk evaluation."""

    timestamp: datetime
    instrument_type: str
    symbol: str
    side: str
    is_exit_order: bool
    intended_contracts: int
    lot_size: int
    price: float
    contract_multiplier: float
    bid_price: float
    ask_price: float
    top_bid_contracts: int
    top_ask_contracts: int
    visible_depth_contracts: int
    expected_bar_volume_contracts: int
    average_daily_notional: float
    realized_volatility_fraction: float
    expected_slippage_bps: float

    def __post_init__(self) -> None:
        string_fields = {
            "instrument_type": self.instrument_type,
            "symbol": self.symbol,
            "side": self.side,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        numeric_values = {
            "price": self.price,
            "contract_multiplier": self.contract_multiplier,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "average_daily_notional": self.average_daily_notional,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "expected_slippage_bps": self.expected_slippage_bps,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.intended_contracts <= 0:
            raise ValueError("intended_contracts must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.price <= 0.0 or self.bid_price <= 0.0 or self.ask_price <= 0.0:
            raise ValueError("price, bid_price, and ask_price must be positive")
        if self.bid_price > self.ask_price:
            raise ValueError("bid_price cannot exceed ask_price")
        if self.contract_multiplier <= 0.0:
            raise ValueError("contract_multiplier must be positive")
        if self.top_bid_contracts < 0 or self.top_ask_contracts < 0:
            raise ValueError("top-of-book quantities must be non-negative")
        if self.visible_depth_contracts <= 0:
            raise ValueError("visible_depth_contracts must be positive")
        if self.expected_bar_volume_contracts <= 0:
            raise ValueError("expected_bar_volume_contracts must be positive")
        if self.average_daily_notional <= 0.0:
            raise ValueError("average_daily_notional must be positive")
        if self.realized_volatility_fraction <= 0.0:
            raise ValueError("realized_volatility_fraction must be positive")
        if self.expected_slippage_bps < 0.0:
            raise ValueError("expected_slippage_bps must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "instrument_type": self.instrument_type,
            "symbol": self.symbol,
            "side": self.side,
            "is_exit_order": self.is_exit_order,
            "intended_contracts": self.intended_contracts,
            "lot_size": self.lot_size,
            "price": self.price,
            "contract_multiplier": self.contract_multiplier,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "top_bid_contracts": self.top_bid_contracts,
            "top_ask_contracts": self.top_ask_contracts,
            "visible_depth_contracts": self.visible_depth_contracts,
            "expected_bar_volume_contracts": self.expected_bar_volume_contracts,
            "average_daily_notional": self.average_daily_notional,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "expected_slippage_bps": self.expected_slippage_bps,
        }


@dataclass(frozen=True, slots=True)
class LiquidityRiskBreakdown:
    """Interpretable liquidity-risk decomposition."""

    spread_bps: float
    top_of_book_fill_fraction: float
    depth_cover_fraction: float
    participation_rate_fraction: float
    notional_share_of_adv_fraction: float
    impact_bps: float
    spread_triggered: bool
    participation_triggered: bool
    impact_triggered: bool
    slippage_triggered: bool
    depth_triggered: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "spread_bps": self.spread_bps,
            "top_of_book_fill_fraction": self.top_of_book_fill_fraction,
            "depth_cover_fraction": self.depth_cover_fraction,
            "participation_rate_fraction": self.participation_rate_fraction,
            "notional_share_of_adv_fraction": self.notional_share_of_adv_fraction,
            "impact_bps": self.impact_bps,
            "spread_triggered": self.spread_triggered,
            "participation_triggered": self.participation_triggered,
            "impact_triggered": self.impact_triggered,
            "slippage_triggered": self.slippage_triggered,
            "depth_triggered": self.depth_triggered,
        }


@dataclass(frozen=True, slots=True)
class LiquidityRiskDecision:
    """Serializable liquidity-risk decision for downstream execution gating."""

    timestamp: datetime
    level: str
    approved_contracts: int
    approved_lots: int
    approved_notional: float
    should_block: bool
    exit_only: bool
    reason: str
    breakdown: LiquidityRiskBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "level": self.level,
            "approved_contracts": self.approved_contracts,
            "approved_lots": self.approved_lots,
            "approved_notional": self.approved_notional,
            "should_block": self.should_block,
            "exit_only": self.exit_only,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


class LiquidityRiskEngine:
    """Deterministic liquidity-risk engine."""

    def __init__(self, *, config: LiquidityRiskConfig | None = None) -> None:
        self._config = config or LiquidityRiskConfig()
        self._last_timestamp: datetime | None = None
        self._processed_snapshots = 0
        self._last_decision: LiquidityRiskDecision | None = None

    def reset(self) -> None:
        self._last_timestamp = None
        self._processed_snapshots = 0
        self._last_decision = None

    def evaluate(
        self,
        *,
        snapshot: LiquidityRiskSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> LiquidityRiskDecision:
        timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        mid_price = (snapshot.bid_price + snapshot.ask_price) / 2.0
        if mid_price <= 0.0:
            raise LiquidityRiskComputationError("mid_price must be positive")

        spread_bps = ((snapshot.ask_price - snapshot.bid_price) / mid_price) * 10000.0
        if snapshot.side.strip().upper() == "BUY":
            top_of_book_contracts = snapshot.top_ask_contracts
        else:
            top_of_book_contracts = snapshot.top_bid_contracts
        top_of_book_fill_fraction = min(1.0, top_of_book_contracts / snapshot.intended_contracts) if snapshot.intended_contracts > 0 else 0.0
        depth_cover_fraction = snapshot.visible_depth_contracts / snapshot.intended_contracts
        participation_rate_fraction = snapshot.intended_contracts / snapshot.expected_bar_volume_contracts
        intended_notional = snapshot.intended_contracts * snapshot.price * snapshot.contract_multiplier
        notional_share_of_adv_fraction = intended_notional / snapshot.average_daily_notional

        impact_bps = (
            self._config.impact_volatility_scalar
            * snapshot.realized_volatility_fraction
            * sqrt(max(participation_rate_fraction, 0.0))
            * 10000.0
        )

        spread_triggered = spread_bps >= self._config.block_spread_bps
        participation_triggered = (
            participation_rate_fraction >= self._config.max_participation_rate_fraction
            or notional_share_of_adv_fraction >= self._config.max_notional_share_of_adv_fraction
        )
        impact_triggered = impact_bps >= self._config.block_impact_bps
        slippage_triggered = snapshot.expected_slippage_bps >= self._config.block_slippage_bps
        depth_triggered = (
            top_of_book_fill_fraction < self._config.min_top_of_book_fill_fraction
            or depth_cover_fraction < self._config.min_depth_cover_fraction
        )

        breakdown = LiquidityRiskBreakdown(
            spread_bps=spread_bps,
            top_of_book_fill_fraction=top_of_book_fill_fraction,
            depth_cover_fraction=depth_cover_fraction,
            participation_rate_fraction=participation_rate_fraction,
            notional_share_of_adv_fraction=notional_share_of_adv_fraction,
            impact_bps=impact_bps,
            spread_triggered=spread_triggered,
            participation_triggered=participation_triggered,
            impact_triggered=impact_triggered,
            slippage_triggered=slippage_triggered,
            depth_triggered=depth_triggered,
        )

        decision = self._decide(timestamp=timestamp, snapshot=snapshot, breakdown=breakdown)
        self._last_timestamp = timestamp
        self._processed_snapshots += 1
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> LiquidityRiskDecision:
        if self._last_decision is None:
            raise LiquidityRiskComputationError("no liquidity-risk decision is available")
        return self._last_decision

    def _validate_snapshot(self, *, snapshot: LiquidityRiskSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise LiquidityRiskInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise LiquidityRiskInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise LiquidityRiskInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _decide(
        self,
        *,
        timestamp: datetime,
        snapshot: LiquidityRiskSnapshot,
        breakdown: LiquidityRiskBreakdown,
    ) -> LiquidityRiskDecision:
        warning_triggers = (
            breakdown.spread_bps >= self._config.warning_spread_bps
            or breakdown.participation_rate_fraction >= self._config.warning_participation_rate_fraction
            or breakdown.impact_bps >= self._config.warning_impact_bps
            or snapshot.expected_slippage_bps >= self._config.warning_slippage_bps
        )
        block_triggers = (
            breakdown.spread_triggered
            or breakdown.participation_triggered
            or breakdown.impact_triggered
            or breakdown.slippage_triggered
            or breakdown.depth_triggered
        )

        if block_triggers:
            if snapshot.is_exit_order:
                haircut_fraction = self._compute_haircut_fraction(breakdown=breakdown, warning_triggers=True)
                downgraded_contracts = self._round_to_lots(
                    contracts=max(1, floor(snapshot.intended_contracts * haircut_fraction)),
                    lot_size=snapshot.lot_size,
                )
                if downgraded_contracts <= 0:
                    downgraded_contracts = snapshot.lot_size
                return self._build_decision(
                    timestamp=timestamp,
                    level="EXIT_DEGRADED",
                    approved_contracts=min(snapshot.intended_contracts, downgraded_contracts),
                    should_block=False,
                    exit_only=True,
                    reason="EXIT_LIQUIDITY_DEGRADED",
                    breakdown=breakdown,
                    snapshot=snapshot,
                )
            return self._build_decision(
                timestamp=timestamp,
                level="BLOCKED",
                approved_contracts=0,
                should_block=True,
                exit_only=False,
                reason="LIQUIDITY_BLOCK_TRIGGER",
                breakdown=breakdown,
                snapshot=snapshot,
            )

        if warning_triggers:
            haircut_fraction = self._compute_haircut_fraction(breakdown=breakdown, warning_triggers=True)
            reduced_contracts = self._round_to_lots(
                contracts=max(snapshot.lot_size, floor(snapshot.intended_contracts * haircut_fraction)),
                lot_size=snapshot.lot_size,
            )
            approved_contracts = min(snapshot.intended_contracts, reduced_contracts)
            return self._build_decision(
                timestamp=timestamp,
                level="REDUCED",
                approved_contracts=approved_contracts,
                should_block=False,
                exit_only=False,
                reason="LIQUIDITY_WARNING_REDUCTION",
                breakdown=breakdown,
                snapshot=snapshot,
            )

        return self._build_decision(
            timestamp=timestamp,
            level="NORMAL",
            approved_contracts=self._round_to_lots(contracts=snapshot.intended_contracts, lot_size=snapshot.lot_size),
            should_block=False,
            exit_only=False,
            reason="LIQUIDITY_ACCEPTABLE",
            breakdown=breakdown,
            snapshot=snapshot,
        )

    def _compute_haircut_fraction(
        self,
        *,
        breakdown: LiquidityRiskBreakdown,
        warning_triggers: bool,
    ) -> float:
        haircut = 1.0
        if warning_triggers:
            haircut *= 0.80
        if breakdown.spread_triggered:
            haircut *= 0.75
        if breakdown.participation_triggered:
            haircut *= 0.60
        if breakdown.impact_triggered:
            haircut *= 0.70
        if breakdown.slippage_triggered:
            haircut *= 0.75
        if breakdown.depth_triggered:
            haircut *= 0.60
        return max(0.10, min(haircut, 1.0))

    def _build_decision(
        self,
        *,
        timestamp: datetime,
        level: str,
        approved_contracts: int,
        should_block: bool,
        exit_only: bool,
        reason: str,
        breakdown: LiquidityRiskBreakdown,
        snapshot: LiquidityRiskSnapshot,
    ) -> LiquidityRiskDecision:
        normalized_contracts = 0 if should_block else max(0, approved_contracts)
        approved_lots = normalized_contracts // snapshot.lot_size if normalized_contracts > 0 else 0
        approved_notional = normalized_contracts * snapshot.price * snapshot.contract_multiplier
        return LiquidityRiskDecision(
            timestamp=timestamp,
            level=level,
            approved_contracts=normalized_contracts,
            approved_lots=approved_lots,
            approved_notional=approved_notional,
            should_block=should_block,
            exit_only=exit_only,
            reason=reason,
            breakdown=breakdown,
            metadata={
                "symbol": snapshot.symbol,
                "instrument_type": snapshot.instrument_type,
                "processed_snapshots": self._processed_snapshots + 1,
            },
        )

    @staticmethod
    def _round_to_lots(*, contracts: int, lot_size: int) -> int:
        if contracts <= 0:
            return 0
        rounded = (contracts // lot_size) * lot_size
        if rounded <= 0:
            return 0
        return rounded



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "LiquidityRiskComputationError",
    "LiquidityRiskConfig",
    "LiquidityRiskBreakdown",
    "LiquidityRiskDecision",
    "LiquidityRiskEngine",
    "LiquidityRiskError",
    "LiquidityRiskInputError",
    "LiquidityRiskSnapshot",
]
