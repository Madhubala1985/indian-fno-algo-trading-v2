# FILE: platform_core/domain/models/greeks.py
"""
platform_core.domain.models.greeks
═══════════════════════════════════════════════════════════════════════
Production-grade options Greeks domain models for Indian Index Futures &
Options workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- expiry-aware,
- suitable for live and replay options analytics.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import GreeksModelType, OptionType
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class OptionGreeksSnapshot(BaseModel):
    """Canonical Greeks snapshot for a single option contract at a point in time."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    underlying_symbol: str = Field(min_length=1)
    option_type: OptionType
    strike_price: Decimal
    expiry: datetime

    exchange_timestamp: datetime
    received_timestamp: datetime
    snapshot_sequence_number: int = Field(ge=0)

    model_type: GreeksModelType
    model_version: str = Field(min_length=1)

    underlying_spot_price: Decimal | None = None
    underlying_futures_price: Decimal | None = None
    option_market_price: Decimal | None = None
    implied_volatility: Decimal | None = None
    risk_free_rate: Decimal | None = None
    dividend_yield: Decimal | None = None
    time_to_expiry_years: Decimal | None = None

    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None
    vomma: Decimal | None = None
    vanna: Decimal | None = None
    charm: Decimal | None = None
    speed: Decimal | None = None
    zomma: Decimal | None = None
    color: Decimal | None = None

    intrinsic_value: Decimal | None = None
    extrinsic_value: Decimal | None = None
    theoretical_price: Decimal | None = None
    pricing_residual: Decimal | None = None

    is_exchange_published: bool = False
    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("instrument_id", "symbol", "underlying_symbol", "model_version", "source", mode="before")
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
    def normalize_datetimes(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator(
        "strike_price",
        "underlying_spot_price",
        "underlying_futures_price",
        "option_market_price",
        "implied_volatility",
        "risk_free_rate",
        "dividend_yield",
        "time_to_expiry_years",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "vomma",
        "vanna",
        "charm",
        "speed",
        "zomma",
        "color",
        "intrinsic_value",
        "extrinsic_value",
        "theoretical_price",
        "pricing_residual",
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
    def validate_temporal_consistency(self) -> "OptionGreeksSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.expiry < self.exchange_timestamp:
            raise ValueError("expiry cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_positive_fields(self) -> "OptionGreeksSnapshot":
        if self.strike_price <= 0:
            raise ValueError("strike_price must be positive")
        for field_name in (
            "underlying_spot_price",
            "underlying_futures_price",
            "option_market_price",
            "implied_volatility",
            "time_to_expiry_years",
            "intrinsic_value",
            "extrinsic_value",
            "theoretical_price",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        return self

    @model_validator(mode="after")
    def validate_greek_ranges(self) -> "OptionGreeksSnapshot":
        if self.delta is not None:
            if self.option_type == OptionType.CALL and not (Decimal("0") <= self.delta <= Decimal("1")):
                raise ValueError("call delta must be within [0, 1]")
            if self.option_type == OptionType.PUT and not (Decimal("-1") <= self.delta <= Decimal("0")):
                raise ValueError("put delta must be within [-1, 0]")
        if self.gamma is not None and self.gamma < 0:
            raise ValueError("gamma cannot be negative")
        if self.vega is not None and self.vega < 0:
            raise ValueError("vega cannot be negative")
        if self.time_to_expiry_years is not None and self.time_to_expiry_years == 0 and self.theta is not None:
            raise ValueError("theta cannot be populated when time_to_expiry_years is zero")
        return self

    @model_validator(mode="after")
    def validate_value_decomposition(self) -> "OptionGreeksSnapshot":
        if self.intrinsic_value is not None and self.extrinsic_value is not None and self.option_market_price is not None:
            decomposed = self.intrinsic_value + self.extrinsic_value
            if decomposed != self.option_market_price:
                raise ValueError("intrinsic_value + extrinsic_value must equal option_market_price")
        if self.theoretical_price is not None and self.option_market_price is not None and self.pricing_residual is None:
            object.__setattr__(self, "pricing_residual", self.option_market_price - self.theoretical_price)
        return self

    @computed_field(return_type=Decimal | None)
    @property
    def elasticity(self) -> Decimal | None:
        if self.delta is None or self.option_market_price is None or self.option_market_price == 0:
            return None
        underlying_reference = self.underlying_spot_price or self.underlying_futures_price
        if underlying_reference is None or underlying_reference == 0:
            return None
        return self.delta * underlying_reference / self.option_market_price

    @computed_field(return_type=Decimal | None)
    @property
    def gamma_exposure_per_lot_unit(self) -> Decimal | None:
        if self.gamma is None:
            return None
        underlying_reference = self.underlying_spot_price or self.underlying_futures_price
        if underlying_reference is None:
            return None
        return self.gamma * (underlying_reference ** 2)

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.instrument_id

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        decimal_fields = [
            "strike_price",
            "underlying_spot_price",
            "underlying_futures_price",
            "option_market_price",
            "implied_volatility",
            "risk_free_rate",
            "dividend_yield",
            "time_to_expiry_years",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "vomma",
            "vanna",
            "charm",
            "speed",
            "zomma",
            "color",
            "intrinsic_value",
            "extrinsic_value",
            "theoretical_price",
            "pricing_residual",
            "elasticity",
            "gamma_exposure_per_lot_unit",
        ]
        for field_name in decimal_fields:
            if record.get(field_name) is not None:
                record[field_name] = format_decimal(record[field_name])
        return record


__all__ = ["OptionGreeksSnapshot"]
