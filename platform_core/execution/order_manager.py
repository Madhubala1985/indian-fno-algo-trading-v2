# FILE: platform_core/execution/order_manager.py
"""
platform_core.execution.order_manager
═══════════════════════════════════════════════════════════════════════
Production-grade asynchronous order manager for institutional Indian index
futures and options execution workflows.

The manager is intentionally exchange-agnostic at the control layer and expects
an injected broker adapter compatible with the Zerodha integration facade or any
future broker module exposing the same async methods.

Key properties:
- Deterministic order lifecycle state machine.
- Idempotent client-order identifiers for retry-safe workflows.
- Replay-safe event journal with JSON-serializable snapshots.
- Async-safe in-flight tracking and per-order locking.
- Pre-trade validation hooks for risk and orchestration layers.
- Separation between submission, acknowledgement, fill updates, cancellation,
  and reconciliation state transitions.

This module does not embed strategy logic, exchange calendars, or market-data
analytics. It strictly manages order intent, lifecycle transitions, and broker
interaction boundaries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Protocol


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class OrderManagerError(RuntimeError):
    """Base exception for order-manager failures."""


class OrderValidationError(OrderManagerError):
    """Raised when an order request violates deterministic constraints."""


class OrderStateError(OrderManagerError):
    """Raised when an invalid state transition is attempted."""


class OrderBrokerError(OrderManagerError):
    """Raised when broker interaction fails."""


class ManagedOrderState(str, Enum):
    """Explicit order lifecycle states for replay-safe transitions."""

    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OrderManagerConfig:
    """Configuration for deterministic order management."""

    max_tag_length: int = 20
    allow_duplicate_broker_order_ids: bool = False
    max_history_events_per_order: int = 512
    reject_zero_or_negative_prices: bool = True
    default_validity: str = "DAY"

    def __post_init__(self) -> None:
        if self.max_tag_length <= 0:
            raise ValueError("max_tag_length must be positive")
        if self.max_history_events_per_order <= 0:
            raise ValueError("max_history_events_per_order must be positive")
        if not self.default_validity.strip():
            raise ValueError("default_validity must be non-empty")


@dataclass(frozen=True, slots=True)
class ManagedOrderRequest:
    """Normalized order intent entering the manager."""

    strategy_id: str
    underlying_symbol: str
    exchange: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    product: str
    variety: str = "regular"
    validity: str = "DAY"
    price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    tag: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        string_fields = {
            "strategy_id": self.strategy_id,
            "underlying_symbol": self.underlying_symbol,
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
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price is not None and self.price <= 0.0:
            raise ValueError("price must be positive when provided")
        if self.trigger_price is not None and self.trigger_price <= 0.0:
            raise ValueError("trigger_price must be positive when provided")
        if self.disclosed_quantity is not None and self.disclosed_quantity <= 0:
            raise ValueError("disclosed_quantity must be positive when provided")
        if self.tag is not None and not self.tag.strip():
            raise ValueError("tag must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strategy_id": self.strategy_id,
            "underlying_symbol": self.underlying_symbol,
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "product": self.product,
            "variety": self.variety,
            "validity": self.validity,
            "price": self.price,
            "trigger_price": self.trigger_price,
            "disclosed_quantity": self.disclosed_quantity,
            "tag": self.tag,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ManagedOrderEvent:
    """Serializable order lifecycle journal event."""

    timestamp: datetime
    client_order_id: str
    event_type: str
    state: ManagedOrderState
    payload: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "client_order_id": self.client_order_id,
            "event_type": self.event_type,
            "state": self.state.value,
            "payload": self.payload,
        }


@dataclass(slots=True)
class ManagedOrderRecord:
    """Mutable in-memory order record with deterministic serialization."""

    client_order_id: str
    created_at: datetime
    request: ManagedOrderRequest
    state: ManagedOrderState = ManagedOrderState.CREATED
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_price: float | None = None
    rejection_reason: str | None = None
    history: list[ManagedOrderEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "created_at": _ensure_utc(self.created_at).isoformat(),
            "request": self.request.to_dict(),
            "state": self.state.value,
            "broker_order_id": self.broker_order_id,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "rejection_reason": self.rejection_reason,
            "history": [event.to_dict() for event in self.history],
        }


class BrokerOrderResponse(Protocol):
    """Minimal broker response protocol accepted by the manager."""

    order_id: str
    status: str
    raw_response: dict[str, JsonValue]


class BrokerIntegrationProtocol(Protocol):
    """Required broker adapter interface for the order manager."""

    async def place_order(self, *, request: Any) -> BrokerOrderResponse:
        ...

    async def modify_order(self, *, request: Any) -> BrokerOrderResponse:
        ...

    async def cancel_order(self, *, order_id: str, variety: str = "regular") -> BrokerOrderResponse:
        ...

    async def get_orders(self) -> list[dict[str, JsonValue]]:
        ...


class OrderManager:
    """Asynchronous deterministic order manager."""

    def __init__(
        self,
        *,
        broker: BrokerIntegrationProtocol,
        config: OrderManagerConfig | None = None,
        broker_order_request_factory: Any | None = None,
        broker_modify_request_factory: Any | None = None,
    ) -> None:
        self._broker = broker
        self._config = config or OrderManagerConfig()
        self._broker_order_request_factory = broker_order_request_factory
        self._broker_modify_request_factory = broker_modify_request_factory
        self._orders: dict[str, ManagedOrderRecord] = {}
        self._broker_order_index: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._journal: list[ManagedOrderEvent] = []
        self._global_lock = asyncio.Lock()

    async def submit_order(self, *, request: ManagedOrderRequest) -> ManagedOrderRecord:
        self._validate_request(request)
        client_order_id = self._compute_client_order_id(request)
        async with self._global_lock:
            existing = self._orders.get(client_order_id)
            if existing is not None:
                return self._copy_record(existing)
            record = ManagedOrderRecord(
                client_order_id=client_order_id,
                created_at=_utc_now(),
                request=request,
            )
            self._orders[client_order_id] = record
            self._locks[client_order_id] = asyncio.Lock()
            self._append_event(
                record,
                event_type="ORDER_CREATED",
                state=ManagedOrderState.CREATED,
                payload={"request": request.to_dict()},
            )

        async with self._locks[client_order_id]:
            record = self._orders[client_order_id]
            if record.state is not ManagedOrderState.CREATED:
                return self._copy_record(record)
            self._transition(record, ManagedOrderState.SUBMITTING, "ORDER_SUBMITTING", {})
            try:
                broker_request = self._build_broker_order_request(record.request)
                broker_response = await self._broker.place_order(request=broker_request)
            except Exception as exc:
                self._fail_record(record, reason=f"broker placement failed: {exc}")
                raise OrderBrokerError(f"broker placement failed for {client_order_id}: {exc}") from exc
            self._acknowledge_record(record, broker_order_id=broker_response.order_id, broker_status=broker_response.status, raw_response=broker_response.raw_response)
            return self._copy_record(record)

    async def cancel_order(self, *, client_order_id: str) -> ManagedOrderRecord:
        record = await self._require_record(client_order_id)
        async with self._locks[client_order_id]:
            record = self._orders[client_order_id]
            if record.state in {ManagedOrderState.CANCELLED, ManagedOrderState.FILLED, ManagedOrderState.REJECTED, ManagedOrderState.FAILED}:
                return self._copy_record(record)
            if record.broker_order_id is None:
                raise OrderStateError("cannot cancel an order without broker_order_id")
            self._transition(record, ManagedOrderState.CANCEL_PENDING, "ORDER_CANCEL_PENDING", {})
            try:
                broker_response = await self._broker.cancel_order(order_id=record.broker_order_id, variety=record.request.variety)
            except Exception as exc:
                self._append_event(
                    record,
                    event_type="ORDER_CANCEL_FAILED",
                    state=record.state,
                    payload={"reason": str(exc)},
                )
                raise OrderBrokerError(f"broker cancellation failed for {client_order_id}: {exc}") from exc
            self._transition(
                record,
                ManagedOrderState.CANCELLED,
                "ORDER_CANCELLED",
                {
                    "broker_order_id": broker_response.order_id,
                    "broker_status": broker_response.status,
                    "raw_response": broker_response.raw_response,
                },
            )
            return self._copy_record(record)

    async def modify_order(
        self,
        *,
        client_order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str | None = None,
    ) -> ManagedOrderRecord:
        record = await self._require_record(client_order_id)
        async with self._locks[client_order_id]:
            record = self._orders[client_order_id]
            if record.state not in {ManagedOrderState.ACKNOWLEDGED, ManagedOrderState.PARTIALLY_FILLED}:
                raise OrderStateError(f"order {client_order_id} cannot be modified from state {record.state.value}")
            if record.broker_order_id is None:
                raise OrderStateError("cannot modify an order without broker_order_id")
            payload: dict[str, JsonValue] = {
                "quantity": quantity,
                "price": price,
                "trigger_price": trigger_price,
                "validity": validity,
            }
            self._append_event(record, event_type="ORDER_MODIFICATION_REQUESTED", state=record.state, payload=payload)
            try:
                broker_request = self._build_broker_modify_request(
                    broker_order_id=record.broker_order_id,
                    variety=record.request.variety,
                    quantity=quantity,
                    price=price,
                    trigger_price=trigger_price,
                    validity=validity,
                )
                broker_response = await self._broker.modify_order(request=broker_request)
            except Exception as exc:
                self._append_event(
                    record,
                    event_type="ORDER_MODIFICATION_FAILED",
                    state=record.state,
                    payload={"reason": str(exc)},
                )
                raise OrderBrokerError(f"broker modification failed for {client_order_id}: {exc}") from exc
            self._append_event(
                record,
                event_type="ORDER_MODIFIED",
                state=record.state,
                payload={
                    "broker_order_id": broker_response.order_id,
                    "broker_status": broker_response.status,
                    "raw_response": broker_response.raw_response,
                },
            )
            return self._copy_record(record)

    async def apply_broker_update(self, *, payload: dict[str, JsonValue]) -> ManagedOrderRecord:
        broker_order_id = str(payload.get("order_id", "")).strip()
        if not broker_order_id:
            raise OrderValidationError("broker update payload missing order_id")
        client_order_id = self._broker_order_index.get(broker_order_id)
        if client_order_id is None:
            raise OrderStateError(f"unknown broker order_id: {broker_order_id}")
        async with self._locks[client_order_id]:
            record = self._orders[client_order_id]
            status = str(payload.get("status", "")).strip().upper()
            filled_quantity = _safe_int(payload.get("filled_quantity"), default=record.filled_quantity)
            average_price = _safe_float(payload.get("average_price"), default=record.average_price)
            state = self._map_broker_status(status=status, filled_quantity=filled_quantity, total_quantity=record.request.quantity)
            record.filled_quantity = filled_quantity
            record.average_price = average_price
            if state == ManagedOrderState.REJECTED:
                record.rejection_reason = str(payload.get("status_message", "REJECTED"))
            if state != record.state:
                self._transition(record, state, "BROKER_STATUS_UPDATE", payload)
            else:
                self._append_event(record, event_type="BROKER_STATUS_SNAPSHOT", state=record.state, payload=payload)
            return self._copy_record(record)

    async def reconcile(self) -> list[ManagedOrderRecord]:
        broker_orders = await self._broker.get_orders()
        updated_records: list[ManagedOrderRecord] = []
        for broker_payload in broker_orders:
            broker_order_id = str(broker_payload.get("order_id", "")).strip()
            if not broker_order_id:
                continue
            if broker_order_id not in self._broker_order_index:
                continue
            updated_records.append(await self.apply_broker_update(payload=broker_payload))
        return updated_records

    async def get_order(self, *, client_order_id: str) -> ManagedOrderRecord:
        record = await self._require_record(client_order_id)
        return self._copy_record(record)

    async def list_orders(self) -> list[ManagedOrderRecord]:
        async with self._global_lock:
            return [self._copy_record(record) for record in self._orders.values()]

    async def export_journal(self) -> list[dict[str, JsonValue]]:
        async with self._global_lock:
            return [event.to_dict() for event in self._journal]

    async def snapshot(self) -> dict[str, JsonValue]:
        async with self._global_lock:
            return {
                "orders": {client_order_id: record.to_dict() for client_order_id, record in self._orders.items()},
                "broker_order_index": dict(self._broker_order_index),
                "journal": [event.to_dict() for event in self._journal],
            }

    async def _require_record(self, client_order_id: str) -> ManagedOrderRecord:
        async with self._global_lock:
            record = self._orders.get(client_order_id)
            if record is None:
                raise OrderStateError(f"unknown client_order_id: {client_order_id}")
            return record

    def _validate_request(self, request: ManagedOrderRequest) -> None:
        if request.tag is not None and len(request.tag) > self._config.max_tag_length:
            raise OrderValidationError("tag length exceeds configured limit")
        if self._config.reject_zero_or_negative_prices:
            if request.price is not None and request.price <= 0.0:
                raise OrderValidationError("price must be positive")
            if request.trigger_price is not None and request.trigger_price <= 0.0:
                raise OrderValidationError("trigger_price must be positive")

    def _compute_client_order_id(self, request: ManagedOrderRequest) -> str:
        normalized = json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"ord_{digest[:24]}"

    def _append_event(
        self,
        record: ManagedOrderRecord,
        *,
        event_type: str,
        state: ManagedOrderState,
        payload: dict[str, JsonValue],
    ) -> None:
        event = ManagedOrderEvent(
            timestamp=_utc_now(),
            client_order_id=record.client_order_id,
            event_type=event_type,
            state=state,
            payload=payload,
        )
        record.history.append(event)
        if len(record.history) > self._config.max_history_events_per_order:
            record.history.pop(0)
        self._journal.append(event)

    def _transition(
        self,
        record: ManagedOrderRecord,
        new_state: ManagedOrderState,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> None:
        allowed = {
            ManagedOrderState.CREATED: {ManagedOrderState.SUBMITTING, ManagedOrderState.FAILED},
            ManagedOrderState.SUBMITTING: {ManagedOrderState.ACKNOWLEDGED, ManagedOrderState.REJECTED, ManagedOrderState.FAILED},
            ManagedOrderState.ACKNOWLEDGED: {ManagedOrderState.PARTIALLY_FILLED, ManagedOrderState.FILLED, ManagedOrderState.CANCEL_PENDING, ManagedOrderState.REJECTED},
            ManagedOrderState.PARTIALLY_FILLED: {ManagedOrderState.FILLED, ManagedOrderState.CANCEL_PENDING, ManagedOrderState.REJECTED},
            ManagedOrderState.CANCEL_PENDING: {ManagedOrderState.CANCELLED, ManagedOrderState.PARTIALLY_FILLED, ManagedOrderState.FILLED, ManagedOrderState.REJECTED},
            ManagedOrderState.CANCELLED: set(),
            ManagedOrderState.FILLED: set(),
            ManagedOrderState.REJECTED: set(),
            ManagedOrderState.FAILED: set(),
        }
        if new_state not in allowed[record.state]:
            raise OrderStateError(
                f"invalid transition for {record.client_order_id}: {record.state.value} -> {new_state.value}"
            )
        record.state = new_state
        self._append_event(record, event_type=event_type, state=new_state, payload=payload)

    def _acknowledge_record(
        self,
        record: ManagedOrderRecord,
        *,
        broker_order_id: str,
        broker_status: str,
        raw_response: dict[str, JsonValue],
    ) -> None:
        broker_order_id = broker_order_id.strip()
        if not broker_order_id:
            self._fail_record(record, reason="empty broker_order_id in acknowledgement")
            raise OrderBrokerError("broker acknowledgement missing order_id")
        existing_client_order_id = self._broker_order_index.get(broker_order_id)
        if existing_client_order_id is not None and existing_client_order_id != record.client_order_id:
            if not self._config.allow_duplicate_broker_order_ids:
                self._fail_record(record, reason="duplicate broker_order_id")
                raise OrderBrokerError(f"duplicate broker_order_id detected: {broker_order_id}")
        record.broker_order_id = broker_order_id
        self._broker_order_index[broker_order_id] = record.client_order_id
        self._transition(
            record,
            ManagedOrderState.ACKNOWLEDGED,
            "ORDER_ACKNOWLEDGED",
            {
                "broker_order_id": broker_order_id,
                "broker_status": broker_status,
                "raw_response": raw_response,
            },
        )

    def _fail_record(self, record: ManagedOrderRecord, *, reason: str) -> None:
        if record.state is ManagedOrderState.FAILED:
            self._append_event(record, event_type="ORDER_FAILURE_RECORDED", state=record.state, payload={"reason": reason})
            return
        if record.state not in {ManagedOrderState.CREATED, ManagedOrderState.SUBMITTING}:
            raise OrderStateError(f"cannot fail order from state {record.state.value}")
        self._transition(record, ManagedOrderState.FAILED, "ORDER_FAILED", {"reason": reason})

    def _build_broker_order_request(self, request: ManagedOrderRequest) -> Any:
        if self._broker_order_request_factory is None:
            return _DefaultBrokerOrderRequest(
                exchange=request.exchange,
                tradingsymbol=request.tradingsymbol,
                transaction_type=request.transaction_type,
                quantity=request.quantity,
                order_type=request.order_type,
                product=request.product,
                variety=request.variety,
                validity=request.validity or self._config.default_validity,
                price=request.price,
                trigger_price=request.trigger_price,
                disclosed_quantity=request.disclosed_quantity,
                tag=request.tag,
            )
        return self._broker_order_request_factory(
            exchange=request.exchange,
            tradingsymbol=request.tradingsymbol,
            transaction_type=request.transaction_type,
            quantity=request.quantity,
            order_type=request.order_type,
            product=request.product,
            variety=request.variety,
            validity=request.validity or self._config.default_validity,
            price=request.price,
            trigger_price=request.trigger_price,
            disclosed_quantity=request.disclosed_quantity,
            tag=request.tag,
        )

    def _build_broker_modify_request(
        self,
        *,
        broker_order_id: str,
        variety: str,
        quantity: int | None,
        price: float | None,
        trigger_price: float | None,
        validity: str | None,
    ) -> Any:
        if self._broker_modify_request_factory is None:
            return _DefaultBrokerModifyRequest(
                order_id=broker_order_id,
                variety=variety,
                quantity=quantity,
                price=price,
                trigger_price=trigger_price,
                validity=validity,
            )
        return self._broker_modify_request_factory(
            order_id=broker_order_id,
            variety=variety,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
        )

    def _map_broker_status(
        self,
        *,
        status: str,
        filled_quantity: int,
        total_quantity: int,
    ) -> ManagedOrderState:
        if status in {"REJECTED", "CANCELLED", "COMPLETE", "OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "MODIFY VALIDATION PENDING", "MODIFY PENDING", "CANCEL PENDING"}:
            if status == "REJECTED":
                return ManagedOrderState.REJECTED
            if status == "CANCELLED":
                return ManagedOrderState.CANCELLED
            if status == "COMPLETE":
                return ManagedOrderState.FILLED
            if status == "CANCEL PENDING":
                return ManagedOrderState.CANCEL_PENDING
            if filled_quantity > 0 and filled_quantity < total_quantity:
                return ManagedOrderState.PARTIALLY_FILLED
            return ManagedOrderState.ACKNOWLEDGED
        if filled_quantity >= total_quantity and total_quantity > 0:
            return ManagedOrderState.FILLED
        if filled_quantity > 0:
            return ManagedOrderState.PARTIALLY_FILLED
        return ManagedOrderState.ACKNOWLEDGED

    def _copy_record(self, record: ManagedOrderRecord) -> ManagedOrderRecord:
        return ManagedOrderRecord(
            client_order_id=record.client_order_id,
            created_at=record.created_at,
            request=record.request,
            state=record.state,
            broker_order_id=record.broker_order_id,
            filled_quantity=record.filled_quantity,
            average_price=record.average_price,
            rejection_reason=record.rejection_reason,
            history=list(record.history),
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
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class _DefaultBrokerModifyRequest:
    order_id: str
    variety: str
    quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    validity: str | None = None



def _safe_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default



def _safe_float(value: Any, *, default: float | None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0.0 else default



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "BrokerIntegrationProtocol",
    "BrokerOrderResponse",
    "ManagedOrderEvent",
    "ManagedOrderRecord",
    "ManagedOrderRequest",
    "ManagedOrderState",
    "OrderBrokerError",
    "OrderManager",
    "OrderManagerConfig",
    "OrderManagerError",
    "OrderStateError",
    "OrderValidationError",
]
