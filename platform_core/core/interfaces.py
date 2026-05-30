# FILE: platform_core/core/interfaces.py
"""
platform_core.core.interfaces
═══════════════════════════════════════════════════════════════════════
Production-grade foundational interfaces for the institutional derivatives
intelligence and automated execution platform.

Design guarantees preserved:
- Full package imports only.
- Async-first architecture.
- Event-driven workflow compatibility.
- Replay-safe and streaming-safe contracts.
- Type-safe abstractions with Protocols and ABCs.
- No business logic and no concrete transport/storage coupling.
- Future compatibility with Redis, PostgreSQL, TimescaleDB, replay engine,
  orchestration, and ML pipeline layers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

from platform_core.core.enums import EventType, ReplayState, ServiceName, StorageBackend
from platform_core.core.events import EventEnvelope


EventHandler = Callable[[EventEnvelope], Awaitable[None]]


@runtime_checkable
class SupportsLifecycle(Protocol):
    """Protocol for components with async lifecycle management."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def healthcheck(self) -> bool: ...


@runtime_checkable
class SupportsReload(Protocol):
    """Protocol for components that support runtime reload."""

    async def reload(self) -> None: ...


class BaseService(ABC):
    """Abstract base class for long-lived platform services."""

    def __init__(self, service_name: ServiceName) -> None:
        self._service_name = service_name

    @property
    def service_name(self) -> ServiceName:
        return self._service_name

    @abstractmethod
    async def start(self) -> None:
        """Start the service and allocate resources."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the service and release resources."""

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Return service health state."""


@runtime_checkable
class EventPublisher(Protocol):
    """Protocol for publishing event envelopes to an event bus or adapter."""

    async def publish(self, event: EventEnvelope) -> None: ...


@runtime_checkable
class EventSubscriber(Protocol):
    """Protocol for event-bus subscription management."""

    async def subscribe(self, event_type: EventType, handler: EventHandler) -> None: ...
    async def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None: ...


@runtime_checkable
class EventBus(EventPublisher, EventSubscriber, Protocol):
    """Composite protocol for the core event bus."""

    async def drain(self) -> None: ...


@runtime_checkable
class Replayable(Protocol):
    """Protocol for services or engines that support deterministic replay."""

    async def start_replay(self) -> None: ...
    async def pause_replay(self) -> None: ...
    async def resume_replay(self) -> None: ...
    async def stop_replay(self) -> None: ...
    def replay_state(self) -> ReplayState: ...


@runtime_checkable
class EventSource(Protocol):
    """Protocol for async event producers."""

    def stream(self) -> AsyncIterator[EventEnvelope]: ...


@runtime_checkable
class EventSink(Protocol):
    """Protocol for async event consumers."""

    async def consume(self, event: EventEnvelope) -> None: ...


@runtime_checkable
class Serializer(Protocol):
    """Protocol for serialization of event and model payloads."""

    def dumps(self, value: Any) -> str: ...
    def loads(self, value: str) -> Any: ...


@runtime_checkable
class CacheClient(Protocol):
    """Protocol for Redis-compatible cache clients."""

    backend: StorageBackend

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...


@runtime_checkable
class AsyncConnection(Protocol):
    """Protocol for async DB or external resource connections."""

    async def close(self) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Protocol for transaction-scoped resource handling."""

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> bool | None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


@runtime_checkable
class StorageWriter(Protocol):
    """Protocol for durable append/write operations."""

    backend: StorageBackend

    async def write_one(self, record: dict[str, Any]) -> None: ...
    async def write_many(self, records: list[dict[str, Any]]) -> None: ...
    async def flush(self) -> None: ...


@runtime_checkable
class StorageReader(Protocol):
    """Protocol for durable read/query operations."""

    backend: StorageBackend

    async def fetch_one(self, query: str, **params: Any) -> dict[str, Any] | None: ...
    async def fetch_many(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class Repository(Protocol):
    """Protocol for repository-style persistence adapters."""

    async def save(self, model: Any) -> None: ...
    async def get(self, identifier: str) -> Any | None: ...


@runtime_checkable
class Clock(Protocol):
    """Protocol for injectable clocks enabling deterministic replay/testing."""

    def now(self) -> Any: ...


@runtime_checkable
class IdGenerator(Protocol):
    """Protocol for deterministic or live identifier generation."""

    def next_id(self) -> str: ...


@runtime_checkable
class MetricsRecorder(Protocol):
    """Protocol for metrics and observability recording."""

    async def increment(self, name: str, value: int = 1, **tags: str) -> None: ...
    async def gauge(self, name: str, value: float, **tags: str) -> None: ...
    async def timing(self, name: str, value_ms: float, **tags: str) -> None: ...


@runtime_checkable
class ServiceRegistry(Protocol):
    """Protocol for orchestration service registration and lookup."""

    def register(self, service: BaseService) -> None: ...
    def get(self, service_name: ServiceName) -> BaseService: ...
    def list_services(self) -> tuple[BaseService, ...]: ...


__all__ = [
    "AsyncConnection",
    "BaseService",
    "CacheClient",
    "Clock",
    "EventBus",
    "EventHandler",
    "EventPublisher",
    "EventSink",
    "EventSource",
    "EventSubscriber",
    "IdGenerator",
    "MetricsRecorder",
    "Replayable",
    "Repository",
    "Serializer",
    "ServiceRegistry",
    "StorageReader",
    "StorageWriter",
    "SupportsLifecycle",
    "SupportsReload",
    "UnitOfWork",
]
