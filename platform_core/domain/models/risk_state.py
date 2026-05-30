# FILE: platform_core/domain/models/risk_state.py
"""
platform_core.domain.models.risk_state
═══════════════════════════════════════════════════════════════════════
Production-grade risk state domain models for Indian Index Futures & Options
workflows.

This module is designed to be:
- serialization-safe,
- replay-compatible,
- event-driven compatible,
- PostgreSQL / TimescaleDB / Redis compatible,
- no-hindsight safe,
- risk-governance aware,
- suitable for live and replay risk-monitoring workflows.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from platform_core.core.enums import RiskLevel, RiskState, TradingHaltReason
from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal


class RiskBudgetSnapshot(BaseModel):
    """Risk budget and utilization metrics for a portfolio or strategy namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    max_position_notional: Decimal = Field(gt=0)
    max_portfolio_notional: Decimal = Field(gt=0)
    max_portfolio_heat: Decimal = Field(gt=0)
    max_drawdown_limit: Decimal = Field(gt=0)
    max_margin_utilization: Decimal = Field(gt=0)
    max_correlation_exposure: Decimal = Field(gt=0)
    max_liquidity_risk_score: Decimal = Field(gt=0)

    @field_validator(
        "max_position_notional",
        "max_portfolio_notional",
        "max_portfolio_heat",
        "max_drawdown_limit",
        "max_margin_utilization",
        "max_correlation_exposure",
        "max_liquidity_risk_score",
        mode="before",
    )
    @classmethod
    def normalize_positive_decimals(cls, value: Any) -> Decimal:
        normalized = safe_decimal(value)
        if normalized <= 0:
            raise ValueError("risk budget values must be positive")
        return normalized


class RiskUtilizationSnapshot(BaseModel):
    """Current risk utilization against the configured budget envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    current_position_notional: Decimal = Decimal("0")
    current_portfolio_notional: Decimal = Decimal("0")
    current_portfolio_heat: Decimal = Decimal("0")
    current_drawdown: Decimal = Decimal("0")
    current_margin_utilization: Decimal = Decimal("0")
    current_correlation_exposure: Decimal = Decimal("0")
    current_liquidity_risk_score: Decimal = Decimal("0")
    slippage_bps_p95: Decimal = Decimal("0")
    latency_ms_p95: Decimal = Decimal("0")
    api_error_rate: Decimal = Decimal("0")

    @field_validator(
        "current_position_notional",
        "current_portfolio_notional",
        "current_portfolio_heat",
        "current_drawdown",
        "current_margin_utilization",
        "current_correlation_exposure",
        "current_liquidity_risk_score",
        "slippage_bps_p95",
        "latency_ms_p95",
        "api_error_rate",
        mode="before",
    )
    @classmethod
    def normalize_non_negative_decimals(cls, value: Any) -> Decimal:
        normalized = safe_decimal(value)
        if normalized < 0:
            raise ValueError("risk utilization values cannot be negative")
        return normalized


class RiskTriggerSnapshot(BaseModel):
    """Discrete risk-trigger state for enforcement, halts, and kill switches."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    drawdown_breach: bool = False
    portfolio_heat_breach: bool = False
    margin_breach: bool = False
    correlation_breach: bool = False
    liquidity_breach: bool = False
    execution_health_breach: bool = False
    event_risk_lockdown: bool = False
    manual_halt: bool = False
    global_kill_switch_active: bool = False


class RiskStateSnapshot(BaseModel):
    """Canonical risk state snapshot for orchestration, execution gating, and replay systems."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, use_enum_values=False)

    risk_state_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    strategy_namespace: str | None = None

    state: RiskState
    risk_level: RiskLevel
    halt_reason: TradingHaltReason | None = None

    budget: RiskBudgetSnapshot
    utilization: RiskUtilizationSnapshot
    triggers: RiskTriggerSnapshot

    exchange_timestamp: datetime
    received_timestamp: datetime
    created_timestamp: datetime
    updated_timestamp: datetime
    effective_from_timestamp: datetime
    effective_to_timestamp: datetime | None = None
    snapshot_sequence_number: int = Field(ge=0)

    is_replay: bool = False
    source: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "risk_state_id",
        "portfolio_id",
        "strategy_namespace",
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
    def validate_required_text_fields(self) -> "RiskStateSnapshot":
        for field_name in ("risk_state_id", "portfolio_id", "source"):
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be non-empty")
        return self

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "RiskStateSnapshot":
        if self.received_timestamp < self.exchange_timestamp:
            raise ValueError("received_timestamp cannot precede exchange_timestamp")
        if self.created_timestamp < self.exchange_timestamp:
            raise ValueError("created_timestamp cannot precede exchange_timestamp")
        if self.updated_timestamp < self.created_timestamp:
            raise ValueError("updated_timestamp cannot precede created_timestamp")
        if self.effective_from_timestamp < self.created_timestamp:
            raise ValueError("effective_from_timestamp cannot precede created_timestamp")
        if self.effective_to_timestamp is not None and self.effective_to_timestamp < self.effective_from_timestamp:
            raise ValueError("effective_to_timestamp cannot precede effective_from_timestamp")
        return self

    @model_validator(mode="after")
    def validate_utilization_consistency(self) -> "RiskStateSnapshot":
        if self.utilization.current_position_notional > self.budget.max_position_notional and not self.triggers.global_kill_switch_active:
            raise ValueError("position notional budget breach must be reflected in enforcement state")
        if self.utilization.current_portfolio_notional > self.budget.max_portfolio_notional and self.state.name == "NORMAL":
            raise ValueError("portfolio notional budget breach cannot remain in normal risk state")
        if self.utilization.current_portfolio_heat > self.budget.max_portfolio_heat and not self.triggers.portfolio_heat_breach:
            raise ValueError("portfolio heat breach requires portfolio_heat_breach trigger")
        if self.utilization.current_drawdown > self.budget.max_drawdown_limit and not self.triggers.drawdown_breach:
            raise ValueError("drawdown breach requires drawdown_breach trigger")
        if self.utilization.current_margin_utilization > self.budget.max_margin_utilization and not self.triggers.margin_breach:
            raise ValueError("margin breach requires margin_breach trigger")
        if self.utilization.current_correlation_exposure > self.budget.max_correlation_exposure and not self.triggers.correlation_breach:
            raise ValueError("correlation breach requires correlation_breach trigger")
        if self.utilization.current_liquidity_risk_score > self.budget.max_liquidity_risk_score and not self.triggers.liquidity_breach:
            raise ValueError("liquidity breach requires liquidity_breach trigger")
        return self

    @model_validator(mode="after")
    def validate_state_consistency(self) -> "RiskStateSnapshot":
        halt_active = any(
            (
                self.triggers.drawdown_breach,
                self.triggers.margin_breach,
                self.triggers.execution_health_breach,
                self.triggers.event_risk_lockdown,
                self.triggers.manual_halt,
                self.triggers.global_kill_switch_active,
            )
        )
        if self.state.name in {"HALTED", "KILL_SWITCHED"} and self.halt_reason is None:
            raise ValueError("halted risk states require halt_reason")
        if self.state.name == "NORMAL" and halt_active:
            raise ValueError("normal risk state cannot contain active halt triggers")
        if self.state.name == "KILL_SWITCHED" and not self.triggers.global_kill_switch_active:
            raise ValueError("kill-switched state requires global_kill_switch_active trigger")
        return self

    @computed_field(return_type=Decimal)
    @property
    def position_notional_utilization(self) -> Decimal:
        return self.utilization.current_position_notional / self.budget.max_position_notional

    @computed_field(return_type=Decimal)
    @property
    def portfolio_notional_utilization(self) -> Decimal:
        return self.utilization.current_portfolio_notional / self.budget.max_portfolio_notional

    @computed_field(return_type=Decimal)
    @property
    def portfolio_heat_utilization(self) -> Decimal:
        return self.utilization.current_portfolio_heat / self.budget.max_portfolio_heat

    @computed_field(return_type=Decimal)
    @property
    def drawdown_utilization(self) -> Decimal:
        return self.utilization.current_drawdown / self.budget.max_drawdown_limit

    @computed_field(return_type=Decimal)
    @property
    def risk_pressure_score(self) -> Decimal:
        components = (
            self.portfolio_notional_utilization,
            self.portfolio_heat_utilization,
            self.drawdown_utilization,
            self.utilization.current_margin_utilization / self.budget.max_margin_utilization,
            self.utilization.current_correlation_exposure / self.budget.max_correlation_exposure,
            self.utilization.current_liquidity_risk_score / self.budget.max_liquidity_risk_score,
        )
        return sum(components, Decimal("0")) / Decimal(len(components))

    @computed_field(return_type=bool)
    @property
    def trading_blocked(self) -> bool:
        return self.state.name in {"HALTED", "KILL_SWITCHED"} or self.triggers.global_kill_switch_active

    @computed_field(return_type=str)
    @property
    def event_partition_key(self) -> str:
        return self.portfolio_id

    def to_event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_storage_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="python")
        decimal_fields = {
            "position_notional_utilization": self.position_notional_utilization,
            "portfolio_notional_utilization": self.portfolio_notional_utilization,
            "portfolio_heat_utilization": self.portfolio_heat_utilization,
            "drawdown_utilization": self.drawdown_utilization,
            "risk_pressure_score": self.risk_pressure_score,
            "budget.max_position_notional": self.budget.max_position_notional,
            "budget.max_portfolio_notional": self.budget.max_portfolio_notional,
            "budget.max_portfolio_heat": self.budget.max_portfolio_heat,
            "budget.max_drawdown_limit": self.budget.max_drawdown_limit,
            "budget.max_margin_utilization": self.budget.max_margin_utilization,
            "budget.max_correlation_exposure": self.budget.max_correlation_exposure,
            "budget.max_liquidity_risk_score": self.budget.max_liquidity_risk_score,
            "utilization.current_position_notional": self.utilization.current_position_notional,
            "utilization.current_portfolio_notional": self.utilization.current_portfolio_notional,
            "utilization.current_portfolio_heat": self.utilization.current_portfolio_heat,
            "utilization.current_drawdown": self.utilization.current_drawdown,
            "utilization.current_margin_utilization": self.utilization.current_margin_utilization,
            "utilization.current_correlation_exposure": self.utilization.current_correlation_exposure,
            "utilization.current_liquidity_risk_score": self.utilization.current_liquidity_risk_score,
            "utilization.slippage_bps_p95": self.utilization.slippage_bps_p95,
            "utilization.latency_ms_p95": self.utilization.latency_ms_p95,
            "utilization.api_error_rate": self.utilization.api_error_rate,
        }
        record["serialized_decimal_fields"] = {
            key: format_decimal(value) for key, value in decimal_fields.items()
        }
        return record


__all__ = [
    "RiskBudgetSnapshot",
    "RiskUtilizationSnapshot",
    "RiskTriggerSnapshot",
    "RiskStateSnapshot",
]
