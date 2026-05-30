# FILE: platform_core/risk/portfolio_heat.py
"""
platform_core.risk.portfolio_heat
═══════════════════════════════════════════════════════════════════════
Production-grade institutional portfolio heat engine specialized for Indian
index futures and options workflows.

The engine aggregates open-position risk into deterministic portfolio heat
metrics using only finalized, causal inputs. It supports:
- Open risk aggregation across positions.
- Concentration and correlation-adjusted heat.
- Directional long/short side heat decomposition.
- SPAN-like margin-awareness inputs without reproducing exchange SPAN logic.
- Liquidity and crowding stress overlays.
- Replay-safe serializable state and outputs.

This module does not calculate exchange margin requirements itself and does not
place or manage orders. It is a risk-monitoring component intended for use by
portfolio controls, kill switches, and orchestration layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import fabs
from math import isfinite
from typing import Sequence


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class PortfolioHeatError(RuntimeError):
    """Base exception for portfolio heat failures."""


class PortfolioHeatInputError(PortfolioHeatError):
    """Raised when portfolio heat inputs are invalid or causally unsafe."""


class PortfolioHeatComputationError(PortfolioHeatError):
    """Raised when portfolio heat computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class PortfolioHeatConfig:
    """Configuration for portfolio heat aggregation and scaling."""

    max_portfolio_heat_fraction: float = 0.020
    warning_heat_fraction: float = 0.012
    critical_heat_fraction: float = 0.018
    concentration_threshold_fraction: float = 0.40
    correlation_penalty_weight: float = 0.60
    liquidity_penalty_weight: float = 0.45
    crowding_penalty_weight: float = 0.35
    span_buffer_weight: float = 0.25
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        bounded_fields = {
            "max_portfolio_heat_fraction": self.max_portfolio_heat_fraction,
            "warning_heat_fraction": self.warning_heat_fraction,
            "critical_heat_fraction": self.critical_heat_fraction,
            "concentration_threshold_fraction": self.concentration_threshold_fraction,
            "correlation_penalty_weight": self.correlation_penalty_weight,
            "liquidity_penalty_weight": self.liquidity_penalty_weight,
            "crowding_penalty_weight": self.crowding_penalty_weight,
            "span_buffer_weight": self.span_buffer_weight,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not self.warning_heat_fraction <= self.critical_heat_fraction <= self.max_portfolio_heat_fraction:
            raise ValueError("warning, critical, and maximum heat fractions must satisfy warning <= critical <= maximum")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class PortfolioContext:
    """Portfolio-level context required for heat monitoring."""

    equity: float
    available_cash: float
    current_span_margin: float
    projected_span_margin_after_buffer: float

    def __post_init__(self) -> None:
        numeric_values = {
            "equity": self.equity,
            "available_cash": self.available_cash,
            "current_span_margin": self.current_span_margin,
            "projected_span_margin_after_buffer": self.projected_span_margin_after_buffer,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.equity <= 0.0:
            raise ValueError("equity must be positive")
        if self.available_cash < 0.0 or self.current_span_margin < 0.0 or self.projected_span_margin_after_buffer < 0.0:
            raise ValueError("cash and margin fields must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "equity": self.equity,
            "available_cash": self.available_cash,
            "current_span_margin": self.current_span_margin,
            "projected_span_margin_after_buffer": self.projected_span_margin_after_buffer,
        }


@dataclass(frozen=True, slots=True)
class OpenRiskPosition:
    """Serializable open-position snapshot for heat aggregation."""

    symbol: str
    timestamp: datetime
    direction: str
    instrument_type: str
    quantity: int
    entry_price: float
    current_price: float
    stop_price: float
    contract_multiplier: float
    risk_amount: float
    notional_exposure: float
    correlation_to_book: float
    liquidity_score: float
    crowding_score: float

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        direction = self.direction.upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if not self.instrument_type.strip():
            raise ValueError("instrument_type must be non-empty")
        numeric_values = {
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "stop_price": self.stop_price,
            "contract_multiplier": self.contract_multiplier,
            "risk_amount": self.risk_amount,
            "notional_exposure": self.notional_exposure,
            "correlation_to_book": self.correlation_to_book,
            "liquidity_score": self.liquidity_score,
            "crowding_score": self.crowding_score,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero")
        if self.entry_price <= 0.0 or self.current_price <= 0.0 or self.stop_price <= 0.0:
            raise ValueError("price fields must be positive")
        if self.contract_multiplier <= 0.0:
            raise ValueError("contract_multiplier must be positive")
        if self.risk_amount < 0.0 or self.notional_exposure < 0.0:
            raise ValueError("risk_amount and notional_exposure must be non-negative")
        bounded_zero_one = {
            "correlation_to_book": self.correlation_to_book,
            "liquidity_score": self.liquidity_score,
            "crowding_score": self.crowding_score,
        }
        for field_name, field_value in bounded_zero_one.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")

    @property
    def normalized_direction(self) -> str:
        return self.direction.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "symbol": self.symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "direction": self.normalized_direction,
            "instrument_type": self.instrument_type,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "stop_price": self.stop_price,
            "contract_multiplier": self.contract_multiplier,
            "risk_amount": self.risk_amount,
            "notional_exposure": self.notional_exposure,
            "correlation_to_book": self.correlation_to_book,
            "liquidity_score": self.liquidity_score,
            "crowding_score": self.crowding_score,
        }


@dataclass(frozen=True, slots=True)
class PortfolioHeatBreakdown:
    """Interpretable decomposition of portfolio heat metrics."""

    gross_open_risk: float
    long_side_risk: float
    short_side_risk: float
    gross_heat_fraction: float
    concentration_penalty: float
    correlation_penalty: float
    liquidity_penalty: float
    crowding_penalty: float
    span_margin_penalty: float
    adjusted_heat_fraction: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gross_open_risk": self.gross_open_risk,
            "long_side_risk": self.long_side_risk,
            "short_side_risk": self.short_side_risk,
            "gross_heat_fraction": self.gross_heat_fraction,
            "concentration_penalty": self.concentration_penalty,
            "correlation_penalty": self.correlation_penalty,
            "liquidity_penalty": self.liquidity_penalty,
            "crowding_penalty": self.crowding_penalty,
            "span_margin_penalty": self.span_margin_penalty,
            "adjusted_heat_fraction": self.adjusted_heat_fraction,
        }


@dataclass(frozen=True, slots=True)
class PortfolioHeatResult:
    """Deterministic portfolio heat evaluation result."""

    timestamp: datetime
    total_positions: int
    status: str
    heat_remaining_fraction: float
    utilization_fraction: float
    concentration_fraction: float
    breakdown: PortfolioHeatBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "total_positions": self.total_positions,
            "status": self.status,
            "heat_remaining_fraction": self.heat_remaining_fraction,
            "utilization_fraction": self.utilization_fraction,
            "concentration_fraction": self.concentration_fraction,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class PortfolioHeatState:
    """Serializable engine state for replay persistence."""

    processed_snapshots: int
    last_timestamp: datetime | None
    last_adjusted_heat_fraction: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_snapshots": self.processed_snapshots,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_adjusted_heat_fraction": self.last_adjusted_heat_fraction,
        }


class PortfolioHeatEngine:
    """Institutional-grade deterministic portfolio heat engine."""

    def __init__(self, *, config: PortfolioHeatConfig | None = None) -> None:
        self._config = config or PortfolioHeatConfig()
        self._processed_snapshots = 0
        self._last_timestamp: datetime | None = None
        self._last_result: PortfolioHeatResult | None = None

    @property
    def state(self) -> PortfolioHeatState:
        return PortfolioHeatState(
            processed_snapshots=self._processed_snapshots,
            last_timestamp=self._last_timestamp,
            last_adjusted_heat_fraction=None if self._last_result is None else self._last_result.breakdown.adjusted_heat_fraction,
        )

    def reset(self) -> None:
        self._processed_snapshots = 0
        self._last_timestamp = None
        self._last_result = None

    def evaluate(
        self,
        *,
        timestamp: datetime,
        portfolio: PortfolioContext,
        positions: Sequence[OpenRiskPosition],
        valuation_timestamp: datetime | None = None,
    ) -> PortfolioHeatResult:
        normalized_timestamp = _ensure_utc(timestamp)
        self._validate_timestamp(timestamp=normalized_timestamp, valuation_timestamp=valuation_timestamp)
        gross_open_risk = sum(position.risk_amount for position in positions)
        long_side_risk = sum(position.risk_amount for position in positions if position.normalized_direction == "LONG")
        short_side_risk = sum(position.risk_amount for position in positions if position.normalized_direction == "SHORT")
        gross_heat_fraction = self._safe_ratio(gross_open_risk, portfolio.equity)
        concentration_fraction = self._concentration_fraction(positions=positions, gross_open_risk=gross_open_risk)
        concentration_penalty = self._concentration_penalty(concentration_fraction=concentration_fraction)
        correlation_penalty = self._correlation_penalty(positions=positions)
        liquidity_penalty = self._liquidity_penalty(positions=positions)
        crowding_penalty = self._crowding_penalty(positions=positions)
        span_margin_penalty = self._span_margin_penalty(portfolio=portfolio)
        adjusted_heat_fraction = gross_heat_fraction * (
            1.0
            + concentration_penalty
            + correlation_penalty
            + liquidity_penalty
            + crowding_penalty
            + span_margin_penalty
        )
        utilization_fraction = self._safe_ratio(adjusted_heat_fraction, self._config.max_portfolio_heat_fraction)
        heat_remaining_fraction = max(0.0, self._config.max_portfolio_heat_fraction - adjusted_heat_fraction)
        status = self._status(adjusted_heat_fraction=adjusted_heat_fraction)
        result = PortfolioHeatResult(
            timestamp=normalized_timestamp,
            total_positions=len(positions),
            status=status,
            heat_remaining_fraction=heat_remaining_fraction,
            utilization_fraction=utilization_fraction,
            concentration_fraction=concentration_fraction,
            breakdown=PortfolioHeatBreakdown(
                gross_open_risk=gross_open_risk,
                long_side_risk=long_side_risk,
                short_side_risk=short_side_risk,
                gross_heat_fraction=gross_heat_fraction,
                concentration_penalty=concentration_penalty,
                correlation_penalty=correlation_penalty,
                liquidity_penalty=liquidity_penalty,
                crowding_penalty=crowding_penalty,
                span_margin_penalty=span_margin_penalty,
                adjusted_heat_fraction=adjusted_heat_fraction,
            ),
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "equity": portfolio.equity,
                "available_cash": portfolio.available_cash,
            },
        )
        self._processed_snapshots += 1
        self._last_timestamp = normalized_timestamp
        self._last_result = result
        return result

    def get_last_result(self) -> PortfolioHeatResult:
        if self._last_result is None:
            raise PortfolioHeatComputationError("no portfolio heat result is available")
        return self._last_result

    def _validate_timestamp(self, *, timestamp: datetime, valuation_timestamp: datetime | None) -> None:
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if timestamp > normalized_valuation_timestamp:
                raise PortfolioHeatInputError("timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise PortfolioHeatInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise PortfolioHeatInputError("timestamp must be non-decreasing for replay determinism")

    def _concentration_fraction(self, *, positions: Sequence[OpenRiskPosition], gross_open_risk: float) -> float:
        if gross_open_risk <= 0.0 or not positions:
            return 0.0
        largest_position_risk = max(position.risk_amount for position in positions)
        return self._safe_ratio(largest_position_risk, gross_open_risk)

    def _concentration_penalty(self, *, concentration_fraction: float) -> float:
        if concentration_fraction <= self._config.concentration_threshold_fraction:
            return 0.0
        excess = concentration_fraction - self._config.concentration_threshold_fraction
        denominator = max(1.0e-12, 1.0 - self._config.concentration_threshold_fraction)
        return excess / denominator

    def _correlation_penalty(self, *, positions: Sequence[OpenRiskPosition]) -> float:
        if not positions:
            return 0.0
        average_correlation = sum(position.correlation_to_book for position in positions) / len(positions)
        return average_correlation * self._config.correlation_penalty_weight

    def _liquidity_penalty(self, *, positions: Sequence[OpenRiskPosition]) -> float:
        if not positions:
            return 0.0
        illiquidity = sum(1.0 - position.liquidity_score for position in positions) / len(positions)
        return illiquidity * self._config.liquidity_penalty_weight

    def _crowding_penalty(self, *, positions: Sequence[OpenRiskPosition]) -> float:
        if not positions:
            return 0.0
        average_crowding = sum(position.crowding_score for position in positions) / len(positions)
        return average_crowding * self._config.crowding_penalty_weight

    def _span_margin_penalty(self, *, portfolio: PortfolioContext) -> float:
        margin_buffer = max(0.0, portfolio.projected_span_margin_after_buffer - portfolio.current_span_margin)
        margin_buffer_fraction = self._safe_ratio(margin_buffer, portfolio.equity)
        return margin_buffer_fraction * self._config.span_buffer_weight

    def _status(self, *, adjusted_heat_fraction: float) -> str:
        if adjusted_heat_fraction >= self._config.max_portfolio_heat_fraction:
            return "BREACH"
        if adjusted_heat_fraction >= self._config.critical_heat_fraction:
            return "CRITICAL"
        if adjusted_heat_fraction >= self._config.warning_heat_fraction:
            return "WARNING"
        return "NORMAL"

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0.0:
            return 0.0
        return numerator / denominator



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OpenRiskPosition",
    "PortfolioContext",
    "PortfolioHeatBreakdown",
    "PortfolioHeatComputationError",
    "PortfolioHeatConfig",
    "PortfolioHeatEngine",
    "PortfolioHeatError",
    "PortfolioHeatInputError",
    "PortfolioHeatResult",
    "PortfolioHeatState",
]
