# FILE: platform_core/domain/models/option_chain.py
"""
platform_core.domain.models.option_chain
═══════════════════════════════════════════════════════════════════════
Production-grade option chain domain models for Indian Index Futures & Options
workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- expiry-aware,
- strike-grid aware,
- suitable for real-time option-chain analytics.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import AssetClass, InstrumentType, OptionType
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class OptionQuoteSide(BaseModel):
    """Top-of-book and last-trade state for a single option contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    option_type: OptionType
    strike_price: Decimal
    expiry: datetime

    bid_price: Decimal | None = None
    bid_quantity: int | None = Field(default=None, ge=0)
    ask_price: Decimal | None = None
    ask_quantity: int | None = Field(default=None, ge=0)
    last_traded_price: Decimal | None = None
    last_traded_quantity: int | None = Field(default=None, ge=0)
    traded_volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    change_in_open_interest: int | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None
    intrinsic_value: Decimal | None = None
    extrinsic_value: Decimal | None = None
    is_tradable: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("instrument_id", "symbol", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("text field must be non-empty")
        return text

    @field_validator("expiry", mode="before")
    @classmethod
    def normalize_expiry(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("expiry must be a datetime instance")
        return ensure_utc(value)

    @field_validator(
        "strike_price",
        "bid_price",
        "ask_price",
        "last_traded_price",
        "implied_volatility",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "intrinsic_value",
        "extrinsic_value",
        mode="before",
    )
    @classmethod
    def normalize_decimal_fields(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return safe_decimal(value)

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_quote_consistency(self) -> "OptionQuoteSide":
        if (self.bid_price is None) ^ (self.bid_quantity is None):
            raise ValueError("bid_price and bid_quantity must co-exist")
        if (self.ask_price is None) ^ (self.ask_quantity is None):
            raise ValueError("ask_price and ask_quantity must co-exist")
        if self.bid_price is not None and self.ask_price is not None and self.bid_price > self.ask_price:
            raise ValueError("bid_price cannot exceed ask_price")
        if self.last_traded_price is not None and self.last_traded_price < 0:
            raise ValueError("last_traded_price cannot be negative")
        if self.strike_price <= 0:
            raise ValueError("strike_price must be positive")
        if self.implied_volatility is not None and self.implied_volatility < 0:
            raise ValueError("implied_volatility cannot be negative")
        if self.extrinsic_value is not None and self.extrinsic_value < 0:
            raise ValueError("extrinsic_value cannot be negative")
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
        if self.bid_price is None or self.ask_price is None:
            return None
        return (self.bid_price + self.ask_price) / Decimal("2")

    @computed_field(return_type=bool)
    @property
    def has_liquid_quote(self) -> bool:
        return self.bid_price is not None and self.ask_price is not None and self.bid_quantity is not None and self.ask_quantity is not None


class OptionChainStrike(BaseModel):
    """Paired call/put representation for a single strike and expiry."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    strike_price: Decimal
    expiry: datetime
    call: OptionQuoteSide | None = None
    put: OptionQuoteSide | None = None

    @field_validator("strike_price", mode="before")
    @classmethod
    def normalize_strike(cls, value: Any) -> Decimal:
        strike = safe_decimal(value)
        if strike <= 0:
            raise ValueError("strike_price must be positive")
        return strike

    @field_validator("expiry", mode="before")
    @classmethod
    def normalize_expiry(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("expiry must be a datetime instance")
        return ensure_utc(value)

    @model_validator(mode="after")
    def validate_side_alignment(self) -> "OptionChainStrike":
        if self.call is None and self.put is None:
            raise ValueError("at least one of call or put must be present")
        if self.call is not None:
            if self.call.option_type != OptionType.CALL:
                raise ValueError("call side must have option_type CALL")
            if self.call.strike_price != self.strike_price or self.call.expiry != self.expiry:
                raise ValueError("call side strike/expiry mismatch")
        if self.put is not None:
            if self.put.option_type != OptionType.PUT:
                raise ValueError("put side must have option_type PUT")
            if self.put.strike_price != self.strike_price or self.put.expiry != self.expiry:
                raise ValueError("put side strike/expiry mismatch")
        return self


class OptionChainSnapshot(BaseModel):
    """
    Canonical option chain snapshot for a single underlying and expiry.

    Snapshot semantics:
    - all strikes refer to one underlying symbol,
    - one expiry per snapshot,
    - timestamps preserve causal ordering,
    - data is suitable for eventing, storage, replay, and analytics.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    underlying_instrument_id: str = Field(min_length=1)
    underlying_symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    asset_class: AssetClass
    instrument_type: InstrumentType
    expiry: datetime

    exchange_timestamp: datetime
    received_timestamp: datetime
    snapshot_sequence_number: int = Field(ge=0)

    underlying_spot_price: Decimal | None = None
    underlying_futures_price: Decimal | None = None
    at_the_money_strike: Decimal | None = None
    strike_interval: Decimal | None = None

    strikes: tuple[OptionChainStrike, ...] = Field(default_factory=tuple)
    is_complete_snapshot: bool = True
    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "underlying_instrument_id",
        "underlying_symbol",
        "exchange",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_required_text(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("text field must be non-empty")
        return text

    @field_validator("raw_event_id", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("expiry", "exchange_timestamp", "received_timestamp", mode="before")
    @classmethod
    def normalize_datetime_fields(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator("underlying_spot_price", "underlying_futures_price", "at_the_money_strike", "strike_interval", mode="before")
    @classmethod
    def normalize_decimal_fields(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        decimal_value = safe_decimal(value)
        if decimal_value < 0:
            raise ValueError("decimal field cannot be negative")
        return decimal_value

    @field_validator("strikes", mode="before")
    @classmethod
    def normalize_strikes(cls, value: Any) -> tuple[OptionChainStrike, ...]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        raise TypeError("strikes must be a list or tuple of OptionChainStrike")

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "OptionChainSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.expiry < self.exchange_timestamp:
            raise ValueError("expiry cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_instrument_scope(self) -> "OptionChainSnapshot":
        if self.instrument_type != InstrumentType.OPTION:
            raise ValueError("option chain snapshot instrument_type must be OPTION")
        if not self.strikes:
            raise ValueError("strikes cannot be empty")
        return self

    @model_validator(mode="after")
    def validate_strike_grid(self) -> "OptionChainSnapshot":
        strike_values = [strike.strike_price for strike in self.strikes]
        if len(set(strike_values)) != len(strike_values):
            raise ValueError("duplicate strike_price values are not allowed")
        sorted_values = sorted(strike_values)
        if list(strike_values) != sorted_values:
            raise ValueError("strikes must be ordered by ascending strike_price")
        if self.strike_interval is not None and len(sorted_values) > 1:
            for idx in range(1, len(sorted_values)):
                gap = sorted_values[idx] - sorted_values[idx - 1]
                if gap != self.strike_interval:
                    raise ValueError("strike grid gap does not match strike_interval")
        return self

    @model_validator(mode="after")
    def validate_snapshot_alignment(self) -> "OptionChainSnapshot":
        for strike in self.strikes:
            if strike.expiry != self.expiry:
                raise ValueError("all strikes must share snapshot expiry")
            if strike.call is not None and strike.call.symbol and self.underlying_symbol not in strike.call.symbol:
                pass
            if strike.put is not None and strike.put.symbol and self.underlying_symbol not in strike.put.symbol:
                pass
        return self

    @computed_field(return_type=int)
    @property
    def strike_count(self) -> int:
        return len(self.strikes)

    @computed_field(return_type=int)
    @property
    def call_count(self) -> int:
        return sum(1 for strike in self.strikes if strike.call is not None)

    @computed_field(return_type=int)
    @property
    def put_count(self) -> int:
        return sum(1 for strike in self.strikes if strike.put is not None)

    @computed_field(return_type=Decimal | None)
    @property
    def total_call_open_interest(self) -> Decimal | None:
        total = sum((strike.call.open_interest or 0) for strike in self.strikes if strike.call is not None)
        return Decimal(total) if total > 0 else None

    @computed_field(return_type=Decimal | None)
    @property
    def total_put_open_interest(self) -> Decimal | None:
        total = sum((strike.put.open_interest or 0) for strike in self.strikes if strike.put is not None)
        return Decimal(total) if total > 0 else None

    @computed_field(return_type=Decimal | None)
    @property
    def put_call_ratio_oi(self) -> Decimal | None:
        call_oi = self.total_call_open_interest
        put_oi = self.total_put_open_interest
        if call_oi is None or call_oi == 0 or put_oi is None:
            return None
        return put_oi / call_oi

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return f"{self.underlying_symbol}:{self.expiry.isoformat()}"

    def get_strike(self, strike_price: Decimal | str | int | float) -> OptionChainStrike | None:
        normalized = safe_decimal(strike_price)
        for strike in self.strikes:
            if strike.strike_price == normalized:
                return strike
        return None

    def nearest_strike(self, reference_price: Decimal | str | int | float) -> OptionChainStrike | None:
        if not self.strikes:
            return None
        ref = safe_decimal(reference_price)
        return min(self.strikes, key=lambda strike: abs(strike.strike_price - ref))

    def to_event_payload(self) -> dict[str, Any]:
        """Return event-bus safe payload representation."""
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        """Return storage-friendly snapshot representation."""
        record = self.model_dump(mode="python")
        decimal_fields = [
            "underlying_spot_price",
            "underlying_futures_price",
            "at_the_money_strike",
            "strike_interval",
            "total_call_open_interest",
            "total_put_open_interest",
            "put_call_ratio_oi",
        ]
        for field_name in decimal_fields:
            if record.get(field_name) is not None:
                record[field_name] = format_decimal(record[field_name])
        record["strikes"] = self.model_dump(mode="json")["strikes"]
        return record


__all__ = ["OptionChainSnapshot", "OptionChainStrike", "OptionQuoteSide"]
