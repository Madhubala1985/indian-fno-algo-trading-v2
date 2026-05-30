# FILE: platform_core/execution/execution_monitor.py
"""
platform_core.execution.execution_monitor
═══════════════════════════════════════════════════════════════════════
Production-grade asynchronous execution monitor for institutional Indian index
futures and options workflows.

The monitor consumes causal execution lifecycle observations and computes
real-time diagnostics for:
- Submission latency
- Acknowledgement latency
- Fill latency
- Partial-fill progress
- Slippage versus intended price
- Order ageing and stale execution detection
- Rejections, failures, and timeout alerts
- JSON-serializable monitoring snapshots for replay and persistence

This module is designed to sit between the order manager, broker integration,
and risk / orchestration layers. It preserves no-hindsight behaviour by using
only observed lifecycle timestamps and confirmed broker updates.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ExecutionMonitorError(RuntimeError):
    """Base exception for execution-monitor failures."""


class ExecutionMonitorInputError(ExecutionMonitorError):
    """Raised when execution-monitor input is invalid."""


class ExecutionMonitorStateError(ExecutionMonitorError):
    """Raised when execution lifecycle sequencing is invalid."""


@dataclass(frozen=True, slots=True)
class ExecutionMonitorConfig:
    """Deterministic thresholds for execution monitoring."""

    acknowledgement_timeout_seconds: float = 3.0
    fill_timeout_seconds: float = 10.0
    stale_partial_fill_timeout_seconds: float = 15.0
    max_allowed_slippage_fraction: float = 0.003
    max_allowed_market_slippage_fraction: float = 0.0075
    severe_latency_threshold_seconds: float = 2.0

    def __post_init__(self) -> None:
        numeric_fields = {
            "acknowledgement_timeout_seconds": self.acknowledgement_timeout_seconds,
            "fill_timeout_seconds": self.fill_timeout_seconds,
            "stale_partial_fill_timeout_seconds": self.stale_partial_fill_timeout_seconds,
            "max_allowed_slippage_fraction": self.max_allowed_slippage_fraction,
            "max_allowed_market_slippage_fraction": self.max_allowed_market_slippage_fraction,
            "severe_latency_threshold_seconds": self.severe_latency_threshold_seconds,
        }
        for field_name, field_value in numeric_fields.items():
            if not isfinite(field_value) or field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive and finite")


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Serializable intended execution specification."""

    client_order_id: str
    underlying_symbol: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    intended_price: float | None
    created_timestamp: datetime

    def __post_init__(self) -> None:
        string_fields = {
            "client_order_id": self.client_order_id,
            "underlying_symbol": self.underlying_symbol,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "order_type": self.order_type,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.intended_price is not None and (self.intended_price <= 0.0 or not isfinite(self.intended_price)):
            raise ValueError("intended_price must be positive and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "underlying_symbol": self.underlying_symbol,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "intended_price": self.intended_price,
            "created_timestamp": _ensure_utc(self.created_timestamp).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """Serializable broker/order-manager observation."""

    timestamp: datetime
    client_order_id: str
    event_type: str
    broker_order_id: str | None = None
    state: str | None = None
    filled_quantity: int | None = None
    average_price: float | None = None
    rejection_reason: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.client_order_id.strip() or not self.event_type.strip():
            raise ValueError("client_order_id and event_type must be non-empty")
        if self.broker_order_id is not None and not self.broker_order_id.strip():
            raise ValueError("broker_order_id must be non-empty when provided")
        if self.state is not None and not self.state.strip():
            raise ValueError("state must be non-empty when provided")
        if self.filled_quantity is not None and self.filled_quantity < 0:
            raise ValueError("filled_quantity cannot be negative")
        if self.average_price is not None and (self.average_price < 0.0 or not isfinite(self.average_price)):
            raise ValueError("average_price must be non-negative and finite when provided")
        if self.rejection_reason is not None and not self.rejection_reason.strip():
            raise ValueError("rejection_reason must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "client_order_id": self.client_order_id,
            "event_type": self.event_type,
            "broker_order_id": self.broker_order_id,
            "state": self.state,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "rejection_reason": self.rejection_reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ExecutionAlert:
    """Serializable execution alert."""

    timestamp: datetime
    client_order_id: str
    severity: str
    code: str
    message: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "client_order_id": self.client_order_id,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class ExecutionMetrics:
    """Mutable execution metrics for one client order."""

    client_order_id: str
    broker_order_id: str | None = None
    state: str = "CREATED"
    submission_timestamp: datetime | None = None
    acknowledgement_timestamp: datetime | None = None
    first_fill_timestamp: datetime | None = None
    final_fill_timestamp: datetime | None = None
    last_update_timestamp: datetime | None = None
    filled_quantity: int = 0
    average_fill_price: float | None = None
    submission_latency_seconds: float | None = None
    acknowledgement_latency_seconds: float | None = None
    first_fill_latency_seconds: float | None = None
    final_fill_latency_seconds: float | None = None
    slippage_fraction: float | None = None
    alerts: list[ExecutionAlert] = field(default_factory=list)
    observations: list[ExecutionObservation] = field(default_factory=list)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "state": self.state,
            "submission_timestamp": _optional_iso(self.submission_timestamp),
            "acknowledgement_timestamp": _optional_iso(self.acknowledgement_timestamp),
            "first_fill_timestamp": _optional_iso(self.first_fill_timestamp),
            "final_fill_timestamp": _optional_iso(self.final_fill_timestamp),
            "last_update_timestamp": _optional_iso(self.last_update_timestamp),
            "filled_quantity": self.filled_quantity,
            "average_fill_price": self.average_fill_price,
            "submission_latency_seconds": self.submission_latency_seconds,
            "acknowledgement_latency_seconds": self.acknowledgement_latency_seconds,
            "first_fill_latency_seconds": self.first_fill_latency_seconds,
            "final_fill_latency_seconds": self.final_fill_latency_seconds,
            "slippage_fraction": self.slippage_fraction,
            "alerts": [alert.to_dict() for alert in self.alerts],
            "observations": [observation.to_dict() for observation in self.observations],
        }


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Replay-safe monitoring snapshot for one order."""

    as_of_timestamp: datetime
    intent: ExecutionIntent
    metrics: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "as_of_timestamp": _ensure_utc(self.as_of_timestamp).isoformat(),
            "intent": self.intent.to_dict(),
            "metrics": self.metrics,
        }


class ExecutionMonitor:
    """Asynchronous execution monitor with deterministic lifecycle analytics."""

    def __init__(self, *, config: ExecutionMonitorConfig | None = None) -> None:
        self._config = config or ExecutionMonitorConfig()
        self._intents: dict[str, ExecutionIntent] = {}
        self._metrics: dict[str, ExecutionMetrics] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._last_timestamp: datetime | None = None

    async def register_intent(self, *, intent: ExecutionIntent) -> None:
        timestamp = _ensure_utc(intent.created_timestamp)
        async with self._global_lock:
            self._validate_monotonic_timestamp(timestamp)
            if intent.client_order_id in self._intents:
                raise ExecutionMonitorInputError(f"duplicate client_order_id: {intent.client_order_id}")
            self._intents[intent.client_order_id] = intent
            self._metrics[intent.client_order_id] = ExecutionMetrics(client_order_id=intent.client_order_id)
            self._locks[intent.client_order_id] = asyncio.Lock()
            self._last_timestamp = timestamp

    async def process_observation(self, *, observation: ExecutionObservation) -> ExecutionSnapshot:
        client_order_id = observation.client_order_id
        intent = self._require_intent(client_order_id)
        timestamp = _ensure_utc(observation.timestamp)
        async with self._locks[client_order_id]:
            self._validate_monotonic_timestamp(timestamp)
            metrics = self._metrics[client_order_id]
            self._apply_observation(intent=intent, metrics=metrics, observation=observation)
            self._evaluate_alerts(intent=intent, metrics=metrics, observation_timestamp=timestamp)
            self._last_timestamp = timestamp
            return ExecutionSnapshot(as_of_timestamp=timestamp, intent=intent, metrics=metrics.to_dict())

    async def evaluate_timeouts(self, *, as_of_timestamp: datetime) -> list[ExecutionAlert]:
        timestamp = _ensure_utc(as_of_timestamp)
        async with self._global_lock:
            self._validate_monotonic_timestamp(timestamp)
            alerts: list[ExecutionAlert] = []
            for client_order_id, intent in self._intents.items():
                metrics = self._metrics[client_order_id]
                self._evaluate_alerts(intent=intent, metrics=metrics, observation_timestamp=timestamp)
                alerts.extend(metrics.alerts[-2:])
            self._last_timestamp = timestamp
            return alerts

    async def snapshot(self, *, client_order_id: str) -> ExecutionSnapshot:
        intent = self._require_intent(client_order_id)
        async with self._locks[client_order_id]:
            metrics = self._metrics[client_order_id]
            return ExecutionSnapshot(as_of_timestamp=_utc_now(), intent=intent, metrics=metrics.to_dict())

    async def all_snapshots(self) -> list[ExecutionSnapshot]:
        snapshots: list[ExecutionSnapshot] = []
        async with self._global_lock:
            for client_order_id, intent in self._intents.items():
                snapshots.append(
                    ExecutionSnapshot(
                        as_of_timestamp=_utc_now(),
                        intent=intent,
                        metrics=self._metrics[client_order_id].to_dict(),
                    )
                )
        return snapshots

    def _require_intent(self, client_order_id: str) -> ExecutionIntent:
        intent = self._intents.get(client_order_id)
        if intent is None:
            raise ExecutionMonitorStateError(f"unknown client_order_id: {client_order_id}")
        return intent

    def _apply_observation(
        self,
        *,
        intent: ExecutionIntent,
        metrics: ExecutionMetrics,
        observation: ExecutionObservation,
    ) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        if metrics.last_update_timestamp is not None and normalized_timestamp < metrics.last_update_timestamp:
            raise ExecutionMonitorStateError("observation timestamp must be non-decreasing per order")
        metrics.last_update_timestamp = normalized_timestamp
        metrics.observations.append(observation)
        event_type = observation.event_type.strip().upper()
        if observation.broker_order_id is not None:
            metrics.broker_order_id = observation.broker_order_id
        if observation.state is not None:
            metrics.state = observation.state.upper()

        if event_type == "ORDER_SUBMITTING":
            if metrics.submission_timestamp is None:
                metrics.submission_timestamp = normalized_timestamp
                metrics.submission_latency_seconds = max(
                    0.0,
                    (normalized_timestamp - _ensure_utc(intent.created_timestamp)).total_seconds(),
                )
        elif event_type in {"ORDER_ACKNOWLEDGED", "BROKER_ACKNOWLEDGED"}:
            if metrics.submission_timestamp is None:
                raise ExecutionMonitorStateError("acknowledgement observed before submission")
            if metrics.acknowledgement_timestamp is None:
                metrics.acknowledgement_timestamp = normalized_timestamp
                metrics.acknowledgement_latency_seconds = max(
                    0.0,
                    (normalized_timestamp - metrics.submission_timestamp).total_seconds(),
                )
        elif event_type in {"BROKER_STATUS_UPDATE", "ORDER_PARTIAL_FILL", "ORDER_FILLED"}:
            self._apply_fill_or_status(intent=intent, metrics=metrics, observation=observation)
        elif event_type in {"ORDER_REJECTED", "ORDER_FAILED", "ORDER_CANCELLED"}:
            metrics.state = event_type.replace("ORDER_", "")
        else:
            if observation.filled_quantity is not None or observation.average_price is not None:
                self._apply_fill_or_status(intent=intent, metrics=metrics, observation=observation)

    def _apply_fill_or_status(
        self,
        *,
        intent: ExecutionIntent,
        metrics: ExecutionMetrics,
        observation: ExecutionObservation,
    ) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        filled_quantity = observation.filled_quantity if observation.filled_quantity is not None else metrics.filled_quantity
        if filled_quantity < metrics.filled_quantity:
            raise ExecutionMonitorStateError("filled_quantity cannot decrease")
        metrics.filled_quantity = filled_quantity
        if observation.average_price is not None:
            metrics.average_fill_price = observation.average_price
        if filled_quantity > 0 and metrics.first_fill_timestamp is None:
            metrics.first_fill_timestamp = normalized_timestamp
            metrics.first_fill_latency_seconds = self._latency_from_submission(metrics, normalized_timestamp)
        if filled_quantity >= intent.quantity:
            metrics.final_fill_timestamp = normalized_timestamp
            metrics.final_fill_latency_seconds = self._latency_from_submission(metrics, normalized_timestamp)
            metrics.state = "FILLED"
        elif filled_quantity > 0:
            metrics.state = "PARTIALLY_FILLED"
        elif observation.state is not None:
            metrics.state = observation.state.upper()
        metrics.slippage_fraction = self._compute_slippage_fraction(intent=intent, average_fill_price=metrics.average_fill_price)

    def _latency_from_submission(self, metrics: ExecutionMetrics, timestamp: datetime) -> float | None:
        if metrics.submission_timestamp is None:
            return None
        return max(0.0, (timestamp - metrics.submission_timestamp).total_seconds())

    def _compute_slippage_fraction(self, *, intent: ExecutionIntent, average_fill_price: float | None) -> float | None:
        if intent.intended_price is None or average_fill_price is None:
            return None
        if intent.intended_price <= 0.0:
            raise ExecutionMonitorInputError("intended_price must be positive for slippage computation")
        direction = 1.0 if intent.transaction_type.strip().upper() == "BUY" else -1.0
        raw_slippage = direction * (average_fill_price - intent.intended_price) / intent.intended_price
        return raw_slippage

    def _evaluate_alerts(
        self,
        *,
        intent: ExecutionIntent,
        metrics: ExecutionMetrics,
        observation_timestamp: datetime,
    ) -> None:
        active_alert_codes = {alert.code for alert in metrics.alerts}
        if metrics.submission_timestamp is not None and metrics.acknowledgement_timestamp is None:
            seconds_waiting = (observation_timestamp - metrics.submission_timestamp).total_seconds()
            if seconds_waiting >= self._config.acknowledgement_timeout_seconds and "ACK_TIMEOUT" not in active_alert_codes:
                metrics.alerts.append(
                    self._build_alert(
                        client_order_id=intent.client_order_id,
                        severity="HIGH",
                        code="ACK_TIMEOUT",
                        message="broker acknowledgement timeout exceeded",
                        metadata={"seconds_waiting": seconds_waiting},
                        timestamp=observation_timestamp,
                    )
                )
        if metrics.first_fill_timestamp is None and metrics.submission_timestamp is not None and metrics.state not in {"REJECTED", "FAILED", "CANCELLED"}:
            seconds_since_submission = (observation_timestamp - metrics.submission_timestamp).total_seconds()
            if seconds_since_submission >= self._config.fill_timeout_seconds and "FILL_TIMEOUT" not in active_alert_codes:
                metrics.alerts.append(
                    self._build_alert(
                        client_order_id=intent.client_order_id,
                        severity="MEDIUM",
                        code="FILL_TIMEOUT",
                        message="fill timeout exceeded without any fill",
                        metadata={"seconds_since_submission": seconds_since_submission},
                        timestamp=observation_timestamp,
                    )
                )
        if 0 < metrics.filled_quantity < intent.quantity and metrics.last_update_timestamp is not None:
            stale_seconds = (observation_timestamp - metrics.last_update_timestamp).total_seconds()
            if stale_seconds >= self._config.stale_partial_fill_timeout_seconds and "STALE_PARTIAL_FILL" not in active_alert_codes:
                metrics.alerts.append(
                    self._build_alert(
                        client_order_id=intent.client_order_id,
                        severity="MEDIUM",
                        code="STALE_PARTIAL_FILL",
                        message="partial fill has become stale",
                        metadata={"stale_seconds": stale_seconds},
                        timestamp=observation_timestamp,
                    )
                )
        if metrics.acknowledgement_latency_seconds is not None and metrics.acknowledgement_latency_seconds >= self._config.severe_latency_threshold_seconds:
            if "SEVERE_ACK_LATENCY" not in active_alert_codes:
                metrics.alerts.append(
                    self._build_alert(
                        client_order_id=intent.client_order_id,
                        severity="MEDIUM",
                        code="SEVERE_ACK_LATENCY",
                        message="acknowledgement latency exceeds severe threshold",
                        metadata={"acknowledgement_latency_seconds": metrics.acknowledgement_latency_seconds},
                        timestamp=observation_timestamp,
                    )
                )
        if metrics.slippage_fraction is not None:
            slippage_limit = (
                self._config.max_allowed_market_slippage_fraction
                if intent.order_type.strip().upper() == "MARKET"
                else self._config.max_allowed_slippage_fraction
            )
            if metrics.slippage_fraction > slippage_limit and "ADVERSE_SLIPPAGE" not in active_alert_codes:
                metrics.alerts.append(
                    self._build_alert(
                        client_order_id=intent.client_order_id,
                        severity="HIGH",
                        code="ADVERSE_SLIPPAGE",
                        message="realized slippage exceeds configured threshold",
                        metadata={
                            "slippage_fraction": metrics.slippage_fraction,
                            "slippage_limit": slippage_limit,
                        },
                        timestamp=observation_timestamp,
                    )
                )
        if metrics.state in {"REJECTED", "FAILED"} and "TERMINAL_EXECUTION_FAILURE" not in active_alert_codes:
            metrics.alerts.append(
                self._build_alert(
                    client_order_id=intent.client_order_id,
                    severity="HIGH",
                    code="TERMINAL_EXECUTION_FAILURE",
                    message=f"order entered terminal failure state: {metrics.state}",
                    metadata={"state": metrics.state},
                    timestamp=observation_timestamp,
                )
            )

    def _build_alert(
        self,
        *,
        client_order_id: str,
        severity: str,
        code: str,
        message: str,
        metadata: dict[str, JsonValue],
        timestamp: datetime,
    ) -> ExecutionAlert:
        return ExecutionAlert(
            timestamp=timestamp,
            client_order_id=client_order_id,
            severity=severity,
            code=code,
            message=message,
            metadata=metadata,
        )

    def _validate_monotonic_timestamp(self, timestamp: datetime) -> None:
        normalized_timestamp = _ensure_utc(timestamp)
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise ExecutionMonitorInputError("timestamps must be globally non-decreasing for replay safety")



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "ExecutionAlert",
    "ExecutionIntent",
    "ExecutionMetrics",
    "ExecutionMonitor",
    "ExecutionMonitorConfig",
    "ExecutionMonitorError",
    "ExecutionMonitorInputError",
    "ExecutionMonitorStateError",
    "ExecutionObservation",
    "ExecutionSnapshot",
]
