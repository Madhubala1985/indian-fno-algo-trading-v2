# FILE: platform_core/domain/models/iv_surface.py
"""
platform_core.domain.models.iv_surface
═══════════════════════════════════════════════════════════════════════
Production-grade implied volatility surface domain models for Indian Index
Futures & Options workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import IVSurfaceInterpolationMethod, OptionType
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class IVSurfacePoint(BaseModel):
    """Single normalized point on an implied volatility surface."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    expiry: datetime
    strike_price: Decimal
    option_type: OptionType | None = None
    delta: Decimal | None = None
    moneyness_ratio: Decimal | None = None
    implied_volatility: Decimal
    bid_implied_volatility: Decimal | None = None
    ask_implied_volatility: Decimal | None = None
    underlying_reference_price: Decimal | None = None
    option_market_price: Decimal | None = None
    open_interest: int | None = Field(default=None, ge=0)
    traded_volume: int | None = Field(default=None, ge=0)
    is_interpolated: bool = False
    is_extrapolated: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("expiry", mode="before")
    @classmethod
    def normalize_expiry(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("expiry must be a datetime instance")
        return ensure_utc(value)

    @field_validator(
        "strike_price",
        "delta",
        "moneyness_ratio",
        "implied_volatility",
        "bid_implied_volatility",
        "ask_implied_volatility",
        "underlying_reference_price",
        "option_market_price",
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
    def validate_ranges(self) -> "IVSurfacePoint":
        if self.strike_price <= 0:
            raise ValueError("strike_price must be positive")
        if self.implied_volatility < 0:
            raise ValueError("implied_volatility cannot be negative")
        if self.bid_implied_volatility is not None and self.bid_implied_volatility < 0:
            raise ValueError("bid_implied_volatility cannot be negative")
        if self.ask_implied_volatility is not None and self.ask_implied_volatility < 0:
            raise ValueError("ask_implied_volatility cannot be negative")
        if self.bid_implied_volatility is not None and self.ask_implied_volatility is not None and self.bid_implied_volatility > self.ask_implied_volatility:
            raise ValueError("bid_implied_volatility cannot exceed ask_implied_volatility")
        if self.delta is not None and not (Decimal("-1") <= self.delta <= Decimal("1")):
            raise ValueError("delta must be within [-1, 1]")
        if self.moneyness_ratio is not None and self.moneyness_ratio <= 0:
            raise ValueError("moneyness_ratio must be positive")
        if self.underlying_reference_price is not None and self.underlying_reference_price <= 0:
            raise ValueError("underlying_reference_price must be positive")
        if self.option_market_price is not None and self.option_market_price < 0:
            raise ValueError("option_market_price cannot be negative")
        return self

    @computed_field(return_type=Decimal | None)
    @property
    def iv_spread(self) -> Decimal | None:
        if self.bid_implied_volatility is None or self.ask_implied_volatility is None:
            return None
        return self.ask_implied_volatility - self.bid_implied_volatility


class IVSurfaceSnapshot(BaseModel):
    """Canonical IV surface snapshot for a single underlying symbol."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    underlying_symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    exchange_timestamp: datetime
    received_timestamp: datetime
    snapshot_sequence_number: int = Field(ge=0)
    underlying_spot_price: Decimal | None = None
    underlying_futures_price: Decimal | None = None
    interpolation_method: IVSurfaceInterpolationMethod
    model_version: str = Field(min_length=1)
    surface_points: tuple[IVSurfacePoint, ...] = Field(default_factory=tuple)
    target_delta_grid: tuple[Decimal, ...] = Field(default_factory=tuple)
    target_tenor_days: tuple[int, ...] = Field(default_factory=tuple)
    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("underlying_symbol", "exchange", "model_version", "source", mode="before")
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

    @field_validator("exchange_timestamp", "received_timestamp", mode="before")
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator("underlying_spot_price", "underlying_futures_price", mode="before")
    @classmethod
    def normalize_underlying_prices(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        decimal_value = safe_decimal(value)
        if decimal_value <= 0:
            raise ValueError("underlying price must be positive")
        return decimal_value

    @field_validator("surface_points", mode="before")
    @classmethod
    def normalize_surface_points(cls, value: Any) -> tuple[IVSurfacePoint, ...]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        raise TypeError("surface_points must be a list or tuple of IVSurfacePoint")

    @field_validator("target_delta_grid", mode="before")
    @classmethod
    def normalize_target_delta_grid(cls, value: Any) -> tuple[Decimal, ...]:
        if isinstance(value, tuple):
            items = value
        elif isinstance(value, list):
            items = tuple(value)
        else:
            raise TypeError("target_delta_grid must be a list or tuple")
        return tuple(safe_decimal(item) for item in items)

    @field_validator("target_tenor_days", mode="before")
    @classmethod
    def normalize_target_tenor_days(cls, value: Any) -> tuple[int, ...]:
        if isinstance(value, tuple):
            items = value
        elif isinstance(value, list):
            items = tuple(value)
        else:
            raise TypeError("target_tenor_days must be a list or tuple")
        return tuple(int(item) for item in items)

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "IVSurfaceSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_surface_integrity(self) -> "IVSurfaceSnapshot":
        if not self.surface_points:
            raise ValueError("surface_points cannot be empty")
        point_keys = {(point.expiry, point.strike_price, point.option_type) for point in self.surface_points}
        if len(point_keys) != len(self.surface_points):
            raise ValueError("duplicate IV surface points are not allowed")
        for tenor in self.target_tenor_days:
            if tenor <= 0:
                raise ValueError("target tenor days must be positive")
        for delta in self.target_delta_grid:
            if not (Decimal("-1") <= delta <= Decimal("1")):
                raise ValueError("target deltas must be within [-1, 1]")
        return self

    @computed_field(return_type=int)
    @property
    def point_count(self) -> int:
        return len(self.surface_points)

    @computed_field(return_type=tuple[datetime, ...])
    @property
    def expiries(self) -> tuple[datetime, ...]:
        return tuple(sorted({point.expiry for point in self.surface_points}))

    @computed_field(return_type=tuple[str, ...])
    @property
    def strike_grid(self) -> tuple[str, ...]:
        strikes = sorted({point.strike_price for point in self.surface_points})
        return tuple(format_decimal(strike) for strike in strikes)

    @computed_field(return_type=Decimal | None)
    @property
    def atm_implied_volatility(self) -> Decimal | None:
        reference_price = self.underlying_futures_price or self.underlying_spot_price
        if reference_price is None:
            return None
        nearest_point = min(self.surface_points, key=lambda point: abs(point.strike_price - reference_price))
        return nearest_point.implied_volatility

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.underlying_symbol

    def points_for_expiry(self, expiry: datetime) -> tuple[IVSurfacePoint, ...]:
        normalized_expiry = ensure_utc(expiry)
        return tuple(point for point in self.surface_points if point.expiry == normalized_expiry)

    def nearest_point(self, *, expiry: datetime, strike_price: Decimal | str | int | float, option_type: OptionType | None = None) -> IVSurfacePoint | None:
        normalized_expiry = ensure_utc(expiry)
        normalized_strike = safe_decimal(strike_price)
        candidates = [point for point in self.surface_points if point.expiry == normalized_expiry]
        if option_type is not None:
            candidates = [point for point in candidates if point.option_type == option_type]
        if not candidates:
            return None
        return min(candidates, key=lambda point: abs(point.strike_price - normalized_strike))

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        for field_name in ("underlying_spot_price", "underlying_futures_price", "atm_implied_volatility"):
            if record.get(field_name) is not None:
                record[field_name] = format_decimal(record[field_name])
        record["target_delta_grid"] = [format_decimal(value) for value in self.target_delta_grid]
        record["surface_points"] = self.model_dump(mode="json")["surface_points"]
        return record


__all__ = ["IVSurfacePoint", "IVSurfaceSnapshot"]
