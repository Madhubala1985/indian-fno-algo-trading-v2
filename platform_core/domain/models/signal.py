# FILE: platform_core/domain/models/signal.py
"""
platform_core.domain.models.signal
═══════════════════════════════════════════════════════════════════════
Production-grade trading signal domain models for Indian Index Futures &
Options workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- causal and finalized-state aware,
- suitable for live and replay signal workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import SignalDirection, SignalState, SignalStrength, StrategyType, Timeframe
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class SignalPriceLevels(BaseModel):
    """Price-level definition for a signal candidate or executable signal."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    entry_price: Decimal
    stop_loss_price: Decimal
    target_price: Decimal | None = None
    invalidation_price: Decimal | None = None
    trailing_trigger_price: Decimal | None = None
    trailing_step_price: Decimal | None = None

    @field_validator(
        "entry_price",
        "stop_loss_price",
        "target_price",
        "invalidation_price",
        "trailing_trigger_price",
        "trailing_step_price",
        mode="before",
    )
    @classmethod
    def normalize_prices(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        price = safe_decimal(value)
        if price <= 0:
            raise ValueError("price levels must be positive")
        return price

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> "SignalPriceLevels":
        if self.trailing_step_price is not None and self.trailing_trigger_price is None:
            raise ValueError("trailing_step_price requires trailing_trigger_price")
        return self


class SignalQualityMetrics(BaseModel):
    """Quantitative quality metrics supporting signal ranking and gating."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    confidence_score: Decimal
    regime_alignment_score: Decimal
    liquidity_score: Decimal
    expected_slippage_bps: Decimal | None = None
    expected_hold_minutes: Decimal | None = None
    estimated_win_probability: Decimal | None = None
    expected_value_per_unit: Decimal | None = None
    expected_reward_to_risk: Decimal | None = None

    @field_validator(
        "confidence_score",
        "regime_alignment_score",
        "liquidity_score",
        "expected_slippage_bps",
        "expected_hold_minutes",
        "estimated_win_probability",
        "expected_value_per_unit",
        "expected_reward_to_risk",
        mode="before",
    )
    @classmethod
    def normalize_metrics(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return safe_decimal(value)

    @model_validator(mode="after")
    def validate_metric_ranges(self) -> "SignalQualityMetrics":
        for field_name in ("confidence_score", "regime_alignment_score", "liquidity_score"):
            value = getattr(self, field_name)
            if value < 0 or value > 1:
                raise ValueError(f"{field_name} must be within [0, 1]")
        if self.estimated_win_probability is not None and not (Decimal("0") <= self.estimated_win_probability <= Decimal("1")):
            raise ValueError("estimated_win_probability must be within [0, 1]")
        if self.expected_slippage_bps is not None and self.expected_slippage_bps < 0:
            raise ValueError("expected_slippage_bps cannot be negative")
        if self.expected_hold_minutes is not None and self.expected_hold_minutes < 0:
            raise ValueError("expected_hold_minutes cannot be negative")
        if self.expected_reward_to_risk is not None and self.expected_reward_to_risk <= 0:
            raise ValueError("expected_reward_to_risk must be positive")
        return self


class SignalStateSnapshot(BaseModel):
    """Canonical trading signal snapshot used across research, replay, and execution gates."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    signal_id: str = Field(min_length=1)
    underlying_symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    strategy_type: StrategyType
    timeframe: Timeframe

    direction: SignalDirection
    state: SignalState
    strength: SignalStrength

    exchange_timestamp: datetime
    received_timestamp: datetime
    decision_timestamp: datetime
    expiry_timestamp: datetime | None = None
    snapshot_sequence_number: int = Field(ge=0)

    instrument_symbol: str | None = None
    option_expiry: datetime | None = None
    option_strike: Decimal | None = None

    price_levels: SignalPriceLevels
    quality_metrics: SignalQualityMetrics

    rationale_codes: tuple[str, ...] = Field(default_factory=tuple)
    feature_values: dict[str, Decimal | str | bool | int | None] = Field(default_factory=dict)
    tags: tuple[str, ...] = Field(default_factory=tuple)

    source_model_name: str = Field(min_length=1)
    source_model_version: str = Field(min_length=1)
    is_finalized_bar: bool = True
    is_replay: bool = False
    source: str = Field(min_length=1)
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "signal_id",
        "underlying_symbol",
        "exchange",
        "instrument_symbol",
        "source_model_name",
        "source_model_version",
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
        "exchange_timestamp",
        "received_timestamp",
        "decision_timestamp",
        "expiry_timestamp",
        "option_expiry",
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

    @field_validator("rationale_codes", "tags", mode="before")
    @classmethod
    def normalize_text_tuples(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, tuple):
            items = value
        elif isinstance(value, list):
            items = tuple(value)
        else:
            raise TypeError("value must be a list or tuple")
        return tuple(str(item).strip() for item in items if str(item).strip())

    @field_validator("feature_values", mode="before")
    @classmethod
    def normalize_feature_values(cls, value: Any) -> dict[str, Decimal | str | bool | int | None]:
        if not isinstance(value, dict):
            raise TypeError("feature_values must be a dictionary")
        normalized: dict[str, Decimal | str | bool | int | None] = {}
        for key, item in value.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                raise ValueError("feature_values keys must be non-empty")
            if item is None or isinstance(item, (bool, int)):
                normalized[normalized_key] = item
            elif isinstance(item, str):
                normalized[normalized_key] = item.strip()
            else:
                normalized[normalized_key] = safe_decimal(item)
        return normalized

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_required_text_fields(self) -> "SignalStateSnapshot":
        for field_name in (
            "signal_id",
            "underlying_symbol",
            "exchange",
            "source_model_name",
            "source_model_version",
            "source",
        ):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "SignalStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.decision_timestamp < self.exchange_timestamp:
            raise ValueError("decision_timestamp cannot precede exchange_timestamp")
        if self.expiry_timestamp is not None and self.expiry_timestamp < self.decision_timestamp:
            raise ValueError("expiry_timestamp cannot precede decision_timestamp")
        if self.option_expiry is not None and self.option_expiry < self.exchange_timestamp:
            raise ValueError("option_expiry cannot precede exchange_timestamp")
        return self

    @model_validator(mode="after")
    def validate_directional_price_logic(self) -> "SignalStateSnapshot":
        entry = self.price_levels.entry_price
        stop = self.price_levels.stop_loss_price
        target = self.price_levels.target_price
        invalidation = self.price_levels.invalidation_price
        if self.direction == SignalDirection.LONG:
            if stop >= entry:
                raise ValueError("long signal requires stop_loss_price below entry_price")
            if target is not None and target <= entry:
                raise ValueError("long signal requires target_price above entry_price")
            if invalidation is not None and invalidation >= entry:
                raise ValueError("long signal requires invalidation_price below entry_price")
        elif self.direction == SignalDirection.SHORT:
            if stop <= entry:
                raise ValueError("short signal requires stop_loss_price above entry_price")
            if target is not None and target >= entry:
                raise ValueError("short signal requires target_price below entry_price")
            if invalidation is not None and invalidation <= entry:
                raise ValueError("short signal requires invalidation_price above entry_price")
        return self

    @computed_field(return_type=Decimal)
    @property
    def risk_per_unit(self) -> Decimal:
        return abs(self.price_levels.entry_price - self.price_levels.stop_loss_price)

    @computed_field(return_type=Decimal | None)
    @property
    def reward_per_unit(self) -> Decimal | None:
        if self.price_levels.target_price is None:
            return None
        return abs(self.price_levels.target_price - self.price_levels.entry_price)

    @computed_field(return_type=Decimal | None)
    @property
    def reward_to_risk_ratio(self) -> Decimal | None:
        reward = self.reward_per_unit
        if reward is None or self.risk_per_unit == 0:
            return None
        return reward / self.risk_per_unit

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.underlying_symbol

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        serialized_feature_values: dict[str, str | bool | int | None] = {}
        for key, value in self.feature_values.items():
            if isinstance(value, Decimal):
                serialized_feature_values[key] = format_decimal(value)
            else:
                serialized_feature_values[key] = value
        record["feature_values"] = serialized_feature_values
        decimal_fields = {
            "option_strike": self.option_strike,
            "risk_per_unit": self.risk_per_unit,
            "reward_per_unit": self.reward_per_unit,
            "reward_to_risk_ratio": self.reward_to_risk_ratio,
            "price_levels.entry_price": self.price_levels.entry_price,
            "price_levels.stop_loss_price": self.price_levels.stop_loss_price,
            "price_levels.target_price": self.price_levels.target_price,
            "price_levels.invalidation_price": self.price_levels.invalidation_price,
            "price_levels.trailing_trigger_price": self.price_levels.trailing_trigger_price,
            "price_levels.trailing_step_price": self.price_levels.trailing_step_price,
            "quality_metrics.confidence_score": self.quality_metrics.confidence_score,
            "quality_metrics.regime_alignment_score": self.quality_metrics.regime_alignment_score,
            "quality_metrics.liquidity_score": self.quality_metrics.liquidity_score,
            "quality_metrics.expected_slippage_bps": self.quality_metrics.expected_slippage_bps,
            "quality_metrics.expected_hold_minutes": self.quality_metrics.expected_hold_minutes,
            "quality_metrics.estimated_win_probability": self.quality_metrics.estimated_win_probability,
            "quality_metrics.expected_value_per_unit": self.quality_metrics.expected_value_per_unit,
            "quality_metrics.expected_reward_to_risk": self.quality_metrics.expected_reward_to_risk,
        }
        record["serialized_decimal_fields"] = {
            key: None if value is None else format_decimal(value) for key, value in decimal_fields.items()
        }
        return record


__all__ = ["SignalPriceLevels", "SignalQualityMetrics", "SignalStateSnapshot"]
