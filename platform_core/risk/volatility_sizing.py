# FILE: platform_core/risk/volatility_sizing.py
"""
platform_core.risk.volatility_sizing
═══════════════════════════════════════════════════════════════════════
Production-grade volatility-adjusted sizing engine specialized for Indian index
futures and options workflows.

The engine transforms finalized market volatility, premium volatility, stop
structure, liquidity, and portfolio risk-budget inputs into deterministic
position sizing recommendations. It is designed to preserve replay safety and
no-hindsight behavior by consuming only causal snapshots and by avoiding any
forward-looking assumptions.

Core sizing capabilities:
- Realized-volatility and ATR-style stop-distance normalization.
- Risk-budget sizing in currency, contracts, and lots.
- Volatility-targeting with configurable floor and cap.
- Liquidity throttling and concentration control.
- Premium-based sizing support for options instruments.
- Conservative fail-closed behavior when inputs are invalid.

This module emits serializable sizing outputs for downstream order management,
risk gating, and execution layers. It does not submit orders or mutate
portfolio state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import floor
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class VolatilitySizingError(RuntimeError):
    """Base exception for volatility sizing failures."""


class VolatilitySizingInputError(VolatilitySizingError):
    """Raised when volatility sizing inputs are invalid or causally unsafe."""


class VolatilitySizingComputationError(VolatilitySizingError):
    """Raised when a volatility sizing computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class VolatilitySizingConfig:
    """Configuration for volatility-adjusted position sizing."""

    base_risk_fraction: float = 0.0025
    target_annualized_volatility_fraction: float = 0.12
    volatility_floor_fraction: float = 0.05
    volatility_cap_fraction: float = 0.60
    max_leverage_multiplier: float = 1.50
    min_leverage_multiplier: float = 0.10
    atr_stop_multiple: float = 1.50
    options_premium_haircut_fraction: float = 0.15
    liquidity_haircut_fraction: float = 0.20
    max_notional_share_of_average_volume: float = 0.015
    max_contracts_per_signal: int = 5000
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_floats = {
            "base_risk_fraction": self.base_risk_fraction,
            "target_annualized_volatility_fraction": self.target_annualized_volatility_fraction,
            "volatility_floor_fraction": self.volatility_floor_fraction,
            "volatility_cap_fraction": self.volatility_cap_fraction,
            "max_leverage_multiplier": self.max_leverage_multiplier,
            "min_leverage_multiplier": self.min_leverage_multiplier,
            "atr_stop_multiple": self.atr_stop_multiple,
            "max_notional_share_of_average_volume": self.max_notional_share_of_average_volume,
        }
        for field_name, field_value in positive_floats.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        bounded_fields = {
            "base_risk_fraction": self.base_risk_fraction,
            "target_annualized_volatility_fraction": self.target_annualized_volatility_fraction,
            "volatility_floor_fraction": self.volatility_floor_fraction,
            "volatility_cap_fraction": self.volatility_cap_fraction,
            "options_premium_haircut_fraction": self.options_premium_haircut_fraction,
            "liquidity_haircut_fraction": self.liquidity_haircut_fraction,
            "max_notional_share_of_average_volume": self.max_notional_share_of_average_volume,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.min_leverage_multiplier > self.max_leverage_multiplier:
            raise ValueError("min_leverage_multiplier must be less than or equal to max_leverage_multiplier")
        if self.volatility_floor_fraction > self.volatility_cap_fraction:
            raise ValueError("volatility_floor_fraction must be less than or equal to volatility_cap_fraction")
        if self.max_contracts_per_signal <= 0:
            raise ValueError("max_contracts_per_signal must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class VolatilitySizingSnapshot:
    """Serializable finalized input snapshot for sizing computation."""

    timestamp: datetime
    instrument_type: str
    underlying_symbol: str
    entry_price: float
    stop_price: float
    lot_size: int
    contract_multiplier: float
    account_equity: float
    free_equity: float
    realized_volatility_fraction: float
    intraday_atr_points: float
    premium_volatility_fraction: float | None
    option_premium: float | None
    implied_volatility_fraction: float | None
    expected_fill_slippage_bps: float
    average_daily_notional: float
    current_signal_conviction: float
    liquidity_score_fraction: float
    portfolio_heat_fraction: float
    correlation_scalar: float

    def __post_init__(self) -> None:
        string_fields = {
            "instrument_type": self.instrument_type,
            "underlying_symbol": self.underlying_symbol,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        numeric_values = {
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "contract_multiplier": self.contract_multiplier,
            "account_equity": self.account_equity,
            "free_equity": self.free_equity,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "intraday_atr_points": self.intraday_atr_points,
            "expected_fill_slippage_bps": self.expected_fill_slippage_bps,
            "average_daily_notional": self.average_daily_notional,
            "current_signal_conviction": self.current_signal_conviction,
            "liquidity_score_fraction": self.liquidity_score_fraction,
            "portfolio_heat_fraction": self.portfolio_heat_fraction,
            "correlation_scalar": self.correlation_scalar,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.entry_price <= 0.0 or self.stop_price <= 0.0:
            raise ValueError("entry_price and stop_price must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.contract_multiplier <= 0.0:
            raise ValueError("contract_multiplier must be positive")
        if self.account_equity <= 0.0 or self.free_equity <= 0.0:
            raise ValueError("account_equity and free_equity must be positive")
        if self.realized_volatility_fraction <= 0.0:
            raise ValueError("realized_volatility_fraction must be positive")
        if self.intraday_atr_points <= 0.0:
            raise ValueError("intraday_atr_points must be positive")
        if self.expected_fill_slippage_bps < 0.0:
            raise ValueError("expected_fill_slippage_bps must be non-negative")
        if self.average_daily_notional <= 0.0:
            raise ValueError("average_daily_notional must be positive")
        if not 0.0 < self.current_signal_conviction <= 1.0:
            raise ValueError("current_signal_conviction must be between 0 and 1")
        if not 0.0 < self.liquidity_score_fraction <= 1.0:
            raise ValueError("liquidity_score_fraction must be between 0 and 1")
        if not 0.0 <= self.portfolio_heat_fraction <= 1.0:
            raise ValueError("portfolio_heat_fraction must be between 0 and 1")
        if self.correlation_scalar <= 0.0:
            raise ValueError("correlation_scalar must be positive")
        optional_positive = {
            "premium_volatility_fraction": self.premium_volatility_fraction,
            "option_premium": self.option_premium,
            "implied_volatility_fraction": self.implied_volatility_fraction,
        }
        for field_name, field_value in optional_positive.items():
            if field_value is not None and field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "instrument_type": self.instrument_type,
            "underlying_symbol": self.underlying_symbol,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "lot_size": self.lot_size,
            "contract_multiplier": self.contract_multiplier,
            "account_equity": self.account_equity,
            "free_equity": self.free_equity,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "intraday_atr_points": self.intraday_atr_points,
            "premium_volatility_fraction": self.premium_volatility_fraction,
            "option_premium": self.option_premium,
            "implied_volatility_fraction": self.implied_volatility_fraction,
            "expected_fill_slippage_bps": self.expected_fill_slippage_bps,
            "average_daily_notional": self.average_daily_notional,
            "current_signal_conviction": self.current_signal_conviction,
            "liquidity_score_fraction": self.liquidity_score_fraction,
            "portfolio_heat_fraction": self.portfolio_heat_fraction,
            "correlation_scalar": self.correlation_scalar,
        }


@dataclass(frozen=True, slots=True)
class VolatilitySizingBreakdown:
    """Interpretable decomposition of the sizing workflow."""

    normalized_volatility_fraction: float
    volatility_leverage_multiplier: float
    conviction_multiplier: float
    heat_multiplier: float
    liquidity_multiplier: float
    correlation_multiplier: float
    premium_multiplier: float
    slippage_multiplier: float
    risk_budget_currency: float
    stop_distance_points: float
    per_contract_risk_currency: float
    volatility_target_notional: float
    liquidity_capped_notional: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "normalized_volatility_fraction": self.normalized_volatility_fraction,
            "volatility_leverage_multiplier": self.volatility_leverage_multiplier,
            "conviction_multiplier": self.conviction_multiplier,
            "heat_multiplier": self.heat_multiplier,
            "liquidity_multiplier": self.liquidity_multiplier,
            "correlation_multiplier": self.correlation_multiplier,
            "premium_multiplier": self.premium_multiplier,
            "slippage_multiplier": self.slippage_multiplier,
            "risk_budget_currency": self.risk_budget_currency,
            "stop_distance_points": self.stop_distance_points,
            "per_contract_risk_currency": self.per_contract_risk_currency,
            "volatility_target_notional": self.volatility_target_notional,
            "liquidity_capped_notional": self.liquidity_capped_notional,
        }


@dataclass(frozen=True, slots=True)
class VolatilitySizingDecision:
    """Serializable sizing recommendation for downstream execution."""

    timestamp: datetime
    recommended_contracts: int
    recommended_lots: int
    recommended_notional: float
    capped_by_liquidity: bool
    capped_by_contract_limit: bool
    capped_by_free_equity: bool
    should_trade: bool
    reason: str
    breakdown: VolatilitySizingBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "recommended_contracts": self.recommended_contracts,
            "recommended_lots": self.recommended_lots,
            "recommended_notional": self.recommended_notional,
            "capped_by_liquidity": self.capped_by_liquidity,
            "capped_by_contract_limit": self.capped_by_contract_limit,
            "capped_by_free_equity": self.capped_by_free_equity,
            "should_trade": self.should_trade,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


class VolatilitySizingEngine:
    """Deterministic volatility-adjusted sizing engine."""

    def __init__(self, *, config: VolatilitySizingConfig | None = None) -> None:
        self._config = config or VolatilitySizingConfig()
        self._last_timestamp: datetime | None = None
        self._processed_snapshots = 0
        self._last_decision: VolatilitySizingDecision | None = None

    def reset(self) -> None:
        self._last_timestamp = None
        self._processed_snapshots = 0
        self._last_decision = None

    def evaluate(
        self,
        *,
        snapshot: VolatilitySizingSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> VolatilitySizingDecision:
        timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        normalized_vol = _clamp(
            snapshot.realized_volatility_fraction,
            lower=self._config.volatility_floor_fraction,
            upper=self._config.volatility_cap_fraction,
        )
        vol_leverage = _clamp(
            self._config.target_annualized_volatility_fraction / normalized_vol,
            lower=self._config.min_leverage_multiplier,
            upper=self._config.max_leverage_multiplier,
        )
        conviction_multiplier = _clamp(snapshot.current_signal_conviction, lower=0.25, upper=1.0)
        heat_multiplier = _clamp(1.0 - snapshot.portfolio_heat_fraction, lower=0.10, upper=1.0)
        liquidity_multiplier = _clamp(
            snapshot.liquidity_score_fraction * (1.0 - self._config.liquidity_haircut_fraction),
            lower=0.10,
            upper=1.0,
        )
        correlation_multiplier = _clamp(1.0 / snapshot.correlation_scalar, lower=0.25, upper=1.0)
        slippage_multiplier = _clamp(1.0 - (snapshot.expected_fill_slippage_bps / 1000.0), lower=0.50, upper=1.0)
        premium_multiplier = self._compute_premium_multiplier(snapshot=snapshot)

        risk_budget_currency = (
            min(snapshot.account_equity, snapshot.free_equity)
            * self._config.base_risk_fraction
            * vol_leverage
            * conviction_multiplier
            * heat_multiplier
            * liquidity_multiplier
            * correlation_multiplier
            * premium_multiplier
            * slippage_multiplier
        )
        if risk_budget_currency <= 0.0:
            decision = self._no_trade_decision(timestamp=timestamp, reason="NON_POSITIVE_RISK_BUDGET")
            self._update_state(timestamp=timestamp, decision=decision)
            return decision

        structural_stop = abs(snapshot.entry_price - snapshot.stop_price)
        atr_stop = snapshot.intraday_atr_points * self._config.atr_stop_multiple
        stop_distance_points = max(structural_stop, atr_stop)
        per_contract_risk_currency = stop_distance_points * snapshot.contract_multiplier
        if per_contract_risk_currency <= 0.0:
            raise VolatilitySizingComputationError("per_contract_risk_currency must be positive")

        raw_contracts = int(floor(risk_budget_currency / per_contract_risk_currency))
        if raw_contracts <= 0:
            decision = self._no_trade_decision(timestamp=timestamp, reason="RISK_BUDGET_BELOW_MINIMUM_CONTRACT")
            self._update_state(timestamp=timestamp, decision=decision)
            return decision

        target_notional_contracts = (
            snapshot.account_equity
            * vol_leverage
            / (snapshot.entry_price * snapshot.contract_multiplier)
        )
        notional_capped_contracts = int(max(1, floor(target_notional_contracts)))
        liquidity_notional_cap = snapshot.average_daily_notional * self._config.max_notional_share_of_average_volume
        liquidity_capped_contracts = int(
            max(
                1,
                floor(liquidity_notional_cap / (snapshot.entry_price * snapshot.contract_multiplier)),
            )
        )
        free_equity_capped_contracts = int(
            max(
                1,
                floor(snapshot.free_equity / (snapshot.entry_price * snapshot.contract_multiplier)),
            )
        )
        contract_limit_capped_contracts = self._config.max_contracts_per_signal

        recommended_contracts = min(
            raw_contracts,
            notional_capped_contracts,
            liquidity_capped_contracts,
            free_equity_capped_contracts,
            contract_limit_capped_contracts,
        )
        if recommended_contracts <= 0:
            decision = self._no_trade_decision(timestamp=timestamp, reason="CONTRACT_CAP_REDUCED_TO_ZERO")
            self._update_state(timestamp=timestamp, decision=decision)
            return decision

        recommended_lots = recommended_contracts // snapshot.lot_size
        if recommended_lots <= 0:
            decision = self._no_trade_decision(timestamp=timestamp, reason="LOT_ROUNDING_REDUCED_TO_ZERO")
            self._update_state(timestamp=timestamp, decision=decision)
            return decision

        final_contracts = recommended_lots * snapshot.lot_size
        final_notional = final_contracts * snapshot.entry_price * snapshot.contract_multiplier
        breakdown = VolatilitySizingBreakdown(
            normalized_volatility_fraction=normalized_vol,
            volatility_leverage_multiplier=vol_leverage,
            conviction_multiplier=conviction_multiplier,
            heat_multiplier=heat_multiplier,
            liquidity_multiplier=liquidity_multiplier,
            correlation_multiplier=correlation_multiplier,
            premium_multiplier=premium_multiplier,
            slippage_multiplier=slippage_multiplier,
            risk_budget_currency=risk_budget_currency,
            stop_distance_points=stop_distance_points,
            per_contract_risk_currency=per_contract_risk_currency,
            volatility_target_notional=target_notional_contracts * snapshot.entry_price * snapshot.contract_multiplier,
            liquidity_capped_notional=liquidity_notional_cap,
        )
        decision = VolatilitySizingDecision(
            timestamp=timestamp,
            recommended_contracts=final_contracts,
            recommended_lots=recommended_lots,
            recommended_notional=final_notional,
            capped_by_liquidity=final_contracts < raw_contracts and liquidity_capped_contracts <= final_contracts,
            capped_by_contract_limit=final_contracts < raw_contracts and contract_limit_capped_contracts <= final_contracts,
            capped_by_free_equity=final_contracts < raw_contracts and free_equity_capped_contracts <= final_contracts,
            should_trade=final_contracts > 0,
            reason="SIZE_COMPUTED",
            breakdown=breakdown,
            metadata={
                "instrument_type": snapshot.instrument_type,
                "underlying_symbol": snapshot.underlying_symbol,
                "processed_snapshots": self._processed_snapshots + 1,
            },
        )
        self._update_state(timestamp=timestamp, decision=decision)
        return decision

    def get_last_decision(self) -> VolatilitySizingDecision:
        if self._last_decision is None:
            raise VolatilitySizingComputationError("no volatility sizing decision is available")
        return self._last_decision

    def _validate_snapshot(self, *, snapshot: VolatilitySizingSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise VolatilitySizingInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise VolatilitySizingInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise VolatilitySizingInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _compute_premium_multiplier(self, *, snapshot: VolatilitySizingSnapshot) -> float:
        instrument_type = snapshot.instrument_type.strip().upper()
        if instrument_type in {"OPTION", "OPTIONS", "FNO_OPTION", "INDEX_OPTION"}:
            premium_vol = snapshot.premium_volatility_fraction or snapshot.realized_volatility_fraction
            implied_vol = snapshot.implied_volatility_fraction or premium_vol
            option_premium = snapshot.option_premium or snapshot.entry_price
            premium_richness = _clamp(implied_vol / max(snapshot.realized_volatility_fraction, 1e-9), lower=0.50, upper=2.00)
            premium_pressure = _clamp(option_premium / max(snapshot.entry_price, 1e-9), lower=0.10, upper=1.50)
            volatility_penalty = _clamp(1.0 / max(premium_vol, 1e-9), lower=0.25, upper=2.0)
            raw_multiplier = volatility_penalty / max(premium_richness * premium_pressure, 1e-9)
            return _clamp(raw_multiplier * (1.0 - self._config.options_premium_haircut_fraction), lower=0.20, upper=1.0)
        return 1.0

    def _no_trade_decision(self, *, timestamp: datetime, reason: str) -> VolatilitySizingDecision:
        breakdown = VolatilitySizingBreakdown(
            normalized_volatility_fraction=0.0,
            volatility_leverage_multiplier=0.0,
            conviction_multiplier=0.0,
            heat_multiplier=0.0,
            liquidity_multiplier=0.0,
            correlation_multiplier=0.0,
            premium_multiplier=0.0,
            slippage_multiplier=0.0,
            risk_budget_currency=0.0,
            stop_distance_points=0.0,
            per_contract_risk_currency=0.0,
            volatility_target_notional=0.0,
            liquidity_capped_notional=0.0,
        )
        return VolatilitySizingDecision(
            timestamp=timestamp,
            recommended_contracts=0,
            recommended_lots=0,
            recommended_notional=0.0,
            capped_by_liquidity=False,
            capped_by_contract_limit=False,
            capped_by_free_equity=False,
            should_trade=False,
            reason=reason,
            breakdown=breakdown,
            metadata={"processed_snapshots": self._processed_snapshots + 1},
        )

    def _update_state(self, *, timestamp: datetime, decision: VolatilitySizingDecision) -> None:
        self._last_timestamp = timestamp
        self._processed_snapshots += 1
        self._last_decision = decision



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _clamp(value: float, *, lower: float, upper: float) -> float:
    if lower > upper:
        raise VolatilitySizingComputationError("invalid clamp bounds")
    return max(lower, min(value, upper))


__all__ = [
    "VolatilitySizingBreakdown",
    "VolatilitySizingComputationError",
    "VolatilitySizingConfig",
    "VolatilitySizingDecision",
    "VolatilitySizingEngine",
    "VolatilitySizingError",
    "VolatilitySizingInputError",
    "VolatilitySizingSnapshot",
]
