# FILE: platform_core/risk/kill_switches.py
"""
platform_core.risk.kill_switches
═══════════════════════════════════════════════════════════════════════
Production-grade institutional kill-switch engine specialized for Indian index
futures and options workflows.

The engine evaluates deterministic, causal safety triggers and escalates to
soft pause, session halt, broker block, or hard kill states. It is designed for
risk-first orchestration and preserves replay safety by using finalized inputs
only. Supported trigger families include:
- Drawdown and portfolio-heat breaches.
- Exposure-limit breaches.
- Execution-quality deterioration.
- Reject-rate and infrastructure instability.
- Duplicate/looping order behavior.
- Market-data freshness failures.
- Manual operator overrides.

This module does not cancel orders or close positions directly. It emits a
structured control decision for downstream orchestration, execution, and
emergency shutdown layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class KillSwitchError(RuntimeError):
    """Base exception for kill-switch failures."""


class KillSwitchInputError(KillSwitchError):
    """Raised when kill-switch inputs are invalid or causally unsafe."""


class KillSwitchComputationError(KillSwitchError):
    """Raised when kill-switch evaluation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class KillSwitchConfig:
    """Configuration for automated kill-switch escalation."""

    warning_slippage_bps: float = 12.0
    soft_pause_slippage_bps: float = 20.0
    hard_kill_slippage_bps: float = 40.0
    warning_reject_rate_fraction: float = 0.08
    broker_block_reject_rate_fraction: float = 0.20
    hard_kill_reject_rate_fraction: float = 0.35
    soft_pause_consecutive_losses: int = 3
    session_halt_consecutive_losses: int = 5
    duplicate_order_loop_threshold: int = 3
    data_staleness_soft_pause_seconds: float = 3.0
    data_staleness_hard_kill_seconds: float = 8.0
    soft_pause_cooldown_minutes: int = 10
    session_halt_cooldown_minutes: int = 60
    hard_kill_requires_manual_reset: bool = True
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        numeric_positive_fields = {
            "warning_slippage_bps": self.warning_slippage_bps,
            "soft_pause_slippage_bps": self.soft_pause_slippage_bps,
            "hard_kill_slippage_bps": self.hard_kill_slippage_bps,
            "warning_reject_rate_fraction": self.warning_reject_rate_fraction,
            "broker_block_reject_rate_fraction": self.broker_block_reject_rate_fraction,
            "hard_kill_reject_rate_fraction": self.hard_kill_reject_rate_fraction,
            "data_staleness_soft_pause_seconds": self.data_staleness_soft_pause_seconds,
            "data_staleness_hard_kill_seconds": self.data_staleness_hard_kill_seconds,
        }
        for field_name, field_value in numeric_positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        bounded_fractions = {
            "warning_reject_rate_fraction": self.warning_reject_rate_fraction,
            "broker_block_reject_rate_fraction": self.broker_block_reject_rate_fraction,
            "hard_kill_reject_rate_fraction": self.hard_kill_reject_rate_fraction,
        }
        for field_name, field_value in bounded_fractions.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not self.warning_slippage_bps <= self.soft_pause_slippage_bps <= self.hard_kill_slippage_bps:
            raise ValueError("slippage thresholds must satisfy warning <= soft_pause <= hard_kill")
        if not self.warning_reject_rate_fraction <= self.broker_block_reject_rate_fraction <= self.hard_kill_reject_rate_fraction:
            raise ValueError("reject-rate thresholds must satisfy warning <= broker_block <= hard_kill")
        if self.soft_pause_consecutive_losses <= 0 or self.session_halt_consecutive_losses <= 0:
            raise ValueError("consecutive loss thresholds must be positive")
        if self.soft_pause_consecutive_losses > self.session_halt_consecutive_losses:
            raise ValueError("soft_pause_consecutive_losses must be less than or equal to session_halt_consecutive_losses")
        if self.duplicate_order_loop_threshold <= 0:
            raise ValueError("duplicate_order_loop_threshold must be positive")
        if self.data_staleness_soft_pause_seconds > self.data_staleness_hard_kill_seconds:
            raise ValueError("data staleness thresholds must satisfy soft_pause <= hard_kill")
        if self.soft_pause_cooldown_minutes <= 0 or self.session_halt_cooldown_minutes <= 0:
            raise ValueError("cooldown durations must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class KillSwitchSnapshot:
    """Serializable finalized snapshot of safety-critical system conditions."""

    timestamp: datetime
    drawdown_status: str
    portfolio_heat_status: str
    exposure_status: str
    trading_allowed_by_drawdown: bool
    adjusted_slippage_bps: float
    reject_rate_fraction: float
    consecutive_losses: int
    duplicate_order_signals: int
    data_staleness_seconds: float
    infrastructure_healthy: bool
    manual_kill_requested: bool
    manual_resume_requested: bool

    def __post_init__(self) -> None:
        statuses = {
            "drawdown_status": self.drawdown_status,
            "portfolio_heat_status": self.portfolio_heat_status,
            "exposure_status": self.exposure_status,
        }
        for field_name, field_value in statuses.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        numeric_values = {
            "adjusted_slippage_bps": self.adjusted_slippage_bps,
            "reject_rate_fraction": self.reject_rate_fraction,
            "data_staleness_seconds": self.data_staleness_seconds,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.adjusted_slippage_bps < 0.0:
            raise ValueError("adjusted_slippage_bps must be non-negative")
        if not 0.0 <= self.reject_rate_fraction <= 1.0:
            raise ValueError("reject_rate_fraction must be between 0 and 1")
        if self.consecutive_losses < 0:
            raise ValueError("consecutive_losses must be non-negative")
        if self.duplicate_order_signals < 0:
            raise ValueError("duplicate_order_signals must be non-negative")
        if self.data_staleness_seconds < 0.0:
            raise ValueError("data_staleness_seconds must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "drawdown_status": self.drawdown_status,
            "portfolio_heat_status": self.portfolio_heat_status,
            "exposure_status": self.exposure_status,
            "trading_allowed_by_drawdown": self.trading_allowed_by_drawdown,
            "adjusted_slippage_bps": self.adjusted_slippage_bps,
            "reject_rate_fraction": self.reject_rate_fraction,
            "consecutive_losses": self.consecutive_losses,
            "duplicate_order_signals": self.duplicate_order_signals,
            "data_staleness_seconds": self.data_staleness_seconds,
            "infrastructure_healthy": self.infrastructure_healthy,
            "manual_kill_requested": self.manual_kill_requested,
            "manual_resume_requested": self.manual_resume_requested,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchBreakdown:
    """Interpretable decomposition of trigger evaluation."""

    drawdown_triggered: bool
    portfolio_heat_triggered: bool
    exposure_triggered: bool
    slippage_warning_triggered: bool
    slippage_hard_triggered: bool
    reject_warning_triggered: bool
    reject_broker_block_triggered: bool
    reject_hard_triggered: bool
    loss_soft_pause_triggered: bool
    loss_session_halt_triggered: bool
    duplicate_loop_triggered: bool
    data_soft_pause_triggered: bool
    data_hard_triggered: bool
    infrastructure_triggered: bool
    manual_kill_triggered: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "drawdown_triggered": self.drawdown_triggered,
            "portfolio_heat_triggered": self.portfolio_heat_triggered,
            "exposure_triggered": self.exposure_triggered,
            "slippage_warning_triggered": self.slippage_warning_triggered,
            "slippage_hard_triggered": self.slippage_hard_triggered,
            "reject_warning_triggered": self.reject_warning_triggered,
            "reject_broker_block_triggered": self.reject_broker_block_triggered,
            "reject_hard_triggered": self.reject_hard_triggered,
            "loss_soft_pause_triggered": self.loss_soft_pause_triggered,
            "loss_session_halt_triggered": self.loss_session_halt_triggered,
            "duplicate_loop_triggered": self.duplicate_loop_triggered,
            "data_soft_pause_triggered": self.data_soft_pause_triggered,
            "data_hard_triggered": self.data_hard_triggered,
            "infrastructure_triggered": self.infrastructure_triggered,
            "manual_kill_triggered": self.manual_kill_triggered,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """Structured downstream control decision."""

    timestamp: datetime
    level: str
    new_entries_allowed: bool
    exits_allowed: bool
    broker_actions_blocked: bool
    manual_reset_required: bool
    cooldown_until: datetime | None
    reason: str
    breakdown: KillSwitchBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "level": self.level,
            "new_entries_allowed": self.new_entries_allowed,
            "exits_allowed": self.exits_allowed,
            "broker_actions_blocked": self.broker_actions_blocked,
            "manual_reset_required": self.manual_reset_required,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """Serializable engine state for replay persistence."""

    processed_snapshots: int
    last_timestamp: datetime | None
    last_level: str | None
    cooldown_until: datetime | None
    hard_kill_latched: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_snapshots": self.processed_snapshots,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_level": self.last_level,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "hard_kill_latched": self.hard_kill_latched,
        }


class KillSwitchEngine:
    """Institutional-grade deterministic kill-switch engine."""

    def __init__(self, *, config: KillSwitchConfig | None = None) -> None:
        self._config = config or KillSwitchConfig()
        self._processed_snapshots = 0
        self._last_timestamp: datetime | None = None
        self._last_level: str | None = None
        self._cooldown_until: datetime | None = None
        self._hard_kill_latched = False
        self._last_decision: KillSwitchDecision | None = None

    @property
    def state(self) -> KillSwitchState:
        return KillSwitchState(
            processed_snapshots=self._processed_snapshots,
            last_timestamp=self._last_timestamp,
            last_level=self._last_level,
            cooldown_until=self._cooldown_until,
            hard_kill_latched=self._hard_kill_latched,
        )

    def reset(self) -> None:
        self._processed_snapshots = 0
        self._last_timestamp = None
        self._last_level = None
        self._cooldown_until = None
        self._hard_kill_latched = False
        self._last_decision = None

    def evaluate(
        self,
        *,
        snapshot: KillSwitchSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> KillSwitchDecision:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        if snapshot.manual_resume_requested and not self._hard_kill_latched:
            self._cooldown_until = None

        breakdown = KillSwitchBreakdown(
            drawdown_triggered=(not snapshot.trading_allowed_by_drawdown) or snapshot.drawdown_status.upper() == "PAUSED",
            portfolio_heat_triggered=snapshot.portfolio_heat_status.upper() in {"CRITICAL", "BREACH"},
            exposure_triggered=snapshot.exposure_status.upper() in {"CRITICAL", "BREACH"},
            slippage_warning_triggered=snapshot.adjusted_slippage_bps >= self._config.warning_slippage_bps,
            slippage_hard_triggered=snapshot.adjusted_slippage_bps >= self._config.hard_kill_slippage_bps,
            reject_warning_triggered=snapshot.reject_rate_fraction >= self._config.warning_reject_rate_fraction,
            reject_broker_block_triggered=snapshot.reject_rate_fraction >= self._config.broker_block_reject_rate_fraction,
            reject_hard_triggered=snapshot.reject_rate_fraction >= self._config.hard_kill_reject_rate_fraction,
            loss_soft_pause_triggered=snapshot.consecutive_losses >= self._config.soft_pause_consecutive_losses,
            loss_session_halt_triggered=snapshot.consecutive_losses >= self._config.session_halt_consecutive_losses,
            duplicate_loop_triggered=snapshot.duplicate_order_signals >= self._config.duplicate_order_loop_threshold,
            data_soft_pause_triggered=snapshot.data_staleness_seconds >= self._config.data_staleness_soft_pause_seconds,
            data_hard_triggered=snapshot.data_staleness_seconds >= self._config.data_staleness_hard_kill_seconds,
            infrastructure_triggered=not snapshot.infrastructure_healthy,
            manual_kill_triggered=snapshot.manual_kill_requested,
        )

        decision = self._decide(timestamp=normalized_timestamp, snapshot=snapshot, breakdown=breakdown)
        self._processed_snapshots += 1
        self._last_timestamp = normalized_timestamp
        self._last_level = decision.level
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> KillSwitchDecision:
        if self._last_decision is None:
            raise KillSwitchComputationError("no kill-switch decision is available")
        return self._last_decision

    def _validate_snapshot(self, *, snapshot: KillSwitchSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise KillSwitchInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise KillSwitchInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise KillSwitchInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _decide(
        self,
        *,
        timestamp: datetime,
        snapshot: KillSwitchSnapshot,
        breakdown: KillSwitchBreakdown,
    ) -> KillSwitchDecision:
        if breakdown.manual_kill_triggered:
            self._hard_kill_latched = True
            self._cooldown_until = None
            return self._build_decision(
                timestamp=timestamp,
                level="HARD_KILL",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=True,
                manual_reset_required=self._config.hard_kill_requires_manual_reset,
                cooldown_until=None,
                reason="MANUAL_KILL_REQUESTED",
                breakdown=breakdown,
            )

        if self._hard_kill_latched:
            if snapshot.manual_resume_requested and not self._config.hard_kill_requires_manual_reset:
                self._hard_kill_latched = False
            else:
                return self._build_decision(
                    timestamp=timestamp,
                    level="HARD_KILL",
                    new_entries_allowed=False,
                    exits_allowed=True,
                    broker_actions_blocked=True,
                    manual_reset_required=True,
                    cooldown_until=None,
                    reason="HARD_KILL_LATCHED",
                    breakdown=breakdown,
                )

        if (
            breakdown.slippage_hard_triggered
            or breakdown.reject_hard_triggered
            or breakdown.data_hard_triggered
            or breakdown.infrastructure_triggered
            or (breakdown.drawdown_triggered and breakdown.exposure_triggered)
        ):
            self._hard_kill_latched = self._config.hard_kill_requires_manual_reset
            self._cooldown_until = None
            return self._build_decision(
                timestamp=timestamp,
                level="HARD_KILL",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=True,
                manual_reset_required=self._config.hard_kill_requires_manual_reset,
                cooldown_until=None,
                reason="HARD_TRIGGER_BREACH",
                breakdown=breakdown,
            )

        if breakdown.reject_broker_block_triggered or breakdown.duplicate_loop_triggered:
            self._cooldown_until = timestamp + timedelta(minutes=self._config.session_halt_cooldown_minutes)
            return self._build_decision(
                timestamp=timestamp,
                level="BROKER_BLOCK",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=True,
                manual_reset_required=False,
                cooldown_until=self._cooldown_until,
                reason="BROKER_RISK_TRIGGER",
                breakdown=breakdown,
            )

        if (
            breakdown.loss_session_halt_triggered
            or breakdown.drawdown_triggered
            or breakdown.portfolio_heat_triggered
            or breakdown.exposure_triggered
        ):
            self._cooldown_until = timestamp + timedelta(minutes=self._config.session_halt_cooldown_minutes)
            return self._build_decision(
                timestamp=timestamp,
                level="SESSION_HALT",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=False,
                manual_reset_required=False,
                cooldown_until=self._cooldown_until,
                reason="SESSION_RISK_TRIGGER",
                breakdown=breakdown,
            )

        if breakdown.data_soft_pause_triggered or breakdown.loss_soft_pause_triggered or breakdown.slippage_warning_triggered:
            self._cooldown_until = timestamp + timedelta(minutes=self._config.soft_pause_cooldown_minutes)
            return self._build_decision(
                timestamp=timestamp,
                level="SOFT_PAUSE",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=False,
                manual_reset_required=False,
                cooldown_until=self._cooldown_until,
                reason="SOFT_RISK_TRIGGER",
                breakdown=breakdown,
            )

        if self._cooldown_until is not None and timestamp < self._cooldown_until:
            return self._build_decision(
                timestamp=timestamp,
                level="COOLDOWN",
                new_entries_allowed=False,
                exits_allowed=True,
                broker_actions_blocked=False,
                manual_reset_required=False,
                cooldown_until=self._cooldown_until,
                reason="COOLDOWN_ACTIVE",
                breakdown=breakdown,
            )

        self._cooldown_until = None
        return self._build_decision(
            timestamp=timestamp,
            level="NORMAL",
            new_entries_allowed=True,
            exits_allowed=True,
            broker_actions_blocked=False,
            manual_reset_required=False,
            cooldown_until=None,
            reason="NO_TRIGGER",
            breakdown=breakdown,
        )

    def _build_decision(
        self,
        *,
        timestamp: datetime,
        level: str,
        new_entries_allowed: bool,
        exits_allowed: bool,
        broker_actions_blocked: bool,
        manual_reset_required: bool,
        cooldown_until: datetime | None,
        reason: str,
        breakdown: KillSwitchBreakdown,
    ) -> KillSwitchDecision:
        return KillSwitchDecision(
            timestamp=timestamp,
            level=level,
            new_entries_allowed=new_entries_allowed,
            exits_allowed=exits_allowed,
            broker_actions_blocked=broker_actions_blocked,
            manual_reset_required=manual_reset_required,
            cooldown_until=cooldown_until,
            reason=reason,
            breakdown=breakdown,
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "hard_kill_latched": self._hard_kill_latched,
            },
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "KillSwitchBreakdown",
    "KillSwitchComputationError",
    "KillSwitchConfig",
    "KillSwitchDecision",
    "KillSwitchEngine",
    "KillSwitchError",
    "KillSwitchInputError",
    "KillSwitchSnapshot",
    "KillSwitchState",
]
