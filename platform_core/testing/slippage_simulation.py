# FILE: platform_core/testing/slippage_simulation.py
"""
platform_core.testing.slippage_simulation
═══════════════════════════════════════════════════════════════════════
Production-grade deterministic slippage simulation framework for the
institutional Indian index futures and options derivatives intelligence and
execution platform.

This module provides replay-safe execution-cost estimation for historical and
synthetic validation workflows. It preserves no-hindsight execution semantics
by operating only on contemporaneously available quote, spread, volatility,
and participation inputs supplied for each order attempt.

Scope:
- Deterministic slippage estimation for futures and options.
- Spread, impact, urgency, and liquidity-aware cost decomposition.
- Partial-fill handling with replay-safe fill snapshots.
- Configurable model parameters without exchange hardcoding.
- JSON-safe outputs for Redis/PostgreSQL/TimescaleDB persistence.
- Foundation for later latency and execution validation integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
from decimal import getcontext
from enum import Enum


getcontext().prec = 28


class SlippageSimulationError(RuntimeError):
    """Base exception for slippage simulation failures."""


class SlippageConfigurationError(SlippageSimulationError):
    """Raised when slippage model inputs or configuration are invalid."""


class InstrumentType(str, Enum):
    """Supported traded instrument types."""

    INDEX_FUTURE = "INDEX_FUTURE"
    INDEX_OPTION = "INDEX_OPTION"


class OrderSide(str, Enum):
    """Explicit order side enumeration."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class SlippageModelParameters:
    """Configurable model parameters for deterministic slippage estimation."""

    impact_coefficient: Decimal = Decimal("0.45")
    impact_exponent: Decimal = Decimal("0.50")
    urgency_coefficient: Decimal = Decimal("0.20")
    volatility_coefficient: Decimal = Decimal("0.15")
    partial_fill_penalty_coefficient: Decimal = Decimal("0.10")
    max_participation_rate: Decimal = Decimal("0.25")
    min_fill_ratio: Decimal = Decimal("0.05")
    price_quantization: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.impact_coefficient < 0:
            raise ValueError("impact_coefficient must be non-negative")
        if self.impact_exponent < 0:
            raise ValueError("impact_exponent must be non-negative")
        if self.urgency_coefficient < 0:
            raise ValueError("urgency_coefficient must be non-negative")
        if self.volatility_coefficient < 0:
            raise ValueError("volatility_coefficient must be non-negative")
        if self.partial_fill_penalty_coefficient < 0:
            raise ValueError("partial_fill_penalty_coefficient must be non-negative")
        if self.max_participation_rate <= 0:
            raise ValueError("max_participation_rate must be positive")
        if not Decimal("0") < self.min_fill_ratio <= Decimal("1"):
            raise ValueError("min_fill_ratio must be within (0, 1]")
        if self.price_quantization <= 0:
            raise ValueError("price_quantization must be positive")

    def to_dict(self) -> dict[str, str]:
        return {
            "impact_coefficient": str(self.impact_coefficient),
            "impact_exponent": str(self.impact_exponent),
            "urgency_coefficient": str(self.urgency_coefficient),
            "volatility_coefficient": str(self.volatility_coefficient),
            "partial_fill_penalty_coefficient": str(self.partial_fill_penalty_coefficient),
            "max_participation_rate": str(self.max_participation_rate),
            "min_fill_ratio": str(self.min_fill_ratio),
            "price_quantization": str(self.price_quantization),
        }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Replay-safe market context used for one execution estimate."""

    timestamp: datetime
    instrument_type: InstrumentType
    instrument_id: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    top_of_book_quantity: int
    interval_volume: int
    realized_volatility: Decimal
    urgency_score: Decimal = Decimal("0")
    lot_size: int = 1
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0 or self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("prices must be positive")
        if self.bid_price > self.ask_price:
            raise ValueError("bid_price cannot exceed ask_price")
        if self.top_of_book_quantity < 0:
            raise ValueError("top_of_book_quantity cannot be negative")
        if self.interval_volume <= 0:
            raise ValueError("interval_volume must be positive")
        if self.realized_volatility < 0:
            raise ValueError("realized_volatility must be non-negative")
        if not Decimal("0") <= self.urgency_score <= Decimal("1"):
            raise ValueError("urgency_score must be within [0, 1]")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.quantity % self.lot_size != 0:
            raise ValueError("quantity must align with lot_size")

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> Decimal:
        return (self.ask_price + self.bid_price) / Decimal("2")

    def to_dict(self) -> dict[str, str | int | dict[str, str]]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "instrument_type": self.instrument_type.value,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "reference_price": str(self.reference_price),
            "bid_price": str(self.bid_price),
            "ask_price": str(self.ask_price),
            "top_of_book_quantity": self.top_of_book_quantity,
            "interval_volume": self.interval_volume,
            "realized_volatility": str(self.realized_volatility),
            "urgency_score": str(self.urgency_score),
            "lot_size": self.lot_size,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class FillEstimate:
    """One deterministic fill estimate component."""

    filled_quantity: int
    fill_price: Decimal
    slippage_per_unit: Decimal
    spread_cost: Decimal
    impact_cost: Decimal
    urgency_cost: Decimal
    volatility_cost: Decimal
    partial_fill_penalty: Decimal

    def to_dict(self) -> dict[str, str | int]:
        return {
            "filled_quantity": self.filled_quantity,
            "fill_price": str(self.fill_price),
            "slippage_per_unit": str(self.slippage_per_unit),
            "spread_cost": str(self.spread_cost),
            "impact_cost": str(self.impact_cost),
            "urgency_cost": str(self.urgency_cost),
            "volatility_cost": str(self.volatility_cost),
            "partial_fill_penalty": str(self.partial_fill_penalty),
        }


@dataclass(frozen=True, slots=True)
class SlippageEstimate:
    """Serializable deterministic order-level slippage estimate."""

    context: ExecutionContext
    fill_estimate: FillEstimate
    unfilled_quantity: int
    participation_rate: Decimal
    fill_ratio: Decimal
    total_slippage_cost: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context.to_dict(),
            "fill_estimate": self.fill_estimate.to_dict(),
            "unfilled_quantity": self.unfilled_quantity,
            "participation_rate": str(self.participation_rate),
            "fill_ratio": str(self.fill_ratio),
            "total_slippage_cost": str(self.total_slippage_cost),
        }


class SlippageSimulator:
    """Deterministic slippage simulator with no-hindsight execution assumptions."""

    def __init__(self, *, parameters: SlippageModelParameters | None = None) -> None:
        self._parameters = parameters or SlippageModelParameters()
        self._history: list[SlippageEstimate] = []

    @property
    def parameters(self) -> SlippageModelParameters:
        return self._parameters

    @property
    def history(self) -> tuple[SlippageEstimate, ...]:
        return tuple(self._history)

    def simulate(self, *, context: ExecutionContext) -> SlippageEstimate:
        available_quantity = min(
            context.quantity,
            max(context.top_of_book_quantity, 0) + max(context.interval_volume // max(context.lot_size, 1), 0) * context.lot_size,
        )
        participation_rate = Decimal(context.quantity) / Decimal(context.interval_volume)
        capped_participation = min(participation_rate, self._parameters.max_participation_rate)
        fill_ratio = Decimal(available_quantity) / Decimal(context.quantity)
        if fill_ratio < self._parameters.min_fill_ratio:
            available_quantity = 0
            fill_ratio = Decimal("0")
        spread_cost = context.spread / Decimal("2")
        impact_cost = self._parameters.impact_coefficient * (capped_participation ** self._parameters.impact_exponent)
        volatility_cost = self._parameters.volatility_coefficient * context.realized_volatility
        urgency_cost = self._parameters.urgency_coefficient * context.urgency_score
        partial_fill_penalty = self._parameters.partial_fill_penalty_coefficient * (Decimal("1") - fill_ratio)
        slippage_per_unit = spread_cost + impact_cost + volatility_cost + urgency_cost + partial_fill_penalty
        directional_slippage = slippage_per_unit if context.side is OrderSide.BUY else (-slippage_per_unit)
        raw_fill_price = context.reference_price + directional_slippage
        fill_price = _quantize_price(raw_fill_price, self._parameters.price_quantization)
        fill_estimate = FillEstimate(
            filled_quantity=available_quantity,
            fill_price=fill_price,
            slippage_per_unit=_quantize_price(abs(fill_price - context.reference_price), self._parameters.price_quantization),
            spread_cost=_quantize_price(spread_cost, self._parameters.price_quantization),
            impact_cost=_quantize_price(impact_cost, self._parameters.price_quantization),
            urgency_cost=_quantize_price(urgency_cost, self._parameters.price_quantization),
            volatility_cost=_quantize_price(volatility_cost, self._parameters.price_quantization),
            partial_fill_penalty=_quantize_price(partial_fill_penalty, self._parameters.price_quantization),
        )
        total_slippage_cost = _quantize_price(
            fill_estimate.slippage_per_unit * Decimal(fill_estimate.filled_quantity),
            self._parameters.price_quantization,
        )
        estimate = SlippageEstimate(
            context=context,
            fill_estimate=fill_estimate,
            unfilled_quantity=context.quantity - available_quantity,
            participation_rate=_quantize_ratio(participation_rate),
            fill_ratio=_quantize_ratio(fill_ratio),
            total_slippage_cost=total_slippage_cost,
        )
        self._history.append(estimate)
        return estimate

    def snapshot(self) -> dict[str, object]:
        return {
            "parameters": self._parameters.to_dict(),
            "history": [item.to_dict() for item in self._history],
        }



def _quantize_price(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise SlippageConfigurationError("price quantization step must be positive")
    units = (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (units * step).quantize(step)



def _quantize_ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ExecutionContext",
    "FillEstimate",
    "InstrumentType",
    "OrderSide",
    "SlippageConfigurationError",
    "SlippageEstimate",
    "SlippageModelParameters",
    "SlippageSimulationError",
    "SlippageSimulator",
]
