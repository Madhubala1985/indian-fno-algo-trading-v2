# FILE: platform_core/domain/models/portfolio_state.py
"""
platform_core.domain.models.portfolio_state
═══════════════════════════════════════════════════════════════════════
Production-grade portfolio state domain models for Indian Index Futures &
Options workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- portfolio-aggregation aware,
- suitable for live and replay portfolio monitoring workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import ExchangeCode, PortfolioState
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class PortfolioCapitalSnapshot(BaseModel):
    """Capital, collateral, and margin balances for a portfolio snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    base_currency: str = Field(min_length=1)
    cash_balance: Decimal = Decimal("0")
    available_cash: Decimal = Decimal("0")
    collateral_value: Decimal = Decimal("0")
    margin_used: Decimal = Decimal("0")
    margin_available: Decimal = Decimal("0")
    initial_equity: Decimal = Field(gt=0)
    net_liquidation_value: Decimal = Field(gt=0)
    peak_equity: Decimal = Field(gt=0)

    @field_validator(
        "base_currency",
        mode="before",
    )
    @classmethod
    def normalize_currency(cls, value: Any) -> str:
        text = str(value).strip().upper()
        if not text:
            raise ValueError("base_currency must be non-empty")
        return text

    @field_validator(
        "cash_balance",
        "available_cash",
        "collateral_value",
        "margin_used",
        "margin_available",
        "initial_equity",
        "net_liquidation_value",
        "peak_equity",
        mode="before",
    )
    @classmethod
    def normalize_decimals(cls, value: Any) -> Decimal:
        normalized = safe_decimal(value)
        if normalized < 0:
            raise ValueError("portfolio capital values cannot be negative")
        return normalized

    @model_validator(mode="after")
    def validate_capital_snapshot(self) -> "PortfolioCapitalSnapshot":
        if self.available_cash > self.cash_balance + self.collateral_value:
            raise ValueError("available_cash cannot exceed cash_balance + collateral_value")
        if self.net_liquidation_value > self.peak_equity:
            raise ValueError("peak_equity must be at least net_liquidation_value")
        return self


class PortfolioExposureSnapshot(BaseModel):
    """Portfolio-level directional, options, and concentration exposures."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    gross_notional_exposure: Decimal = Decimal("0")
    net_notional_exposure: Decimal = Decimal("0")
    gross_delta_exposure: Decimal = Decimal("0")
    net_delta_exposure: Decimal = Decimal("0")
    net_gamma_exposure: Decimal = Decimal("0")
    net_theta_exposure: Decimal = Decimal("0")
    net_vega_exposure: Decimal = Decimal("0")
    correlation_exposure_score: Decimal = Decimal("0")
    concentration_score: Decimal = Decimal("0")
    liquidity_risk_score: Decimal = Decimal("0")

    @field_validator(
        "gross_notional_exposure",
        "net_notional_exposure",
        "gross_delta_exposure",
        "net_delta_exposure",
        "net_gamma_exposure",
        "net_theta_exposure",
        "net_vega_exposure",
        "correlation_exposure_score",
        "concentration_score",
        "liquidity_risk_score",
        mode="before",
    )
    @classmethod
    def normalize_exposures(cls, value: Any) -> Decimal:
        return safe_decimal(value)

    @model_validator(mode="after")
    def validate_exposure_snapshot(self) -> "PortfolioExposureSnapshot":
        if self.gross_notional_exposure < 0:
            raise ValueError("gross_notional_exposure cannot be negative")
        if self.correlation_exposure_score < 0:
            raise ValueError("correlation_exposure_score cannot be negative")
        if self.concentration_score < 0:
            raise ValueError("concentration_score cannot be negative")
        if self.liquidity_risk_score < 0:
            raise ValueError("liquidity_risk_score cannot be negative")
        return self


class PortfolioPnLSnapshot(BaseModel):
    """Portfolio-level realized, unrealized, and drawdown metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    carry_pnl: Decimal = Decimal("0")
    day_pnl: Decimal = Decimal("0")
    max_drawdown_to_date: Decimal = Decimal("0")

    @field_validator(
        "realized_pnl",
        "unrealized_pnl",
        "fees_paid",
        "carry_pnl",
        "day_pnl",
        "max_drawdown_to_date",
        mode="before",
    )
    @classmethod
    def normalize_pnl_fields(cls, value: Any) -> Decimal:
        return safe_decimal(value)

    @model_validator(mode="after")
    def validate_pnl_fields(self) -> "PortfolioPnLSnapshot":
        if self.fees_paid < 0:
            raise ValueError("fees_paid cannot be negative")
        if self.max_drawdown_to_date < 0:
            raise ValueError("max_drawdown_to_date cannot be negative")
        return self


class PortfolioInventorySnapshot(BaseModel):
    """Inventory counters for active portfolio state."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    active_positions: int = Field(ge=0)
    open_orders: int = Field(ge=0)
    pending_orders: int = Field(ge=0)
    rejected_orders: int = Field(ge=0)
    symbols_traded_today: int = Field(ge=0)
    expiries_active: int = Field(ge=0)


class PortfolioStateSnapshot(BaseModel):
    """Canonical portfolio lifecycle snapshot for orchestration, risk, and replay systems."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    portfolio_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    strategy_namespace: str | None = None
    broker_account_reference: str | None = None

    state: PortfolioState
    exchange: ExchangeCode

    capital: PortfolioCapitalSnapshot
    exposure: PortfolioExposureSnapshot
    pnl: PortfolioPnLSnapshot
    inventory: PortfolioInventorySnapshot

    exchange_timestamp: datetime
    received_timestamp: datetime
    created_timestamp: datetime
    updated_timestamp: datetime
    snapshot_sequence_number: int = Field(ge=0)

    is_replay: bool = False
    source: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "portfolio_id",
        "account_id",
        "strategy_namespace",
        "broker_account_reference",
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
        "exchange_timestamp",
        "received_timestamp",
        "created_timestamp",
        "updated_timestamp",
        mode="before",
    )
    @classmethod
    def normalize_datetimes(cls, value: Any) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("datetime field must be a datetime instance")
        return ensure_utc(value)

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
    def validate_required_text_fields(self) -> "PortfolioStateSnapshot":
        for field_name in ("portfolio_id", "account_id", "source"):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "PortfolioStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.created_timestamp < self.exchange_timestamp:
            raise ValueError("created_timestamp cannot precede exchange_timestamp")
        if self.updated_timestamp < self.created_timestamp:
            raise ValueError("updated_timestamp cannot precede created_timestamp")
        return self

    @model_validator(mode="after")
    def validate_state_consistency(self) -> "PortfolioStateSnapshot":
        if self.inventory.active_positions == 0 and self.exposure.gross_notional_exposure != 0:
            raise ValueError("gross_notional_exposure must be zero when no active positions exist")
        if self.state.name == "HALTED" and self.inventory.pending_orders > 0:
            raise ValueError("halted portfolios cannot retain pending_orders")
        if self.inventory.open_orders < self.inventory.pending_orders:
            raise ValueError("open_orders cannot be less than pending_orders")
        return self

    @computed_field(return_type=Decimal)
    @property
    def gross_pnl(self) -> Decimal:
        return self.pnl.realized_pnl + self.pnl.unrealized_pnl + self.pnl.carry_pnl

    @computed_field(return_type=Decimal)
    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.pnl.fees_paid

    @computed_field(return_type=Decimal)
    @property
    def equity_change(self) -> Decimal:
        return self.capital.net_liquidation_value - self.capital.initial_equity

    @computed_field(return_type=Decimal)
    @property
    def current_drawdown(self) -> Decimal:
        return self.capital.peak_equity - self.capital.net_liquidation_value

    @computed_field(return_type=Decimal)
    @property
    def margin_utilization(self) -> Decimal:
        denominator = self.capital.margin_used + self.capital.margin_available
        if denominator == 0:
            return Decimal("0")
        return self.capital.margin_used / denominator

    @computed_field(return_type=Decimal)
    @property
    def liquidity_buffer(self) -> Decimal:
        return self.capital.available_cash + self.capital.collateral_value - self.capital.margin_used

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.portfolio_id

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        decimal_fields = {
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
            "equity_change": self.equity_change,
            "current_drawdown": self.current_drawdown,
            "margin_utilization": self.margin_utilization,
            "liquidity_buffer": self.liquidity_buffer,
            "capital.cash_balance": self.capital.cash_balance,
            "capital.available_cash": self.capital.available_cash,
            "capital.collateral_value": self.capital.collateral_value,
            "capital.margin_used": self.capital.margin_used,
            "capital.margin_available": self.capital.margin_available,
            "capital.initial_equity": self.capital.initial_equity,
            "capital.net_liquidation_value": self.capital.net_liquidation_value,
            "capital.peak_equity": self.capital.peak_equity,
            "exposure.gross_notional_exposure": self.exposure.gross_notional_exposure,
            "exposure.net_notional_exposure": self.exposure.net_notional_exposure,
            "exposure.gross_delta_exposure": self.exposure.gross_delta_exposure,
            "exposure.net_delta_exposure": self.exposure.net_delta_exposure,
            "exposure.net_gamma_exposure": self.exposure.net_gamma_exposure,
            "exposure.net_theta_exposure": self.exposure.net_theta_exposure,
            "exposure.net_vega_exposure": self.exposure.net_vega_exposure,
            "exposure.correlation_exposure_score": self.exposure.correlation_exposure_score,
            "exposure.concentration_score": self.exposure.concentration_score,
            "exposure.liquidity_risk_score": self.exposure.liquidity_risk_score,
            "pnl.realized_pnl": self.pnl.realized_pnl,
            "pnl.unrealized_pnl": self.pnl.unrealized_pnl,
            "pnl.fees_paid": self.pnl.fees_paid,
            "pnl.carry_pnl": self.pnl.carry_pnl,
            "pnl.day_pnl": self.pnl.day_pnl,
            "pnl.max_drawdown_to_date": self.pnl.max_drawdown_to_date,
        }
        record["serialized_decimal_fields"] = {
            key: format_decimal(value) for key, value in decimal_fields.items()
        }
        return record


__all__ = [
    "PortfolioCapitalSnapshot",
    "PortfolioExposureSnapshot",
    "PortfolioPnLSnapshot",
    "PortfolioInventorySnapshot",
    "PortfolioStateSnapshot",
]
