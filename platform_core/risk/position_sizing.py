# FILE: platform_core/risk/position_sizing.py
"""
platform_core.risk.position_sizing
═══════════════════════════════════════════════════════════════════════
Production-grade institutional position sizing engine specialized for Indian
index futures and options workflows.

The engine computes deterministic, replay-safe trade sizing recommendations from
causal inputs only. It preserves capital-first risk controls and incorporates:
- Per-trade risk budgets.
- Volatility-adjusted sizing.
- Confidence-conditioned scaling.
- Liquidity-aware caps.
- Drawdown-aware de-risking.
- Portfolio heat and correlation throttles.
- Contract- and lot-based execution compatibility.

This module intentionally avoids exchange adapters and order placement logic.
All outputs are JSON-serializable and suitable for Redis, PostgreSQL,
TimescaleDB, and deterministic replay pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import fabs
from math import floor
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class PositionSizingError(RuntimeError):
    """Base exception for position sizing failures."""


class PositionSizingInputError(PositionSizingError):
    """Raised when sizing inputs are invalid or causally unsafe."""


class PositionSizingComputationError(PositionSizingError):
    """Raised when a safe position size cannot be computed."""


@dataclass(frozen=True, slots=True)
class PositionSizingConfig:
    """Configuration for institutional position sizing."""

    max_risk_fraction_per_trade: float = 0.005
    max_portfolio_heat_fraction: float = 0.020
    max_confidence_scale: float = 1.25
    min_confidence_scale: float = 0.25
    default_stop_multiple: float = 1.00
    max_volatility_ratio_for_full_risk: float = 1.00
    volatility_floor_ratio: float = 0.35
    liquidity_haircut_floor: float = 0.10
    drawdown_haircut_floor: float = 0.10
    correlation_haircut_floor: float = 0.15
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        bounded_positive_fields = {
            "max_risk_fraction_per_trade": self.max_risk_fraction_per_trade,
            "max_portfolio_heat_fraction": self.max_portfolio_heat_fraction,
            "max_confidence_scale": self.max_confidence_scale,
            "min_confidence_scale": self.min_confidence_scale,
            "default_stop_multiple": self.default_stop_multiple,
            "max_volatility_ratio_for_full_risk": self.max_volatility_ratio_for_full_risk,
            "volatility_floor_ratio": self.volatility_floor_ratio,
            "liquidity_haircut_floor": self.liquidity_haircut_floor,
            "drawdown_haircut_floor": self.drawdown_haircut_floor,
            "correlation_haircut_floor": self.correlation_haircut_floor,
        }
        for field_name, field_value in bounded_positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_risk_fraction_per_trade > 1.0:
            raise ValueError("max_risk_fraction_per_trade must not exceed 1")
        if self.max_portfolio_heat_fraction > 1.0:
            raise ValueError("max_portfolio_heat_fraction must not exceed 1")
        if self.min_confidence_scale > self.max_confidence_scale:
            raise ValueError("min_confidence_scale must be less than or equal to max_confidence_scale")
        if any(value > 1.0 for value in (self.volatility_floor_ratio, self.liquidity_haircut_floor, self.drawdown_haircut_floor, self.correlation_haircut_floor)):
            raise ValueError("floor ratios must not exceed 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    """Serializable snapshot of current portfolio-level risk constraints."""

    equity: float
    available_cash: float
    current_portfolio_heat: float
    current_drawdown_fraction: float
    max_allowed_drawdown_fraction: float
    correlation_exposure_fraction: float
    open_position_count: int

    def __post_init__(self) -> None:
        numeric_values = {
            "equity": self.equity,
            "available_cash": self.available_cash,
            "current_portfolio_heat": self.current_portfolio_heat,
            "current_drawdown_fraction": self.current_drawdown_fraction,
            "max_allowed_drawdown_fraction": self.max_allowed_drawdown_fraction,
            "correlation_exposure_fraction": self.correlation_exposure_fraction,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.equity <= 0.0:
            raise ValueError("equity must be positive")
        if self.available_cash < 0.0:
            raise ValueError("available_cash must be non-negative")
        bounded_zero_one = {
            "current_portfolio_heat": self.current_portfolio_heat,
            "current_drawdown_fraction": self.current_drawdown_fraction,
            "max_allowed_drawdown_fraction": self.max_allowed_drawdown_fraction,
            "correlation_exposure_fraction": self.correlation_exposure_fraction,
        }
        for field_name, field_value in bounded_zero_one.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.open_position_count < 0:
            raise ValueError("open_position_count must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "equity": self.equity,
            "available_cash": self.available_cash,
            "current_portfolio_heat": self.current_portfolio_heat,
            "current_drawdown_fraction": self.current_drawdown_fraction,
            "max_allowed_drawdown_fraction": self.max_allowed_drawdown_fraction,
            "correlation_exposure_fraction": self.correlation_exposure_fraction,
            "open_position_count": self.open_position_count,
        }


@dataclass(frozen=True, slots=True)
class PositionSizingRequest:
    """Causal trade sizing request."""

    underlying_symbol: str
    timestamp: datetime
    direction: str
    instrument_type: str
    entry_price: float
    stop_price: float
    contract_multiplier: float
    lot_size: int
    minimum_lot_count: int
    maximum_lot_count: int
    confidence_score: float
    actionability_score: float
    realized_volatility_ratio: float
    liquidity_score: float
    spread_bps: float
    slippage_bps_estimate: float
    expected_correlation_to_book: float
    portfolio: PortfolioRiskSnapshot
    explicit_risk_fraction_override: float | None = None
    stop_multiple: float | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        direction = self.direction.upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if not self.instrument_type.strip():
            raise ValueError("instrument_type must be non-empty")
        numeric_values = {
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "contract_multiplier": self.contract_multiplier,
            "confidence_score": self.confidence_score,
            "actionability_score": self.actionability_score,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "liquidity_score": self.liquidity_score,
            "spread_bps": self.spread_bps,
            "slippage_bps_estimate": self.slippage_bps_estimate,
            "expected_correlation_to_book": self.expected_correlation_to_book,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.entry_price <= 0.0 or self.stop_price <= 0.0:
            raise ValueError("entry_price and stop_price must be positive")
        if self.contract_multiplier <= 0.0:
            raise ValueError("contract_multiplier must be positive")
        bounded_zero_one = {
            "confidence_score": self.confidence_score,
            "actionability_score": self.actionability_score,
            "liquidity_score": self.liquidity_score,
            "expected_correlation_to_book": self.expected_correlation_to_book,
        }
        for field_name, field_value in bounded_zero_one.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if self.realized_volatility_ratio <= 0.0:
            raise ValueError("realized_volatility_ratio must be positive")
        if self.spread_bps < 0.0 or self.slippage_bps_estimate < 0.0:
            raise ValueError("spread_bps and slippage_bps_estimate must be non-negative")
        if self.lot_size <= 0 or self.minimum_lot_count <= 0 or self.maximum_lot_count <= 0:
            raise ValueError("lot sizing integers must be positive")
        if self.minimum_lot_count > self.maximum_lot_count:
            raise ValueError("minimum_lot_count must be less than or equal to maximum_lot_count")
        if self.explicit_risk_fraction_override is not None and not 0.0 < self.explicit_risk_fraction_override <= 1.0:
            raise ValueError("explicit_risk_fraction_override must be between 0 and 1 when provided")
        if self.stop_multiple is not None and self.stop_multiple <= 0.0:
            raise ValueError("stop_multiple must be positive when provided")

    @property
    def normalized_direction(self) -> str:
        return self.direction.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "direction": self.normalized_direction,
            "instrument_type": self.instrument_type,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "contract_multiplier": self.contract_multiplier,
            "lot_size": self.lot_size,
            "minimum_lot_count": self.minimum_lot_count,
            "maximum_lot_count": self.maximum_lot_count,
            "confidence_score": self.confidence_score,
            "actionability_score": self.actionability_score,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "liquidity_score": self.liquidity_score,
            "spread_bps": self.spread_bps,
            "slippage_bps_estimate": self.slippage_bps_estimate,
            "expected_correlation_to_book": self.expected_correlation_to_book,
            "portfolio": self.portfolio.to_dict(),
            "explicit_risk_fraction_override": self.explicit_risk_fraction_override,
            "stop_multiple": self.stop_multiple,
        }


@dataclass(frozen=True, slots=True)
class PositionSizingBreakdown:
    """Interpretable decomposition of position sizing controls."""

    base_risk_budget: float
    confidence_scale: float
    volatility_scale: float
    liquidity_scale: float
    drawdown_scale: float
    correlation_scale: float
    portfolio_heat_scale: float
    cash_scale: float
    effective_risk_budget: float
    per_unit_risk: float
    preliminary_units: float
    capped_units: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "base_risk_budget": self.base_risk_budget,
            "confidence_scale": self.confidence_scale,
            "volatility_scale": self.volatility_scale,
            "liquidity_scale": self.liquidity_scale,
            "drawdown_scale": self.drawdown_scale,
            "correlation_scale": self.correlation_scale,
            "portfolio_heat_scale": self.portfolio_heat_scale,
            "cash_scale": self.cash_scale,
            "effective_risk_budget": self.effective_risk_budget,
            "per_unit_risk": self.per_unit_risk,
            "preliminary_units": self.preliminary_units,
            "capped_units": self.capped_units,
        }


@dataclass(frozen=True, slots=True)
class PositionSizingResult:
    """Deterministic position sizing recommendation."""

    underlying_symbol: str
    timestamp: datetime
    direction: str
    recommended_units: int
    recommended_lots: int
    notional_exposure: float
    estimated_risk_amount: float
    estimated_round_trip_cost: float
    tradable: bool
    rejection_reason: str | None
    breakdown: PositionSizingBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "direction": self.direction,
            "recommended_units": self.recommended_units,
            "recommended_lots": self.recommended_lots,
            "notional_exposure": self.notional_exposure,
            "estimated_risk_amount": self.estimated_risk_amount,
            "estimated_round_trip_cost": self.estimated_round_trip_cost,
            "tradable": self.tradable,
            "rejection_reason": self.rejection_reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class PositionSizingState:
    """Serializable engine state for replay persistence."""

    processed_requests: int
    last_underlying_symbol: str | None
    last_timestamp: datetime | None
    last_recommended_units: int | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_requests": self.processed_requests,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_recommended_units": self.last_recommended_units,
        }


class PositionSizer:
    """Institutional-grade deterministic position sizing engine."""

    def __init__(self, *, config: PositionSizingConfig | None = None) -> None:
        self._config = config or PositionSizingConfig()
        self._processed_requests = 0
        self._last_underlying_symbol: str | None = None
        self._last_timestamp: datetime | None = None
        self._last_result: PositionSizingResult | None = None

    @property
    def state(self) -> PositionSizingState:
        return PositionSizingState(
            processed_requests=self._processed_requests,
            last_underlying_symbol=self._last_underlying_symbol,
            last_timestamp=self._last_timestamp,
            last_recommended_units=None if self._last_result is None else self._last_result.recommended_units,
        )

    def reset(self) -> None:
        self._processed_requests = 0
        self._last_underlying_symbol = None
        self._last_timestamp = None
        self._last_result = None

    def size_position(
        self,
        *,
        request: PositionSizingRequest,
        valuation_timestamp: datetime | None = None,
    ) -> PositionSizingResult:
        self._validate_request(request=request, valuation_timestamp=valuation_timestamp)
        normalized_timestamp = _ensure_utc(request.timestamp)
        stop_multiple = request.stop_multiple or self._config.default_stop_multiple
        price_distance = fabs(request.entry_price - request.stop_price) * stop_multiple
        per_unit_risk = price_distance * request.contract_multiplier
        if per_unit_risk <= 0.0:
            raise PositionSizingInputError("per-unit risk must be positive; entry_price and stop_price cannot be equal")

        base_risk_fraction = (
            request.explicit_risk_fraction_override
            if request.explicit_risk_fraction_override is not None
            else self._config.max_risk_fraction_per_trade
        )
        base_risk_budget = request.portfolio.equity * base_risk_fraction
        confidence_scale = self._confidence_scale(request=request)
        volatility_scale = self._volatility_scale(request=request)
        liquidity_scale = self._liquidity_scale(request=request)
        drawdown_scale = self._drawdown_scale(request=request)
        correlation_scale = self._correlation_scale(request=request)
        portfolio_heat_scale = self._portfolio_heat_scale(request=request)
        cash_scale = self._cash_scale(request=request)

        effective_risk_budget = (
            base_risk_budget
            * confidence_scale
            * volatility_scale
            * liquidity_scale
            * drawdown_scale
            * correlation_scale
            * portfolio_heat_scale
            * cash_scale
        )
        if effective_risk_budget < 0.0:
            raise PositionSizingComputationError("effective_risk_budget cannot be negative")

        preliminary_units = effective_risk_budget / per_unit_risk if per_unit_risk > 0.0 else 0.0
        capped_units = min(preliminary_units, request.maximum_lot_count * request.lot_size)
        lot_count = int(floor(capped_units / request.lot_size))
        tradable = True
        rejection_reason: str | None = None
        if lot_count < request.minimum_lot_count:
            lot_count = 0
            tradable = False
            rejection_reason = "INSUFFICIENT_RISK_BUDGET"
        recommended_units = lot_count * request.lot_size
        notional_exposure = recommended_units * request.entry_price * request.contract_multiplier
        estimated_risk_amount = recommended_units * per_unit_risk
        estimated_round_trip_cost = self._round_trip_cost(request=request, units=recommended_units)

        if request.portfolio.available_cash + 1.0e-12 < estimated_round_trip_cost:
            tradable = False
            rejection_reason = "INSUFFICIENT_CASH_FOR_ESTIMATED_COST"
            recommended_units = 0
            lot_count = 0
            notional_exposure = 0.0
            estimated_risk_amount = 0.0
            estimated_round_trip_cost = 0.0

        result = PositionSizingResult(
            underlying_symbol=request.underlying_symbol,
            timestamp=normalized_timestamp,
            direction=request.normalized_direction,
            recommended_units=recommended_units,
            recommended_lots=lot_count,
            notional_exposure=notional_exposure,
            estimated_risk_amount=estimated_risk_amount,
            estimated_round_trip_cost=estimated_round_trip_cost,
            tradable=tradable,
            rejection_reason=rejection_reason,
            breakdown=PositionSizingBreakdown(
                base_risk_budget=base_risk_budget,
                confidence_scale=confidence_scale,
                volatility_scale=volatility_scale,
                liquidity_scale=liquidity_scale,
                drawdown_scale=drawdown_scale,
                correlation_scale=correlation_scale,
                portfolio_heat_scale=portfolio_heat_scale,
                cash_scale=cash_scale,
                effective_risk_budget=effective_risk_budget,
                per_unit_risk=per_unit_risk,
                preliminary_units=preliminary_units,
                capped_units=capped_units,
            ),
            metadata={
                "processed_requests": self._processed_requests + 1,
                "instrument_type": request.instrument_type,
                "lot_size": request.lot_size,
                "stop_multiple": stop_multiple,
            },
        )
        self._processed_requests += 1
        self._last_underlying_symbol = request.underlying_symbol
        self._last_timestamp = normalized_timestamp
        self._last_result = result
        return result

    def get_last_result(self) -> PositionSizingResult:
        if self._last_result is None:
            raise PositionSizingComputationError("no position sizing result is available")
        return self._last_result

    def _validate_request(self, *, request: PositionSizingRequest, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(request.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise PositionSizingInputError("request timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise PositionSizingInputError("request is stale relative to valuation_timestamp")
        if self._last_underlying_symbol is not None and request.underlying_symbol != self._last_underlying_symbol:
            raise PositionSizingInputError("sizer instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise PositionSizingInputError("request timestamp must be non-decreasing for replay determinism")

    def _confidence_scale(self, *, request: PositionSizingRequest) -> float:
        raw_scale = 0.50 * request.confidence_score + 0.50 * request.actionability_score
        scaled = self._config.min_confidence_scale + raw_scale * (self._config.max_confidence_scale - self._config.min_confidence_scale)
        return self._clip(scaled, self._config.min_confidence_scale, self._config.max_confidence_scale)

    def _volatility_scale(self, *, request: PositionSizingRequest) -> float:
        if request.realized_volatility_ratio <= self._config.max_volatility_ratio_for_full_risk:
            return 1.0
        excess_ratio = request.realized_volatility_ratio / self._config.max_volatility_ratio_for_full_risk
        scaled = 1.0 / excess_ratio
        return self._clip(scaled, self._config.volatility_floor_ratio, 1.0)

    def _liquidity_scale(self, *, request: PositionSizingRequest) -> float:
        spread_penalty = max(0.0, 1.0 - min(request.spread_bps, 40.0) / 40.0)
        slippage_penalty = max(0.0, 1.0 - min(request.slippage_bps_estimate, 50.0) / 50.0)
        raw = 0.55 * request.liquidity_score + 0.25 * spread_penalty + 0.20 * slippage_penalty
        return self._clip(raw, self._config.liquidity_haircut_floor, 1.0)

    def _drawdown_scale(self, *, request: PositionSizingRequest) -> float:
        max_allowed = request.portfolio.max_allowed_drawdown_fraction
        if max_allowed <= 0.0:
            return self._config.drawdown_haircut_floor
        utilization = request.portfolio.current_drawdown_fraction / max_allowed
        scaled = 1.0 - utilization
        return self._clip(scaled, self._config.drawdown_haircut_floor, 1.0)

    def _correlation_scale(self, *, request: PositionSizingRequest) -> float:
        combined_exposure = max(request.expected_correlation_to_book, request.portfolio.correlation_exposure_fraction)
        scaled = 1.0 - combined_exposure
        return self._clip(scaled, self._config.correlation_haircut_floor, 1.0)

    def _portfolio_heat_scale(self, *, request: PositionSizingRequest) -> float:
        limit = self._config.max_portfolio_heat_fraction
        if limit <= 0.0:
            return 0.0
        remaining_heat = max(0.0, limit - request.portfolio.current_portfolio_heat)
        if remaining_heat <= 0.0:
            return 0.0
        return self._clip(remaining_heat / limit, 0.0, 1.0)

    @staticmethod
    def _cash_scale(*, request: PositionSizingRequest) -> float:
        max_notional_affordable = request.portfolio.available_cash / max(request.entry_price * request.contract_multiplier, 1.0e-12)
        affordable_units = max_notional_affordable
        maximum_units = request.maximum_lot_count * request.lot_size
        if maximum_units <= 0:
            return 0.0
        return PositionSizer._clip(affordable_units / maximum_units, 0.0, 1.0)

    @staticmethod
    def _round_trip_cost(*, request: PositionSizingRequest, units: int) -> float:
        if units <= 0:
            return 0.0
        notional = units * request.entry_price * request.contract_multiplier
        total_bps = request.spread_bps + request.slippage_bps_estimate
        return notional * (total_bps / 10000.0)

    @staticmethod
    def _clip(value: float, minimum: float, maximum: float) -> float:
        if value <= minimum:
            return minimum
        if value >= maximum:
            return maximum
        return value



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PortfolioRiskSnapshot",
    "PositionSizer",
    "PositionSizingBreakdown",
    "PositionSizingComputationError",
    "PositionSizingConfig",
    "PositionSizingError",
    "PositionSizingInputError",
    "PositionSizingRequest",
    "PositionSizingResult",
    "PositionSizingState",
]
