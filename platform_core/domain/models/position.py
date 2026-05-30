# FILE: platform_core/domain/models/position.py
"""
platform_core.domain.models.position
═══════════════════════════════════════════════════════════════════════
Production-grade position domain models for Indian Index Futures & Options
workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- position-lifecycle aware,
- suitable for live and replay portfolio workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import ExchangeCode, InstrumentType, OptionRight, PositionDirection, PositionState
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class PositionCostBasis(BaseModel):
    """Cost basis and mark inputs required to value a position snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    average_entry_price: Decimal = Field(gt=0)
    average_exit_price: Decimal | None = None
    mark_price: Decimal | None = None
    settlement_price: Decimal | None = None
    point_value: Decimal = Field(default=Decimal("1"), gt=0)
    cumulative_fees: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator(
        "average_entry_price",
        "average_exit_price",
        "mark_price",
        "settlement_price",
        "point_value",
        "cumulative_fees",
        mode="before",
    )
    @classmethod
    def normalize_decimals(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        normalized = safe_decimal(value)
        if normalized < 0:
            raise ValueError("cost basis decimal values cannot be negative")
        return normalized

    @model_validator(mode="after")
    def validate_cost_basis(self) -> "PositionCostBasis":
        if self.point_value <= 0:
            raise ValueError("point_value must be positive")
        return self


class PositionQuantitySummary(BaseModel):
    """Canonical quantity decomposition for open and closed position volume."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    net_quantity: int = Field(ge=0)
    buy_quantity: int = Field(ge=0)
    sell_quantity: int = Field(ge=0)
    opened_quantity: int = Field(ge=0)
    closed_quantity: int = Field(ge=0)
    peak_quantity: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_quantities(self) -> "PositionQuantitySummary":
        if self.peak_quantity < self.net_quantity:
            raise ValueError("peak_quantity cannot be less than net_quantity")
        if self.peak_quantity < self.opened_quantity:
            raise ValueError("peak_quantity cannot be less than opened_quantity")
        if self.buy_quantity + self.sell_quantity == 0 and self.net_quantity != 0:
            raise ValueError("non-zero net_quantity requires executed quantity")
        if self.closed_quantity > self.opened_quantity:
            raise ValueError("closed_quantity cannot exceed opened_quantity")
        return self


class PositionGreeksSnapshot(BaseModel):
    """Optional Greeks attached to a position snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    delta: Decimal = Decimal("0")
    gamma: Decimal = Decimal("0")
    theta: Decimal = Decimal("0")
    vega: Decimal = Decimal("0")
    rho: Decimal = Decimal("0")

    @field_validator("delta", "gamma", "theta", "vega", "rho", mode="before")
    @classmethod
    def normalize_greeks(cls, value: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        return safe_decimal(value)


class PositionStateSnapshot(BaseModel):
    """Canonical position lifecycle snapshot for portfolio, risk, and replay systems."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    position_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    strategy_id: str | None = None
    signal_id: str | None = None

    exchange: ExchangeCode
    instrument_type: InstrumentType
    instrument_symbol: str = Field(min_length=1)
    underlying_symbol: str = Field(min_length=1)
    option_right: OptionRight | None = None
    option_expiry: datetime | None = None
    option_strike: Decimal | None = None

    direction: PositionDirection
    state: PositionState
    quantity_summary: PositionQuantitySummary
    cost_basis: PositionCostBasis
    greeks: PositionGreeksSnapshot = Field(default_factory=PositionGreeksSnapshot)

    realized_pnl: Decimal = Decimal("0")
    carry_pnl: Decimal = Decimal("0")
    hedge_ratio: Decimal | None = None
    margin_used: Decimal | None = None

    exchange_timestamp: datetime
    received_timestamp: datetime
    created_timestamp: datetime
    updated_timestamp: datetime
    opened_timestamp: datetime
    closed_timestamp: datetime | None = None
    snapshot_sequence_number: int = Field(ge=0)

    is_replay: bool = False
    source: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "position_id",
        "portfolio_id",
        "strategy_id",
        "signal_id",
        "instrument_symbol",
        "underlying_symbol",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_text_fields(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator(
        "option_expiry",
        "exchange_timestamp",
        "received_timestamp",
        "created_timestamp",
        "updated_timestamp",
        "opened_timestamp",
        "closed_timestamp",
        mode="before",
    )
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator("option_strike", "realized_pnl", "carry_pnl", "hedge_ratio", "margin_used", mode="before")
    @classmethod
    def normalize_optional_decimals(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return safe_decimal(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return tuple()
        if isinstance(value, str):
            items = [value]
        else:
            items = list(value)
        normalized = tuple(item.strip() for item in map(str, items) if str(item).strip())
        return tuple(dict.fromkeys(normalized))

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_required_text_fields(self) -> "PositionStateSnapshot":
        for field_name in ("position_id", "portfolio_id", "instrument_symbol", "underlying_symbol", "source"):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "PositionStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.created_timestamp < self.exchange_timestamp:
            raise ValueError("created_timestamp cannot precede exchange_timestamp")
        if self.updated_timestamp < self.created_timestamp:
            raise ValueError("updated_timestamp cannot precede created_timestamp")
        if self.opened_timestamp < self.created_timestamp:
            raise ValueError("opened_timestamp cannot precede created_timestamp")
        if self.closed_timestamp is not None and self.closed_timestamp < self.opened_timestamp:
            raise ValueError("closed_timestamp cannot precede opened_timestamp")
        if self.option_expiry is not None and self.option_expiry < self.exchange_timestamp:
            raise ValueError("option_expiry cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_instrument_consistency(self) -> "PositionStateSnapshot":
        if self.instrument_type.name == "OPTION":
            if self.option_right is None or self.option_expiry is None or self.option_strike is None:
                raise ValueError("option positions require option_right, option_expiry, and option_strike")
        return self

    @model_validator(mode="after")
    def validate_position_state_consistency(self) -> "PositionStateSnapshot":
        net_quantity = self.quantity_summary.net_quantity
        if self.state.name == "OPEN" and net_quantity == 0:
            raise ValueError("open positions must have non-zero net_quantity")
        if self.state.name == "CLOSED" and net_quantity != 0:
            raise ValueError("closed positions must have zero net_quantity")
        if self.state.name == "CLOSED" and self.closed_timestamp is None:
            raise ValueError("closed positions require closed_timestamp")
        if self.state.name != "CLOSED" and self.closed_timestamp is not None:
            raise ValueError("closed_timestamp is only valid for closed positions")
        return self

    @model_validator(mode="after")
    def validate_direction_consistency(self) -> "PositionStateSnapshot":
        net_quantity = self.quantity_summary.net_quantity
        if self.direction.name == "FLAT" and net_quantity != 0:
            raise ValueError("flat direction requires zero net_quantity")
        if self.direction.name != "FLAT" and net_quantity == 0 and self.state.name != "CLOSED":
            raise ValueError("non-flat direction requires non-zero net_quantity unless position is closed")
        return self

    @computed_field(return_type=Decimal | None)
    @property
    def market_value(self) -> Decimal | None:
        if self.cost_basis.mark_price is None:
            return None
        return self.cost_basis.mark_price * Decimal(self.quantity_summary.net_quantity) * self.cost_basis.point_value

    @computed_field(return_type=Decimal)
    @property
    def gross_cost(self) -> Decimal:
        return self.cost_basis.average_entry_price * Decimal(self.quantity_summary.opened_quantity) * self.cost_basis.point_value

    @computed_field(return_type=Decimal | None)
    @property
    def unrealized_pnl(self) -> Decimal | None:
        if self.cost_basis.mark_price is None:
            return None
        quantity = Decimal(self.quantity_summary.net_quantity)
        price_diff = self.cost_basis.mark_price - self.cost_basis.average_entry_price
        directional_multiplier = Decimal("1") if self.direction.name == "LONG" else Decimal("-1")
        if self.direction.name == "FLAT":
            return Decimal("0")
        return price_diff * quantity * self.cost_basis.point_value * directional_multiplier

    @computed_field(return_type=Decimal)
    @property
    def net_realized_pnl(self) -> Decimal:
        return self.realized_pnl - self.cost_basis.cumulative_fees

    @computed_field(return_type=Decimal | None)
    @property
    def total_pnl(self) -> Decimal | None:
        unrealized = self.unrealized_pnl
        if unrealized is None:
            return None
        return self.net_realized_pnl + unrealized + self.carry_pnl

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.underlying_symbol

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        decimal_fields = {
            "option_strike": self.option_strike,
            "realized_pnl": self.realized_pnl,
            "carry_pnl": self.carry_pnl,
            "hedge_ratio": self.hedge_ratio,
            "margin_used": self.margin_used,
            "market_value": self.market_value,
            "gross_cost": self.gross_cost,
            "unrealized_pnl": self.unrealized_pnl,
            "net_realized_pnl": self.net_realized_pnl,
            "total_pnl": self.total_pnl,
            "cost_basis.average_entry_price": self.cost_basis.average_entry_price,
            "cost_basis.average_exit_price": self.cost_basis.average_exit_price,
            "cost_basis.mark_price": self.cost_basis.mark_price,
            "cost_basis.settlement_price": self.cost_basis.settlement_price,
            "cost_basis.point_value": self.cost_basis.point_value,
            "cost_basis.cumulative_fees": self.cost_basis.cumulative_fees,
            "greeks.delta": self.greeks.delta,
            "greeks.gamma": self.greeks.gamma,
            "greeks.theta": self.greeks.theta,
            "greeks.vega": self.greeks.vega,
            "greeks.rho": self.greeks.rho,
        }
        record["serialized_decimal_fields"] = {
            key: None if value is None else format_decimal(value) for key, value in decimal_fields.items()
        }
        return record


__all__ = [
    "PositionCostBasis",
    "PositionQuantitySummary",
    "PositionGreeksSnapshot",
    "PositionStateSnapshot",
]
