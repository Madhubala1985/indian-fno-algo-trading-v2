# FILE: platform_core/domain/models/regime_state.py
"""
platform_core.domain.models.regime_state
═══════════════════════════════════════════════════════════════════════
Production-grade market regime state domain models for Indian Index Futures &
Options workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- causal and finalized-state aware,
- suitable for live and replay regime analytics.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import ClassificationWindowType, RegimeStateType, Timeframe
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class RegimeProbabilityVector(BaseModel):
    """Probability distribution across supported regime states."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    trending: Decimal = Field(default=Decimal("0"))
    mean_reverting: Decimal = Field(default=Decimal("0"))
    volatility_expansion: Decimal = Field(default=Decimal("0"))
    volatility_compression: Decimal = Field(default=Decimal("0"))
    expiry_driven: Decimal = Field(default=Decimal("0"))
    event_driven: Decimal = Field(default=Decimal("0"))

    @field_validator(
        "trending",
        "mean_reverting",
        "volatility_expansion",
        "volatility_compression",
        "expiry_driven",
        "event_driven",
        mode="before",
    )
    @classmethod
    def normalize_probabilities(cls, value: Any) -> Decimal:
        normalized = safe_decimal(value)
        if normalized < 0 or normalized > 1:
            raise ValueError("regime probability must be within [0, 1]")
        return normalized

    @computed_field(return_type=Decimal)
    @property
    def total_probability(self) -> Decimal:
        return (
            self.trending
            + self.mean_reverting
            + self.volatility_expansion
            + self.volatility_compression
            + self.expiry_driven
            + self.event_driven
        )

    @model_validator(mode="after")
    def validate_probability_mass(self) -> "RegimeProbabilityVector":
        if self.total_probability != Decimal("1"):
            raise ValueError("regime probabilities must sum exactly to 1")
        return self

    def dominant_regime(self) -> RegimeStateType:
        mapping = {
            RegimeStateType.TRENDING: self.trending,
            RegimeStateType.MEAN_REVERTING: self.mean_reverting,
            RegimeStateType.VOLATILITY_EXPANSION: self.volatility_expansion,
            RegimeStateType.VOLATILITY_COMPRESSION: self.volatility_compression,
            RegimeStateType.EXPIRY_DRIVEN: self.expiry_driven,
            RegimeStateType.EVENT_DRIVEN: self.event_driven,
        }
        return max(mapping, key=mapping.get)

    def as_dict(self) -> dict[RegimeStateType, Decimal]:
        return {
            RegimeStateType.TRENDING: self.trending,
            RegimeStateType.MEAN_REVERTING: self.mean_reverting,
            RegimeStateType.VOLATILITY_EXPANSION: self.volatility_expansion,
            RegimeStateType.VOLATILITY_COMPRESSION: self.volatility_compression,
            RegimeStateType.EXPIRY_DRIVEN: self.expiry_driven,
            RegimeStateType.EVENT_DRIVEN: self.event_driven,
        }


class RegimeFeatureSnapshot(BaseModel):
    """Normalized feature snapshot supporting causal regime classification."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    realized_volatility_short: Decimal | None = None
    realized_volatility_long: Decimal | None = None
    implied_volatility_atm: Decimal | None = None
    volatility_risk_premium: Decimal | None = None
    adx: Decimal | None = None
    hurst_exponent: Decimal | None = None
    variance_ratio: Decimal | None = None
    order_flow_imbalance: Decimal | None = None
    gamma_exposure: Decimal | None = None
    liquidity_score: Decimal | None = None
    event_risk_score: Decimal | None = None
    expiry_proximity_days: Decimal | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "realized_volatility_short",
        "realized_volatility_long",
        "implied_volatility_atm",
        "volatility_risk_premium",
        "adx",
        "hurst_exponent",
        "variance_ratio",
        "order_flow_imbalance",
        "gamma_exposure",
        "liquidity_score",
        "event_risk_score",
        "expiry_proximity_days",
        mode="before",
    )
    @classmethod
    def normalize_decimals(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return safe_decimal(value)

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_feature_ranges(self) -> "RegimeFeatureSnapshot":
        if self.realized_volatility_short is not None and self.realized_volatility_short < 0:
            raise ValueError("realized_volatility_short cannot be negative")
        if self.realized_volatility_long is not None and self.realized_volatility_long < 0:
            raise ValueError("realized_volatility_long cannot be negative")
        if self.implied_volatility_atm is not None and self.implied_volatility_atm < 0:
            raise ValueError("implied_volatility_atm cannot be negative")
        if self.adx is not None and self.adx < 0:
            raise ValueError("adx cannot be negative")
        if self.hurst_exponent is not None and not (Decimal("0") <= self.hurst_exponent <= Decimal("1")):
            raise ValueError("hurst_exponent must be within [0, 1]")
        if self.variance_ratio is not None and self.variance_ratio < 0:
            raise ValueError("variance_ratio cannot be negative")
        if self.liquidity_score is not None and self.liquidity_score < 0:
            raise ValueError("liquidity_score cannot be negative")
        if self.event_risk_score is not None and self.event_risk_score < 0:
            raise ValueError("event_risk_score cannot be negative")
        if self.expiry_proximity_days is not None and self.expiry_proximity_days < 0:
            raise ValueError("expiry_proximity_days cannot be negative")
        return self


class RegimeStateSnapshot(BaseModel):
    """Canonical market regime classification snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    underlying_symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    timeframe: Timeframe
    classification_window_type: ClassificationWindowType

    exchange_timestamp: datetime
    received_timestamp: datetime
    effective_from_timestamp: datetime
    effective_to_timestamp: datetime | None = None
    snapshot_sequence_number: int = Field(ge=0)

    regime_state: RegimeStateType
    regime_confidence: Decimal
    probability_vector: RegimeProbabilityVector
    features: RegimeFeatureSnapshot

    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    training_window_id: str | None = None
    source: str = Field(min_length=1)
    is_finalized_bar: bool = True
    is_replay: bool = False
    raw_event_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "underlying_symbol",
        "exchange",
        "model_name",
        "model_version",
        "source",
        mode="before",
    )
    @classmethod
    def normalize_required_text(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("text field must be non-empty")
        return text

    @field_validator("training_window_id", "raw_event_id", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator(
        "exchange_timestamp",
        "received_timestamp",
        "effective_from_timestamp",
        "effective_to_timestamp",
        mode="before",
    )
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

    @field_validator("regime_confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> Decimal:
        normalized = safe_decimal(value)
        if normalized < 0 or normalized > 1:
            raise ValueError("regime_confidence must be within [0, 1]")
        return normalized

    @field_validator("metadata")
    @classmethod
    def normalize_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key).strip(): str(item).strip() for key, item in value.items()}

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "RegimeStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.effective_from_timestamp < self.exchange_timestamp:
            raise ValueError("effective_from_timestamp cannot precede exchange_timestamp")
        if self.effective_to_timestamp is not None and self.effective_to_timestamp < self.effective_from_timestamp:
            raise ValueError("effective_to_timestamp cannot precede effective_from_timestamp")
        return self

    @model_validator(mode="after")
    def validate_regime_alignment(self) -> "RegimeStateSnapshot":
        if self.probability_vector.dominant_regime() != self.regime_state:
            raise ValueError("regime_state must match dominant probability regime")
        dominant_probability = self.probability_vector.as_dict()[self.regime_state]
        if self.regime_confidence != dominant_probability:
            raise ValueError("regime_confidence must equal dominant regime probability")
        return self

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.underlying_symbol

    @computed_field(return_type=Decimal | None)
    @property
    def realized_to_implied_volatility_ratio(self) -> Decimal | None:
        if self.features.realized_volatility_short is None or self.features.implied_volatility_atm is None:
            return None
        if self.features.implied_volatility_atm == 0:
            return None
        return self.features.realized_volatility_short / self.features.implied_volatility_atm

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        decimal_paths = {
            "regime_confidence": self.regime_confidence,
            "realized_to_implied_volatility_ratio": self.realized_to_implied_volatility_ratio,
            "probability_vector.trending": self.probability_vector.trending,
            "probability_vector.mean_reverting": self.probability_vector.mean_reverting,
            "probability_vector.volatility_expansion": self.probability_vector.volatility_expansion,
            "probability_vector.volatility_compression": self.probability_vector.volatility_compression,
            "probability_vector.expiry_driven": self.probability_vector.expiry_driven,
            "probability_vector.event_driven": self.probability_vector.event_driven,
            "features.realized_volatility_short": self.features.realized_volatility_short,
            "features.realized_volatility_long": self.features.realized_volatility_long,
            "features.implied_volatility_atm": self.features.implied_volatility_atm,
            "features.volatility_risk_premium": self.features.volatility_risk_premium,
            "features.adx": self.features.adx,
            "features.hurst_exponent": self.features.hurst_exponent,
            "features.variance_ratio": self.features.variance_ratio,
            "features.order_flow_imbalance": self.features.order_flow_imbalance,
            "features.gamma_exposure": self.features.gamma_exposure,
            "features.liquidity_score": self.features.liquidity_score,
            "features.event_risk_score": self.features.event_risk_score,
            "features.expiry_proximity_days": self.features.expiry_proximity_days,
        }
        record["serialized_decimal_fields"] = {
            key: None if value is None else format_decimal(value) for key, value in decimal_paths.items()
        }
        return record


__all__ = ["RegimeProbabilityVector", "RegimeFeatureSnapshot", "RegimeStateSnapshot"]
