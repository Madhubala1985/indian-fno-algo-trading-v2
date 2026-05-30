# FILE: platform_core/domain/models/order.py
"""
platform_core.domain.models.order
═══════════════════════════════════════════════════════════════════════
Production-grade order domain models for Indian Index Futures & Options
workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- execution-lifecycle aware,
- suitable for live and replay order workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import (
    ExchangeCode,
    InstrumentType,
    OrderAction,
    OrderRejectReason,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class OrderPriceParameters(BaseModel):
    """Explicit order pricing parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trigger_price: Decimal | None = None
    disclosed_quantity: int | None = Field(default=None, ge=0)

    @field_validator("limit_price", "stop_price", "trigger_price", mode="before")
    @classmethod
    def normalize_prices(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        price = safe_decimal(value)
        if price <= 0:
            raise ValueError("order price parameters must be positive")
        return price

    @model_validator(mode="after")
    def validate_disclosed_quantity(self) -> "OrderPriceParameters":
        if self.disclosed_quantity == 0:
            raise ValueError("disclosed_quantity must be positive when provided")
        return self


class OrderFillSummary(BaseModel):
    """Aggregated execution summary for an order."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    filled_quantity: int = Field(ge=0)
    remaining_quantity: int = Field(ge=0)
    average_fill_price: Decimal | None = None
    last_fill_price: Decimal | None = None
    last_fill_quantity: int | None = Field(default=None, ge=0)
    cumulative_fees: Decimal | None = None

    @field_validator("average_fill_price", "last_fill_price", "cumulative_fees", mode="before")
    @classmethod
    def normalize_decimals(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        normalized = safe_decimal(value)
        if normalized < 0:
            raise ValueError("fill summary decimals cannot be negative")
        return normalized

    @model_validator(mode="after")
    def validate_fill_consistency(self) -> "OrderFillSummary":
        if self.last_fill_quantity == 0:
            raise ValueError("last_fill_quantity must be positive when provided")
        if self.filled_quantity == 0 and self.average_fill_price is not None:
            raise ValueError("average_fill_price cannot be populated when filled_quantity is zero")
        if self.remaining_quantity == 0 and self.filled_quantity == 0:
            raise ValueError("at least one of filled_quantity or remaining_quantity must be positive")
        return self


class OrderStateSnapshot(BaseModel):
    """Canonical order lifecycle snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    correlation_id: str | None = None
    parent_order_id: str | None = None
    signal_id: str | None = None

    exchange: ExchangeCode
    instrument_type: InstrumentType
    instrument_symbol: str = Field(min_length=1)
    underlying_symbol: str = Field(min_length=1)
    option_expiry: datetime | None = None
    option_strike: Decimal | None = None

    side: OrderSide
    action: OrderAction
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    reject_reason: OrderRejectReason | None = None

    original_quantity: int = Field(gt=0)
    price_parameters: OrderPriceParameters = Field(default_factory=OrderPriceParameters)
    fill_summary: OrderFillSummary

    exchange_timestamp: datetime
    received_timestamp: datetime
    created_timestamp: datetime
    updated_timestamp: datetime
    acknowledged_timestamp: datetime | None = None
    completed_timestamp: datetime | None = None
    snapshot_sequence_number: int = Field(ge=0)

    venue_order_reference: str | None = None
    broker_order_reference: str | None = None
    route: str | None = None

    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "order_id",
        "client_order_id",
        "correlation_id",
        "parent_order_id",
        "signal_id",
        "instrument_symbol",
        "underlying_symbol",
        "venue_order_reference",
        "broker_order_reference",
        "route",
        "source",
        "raw_event_id",
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
        "acknowledged_timestamp",
        "completed_timestamp",
        mode="before",
    )
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator("option_strike", mode="before")
    @classmethod
    def normalize_option_strike(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        strike = safe_decimal(value)
        if strike <= 0:
            raise ValueError("option_strike must be positive")
        return strike

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_required_text_fields(self) -> "OrderStateSnapshot":
        for field_name in ("order_id", "client_order_id", "instrument_symbol", "underlying_symbol", "source"):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "OrderStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.created_timestamp < self.exchange_timestamp:
            raise ValueError("created_timestamp cannot precede exchange_timestamp")
        if self.updated_timestamp < self.created_timestamp:
            raise ValueError("updated_timestamp cannot precede created_timestamp")
        if self.acknowledged_timestamp is not None and self.acknowledged_timestamp < self.created_timestamp:
            raise ValueError("acknowledged_timestamp cannot precede created_timestamp")
        if self.completed_timestamp is not None and self.completed_timestamp < self.created_timestamp:
            raise ValueError("completed_timestamp cannot precede created_timestamp")
        if self.option_expiry is not None and self.option_expiry < self.exchange_timestamp:
            raise ValueError("option_expiry cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_instrument_consistency(self) -> "OrderStateSnapshot":
        if self.instrument_type.name == "OPTION":
            if self.option_expiry is None or self.option_strike is None:
                raise ValueError("option orders require option_expiry and option_strike")
        return self

    @model_validator(mode="after")
    def validate_order_type_consistency(self) -> "OrderStateSnapshot":
        limit_required = self.order_type.name in {"LIMIT", "STOP_LIMIT"}
        stop_required = self.order_type.name in {"STOP_MARKET", "STOP_LIMIT"}
        if limit_required and self.price_parameters.limit_price is None:
            raise ValueError("limit_price is required for limit and stop-limit orders")
        if stop_required and self.price_parameters.stop_price is None and self.price_parameters.trigger_price is None:
            raise ValueError("stop or trigger price is required for stop orders")
        return self

    @model_validator(mode="after")
    def validate_status_consistency(self) -> "OrderStateSnapshot":
        filled = self.fill_summary.filled_quantity
        remaining = self.fill_summary.remaining_quantity
        total = filled + remaining
        if total != self.original_quantity:
            raise ValueError("filled_quantity + remaining_quantity must equal original_quantity")
        if self.status.name == "FILLED" and remaining != 0:
            raise ValueError("filled orders must have zero remaining_quantity")
        if self.status.name == "PARTIALLY_FILLED" and not (0 < filled < self.original_quantity):
            raise ValueError("partially filled orders must have partial execution")
        if self.status.name == "NEW" and filled != 0:
            raise ValueError("new orders cannot have filled quantity")
        if self.status.name == "REJECTED" and self.reject_reason is None:
            raise ValueError("rejected orders require reject_reason")
        return self

    @computed_field(return_type=Decimal | None)
    @property
    def notional_order_value(self) -> Decimal | None:
        reference_price = self.price_parameters.limit_price or self.fill_summary.average_fill_price
        if reference_price is None:
            return None
        return reference_price * Decimal(self.original_quantity)

    @computed_field(return_type=Decimal | None)
    @property
    def filled_notional_value(self) -> Decimal | None:
        if self.fill_summary.average_fill_price is None:
            return None
        return self.fill_summary.average_fill_price * Decimal(self.fill_summary.filled_quantity)

    @computed_field(return_type=Decimal)
    @property
    def fill_ratio(self) -> Decimal:
        return Decimal(self.fill_summary.filled_quantity) / Decimal(self.original_quantity)

    @computed_field(return_type=bool)
    @property
    def is_terminal_state(self) -> bool:
        return self.status.name in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}

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
            "notional_order_value": self.notional_order_value,
            "filled_notional_value": self.filled_notional_value,
            "fill_ratio": self.fill_ratio,
            "price_parameters.limit_price": self.price_parameters.limit_price,
            "price_parameters.stop_price": self.price_parameters.stop_price,
            "price_parameters.trigger_price": self.price_parameters.trigger_price,
            "fill_summary.average_fill_price": self.fill_summary.average_fill_price,
            "fill_summary.last_fill_price": self.fill_summary.last_fill_price,
            "fill_summary.cumulative_fees": self.fill_summary.cumulative_fees,
        }
        record["serialized_decimal_fields"] = {
            key: None if value is None else format_decimal(value) for key, value in decimal_fields.items()
        }
        return record


__all__ = ["OrderPriceParameters", "OrderFillSummary", "OrderStateSnapshot"]
