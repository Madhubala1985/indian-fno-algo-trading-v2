# FILE: platform_core/execution/failsafe_exits.py
"""
platform_core.execution.failsafe_exits
═══════════════════════════════════════════════════════════════════════
Production-grade fail-safe exit controller for institutional execution
workflows specialized for Indian index futures and options markets.

The controller transforms execution/risk emergency signals into deterministic
cancel-and-flatten actions while preserving no-hindsight, replay safety, and
strict separation from strategy logic.

Core behavior:
- Trigger-aware emergency exit planning.
- Cancel-pending-first workflow before flattening.
- Remaining-quantity aware flatten plan generation.
- Deterministic state transitions and JSON-serializable audit records.
- Async-first orchestration support for broker-integrated cancellation and exit.
- Safety-first handling of ambiguous states through escalation.

This module does not perform reconciliation or risk analytics internally. It
expects upstream components to provide the current order/position state and a
broker adapter capable of cancelling orders and placing exit orders.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from math import isfinite
from typing import Any
from typing import Protocol


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class FailSafeExitError(RuntimeError):
    """Base exception for fail-safe exit failures."""


class FailSafeExitValidationError(FailSafeExitError):
    """Raised when fail-safe exit inputs are invalid."""


class FailSafeExitStateError(FailSafeExitError):
    """Raised when a fail-safe exit state transition is invalid."""


class FailSafeExitBrokerError(FailSafeExitError):
    """Raised when broker interaction fails during emergency exit."""


class FailSafeTrigger(str, Enum):
    """Explicit fail-safe triggers."""

    RISK_KILL_SWITCH = "RISK_KILL_SWITCH"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    STALE_PARTIAL_FILL = "STALE_PARTIAL_FILL"
    ORDER_REJECTION_CASCADE = "ORDER_REJECTION_CASCADE"
    ORCHESTRATION_SHUTDOWN = "ORCHESTRATION_SHUTDOWN"
    MANUAL_EMERGENCY_STOP = "MANUAL_EMERGENCY_STOP"


class FailSafeActionStatus(str, Enum):
    """Deterministic lifecycle states for fail-safe actions."""

    PLANNED = "PLANNED"
    CANCEL_IN_PROGRESS = "CANCEL_IN_PROGRESS"
    CANCELLED_PENDING_ORDERS = "CANCELLED_PENDING_ORDERS"
    EXIT_IN_PROGRESS = "EXIT_IN_PROGRESS"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    EXIT_COMPLETED = "EXIT_COMPLETED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class FailSafeExitConfig:
    """Configuration for deterministic fail-safe exits."""

    use_market_orders_for_emergency_exit: bool = True
    max_cancel_errors_before_escalation: int = 1
    allow_partial_cancel_failures: bool = False
    flatten_only_if_position_nonzero: bool = True

    def __post_init__(self) -> None:
        if self.max_cancel_errors_before_escalation < 0:
            raise ValueError("max_cancel_errors_before_escalation must be >= 0")


@dataclass(frozen=True, slots=True)
class OpenOrderState:
    """Serializable pending/open order state."""

    client_order_id: str
    broker_order_id: str
    tradingsymbol: str
    variety: str
    quantity: int
    filled_quantity: int
    transaction_type: str
    state: str

    def __post_init__(self) -> None:
        string_fields = {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "tradingsymbol": self.tradingsymbol,
            "variety": self.variety,
            "transaction_type": self.transaction_type,
            "state": self.state,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be between 0 and quantity")

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "tradingsymbol": self.tradingsymbol,
            "variety": self.variety,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "transaction_type": self.transaction_type,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class PositionState:
    """Serializable position state requiring potential flattening."""

    underlying_symbol: str
    tradingsymbol: str
    net_quantity: int
    exchange: str
    product: str
    last_reference_price: float | None = None

    def __post_init__(self) -> None:
        string_fields = {
            "underlying_symbol": self.underlying_symbol,
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "product": self.product,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.last_reference_price is not None and (self.last_reference_price <= 0.0 or not isfinite(self.last_reference_price)):
            raise ValueError("last_reference_price must be positive and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "tradingsymbol": self.tradingsymbol,
            "net_quantity": self.net_quantity,
            "exchange": self.exchange,
            "product": self.product,
            "last_reference_price": self.last_reference_price,
        }


@dataclass(frozen=True, slots=True)
class FailSafeExitRequest:
    """Serializable emergency exit request."""

    request_id: str
    trigger: FailSafeTrigger
    timestamp: datetime
    position: PositionState
    open_orders: tuple[OpenOrderState, ...]
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "trigger": self.trigger.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "position": self.position.to_dict(),
            "open_orders": [order.to_dict() for order in self.open_orders],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ExitOrderPlan:
    """Serializable flatten-order plan."""

    exchange: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    product: str
    variety: str = "regular"
    validity: str = "DAY"
    price: float | None = None
    tag: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        string_fields = {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "order_type": self.order_type,
            "product": self.product,
            "variety": self.variety,
            "validity": self.validity,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.price is not None and (self.price <= 0.0 or not isfinite(self.price)):
            raise ValueError("price must be positive and finite when provided")
        if self.tag is not None and not self.tag.strip():
            raise ValueError("tag must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "product": self.product,
            "variety": self.variety,
            "validity": self.validity,
            "price": self.price,
            "tag": self.tag,
        }


@dataclass(frozen=True, slots=True)
class FailSafeExitEvent:
    """Serializable audit event for fail-safe execution."""

    timestamp: datetime
    request_id: str
    status: FailSafeActionStatus
    message: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "request_id": self.request_id,
            "status": self.status.value,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class FailSafeExitResult:
    """Mutable fail-safe exit result with deterministic serialization."""

    request: FailSafeExitRequest
    status: FailSafeActionStatus = FailSafeActionStatus.PLANNED
    cancelled_order_ids: list[str] = field(default_factory=list)
    cancel_errors: list[str] = field(default_factory=list)
    exit_order_id: str | None = None
    exit_plan: ExitOrderPlan | None = None
    events: list[FailSafeExitEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "cancelled_order_ids": list(self.cancelled_order_ids),
            "cancel_errors": list(self.cancel_errors),
            "exit_order_id": self.exit_order_id,
            "exit_plan": self.exit_plan.to_dict() if self.exit_plan is not None else None,
            "events": [event.to_dict() for event in self.events],
        }


class FailSafeBrokerProtocol(Protocol):
    """Required broker adapter interface for fail-safe exits."""

    async def cancel_order(self, *, order_id: str, variety: str = "regular") -> Any:
        ...

    async def place_order(self, *, request: Any) -> Any:
        ...


class FailSafeExitController:
    """Deterministic async emergency cancel-and-flatten controller."""

    def __init__(
        self,
        *,
        broker: FailSafeBrokerProtocol,
        config: FailSafeExitConfig | None = None,
        broker_order_request_factory: Any | None = None,
    ) -> None:
        self._broker = broker
        self._config = config or FailSafeExitConfig()
        self._broker_order_request_factory = broker_order_request_factory
        self._results: dict[str, FailSafeExitResult] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._last_timestamp: datetime | None = None

    async def execute(self, *, request: FailSafeExitRequest) -> FailSafeExitResult:
        normalized_timestamp = _ensure_utc(request.timestamp)
        async with self._global_lock:
            self._validate_timestamp(normalized_timestamp)
            existing = self._results.get(request.request_id)
            if existing is not None:
                return self._copy_result(existing)
            result = FailSafeExitResult(request=request)
            self._results[request.request_id] = result
            self._locks[request.request_id] = asyncio.Lock()
            self._append_event(
                result,
                status=FailSafeActionStatus.PLANNED,
                message="fail-safe exit planned",
                metadata={"trigger": request.trigger.value},
            )
            self._last_timestamp = normalized_timestamp

        async with self._locks[request.request_id]:
            result = self._results[request.request_id]
            await self._cancel_open_orders(result=result)
            await self._submit_exit_if_required(result=result)
            return self._copy_result(result)

    async def snapshot(self, *, request_id: str) -> dict[str, JsonValue]:
        result = self._require_result(request_id)
        async with self._locks[request_id]:
            return result.to_dict()

    async def all_snapshots(self) -> list[dict[str, JsonValue]]:
        async with self._global_lock:
            return [result.to_dict() for result in self._results.values()]

    async def _cancel_open_orders(self, *, result: FailSafeExitResult) -> None:
        open_orders = [order for order in result.request.open_orders if order.remaining_quantity > 0]
        if not open_orders:
            self._append_event(
                result,
                status=result.status,
                message="no cancellable open orders present",
                metadata={},
            )
            return
        result.status = FailSafeActionStatus.CANCEL_IN_PROGRESS
        self._append_event(
            result,
            status=result.status,
            message="cancelling open orders before flattening",
            metadata={"open_order_count": len(open_orders)},
        )
        for order in open_orders:
            try:
                await self._broker.cancel_order(order_id=order.broker_order_id, variety=order.variety)
                result.cancelled_order_ids.append(order.broker_order_id)
            except Exception as exc:
                result.cancel_errors.append(f"{order.broker_order_id}:{exc}")
                if len(result.cancel_errors) > self._config.max_cancel_errors_before_escalation:
                    result.status = FailSafeActionStatus.RECONCILIATION_REQUIRED
                    self._append_event(
                        result,
                        status=result.status,
                        message="cancel error threshold exceeded; reconciliation required",
                        metadata={"cancel_errors": list(result.cancel_errors)},
                    )
                    if not self._config.allow_partial_cancel_failures:
                        return
        result.status = FailSafeActionStatus.CANCELLED_PENDING_ORDERS
        self._append_event(
            result,
            status=result.status,
            message="pending orders cancelled or tolerated within threshold",
            metadata={
                "cancelled_order_ids": list(result.cancelled_order_ids),
                "cancel_errors": list(result.cancel_errors),
            },
        )

    async def _submit_exit_if_required(self, *, result: FailSafeExitResult) -> None:
        if result.status is FailSafeActionStatus.RECONCILIATION_REQUIRED:
            return
        net_quantity = result.request.position.net_quantity
        if net_quantity == 0 and self._config.flatten_only_if_position_nonzero:
            result.status = FailSafeActionStatus.EXIT_COMPLETED
            self._append_event(
                result,
                status=result.status,
                message="position already flat; no emergency exit order required",
                metadata={},
            )
            return
        plan = self._build_exit_plan(position=result.request.position)
        result.exit_plan = plan
        result.status = FailSafeActionStatus.EXIT_IN_PROGRESS
        self._append_event(
            result,
            status=result.status,
            message="submitting emergency flatten order",
            metadata={"exit_plan": plan.to_dict()},
        )
        try:
            broker_request = self._build_broker_order_request(plan)
            broker_response = await self._broker.place_order(request=broker_request)
        except Exception as exc:
            result.status = FailSafeActionStatus.FAILED
            self._append_event(
                result,
                status=result.status,
                message="emergency flatten order submission failed",
                metadata={"error": str(exc)},
            )
            raise FailSafeExitBrokerError(f"failed to place emergency exit order: {exc}") from exc
        result.exit_order_id = str(getattr(broker_response, "order_id", "")).strip() or None
        result.status = FailSafeActionStatus.EXIT_SUBMITTED
        self._append_event(
            result,
            status=result.status,
            message="emergency flatten order submitted",
            metadata={
                "exit_order_id": result.exit_order_id,
                "raw_response": _response_to_json_dict(broker_response),
            },
        )

    def mark_exit_completed(self, *, request_id: str, timestamp: datetime, metadata: dict[str, JsonValue] | None = None) -> None:
        result = self._require_result(request_id)
        normalized_timestamp = _ensure_utc(timestamp)
        self._validate_timestamp(normalized_timestamp)
        if result.status not in {FailSafeActionStatus.EXIT_SUBMITTED, FailSafeActionStatus.EXIT_IN_PROGRESS}:
            raise FailSafeExitStateError(f"cannot mark exit completed from status {result.status.value}")
        result.status = FailSafeActionStatus.EXIT_COMPLETED
        self._append_event(
            result,
            status=result.status,
            message="emergency flatten workflow completed",
            metadata=metadata or {},
            timestamp=normalized_timestamp,
        )
        self._last_timestamp = normalized_timestamp

    def _build_exit_plan(self, *, position: PositionState) -> ExitOrderPlan:
        quantity = abs(position.net_quantity)
        if quantity <= 0:
            raise FailSafeExitValidationError("cannot build exit plan for zero quantity")
        transaction_type = "SELL" if position.net_quantity > 0 else "BUY"
        order_type = "MARKET" if self._config.use_market_orders_for_emergency_exit else "LIMIT"
        price = None
        if order_type == "LIMIT":
            if position.last_reference_price is None:
                raise FailSafeExitValidationError("last_reference_price is required for limit-based fail-safe exits")
            price = position.last_reference_price
        return ExitOrderPlan(
            exchange=position.exchange,
            tradingsymbol=position.tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product=position.product,
            price=price,
            tag="failsafe_exit",
        )

    def _build_broker_order_request(self, plan: ExitOrderPlan) -> Any:
        if self._broker_order_request_factory is None:
            return _DefaultBrokerOrderRequest(
                exchange=plan.exchange,
                tradingsymbol=plan.tradingsymbol,
                transaction_type=plan.transaction_type,
                quantity=plan.quantity,
                order_type=plan.order_type,
                product=plan.product,
                variety=plan.variety,
                validity=plan.validity,
                price=plan.price,
                tag=plan.tag,
            )
        return self._broker_order_request_factory(
            exchange=plan.exchange,
            tradingsymbol=plan.tradingsymbol,
            transaction_type=plan.transaction_type,
            quantity=plan.quantity,
            order_type=plan.order_type,
            product=plan.product,
            variety=plan.variety,
            validity=plan.validity,
            price=plan.price,
            tag=plan.tag,
        )

    def _append_event(
        self,
        result: FailSafeExitResult,
        *,
        status: FailSafeActionStatus,
        message: str,
        metadata: dict[str, JsonValue],
        timestamp: datetime | None = None,
    ) -> None:
        event = FailSafeExitEvent(
            timestamp=timestamp or _utc_now(),
            request_id=result.request.request_id,
            status=status,
            message=message,
            metadata=metadata,
        )
        result.events.append(event)

    def _validate_timestamp(self, timestamp: datetime) -> None:
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise FailSafeExitValidationError("timestamps must be globally non-decreasing for replay safety")

    def _require_result(self, request_id: str) -> FailSafeExitResult:
        result = self._results.get(request_id)
        if result is None:
            raise FailSafeExitStateError(f"unknown request_id: {request_id}")
        return result

    def _copy_result(self, result: FailSafeExitResult) -> FailSafeExitResult:
        return FailSafeExitResult(
            request=result.request,
            status=result.status,
            cancelled_order_ids=list(result.cancelled_order_ids),
            cancel_errors=list(result.cancel_errors),
            exit_order_id=result.exit_order_id,
            exit_plan=result.exit_plan,
            events=list(result.events),
        )


@dataclass(frozen=True, slots=True)
class _DefaultBrokerOrderRequest:
    exchange: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    product: str
    variety: str
    validity: str
    price: float | None = None
    tag: str | None = None



def _response_to_json_dict(response: Any) -> dict[str, JsonValue]:
    raw_response = getattr(response, "raw_response", None)
    if isinstance(raw_response, dict):
        return {str(key): _to_json_value(value) for key, value in raw_response.items()}
    return {"repr": str(response)}



def _to_json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_value(item) for item in value]
    return str(value)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "ExitOrderPlan",
    "FailSafeActionStatus",
    "FailSafeBrokerError",
    "FailSafeBrokerProtocol",
    "FailSafeExitConfig",
    "FailSafeExitController",
    "FailSafeExitError",
    "FailSafeExitEvent",
    "FailSafeExitRequest",
    "FailSafeExitResult",
    "FailSafeExitStateError",
    "FailSafeExitValidationError",
    "FailSafeTrigger",
    "OpenOrderState",
    "PositionState",
]
