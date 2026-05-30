# FILE: platform_core/domain/models/market_tick.py
"""
platform_core.domain.models.market_tick
═══════════════════════════════════════════════════════════════════════
Production-grade market tick domain model for Indian Index Futures & Options
workflows.

This model is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- precise for premium, quote, and trade microstructure handling.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import AssetClass, InstrumentType, MarketDataType, OptionType
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class MarketTick(BaseModel):
    """
    Canonical normalized market tick model.

    A tick may represent a trade, quote update, synthetic midpoint update,
    or depth-derived best bid/ask snapshot depending on `market_data_type`.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        use_enum_values=False,
    )

    instrument_id: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    instrument_type: InstrumentType
    market_data_type: MarketDataType

    exchange_timestamp: datetime
    received_timestamp: datetime

    sequence_number: int = Field(ge=0)

    last_traded_price: Decimal | None = None
    last_traded_quantity: int | None = Field(default=None, ge=0)
    last_trade_notional: Decimal | None = None
    cumulative_traded_volume: int | None = Field(default=None, ge=0)
    cumulative_traded_notional: Decimal | None = None

    bid_price: Decimal | None = None
    bid_quantity: int | None = Field(default=None, ge=0)
    ask_price: Decimal | None = None
    ask_quantity: int | None = Field(default=None, ge=0)

    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    average_traded_price: Decimal | None = None
    previous_close_price: Decimal | None = None
    open_interest: int | None = Field(default=None, ge=0)

    strike_price: Decimal | None = None
    option_type: OptionType | None = None
    expiry: datetime | None = None
    underlying_symbol: str | None = None
    underlying_spot_price: Decimal | None = None
    underlying_futures_price: Decimal | None = None

    is_tradable: bool = True
    is_snapshot: bool = False
    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "instrument_id",
        "exchange",
        "symbol",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_required_text(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("text field must be non-empty")
        return text

    @field_validator("underlying_symbol", "raw_event_id", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("exchange_timestamp", "received_timestamp", "expiry", mode="before")
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator(
        "last_traded_price",
        "last_trade_notional",
        "cumulative_traded_notional",
        "bid_price",
        "ask_price",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "average_traded_price",
        "previous_close_price",
        "strike_price",
        "underlying_spot_price",
        "underlying_futures_price",
        mode="before",
    )
    @classmethod
    def normalize_decimal_fields(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        decimal_value = safe_decimal(value)
        if decimal_value < 0:
            raise ValueError("decimal field cannot be negative")
        return decimal_value

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            normalized[str(key).strip()] = str(item).strip()
        return normalized

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "MarketTick":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.expiry is not None and self.expiry < self.exchange_timestamp:
            raise ValueError("expiry cannot precede exchange timestamp")
        return self

    @model_validator(mode="after")
    def validate_quote_consistency(self) -> "MarketTick":
        if (self.bid_price is None) ^ (self.bid_quantity is None):
            raise ValueError("bid_price and bid_quantity must co-exist")
        if (self.ask_price is None) ^ (self.ask_quantity is None):
            raise ValueError("ask_price and ask_quantity must co-exist")
        if self.bid_price is not None and self.ask_price is not None and self.bid_price > self.ask_price:
            raise ValueError("bid_price cannot exceed ask_price")
        return self

    @model_validator(mode="after")
    def validate_trade_consistency(self) -> "MarketTick":
        if (self.last_traded_price is None) ^ (self.last_traded_quantity is None):
            raise ValueError("last_traded_price and last_traded_quantity must co-exist")
        if self.last_trade_notional is None and self.last_traded_price is not None and self.last_traded_quantity is not None:
            object.__setattr__(self, "last_trade_notional", self.last_traded_price * Decimal(self.last_traded_quantity))
        if self.last_trade_notional is not None and self.last_trade_notional < 0:
            raise ValueError("last_trade_notional cannot be negative")
        return self

    @model_validator(mode="after")
    def validate_price_range_consistency(self) -> "MarketTick":
        intraday_prices = [price for price in (self.open_price, self.high_price, self.low_price, self.close_price) if price is not None]
        if self.high_price is not None and self.low_price is not None and self.low_price > self.high_price:
            raise ValueError("low_price cannot exceed high_price")
        if intraday_prices and self.high_price is not None and any(price > self.high_price for price in intraday_prices):
            raise ValueError("intraday prices cannot exceed high_price")
        if intraday_prices and self.low_price is not None and any(price < self.low_price for price in intraday_prices):
            raise ValueError("intraday prices cannot be below low_price")
        return self

    @model_validator(mode="after")
    def validate_option_fields(self) -> "MarketTick":
        if self.instrument_type == InstrumentType.OPTION:
            if self.option_type is None:
                raise ValueError("option_type is required for option instruments")
            if self.strike_price is None:
                raise ValueError("strike_price is required for option instruments")
            if self.expiry is None:
                raise ValueError("expiry is required for option instruments")
            if self.underlying_symbol is None:
                raise ValueError("underlying_symbol is required for option instruments")
        else:
            if self.option_type is not None or self.strike_price is not None:
                raise ValueError("option-specific fields are only valid for option instruments")
        return self

    @computed_field(return_type=Decimal | None)
    @property
    def spread(self) -> Decimal | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        return self.ask_price - self.bid_price

    @computed_field(return_type=Decimal | None)
    @property
    def mid_price(self) -> Decimal | None:
        if self.bid_price is not None and self.ask_price is not None:
            return (self.bid_price + self.ask_price) / Decimal("2")
        return None

    @computed_field(return_type=Decimal | None)
    @property
    def microprice(self) -> Decimal | None:
        if (
            self.bid_price is None
            or self.ask_price is None
            or self.bid_quantity is None
            or self.ask_quantity is None
            or self.bid_quantity + self.ask_quantity == 0
        ):
            return None
        total_qty = Decimal(self.bid_quantity + self.ask_quantity)
        return ((self.ask_price * Decimal(self.bid_quantity)) + (self.bid_price * Decimal(self.ask_quantity))) / total_qty

    @computed_field(return_type=Decimal | None)
    @property
    def quote_imbalance(self) -> Decimal | None:
        if self.bid_quantity is None or self.ask_quantity is None:
            return None
        total = self.bid_quantity + self.ask_quantity
        if total == 0:
            return None
        return Decimal(self.bid_quantity - self.ask_quantity) / Decimal(total)

    @computed_field(return_type=bool)
    @property
    def has_trade(self) -> bool:
        return self.last_traded_price is not None and self.last_traded_quantity is not None

    @computed_field(return_type=bool)
    @property
    def has_quote(self) -> bool:
        return self.bid_price is not None and self.ask_price is not None

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.instrument_id

    def to_event_payload(self) -> dict[str, Any]:
        """Return event-bus safe payload representation."""
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        """Return database-friendly row representation."""
        record = self.model_dump(mode="python")
        decimal_fields = [
            "last_traded_price",
            "last_trade_notional",
            "cumulative_traded_notional",
            "bid_price",
            "ask_price",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "average_traded_price",
            "previous_close_price",
            "strike_price",
            "underlying_spot_price",
            "underlying_futures_price",
            "spread",
            "mid_price",
            "microprice",
            "quote_imbalance",
        ]
        for field_name in decimal_fields:
            if record.get(field_name) is not None:
                record[field_name] = format_decimal(record[field_name])
        return record


__all__ = ["MarketTick"]
