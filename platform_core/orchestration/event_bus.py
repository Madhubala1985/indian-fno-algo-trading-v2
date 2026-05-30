# FILE: platform_core/orchestration/event_bus.py
"""
platform_core.orchestration.event_bus
═══════════════════════════════════════════════════════════════════════
Production-grade in-process async event bus for the institutional Indian index
futures and options derivatives platform.

The implementation provides a deterministic event envelope, async publish/
subscribe routing, per-subscription filtering, bounded audit retention,
replay-safe ordering, and explicit delivery outcomes while preserving service
isolation and future compatibility with external brokers.

Scope:
- In-process async event distribution.
- Deterministic event envelope generation.
- Ordered publish semantics within a single runtime instance.
- Replay-safe audit capture and subscription metadata snapshots.
- Filtered fan-out to multiple subscribers.
- Failure-aware delivery reporting without hidden retries.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from inspect import isawaitable
from typing import Any
from typing import Awaitable
from typing import Callable


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
EventHandler = Callable[["EventEnvelope"], Awaitable[None] | None]
EventFilter = Callable[["EventEnvelope"], bool]


class EventBusError(RuntimeError):
    """Base exception for event bus failures."""


class EventPublicationError(EventBusError):
    """Raised when event publication fails."""


class EventSubscriptionError(EventBusError):
    """Raised when subscription management fails."""


class EventOrderingError(EventBusError):
    """Raised when event ordering violates replay-safe assumptions."""


class DeliveryStatus(str, Enum):
    """Explicit per-subscriber delivery outcomes."""

    DELIVERED = "DELIVERED"
    FILTERED = "FILTERED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """JSON-safe event envelope with deterministic metadata."""

    event_id: str
    event_type: str
    source: str
    timestamp: datetime
    sequence_number: int
    correlation_id: str | None = None
    causation_id: str | None = None
    partition_key: str | None = None
    payload: dict[str, JsonValue] = field(default_factory=dict)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must be non-empty")
        if not self.event_type.strip():
            raise ValueError("event_type must be non-empty")
        if not self.source.strip():
            raise ValueError("source must be non-empty")
        if self.sequence_number <= 0:
            raise ValueError("sequence_number must be positive")
        if self.correlation_id is not None and not self.correlation_id.strip():
            raise ValueError("correlation_id must be non-empty when provided")
        if self.causation_id is not None and not self.causation_id.strip():
            raise ValueError("causation_id must be non-empty when provided")
        if self.partition_key is not None and not self.partition_key.strip():
            raise ValueError("partition_key must be non-empty when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "sequence_number": self.sequence_number,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "partition_key": self.partition_key,
            "payload": self.payload,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Serializable per-subscriber delivery outcome."""

    subscriber_name: str
    status: DeliveryStatus
    error: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "subscriber_name": self.subscriber_name,
            "status": self.status.value,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Serializable event publication result."""

    event: EventEnvelope
    delivery_results: tuple[DeliveryResult, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event": self.event.to_dict(),
            "delivery_results": [result.to_dict() for result in self.delivery_results],
        }


@dataclass(slots=True)
class SubscriptionRecord:
    """Subscription metadata and callback wiring."""

    subscriber_name: str
    event_types: tuple[str, ...]
    handler: EventHandler
    event_filter: EventFilter | None = None
    active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "subscriber_name": self.subscriber_name,
            "event_types": list(self.event_types),
            "active": self.active,
            "created_at": _ensure_utc(self.created_at).isoformat(),
        }


class EventBus:
    """Deterministic async event bus with bounded audit retention."""

    def __init__(self, *, max_audit_events: int = 10_000) -> None:
        if max_audit_events <= 0:
            raise ValueError("max_audit_events must be positive")
        self._max_audit_events = max_audit_events
        self._subscriptions: dict[str, SubscriptionRecord] = {}
        self._audit_events: list[EventEnvelope] = []
        self._publish_results: list[PublishResult] = []
        self._sequence_number = 0
        self._last_timestamp: datetime | None = None
        self._lock = asyncio.Lock()

    def subscribe(
        self,
        *,
        subscriber_name: str,
        event_types: tuple[str, ...],
        handler: EventHandler,
        event_filter: EventFilter | None = None,
    ) -> None:
        if not subscriber_name.strip():
            raise EventSubscriptionError("subscriber_name must be non-empty")
        if subscriber_name in self._subscriptions and self._subscriptions[subscriber_name].active:
            raise EventSubscriptionError(f"active subscription already exists: {subscriber_name}")
        if not event_types:
            raise EventSubscriptionError("event_types must be non-empty")
        normalized_event_types = tuple(_validate_event_type(item) for item in event_types)
        if len(set(normalized_event_types)) != len(normalized_event_types):
            raise EventSubscriptionError("event_types must be unique per subscriber")
        self._subscriptions[subscriber_name] = SubscriptionRecord(
            subscriber_name=subscriber_name,
            event_types=normalized_event_types,
            handler=handler,
            event_filter=event_filter,
        )

    def unsubscribe(self, *, subscriber_name: str) -> None:
        record = self._subscriptions.get(subscriber_name)
        if record is None or not record.active:
            raise EventSubscriptionError(f"unknown active subscriber: {subscriber_name}")
        record.active = False

    async def publish(
        self,
        *,
        event_type: str,
        source: str,
        payload: dict[str, JsonValue] | None = None,
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        partition_key: str | None = None,
    ) -> PublishResult:
        normalized_event_type = _validate_event_type(event_type)
        normalized_source = _validate_non_empty("source", source)
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        async with self._lock:
            if self._last_timestamp is not None and event_timestamp < self._last_timestamp:
                raise EventOrderingError("event timestamps must be non-decreasing for replay safety")
            self._sequence_number += 1
            envelope = EventEnvelope(
                event_id=f"event-{self._sequence_number}",
                event_type=normalized_event_type,
                source=normalized_source,
                timestamp=event_timestamp,
                sequence_number=self._sequence_number,
                correlation_id=correlation_id,
                causation_id=causation_id,
                partition_key=partition_key,
                payload=payload or {},
                metadata=metadata or {},
            )
            active_subscriptions = [
                record for record in self._subscriptions.values() if record.active and normalized_event_type in record.event_types
            ]
            self._audit_events.append(envelope)
            if len(self._audit_events) > self._max_audit_events:
                self._audit_events.pop(0)
            self._last_timestamp = event_timestamp
        delivery_results: list[DeliveryResult] = []
        for subscription in active_subscriptions:
            try:
                if subscription.event_filter is not None and not subscription.event_filter(envelope):
                    delivery_results.append(
                        DeliveryResult(
                            subscriber_name=subscription.subscriber_name,
                            status=DeliveryStatus.FILTERED,
                        )
                    )
                    continue
                callback_result = subscription.handler(envelope)
                if isawaitable(callback_result):
                    await callback_result
                delivery_results.append(
                    DeliveryResult(
                        subscriber_name=subscription.subscriber_name,
                        status=DeliveryStatus.DELIVERED,
                    )
                )
            except Exception as exc:
                delivery_results.append(
                    DeliveryResult(
                        subscriber_name=subscription.subscriber_name,
                        status=DeliveryStatus.FAILED,
                        error=str(exc),
                    )
                )
        publish_result = PublishResult(event=envelope, delivery_results=tuple(delivery_results))
        async with self._lock:
            self._publish_results.append(publish_result)
            if len(self._publish_results) > self._max_audit_events:
                self._publish_results.pop(0)
        return publish_result

    def replay(self, *, from_sequence_number: int = 1, event_types: tuple[str, ...] | None = None) -> tuple[EventEnvelope, ...]:
        if from_sequence_number <= 0:
            raise EventPublicationError("from_sequence_number must be positive")
        normalized_event_types = None
        if event_types is not None:
            if not event_types:
                raise EventPublicationError("event_types must be non-empty when provided")
            normalized_event_types = {_validate_event_type(item) for item in event_types}
        replayed = [
            event
            for event in self._audit_events
            if event.sequence_number >= from_sequence_number
            and (normalized_event_types is None or event.event_type in normalized_event_types)
        ]
        return tuple(replayed)

    def subscriptions_snapshot(self) -> list[dict[str, JsonValue]]:
        return [record.to_dict() for record in sorted(self._subscriptions.values(), key=lambda item: item.subscriber_name)]

    def audit_snapshot(self) -> dict[str, JsonValue]:
        return {
            "last_timestamp": _optional_iso(self._last_timestamp),
            "sequence_number": self._sequence_number,
            "subscription_count": len([item for item in self._subscriptions.values() if item.active]),
            "subscriptions": self.subscriptions_snapshot(),
            "events": [event.to_dict() for event in self._audit_events],
            "publish_results": [result.to_dict() for result in self._publish_results],
        }



def _validate_event_type(value: str) -> str:
    normalized = _validate_non_empty("event_type", value)
    return normalized.upper()



def _validate_non_empty(field_name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise EventSubscriptionError(f"{field_name} must be non-empty")
    return normalized



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
    "DeliveryResult",
    "DeliveryStatus",
    "EventBus",
    "EventBusError",
    "EventEnvelope",
    "EventOrderingError",
    "EventPublicationError",
    "EventSubscriptionError",
    "PublishResult",
    "SubscriptionRecord",
]
