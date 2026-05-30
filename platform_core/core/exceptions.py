# FILE: platform_core/core/exceptions.py
"""
platform_core.core.exceptions
═══════════════════════════════════════════════════════════════════════
Production-grade exception hierarchy for the institutional derivatives
intelligence and automated execution platform.

Design constraints preserved from validated architecture:
- No business logic.
- Event-driven, async-first, replay-safe foundation.
- Type-safe, serialization-friendly structured context.
- Clean exception taxonomy for routing, triage, and fail-safe actions.
- No local imports; only full package imports.
- Compatible with mypy, ruff, black, pytest, Redis, PostgreSQL, and
  TimescaleDB-backed workflows.

Exception design principles applied:
- Specific exception subclasses over generic failure buckets.
- Stable machine-readable error_code per exception class.
- Optional structured context for logging, event emission, and dead-letter use.
- Retryability and severity flags carried by exception instances.
- Safe conversion to dict payloads for monitoring and event buses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from platform_core.core.constants import UTC
from platform_core.core.enums import EventDomain, KillSwitchReason, RiskLevel, ServiceName


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(slots=True, frozen=True)
class ExceptionContext:
    """
    Structured exception context for machine-readable diagnostics.

    Intended uses:
    - log enrichment,
    - event-bus dead-letter publication,
    - retry classification,
    - alert deduplication,
    - replay incident persistence.
    """

    service: ServiceName | None = None
    domain: EventDomain | None = None
    correlation_id: str | None = None
    event_id: str | None = None
    instrument_id: str | None = None
    underlying: str | None = None
    replay_run_id: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a JSON-compatible dictionary representation."""
        payload: dict[str, JsonValue] = {
            "service": self.service.value if self.service else None,
            "domain": self.domain.value if self.domain else None,
            "correlation_id": self.correlation_id,
            "event_id": self.event_id,
            "instrument_id": self.instrument_id,
            "underlying": self.underlying,
            "replay_run_id": self.replay_run_id,
            "metadata": self.metadata,
            "occurred_at": self.occurred_at.isoformat(),
        }
        return payload


class PlatformError(Exception):
    """
    Base exception for all platform-defined failures.

    Attributes:
        message: Human-readable diagnostic message.
        error_code: Stable machine-readable code for routing and alerting.
        retryable: Indicates whether caller may safely retry.
        severity: Severity ladder for monitoring and escalation.
        context: Optional structured context for correlation and logging.
        cause: Original exception, if any.
    """

    default_error_code = "PLATFORM_ERROR"
    default_retryable = False
    default_severity = RiskLevel.WARNING

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retryable: bool | None = None,
        severity: RiskLevel | None = None,
        context: ExceptionContext | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.default_error_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.severity = severity or self.default_severity
        self.context = context
        self.cause = cause

    def __str__(self) -> str:
        return f"{self.error_code}: {self.message}"

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize the exception into a JSON-compatible payload."""
        payload: dict[str, JsonValue] = {
            "type": self.__class__.__name__,
            "message": self.message,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "severity": self.severity.value,
            "context": self.context.to_dict() if self.context else None,
            "cause_type": self.cause.__class__.__name__ if self.cause else None,
            "cause_message": str(self.cause) if self.cause else None,
        }
        return payload


class ConfigurationError(PlatformError):
    """Configuration initialization or validation failure."""

    default_error_code = "CONFIGURATION_ERROR"
    default_retryable = False
    default_severity = RiskLevel.CRITICAL


class DependencyError(PlatformError):
    """Dependency wiring or service registration failure."""

    default_error_code = "DEPENDENCY_ERROR"
    default_retryable = False
    default_severity = RiskLevel.CRITICAL


class LifecycleError(PlatformError):
    """Service lifecycle transition failure."""

    default_error_code = "LIFECYCLE_ERROR"
    default_retryable = False
    default_severity = RiskLevel.CRITICAL


class EventError(PlatformError):
    """Base exception for event-system failures."""

    default_error_code = "EVENT_ERROR"
    default_retryable = False
    default_severity = RiskLevel.WARNING


class EventValidationError(EventError):
    """Raised when an event payload fails schema or integrity validation."""

    default_error_code = "EVENT_VALIDATION_ERROR"


class EventSerializationError(EventError):
    """Raised when an event cannot be serialized safely."""

    default_error_code = "EVENT_SERIALIZATION_ERROR"


class EventPublicationError(EventError):
    """Raised when an event cannot be published to the bus."""

    default_error_code = "EVENT_PUBLICATION_ERROR"
    default_retryable = True
    default_severity = RiskLevel.BREACH


class DataError(PlatformError):
    """Base exception for market-data and persistence issues."""

    default_error_code = "DATA_ERROR"
    default_retryable = True
    default_severity = RiskLevel.WARNING


class DataValidationError(DataError):
    """Raised when market data violates validation constraints."""

    default_error_code = "DATA_VALIDATION_ERROR"


class DataQualityError(DataError):
    """Raised when data quality deteriorates below acceptable thresholds."""

    default_error_code = "DATA_QUALITY_ERROR"
    default_severity = RiskLevel.BREACH


class StaleDataError(DataError):
    """Raised when the market-data feed exceeds staleness constraints."""

    default_error_code = "STALE_DATA_ERROR"
    default_retryable = True
    default_severity = RiskLevel.BREACH


class PersistenceError(DataError):
    """Base exception for durable-storage failures."""

    default_error_code = "PERSISTENCE_ERROR"
    default_retryable = True
    default_severity = RiskLevel.BREACH


class CacheError(DataError):
    """Raised when Redis or in-memory cache operations fail."""

    default_error_code = "CACHE_ERROR"
    default_retryable = True


class ReplayError(PlatformError):
    """Base exception for replay-engine failures."""

    default_error_code = "REPLAY_ERROR"
    default_retryable = False
    default_severity = RiskLevel.BREACH


class ReplayIntegrityError(ReplayError):
    """Raised when deterministic replay guarantees are violated."""

    default_error_code = "REPLAY_INTEGRITY_ERROR"
    default_severity = RiskLevel.CRITICAL


class ReplayStateError(ReplayError):
    """Raised for illegal replay lifecycle transitions."""

    default_error_code = "REPLAY_STATE_ERROR"


class AnalyticsError(PlatformError):
    """Base exception for analytics and feature-generation failures."""

    default_error_code = "ANALYTICS_ERROR"
    default_retryable = False
    default_severity = RiskLevel.WARNING


class StructureComputationError(AnalyticsError):
    """Raised when market-structure state cannot be computed causally."""

    default_error_code = "STRUCTURE_COMPUTATION_ERROR"


class OptionAnalyticsError(AnalyticsError):
    """Raised when options-intelligence computations fail."""

    default_error_code = "OPTION_ANALYTICS_ERROR"


class RegimeClassificationError(AnalyticsError):
    """Raised when regime state cannot be derived safely."""

    default_error_code = "REGIME_CLASSIFICATION_ERROR"


class RiskError(PlatformError):
    """Base exception for risk-engine failures."""

    default_error_code = "RISK_ERROR"
    default_retryable = False
    default_severity = RiskLevel.BREACH


class RiskLimitBreachError(RiskError):
    """Raised when a hard risk limit is breached."""

    default_error_code = "RISK_LIMIT_BREACH_ERROR"
    default_severity = RiskLevel.CRITICAL


class KillSwitchTriggeredError(RiskError):
    """
    Raised when the platform enters a kill-switch state.

    The reason is modeled explicitly to preserve deterministic routing across
    orchestration, risk, and execution services.
    """

    default_error_code = "KILL_SWITCH_TRIGGERED_ERROR"
    default_severity = RiskLevel.CRITICAL

    def __init__(
        self,
        message: str,
        *,
        reason: KillSwitchReason,
        context: ExceptionContext | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            error_code=self.default_error_code,
            retryable=False,
            severity=self.default_severity,
            context=context,
            cause=cause,
        )
        self.reason = reason

    def to_dict(self) -> dict[str, JsonValue]:
        payload = super().to_dict()
        payload["reason"] = self.reason.value
        return payload


class EmergencyShutdownError(RiskError):
    """Raised when emergency shutdown is triggered."""

    default_error_code = "EMERGENCY_SHUTDOWN_ERROR"
    default_severity = RiskLevel.CRITICAL


class ExecutionError(PlatformError):
    """Base exception for execution and broker integration failures."""

    default_error_code = "EXECUTION_ERROR"
    default_retryable = True
    default_severity = RiskLevel.BREACH


class OrderValidationError(ExecutionError):
    """Raised when an order intent fails validation."""

    default_error_code = "ORDER_VALIDATION_ERROR"
    default_retryable = False


class OrderSubmissionError(ExecutionError):
    """Raised when an order cannot be submitted to the broker."""

    default_error_code = "ORDER_SUBMISSION_ERROR"


class OrderReconciliationError(ExecutionError):
    """Raised when broker/order state cannot be reconciled deterministically."""

    default_error_code = "ORDER_RECONCILIATION_ERROR"
    default_severity = RiskLevel.CRITICAL


class ExchangeRejectStormError(ExecutionError):
    """Raised when repeated exchange rejects require fail-safe escalation."""

    default_error_code = "EXCHANGE_REJECT_STORM_ERROR"
    default_retryable = False
    default_severity = RiskLevel.CRITICAL


class LatencyBreachError(ExecutionError):
    """Raised when execution or transport latency exceeds hard limits."""

    default_error_code = "LATENCY_BREACH_ERROR"
    default_retryable = False
    default_severity = RiskLevel.CRITICAL


def ensure(condition: bool, exc: PlatformError) -> None:
    """
    Raise the provided platform exception if condition is false.

    This utility keeps guard clauses concise while preserving specific,
    typed exceptions and machine-readable context.
    """
    if not condition:
        raise exc


def exception_from_message(
    message: str,
    *,
    error_code: str = "PLATFORM_ERROR",
    severity: RiskLevel = RiskLevel.WARNING,
    retryable: bool = False,
    context: ExceptionContext | None = None,
    cause: BaseException | None = None,
) -> PlatformError:
    """
    Build a generic `PlatformError` with structured metadata.

    Useful in boundaries where external exceptions are normalized into the
    internal exception taxonomy before publication or logging.
    """
    return PlatformError(
        message=message,
        error_code=error_code,
        severity=severity,
        retryable=retryable,
        context=context,
        cause=cause,
    )


def merge_exception_metadata(
    base: Mapping[str, JsonValue] | None,
    extra: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """
    Merge two JSON-compatible metadata mappings.

    Later keys override earlier keys. Returned value is always a new dict.
    """
    merged: dict[str, JsonValue] = {}
    if base:
        merged.update(dict(base))
    if extra:
        merged.update(dict(extra))
    return merged


__all__ = [
    "AnalyticsError",
    "CacheError",
    "ConfigurationError",
    "DataError",
    "DataQualityError",
    "DataValidationError",
    "DependencyError",
    "EmergencyShutdownError",
    "EventError",
    "EventPublicationError",
    "EventSerializationError",
    "EventValidationError",
    "ExceptionContext",
    "ExchangeRejectStormError",
    "ExecutionError",
    "JsonScalar",
    "JsonValue",
    "KillSwitchTriggeredError",
    "LatencyBreachError",
    "LifecycleError",
    "OptionAnalyticsError",
    "OrderReconciliationError",
    "OrderSubmissionError",
    "OrderValidationError",
    "PersistenceError",
    "PlatformError",
    "RegimeClassificationError",
    "ReplayError",
    "ReplayIntegrityError",
    "ReplayStateError",
    "RiskError",
    "RiskLimitBreachError",
    "StaleDataError",
    "StructureComputationError",
    "ensure",
    "exception_from_message",
    "merge_exception_metadata",
]
