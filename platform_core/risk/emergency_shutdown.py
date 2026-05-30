# FILE: platform_core/risk/emergency_shutdown.py
"""
platform_core.risk.emergency_shutdown
═══════════════════════════════════════════════════════════════════════
Production-grade institutional emergency shutdown engine specialized for Indian
index futures and options workflows.

The engine converts finalized kill-switch and runtime-failure conditions into a
fail-safe emergency shutdown plan. It preserves deterministic replay behavior
and emits explicit operational directives for downstream orchestration,
execution, reconciliation, and runtime lifecycle management.

Supported shutdown decisions include:
- Partial containment when new entries must stop but exits remain allowed.
- Full execution freeze with controlled exit-only mode.
- Broker/session isolation during infrastructure or reject crises.
- Latched emergency-stop mode requiring explicit operator intervention.

This module does not perform broker API calls, process termination, or position
liquidation itself. It generates a serializable shutdown directive and
escalation state for downstream executors.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class EmergencyShutdownError(RuntimeError):
    """Base exception for emergency shutdown failures."""


class EmergencyShutdownInputError(EmergencyShutdownError):
    """Raised when emergency shutdown inputs are invalid or causally unsafe."""


class EmergencyShutdownComputationError(EmergencyShutdownError):
    """Raised when emergency shutdown evaluation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class EmergencyShutdownConfig:
    """Configuration for emergency shutdown escalation and latching."""

    shutdown_data_staleness_seconds: float = 10.0
    shutdown_reject_rate_fraction: float = 0.40
    shutdown_slippage_bps: float = 45.0
    shutdown_consecutive_failures: int = 5
    containment_cooldown_minutes: int = 15
    shutdown_cooldown_minutes: int = 120
    require_manual_reset_after_shutdown: bool = True
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        numeric_positive_fields = {
            "shutdown_data_staleness_seconds": self.shutdown_data_staleness_seconds,
            "shutdown_reject_rate_fraction": self.shutdown_reject_rate_fraction,
            "shutdown_slippage_bps": self.shutdown_slippage_bps,
        }
        for field_name, field_value in numeric_positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not 0.0 < self.shutdown_reject_rate_fraction <= 1.0:
            raise ValueError("shutdown_reject_rate_fraction must be between 0 and 1")
        if self.shutdown_consecutive_failures <= 0:
            raise ValueError("shutdown_consecutive_failures must be positive")
        if self.containment_cooldown_minutes <= 0 or self.shutdown_cooldown_minutes <= 0:
            raise ValueError("cooldown durations must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class EmergencyRuntimeSnapshot:
    """Serializable finalized snapshot for shutdown control evaluation."""

    timestamp: datetime
    kill_switch_level: str
    kill_switch_manual_reset_required: bool
    kill_switch_broker_actions_blocked: bool
    new_entries_allowed: bool
    exits_allowed: bool
    data_staleness_seconds: float
    reject_rate_fraction: float
    adjusted_slippage_bps: float
    consecutive_execution_failures: int
    execution_monitor_healthy: bool
    broker_connectivity_healthy: bool
    event_bus_healthy: bool
    reconciliation_healthy: bool
    manual_shutdown_requested: bool
    manual_resume_requested: bool

    def __post_init__(self) -> None:
        if not self.kill_switch_level.strip():
            raise ValueError("kill_switch_level must be non-empty")
        numeric_values = {
            "data_staleness_seconds": self.data_staleness_seconds,
            "reject_rate_fraction": self.reject_rate_fraction,
            "adjusted_slippage_bps": self.adjusted_slippage_bps,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.data_staleness_seconds < 0.0:
            raise ValueError("data_staleness_seconds must be non-negative")
        if not 0.0 <= self.reject_rate_fraction <= 1.0:
            raise ValueError("reject_rate_fraction must be between 0 and 1")
        if self.adjusted_slippage_bps < 0.0:
            raise ValueError("adjusted_slippage_bps must be non-negative")
        if self.consecutive_execution_failures < 0:
            raise ValueError("consecutive_execution_failures must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "kill_switch_level": self.kill_switch_level,
            "kill_switch_manual_reset_required": self.kill_switch_manual_reset_required,
            "kill_switch_broker_actions_blocked": self.kill_switch_broker_actions_blocked,
            "new_entries_allowed": self.new_entries_allowed,
            "exits_allowed": self.exits_allowed,
            "data_staleness_seconds": self.data_staleness_seconds,
            "reject_rate_fraction": self.reject_rate_fraction,
            "adjusted_slippage_bps": self.adjusted_slippage_bps,
            "consecutive_execution_failures": self.consecutive_execution_failures,
            "execution_monitor_healthy": self.execution_monitor_healthy,
            "broker_connectivity_healthy": self.broker_connectivity_healthy,
            "event_bus_healthy": self.event_bus_healthy,
            "reconciliation_healthy": self.reconciliation_healthy,
            "manual_shutdown_requested": self.manual_shutdown_requested,
            "manual_resume_requested": self.manual_resume_requested,
        }


@dataclass(frozen=True, slots=True)
class EmergencyShutdownBreakdown:
    """Interpretable decomposition of shutdown triggers."""

    manual_shutdown_triggered: bool
    kill_switch_hard_triggered: bool
    broker_isolation_triggered: bool
    data_failure_triggered: bool
    reject_failure_triggered: bool
    slippage_failure_triggered: bool
    execution_failure_triggered: bool
    monitor_failure_triggered: bool
    broker_connectivity_failure_triggered: bool
    event_bus_failure_triggered: bool
    reconciliation_failure_triggered: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "manual_shutdown_triggered": self.manual_shutdown_triggered,
            "kill_switch_hard_triggered": self.kill_switch_hard_triggered,
            "broker_isolation_triggered": self.broker_isolation_triggered,
            "data_failure_triggered": self.data_failure_triggered,
            "reject_failure_triggered": self.reject_failure_triggered,
            "slippage_failure_triggered": self.slippage_failure_triggered,
            "execution_failure_triggered": self.execution_failure_triggered,
            "monitor_failure_triggered": self.monitor_failure_triggered,
            "broker_connectivity_failure_triggered": self.broker_connectivity_failure_triggered,
            "event_bus_failure_triggered": self.event_bus_failure_triggered,
            "reconciliation_failure_triggered": self.reconciliation_failure_triggered,
        }


@dataclass(frozen=True, slots=True)
class EmergencyShutdownDirective:
    """Serializable downstream shutdown directive."""

    timestamp: datetime
    level: str
    halt_new_entries: bool
    allow_exit_orders: bool
    block_broker_submissions: bool
    isolate_runtime: bool
    trigger_reconciliation: bool
    require_manual_reset: bool
    cooldown_until: datetime | None
    reason: str
    breakdown: EmergencyShutdownBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "level": self.level,
            "halt_new_entries": self.halt_new_entries,
            "allow_exit_orders": self.allow_exit_orders,
            "block_broker_submissions": self.block_broker_submissions,
            "isolate_runtime": self.isolate_runtime,
            "trigger_reconciliation": self.trigger_reconciliation,
            "require_manual_reset": self.require_manual_reset,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class EmergencyShutdownState:
    """Serializable engine state for replay persistence."""

    processed_snapshots: int
    last_timestamp: datetime | None
    last_level: str | None
    cooldown_until: datetime | None
    shutdown_latched: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_snapshots": self.processed_snapshots,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_level": self.last_level,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "shutdown_latched": self.shutdown_latched,
        }


class EmergencyShutdownEngine:
    """Institutional-grade deterministic emergency shutdown engine."""

    def __init__(self, *, config: EmergencyShutdownConfig | None = None) -> None:
        self._config = config or EmergencyShutdownConfig()
        self._processed_snapshots = 0
        self._last_timestamp: datetime | None = None
        self._last_level: str | None = None
        self._cooldown_until: datetime | None = None
        self._shutdown_latched = False
        self._last_directive: EmergencyShutdownDirective | None = None

    @property
    def state(self) -> EmergencyShutdownState:
        return EmergencyShutdownState(
            processed_snapshots=self._processed_snapshots,
            last_timestamp=self._last_timestamp,
            last_level=self._last_level,
            cooldown_until=self._cooldown_until,
            shutdown_latched=self._shutdown_latched,
        )

    def reset(self) -> None:
        self._processed_snapshots = 0
        self._last_timestamp = None
        self._last_level = None
        self._cooldown_until = None
        self._shutdown_latched = False
        self._last_directive = None

    def evaluate(
        self,
        *,
        snapshot: EmergencyRuntimeSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> EmergencyShutdownDirective:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        if snapshot.manual_resume_requested and not self._shutdown_latched:
            self._cooldown_until = None

        breakdown = EmergencyShutdownBreakdown(
            manual_shutdown_triggered=snapshot.manual_shutdown_requested,
            kill_switch_hard_triggered=snapshot.kill_switch_level.upper() == "HARD_KILL",
            broker_isolation_triggered=snapshot.kill_switch_broker_actions_blocked,
            data_failure_triggered=snapshot.data_staleness_seconds >= self._config.shutdown_data_staleness_seconds,
            reject_failure_triggered=snapshot.reject_rate_fraction >= self._config.shutdown_reject_rate_fraction,
            slippage_failure_triggered=snapshot.adjusted_slippage_bps >= self._config.shutdown_slippage_bps,
            execution_failure_triggered=snapshot.consecutive_execution_failures >= self._config.shutdown_consecutive_failures,
            monitor_failure_triggered=not snapshot.execution_monitor_healthy,
            broker_connectivity_failure_triggered=not snapshot.broker_connectivity_healthy,
            event_bus_failure_triggered=not snapshot.event_bus_healthy,
            reconciliation_failure_triggered=not snapshot.reconciliation_healthy,
        )
        directive = self._decide(timestamp=normalized_timestamp, snapshot=snapshot, breakdown=breakdown)
        self._processed_snapshots += 1
        self._last_timestamp = normalized_timestamp
        self._last_level = directive.level
        self._last_directive = directive
        return directive

    def get_last_directive(self) -> EmergencyShutdownDirective:
        if self._last_directive is None:
            raise EmergencyShutdownComputationError("no emergency shutdown directive is available")
        return self._last_directive

    def _validate_snapshot(self, *, snapshot: EmergencyRuntimeSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise EmergencyShutdownInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise EmergencyShutdownInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise EmergencyShutdownInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _decide(
        self,
        *,
        timestamp: datetime,
        snapshot: EmergencyRuntimeSnapshot,
        breakdown: EmergencyShutdownBreakdown,
    ) -> EmergencyShutdownDirective:
        if breakdown.manual_shutdown_triggered:
            self._shutdown_latched = True
            self._cooldown_until = None
            return self._build_directive(
                timestamp=timestamp,
                level="EMERGENCY_SHUTDOWN",
                halt_new_entries=True,
                allow_exit_orders=False,
                block_broker_submissions=True,
                isolate_runtime=True,
                trigger_reconciliation=True,
                require_manual_reset=True,
                cooldown_until=None,
                reason="MANUAL_SHUTDOWN_REQUESTED",
                breakdown=breakdown,
            )

        if self._shutdown_latched:
            if snapshot.manual_resume_requested and not self._config.require_manual_reset_after_shutdown:
                self._shutdown_latched = False
            else:
                return self._build_directive(
                    timestamp=timestamp,
                    level="EMERGENCY_SHUTDOWN",
                    halt_new_entries=True,
                    allow_exit_orders=False,
                    block_broker_submissions=True,
                    isolate_runtime=True,
                    trigger_reconciliation=True,
                    require_manual_reset=True,
                    cooldown_until=None,
                    reason="SHUTDOWN_LATCHED",
                    breakdown=breakdown,
                )

        hard_failures = (
            breakdown.kill_switch_hard_triggered
            or breakdown.data_failure_triggered
            or breakdown.reject_failure_triggered
            or breakdown.slippage_failure_triggered
            or breakdown.execution_failure_triggered
            or breakdown.monitor_failure_triggered
            or breakdown.broker_connectivity_failure_triggered
            or breakdown.event_bus_failure_triggered
        )
        if hard_failures:
            self._shutdown_latched = self._config.require_manual_reset_after_shutdown
            self._cooldown_until = timestamp + timedelta(minutes=self._config.shutdown_cooldown_minutes)
            return self._build_directive(
                timestamp=timestamp,
                level="EMERGENCY_SHUTDOWN",
                halt_new_entries=True,
                allow_exit_orders=False,
                block_broker_submissions=True,
                isolate_runtime=True,
                trigger_reconciliation=True,
                require_manual_reset=self._config.require_manual_reset_after_shutdown,
                cooldown_until=self._cooldown_until,
                reason="HARD_RUNTIME_FAILURE",
                breakdown=breakdown,
            )

        containment_required = (
            breakdown.broker_isolation_triggered
            or breakdown.reconciliation_failure_triggered
            or not snapshot.new_entries_allowed
            or not snapshot.exits_allowed
        )
        if containment_required:
            self._cooldown_until = timestamp + timedelta(minutes=self._config.containment_cooldown_minutes)
            return self._build_directive(
                timestamp=timestamp,
                level="CONTAINMENT",
                halt_new_entries=True,
                allow_exit_orders=snapshot.exits_allowed,
                block_broker_submissions=breakdown.broker_isolation_triggered,
                isolate_runtime=False,
                trigger_reconciliation=True,
                require_manual_reset=False,
                cooldown_until=self._cooldown_until,
                reason="CONTAINMENT_REQUIRED",
                breakdown=breakdown,
            )

        if self._cooldown_until is not None and timestamp < self._cooldown_until:
            return self._build_directive(
                timestamp=timestamp,
                level="COOLDOWN",
                halt_new_entries=True,
                allow_exit_orders=True,
                block_broker_submissions=False,
                isolate_runtime=False,
                trigger_reconciliation=False,
                require_manual_reset=False,
                cooldown_until=self._cooldown_until,
                reason="COOLDOWN_ACTIVE",
                breakdown=breakdown,
            )

        self._cooldown_until = None
        return self._build_directive(
            timestamp=timestamp,
            level="NORMAL",
            halt_new_entries=False,
            allow_exit_orders=True,
            block_broker_submissions=False,
            isolate_runtime=False,
            trigger_reconciliation=False,
            require_manual_reset=False,
            cooldown_until=None,
            reason="NO_SHUTDOWN_TRIGGER",
            breakdown=breakdown,
        )

    def _build_directive(
        self,
        *,
        timestamp: datetime,
        level: str,
        halt_new_entries: bool,
        allow_exit_orders: bool,
        block_broker_submissions: bool,
        isolate_runtime: bool,
        trigger_reconciliation: bool,
        require_manual_reset: bool,
        cooldown_until: datetime | None,
        reason: str,
        breakdown: EmergencyShutdownBreakdown,
    ) -> EmergencyShutdownDirective:
        return EmergencyShutdownDirective(
            timestamp=timestamp,
            level=level,
            halt_new_entries=halt_new_entries,
            allow_exit_orders=allow_exit_orders,
            block_broker_submissions=block_broker_submissions,
            isolate_runtime=isolate_runtime,
            trigger_reconciliation=trigger_reconciliation,
            require_manual_reset=require_manual_reset,
            cooldown_until=cooldown_until,
            reason=reason,
            breakdown=breakdown,
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "shutdown_latched": self._shutdown_latched,
            },
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "EmergencyRuntimeSnapshot",
    "EmergencyShutdownBreakdown",
    "EmergencyShutdownComputationError",
    "EmergencyShutdownConfig",
    "EmergencyShutdownDirective",
    "EmergencyShutdownEngine",
    "EmergencyShutdownError",
    "EmergencyShutdownInputError",
    "EmergencyShutdownState",
]
