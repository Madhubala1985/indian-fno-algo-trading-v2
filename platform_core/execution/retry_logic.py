# FILE: platform_core/execution/retry_logic.py
"""
platform_core.execution.retry_logic
═══════════════════════════════════════════════════════════════════════
Production-grade asynchronous retry controller for institutional execution
workflows in Indian index futures and options markets.

This module manages retry eligibility, deterministic backoff scheduling, and
idempotent retry decisions for broker-facing execution operations. It is
specifically designed to sit between the execution monitor, order manager, and
broker integration layer without introducing duplicate-order risk or hidden
workflow redesign.

Core guarantees:
- Retry decisions are deterministic and replay-safe.
- Only explicitly retryable failures are retried.
- Idempotency keys remain stable across attempts.
- Backoff schedules are bounded and JSON-serializable.
- Retry state is causal and uses non-decreasing timestamps.
- Safety-first defaults prefer blocking over ambiguous re-submission.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RetryLogicError(RuntimeError):
    """Base exception for retry logic failures."""


class RetryValidationError(RetryLogicError):
    """Raised when retry input is invalid or causally unsafe."""


class RetryStateError(RetryLogicError):
    """Raised when retry state transitions violate deterministic rules."""


class RetryFailureClass(str, Enum):
    """Explicit failure classes used for retry eligibility."""

    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    NETWORK_DISCONNECT = "NETWORK_DISCONNECT"
    RATE_LIMIT = "RATE_LIMIT"
    BROKER_5XX = "BROKER_5XX"
    TEMPORARY_REJECTION = "TEMPORARY_REJECTION"
    DUPLICATE_ORDER_RISK = "DUPLICATE_ORDER_RISK"
    VALIDATION_REJECTION = "VALIDATION_REJECTION"
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    UNKNOWN = "UNKNOWN"


class RetryDecisionAction(str, Enum):
    """Deterministic retry decisions."""

    RETRY = "RETRY"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    REQUIRE_RECONCILIATION = "REQUIRE_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Configurable bounded retry policy."""

    max_attempts: int = 3
    base_backoff_seconds: float = 0.50
    exponential_multiplier: float = 2.0
    max_backoff_seconds: float = 5.0
    retryable_failures: tuple[RetryFailureClass, ...] = (
        RetryFailureClass.NETWORK_TIMEOUT,
        RetryFailureClass.NETWORK_DISCONNECT,
        RetryFailureClass.RATE_LIMIT,
        RetryFailureClass.BROKER_5XX,
        RetryFailureClass.TEMPORARY_REJECTION,
    )
    reconciliation_failures: tuple[RetryFailureClass, ...] = (
        RetryFailureClass.DUPLICATE_ORDER_RISK,
        RetryFailureClass.UNKNOWN,
    )

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        numeric_fields = {
            "base_backoff_seconds": self.base_backoff_seconds,
            "exponential_multiplier": self.exponential_multiplier,
            "max_backoff_seconds": self.max_backoff_seconds,
        }
        for field_name, field_value in numeric_fields.items():
            if not isfinite(field_value) or field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive and finite")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")


@dataclass(frozen=True, slots=True)
class RetryIntent:
    """Serializable retry identity and invariant order context."""

    client_order_id: str
    idempotency_key: str
    operation_name: str
    created_timestamp: datetime
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id must be non-empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if not self.operation_name.strip():
            raise ValueError("operation_name must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "idempotency_key": self.idempotency_key,
            "operation_name": self.operation_name,
            "created_timestamp": _ensure_utc(self.created_timestamp).isoformat(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RetryObservation:
    """Execution failure or outcome observation that drives retry decisions."""

    timestamp: datetime
    client_order_id: str
    attempt_number: int
    success: bool
    failure_class: RetryFailureClass | None = None
    error_code: str | None = None
    error_message: str | None = None
    broker_order_id: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id must be non-empty")
        if self.attempt_number <= 0:
            raise ValueError("attempt_number must be positive")
        if self.success and self.failure_class is not None:
            raise ValueError("failure_class must be None on success observations")
        if not self.success and self.failure_class is None:
            raise ValueError("failure_class is required on failed observations")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("error_code must be non-empty when provided")
        if self.error_message is not None and not self.error_message.strip():
            raise ValueError("error_message must be non-empty when provided")
        if self.broker_order_id is not None and not self.broker_order_id.strip():
            raise ValueError("broker_order_id must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "client_order_id": self.client_order_id,
            "attempt_number": self.attempt_number,
            "success": self.success,
            "failure_class": self.failure_class.value if self.failure_class is not None else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "broker_order_id": self.broker_order_id,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Serializable retry decision."""

    timestamp: datetime
    client_order_id: str
    action: RetryDecisionAction
    attempt_number: int
    next_attempt_number: int | None
    scheduled_retry_timestamp: datetime | None
    backoff_seconds: float | None
    idempotency_key: str
    reason: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "client_order_id": self.client_order_id,
            "action": self.action.value,
            "attempt_number": self.attempt_number,
            "next_attempt_number": self.next_attempt_number,
            "scheduled_retry_timestamp": _optional_iso(self.scheduled_retry_timestamp),
            "backoff_seconds": self.backoff_seconds,
            "idempotency_key": self.idempotency_key,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class RetryState:
    """Mutable causal retry state."""

    intent: RetryIntent
    last_attempt_number: int = 0
    last_observation_timestamp: datetime | None = None
    terminal: bool = False
    reconciliation_required: bool = False
    decisions: list[RetryDecision] = field(default_factory=list)
    observations: list[RetryObservation] = field(default_factory=list)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "intent": self.intent.to_dict(),
            "last_attempt_number": self.last_attempt_number,
            "last_observation_timestamp": _optional_iso(self.last_observation_timestamp),
            "terminal": self.terminal,
            "reconciliation_required": self.reconciliation_required,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "observations": [observation.to_dict() for observation in self.observations],
        }


class RetryController:
    """Deterministic asynchronous retry controller."""

    def __init__(self, *, policy: RetryPolicy | None = None) -> None:
        self._policy = policy or RetryPolicy()
        self._states: dict[str, RetryState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._last_timestamp: datetime | None = None

    async def register_intent(self, *, intent: RetryIntent) -> None:
        timestamp = _ensure_utc(intent.created_timestamp)
        async with self._global_lock:
            self._validate_global_timestamp(timestamp)
            if intent.client_order_id in self._states:
                raise RetryValidationError(f"duplicate client_order_id: {intent.client_order_id}")
            self._states[intent.client_order_id] = RetryState(intent=intent)
            self._locks[intent.client_order_id] = asyncio.Lock()
            self._last_timestamp = timestamp

    async def evaluate(self, *, observation: RetryObservation) -> RetryDecision:
        state = self._require_state(observation.client_order_id)
        async with self._locks[observation.client_order_id]:
            self._validate_observation(state=state, observation=observation)
            normalized_timestamp = _ensure_utc(observation.timestamp)
            state.last_attempt_number = observation.attempt_number
            state.last_observation_timestamp = normalized_timestamp
            state.observations.append(observation)
            decision = self._build_decision(state=state, observation=observation)
            state.decisions.append(decision)
            if decision.action is not RetryDecisionAction.RETRY:
                state.terminal = True
            if decision.action is RetryDecisionAction.REQUIRE_RECONCILIATION:
                state.reconciliation_required = True
            self._last_timestamp = normalized_timestamp
            return decision

    async def next_retry_delay(self, *, client_order_id: str) -> float | None:
        state = self._require_state(client_order_id)
        async with self._locks[client_order_id]:
            if not state.decisions:
                return None
            last_decision = state.decisions[-1]
            return last_decision.backoff_seconds

    async def snapshot(self, *, client_order_id: str) -> dict[str, JsonValue]:
        state = self._require_state(client_order_id)
        async with self._locks[client_order_id]:
            return state.to_dict()

    async def all_snapshots(self) -> list[dict[str, JsonValue]]:
        async with self._global_lock:
            return [state.to_dict() for state in self._states.values()]

    def _require_state(self, client_order_id: str) -> RetryState:
        state = self._states.get(client_order_id)
        if state is None:
            raise RetryStateError(f"unknown client_order_id: {client_order_id}")
        return state

    def _validate_observation(self, *, state: RetryState, observation: RetryObservation) -> None:
        timestamp = _ensure_utc(observation.timestamp)
        self._validate_global_timestamp(timestamp)
        if state.terminal:
            raise RetryStateError("cannot evaluate observation for terminal retry state")
        if state.last_observation_timestamp is not None and timestamp < state.last_observation_timestamp:
            raise RetryValidationError("observation timestamp must be non-decreasing per order")
        expected_attempt = state.last_attempt_number + 1
        if observation.attempt_number != expected_attempt:
            raise RetryValidationError(
                f"attempt_number must increment sequentially; expected {expected_attempt}, got {observation.attempt_number}"
            )

    def _build_decision(self, *, state: RetryState, observation: RetryObservation) -> RetryDecision:
        timestamp = _ensure_utc(observation.timestamp)
        if observation.success:
            return RetryDecision(
                timestamp=timestamp,
                client_order_id=observation.client_order_id,
                action=RetryDecisionAction.DO_NOT_RETRY,
                attempt_number=observation.attempt_number,
                next_attempt_number=None,
                scheduled_retry_timestamp=None,
                backoff_seconds=None,
                idempotency_key=state.intent.idempotency_key,
                reason="SUCCESS_OBSERVED",
                metadata={"broker_order_id": observation.broker_order_id},
            )

        failure_class = observation.failure_class
        if failure_class is None:
            raise RetryValidationError("failure_class is required for unsuccessful observations")
        if failure_class in self._policy.reconciliation_failures:
            return RetryDecision(
                timestamp=timestamp,
                client_order_id=observation.client_order_id,
                action=RetryDecisionAction.REQUIRE_RECONCILIATION,
                attempt_number=observation.attempt_number,
                next_attempt_number=None,
                scheduled_retry_timestamp=None,
                backoff_seconds=None,
                idempotency_key=state.intent.idempotency_key,
                reason=f"RECONCILIATION_REQUIRED_{failure_class.value}",
                metadata={
                    "error_code": observation.error_code,
                    "error_message": observation.error_message,
                    "failure_class": failure_class.value,
                },
            )
        if failure_class not in self._policy.retryable_failures:
            return RetryDecision(
                timestamp=timestamp,
                client_order_id=observation.client_order_id,
                action=RetryDecisionAction.DO_NOT_RETRY,
                attempt_number=observation.attempt_number,
                next_attempt_number=None,
                scheduled_retry_timestamp=None,
                backoff_seconds=None,
                idempotency_key=state.intent.idempotency_key,
                reason=f"NON_RETRYABLE_{failure_class.value}",
                metadata={
                    "error_code": observation.error_code,
                    "error_message": observation.error_message,
                    "failure_class": failure_class.value,
                },
            )
        if observation.attempt_number >= self._policy.max_attempts:
            return RetryDecision(
                timestamp=timestamp,
                client_order_id=observation.client_order_id,
                action=RetryDecisionAction.DO_NOT_RETRY,
                attempt_number=observation.attempt_number,
                next_attempt_number=None,
                scheduled_retry_timestamp=None,
                backoff_seconds=None,
                idempotency_key=state.intent.idempotency_key,
                reason="MAX_ATTEMPTS_EXHAUSTED",
                metadata={
                    "max_attempts": self._policy.max_attempts,
                    "failure_class": failure_class.value,
                },
            )

        backoff_seconds = self._compute_backoff_seconds(observation.attempt_number)
        scheduled_retry_timestamp = timestamp + timedelta(seconds=backoff_seconds)
        return RetryDecision(
            timestamp=timestamp,
            client_order_id=observation.client_order_id,
            action=RetryDecisionAction.RETRY,
            attempt_number=observation.attempt_number,
            next_attempt_number=observation.attempt_number + 1,
            scheduled_retry_timestamp=scheduled_retry_timestamp,
            backoff_seconds=backoff_seconds,
            idempotency_key=state.intent.idempotency_key,
            reason=f"RETRYABLE_{failure_class.value}",
            metadata={
                "failure_class": failure_class.value,
                "error_code": observation.error_code,
                "error_message": observation.error_message,
            },
        )

    def _compute_backoff_seconds(self, attempt_number: int) -> float:
        raw_backoff = self._policy.base_backoff_seconds * (self._policy.exponential_multiplier ** (attempt_number - 1))
        return min(self._policy.max_backoff_seconds, raw_backoff)

    def _validate_global_timestamp(self, timestamp: datetime) -> None:
        normalized_timestamp = _ensure_utc(timestamp)
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise RetryValidationError("timestamps must be globally non-decreasing for replay safety")



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()


__all__ = [
    "RetryController",
    "RetryDecision",
    "RetryDecisionAction",
    "RetryFailureClass",
    "RetryIntent",
    "RetryLogicError",
    "RetryObservation",
    "RetryPolicy",
    "RetryState",
    "RetryStateError",
    "RetryValidationError",
]
