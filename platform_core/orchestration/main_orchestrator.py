# FILE: platform_core/orchestration/main_orchestrator.py
"""
platform_core.orchestration.main_orchestrator
═══════════════════════════════════════════════════════════════════════
Production-grade async main orchestration layer for the institutional
Indian index derivatives intelligence and automated execution platform.

This module preserves an event-driven, service-isolated architecture and
provides deterministic runtime lifecycle control, dependency-aware startup,
structured shutdown, health inspection, and publish/subscribe coordination
without redesigning upstream or downstream workflows.

Design constraints preserved:
- Async-first orchestration.
- Event-driven coordination with explicit event envelopes.
- Dependency-aware startup and reverse-order shutdown.
- Replay-safe, JSON-serializable orchestration audit state.
- No implicit business-logic coupling across service domains.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from inspect import isawaitable
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Protocol


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
EventHandler = Callable[["OrchestrationEvent"], Awaitable[None] | None]


class OrchestratorError(RuntimeError):
    """Base exception for orchestration failures."""


class ServiceRegistrationError(OrchestratorError):
    """Raised when a service cannot be registered consistently."""


class DependencyResolutionError(OrchestratorError):
    """Raised when service dependencies are invalid or cyclic."""


class LifecycleStateError(OrchestratorError):
    """Raised when runtime lifecycle operations are invalid."""


class EventBusError(OrchestratorError):
    """Raised when event bus operations fail."""


class RuntimePhase(str, Enum):
    """Explicit orchestrator lifecycle phases."""

    INITIALIZED = "INITIALIZED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class ServiceHealthStatus(str, Enum):
    """Explicit health states for registered services."""

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


class OrchestrationEventType(str, Enum):
    """Core orchestration event taxonomy."""

    RUNTIME_STARTING = "RUNTIME_STARTING"
    RUNTIME_STARTED = "RUNTIME_STARTED"
    RUNTIME_STOPPING = "RUNTIME_STOPPING"
    RUNTIME_STOPPED = "RUNTIME_STOPPED"
    SERVICE_STARTING = "SERVICE_STARTING"
    SERVICE_STARTED = "SERVICE_STARTED"
    SERVICE_STOPPING = "SERVICE_STOPPING"
    SERVICE_STOPPED = "SERVICE_STOPPED"
    SERVICE_HEALTH = "SERVICE_HEALTH"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    USER_EVENT = "USER_EVENT"


@dataclass(frozen=True, slots=True)
class OrchestrationEvent:
    """JSON-safe orchestration event envelope."""

    event_id: str
    event_type: OrchestrationEventType
    timestamp: datetime
    source: str
    payload: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "source": self.source,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ServiceHealthSnapshot:
    """Serializable health snapshot."""

    service_name: str
    status: ServiceHealthStatus
    timestamp: datetime
    details: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "service_name": self.service_name,
            "status": self.status.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "details": self.details,
        }


class OrchestratedService(Protocol):
    """Protocol for dependency-managed async services."""

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def health_check(self) -> ServiceHealthSnapshot | dict[str, JsonValue] | None:
        ...


@dataclass(slots=True)
class ServiceRegistration:
    """Internal registration metadata for orchestration."""

    service_name: str
    service: OrchestratedService
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    started: bool = False
    last_health: ServiceHealthSnapshot | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "service_name": self.service_name,
            "dependencies": list(self.dependencies),
            "started": self.started,
            "last_health": self.last_health.to_dict() if self.last_health is not None else None,
        }


class AsyncEventBus:
    """Async publish/subscribe event bus with deterministic audit buffering."""

    def __init__(self, *, max_audit_events: int = 10_000) -> None:
        if max_audit_events <= 0:
            raise ValueError("max_audit_events must be positive")
        self._subscribers: dict[OrchestrationEventType, list[EventHandler]] = defaultdict(list)
        self._audit_events: list[OrchestrationEvent] = []
        self._max_audit_events = max_audit_events
        self._publish_lock = asyncio.Lock()
        self._sequence = 0
        self._last_timestamp: datetime | None = None

    def subscribe(self, *, event_type: OrchestrationEventType, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    async def publish(
        self,
        *,
        event_type: OrchestrationEventType,
        source: str,
        payload: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> OrchestrationEvent:
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        async with self._publish_lock:
            if self._last_timestamp is not None and event_timestamp < self._last_timestamp:
                raise EventBusError("event timestamps must be non-decreasing for replay safety")
            self._sequence += 1
            event = OrchestrationEvent(
                event_id=f"orchestration-event-{self._sequence}",
                event_type=event_type,
                timestamp=event_timestamp,
                source=source,
                payload=payload or {},
            )
            self._audit_events.append(event)
            if len(self._audit_events) > self._max_audit_events:
                self._audit_events.pop(0)
            self._last_timestamp = event_timestamp
            handlers = list(self._subscribers.get(event_type, []))
        for handler in handlers:
            try:
                result = handler(event)
                if isawaitable(result):
                    await result
            except Exception as exc:
                raise EventBusError(f"handler failure for {event_type.value}: {exc}") from exc
        return event

    def audit_snapshot(self) -> list[dict[str, JsonValue]]:
        return [event.to_dict() for event in self._audit_events]


class MainOrchestrator:
    """Dependency-aware async runtime orchestrator."""

    def __init__(self, *, event_bus: AsyncEventBus | None = None) -> None:
        self._event_bus = event_bus or AsyncEventBus()
        self._services: dict[str, ServiceRegistration] = {}
        self._phase = RuntimePhase.INITIALIZED
        self._service_order: tuple[str, ...] = ()
        self._runtime_started_at: datetime | None = None
        self._runtime_stopped_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    @property
    def event_bus(self) -> AsyncEventBus:
        return self._event_bus

    @property
    def phase(self) -> RuntimePhase:
        return self._phase

    def register_service(
        self,
        *,
        service_name: str,
        service: OrchestratedService,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        if self._phase is not RuntimePhase.INITIALIZED:
            raise ServiceRegistrationError("services can only be registered before runtime start")
        if not service_name.strip():
            raise ServiceRegistrationError("service_name must be non-empty")
        if service_name in self._services:
            raise ServiceRegistrationError(f"duplicate service registration: {service_name}")
        if service_name in dependencies:
            raise ServiceRegistrationError("service cannot depend on itself")
        dependency_set = set(dependencies)
        if len(dependency_set) != len(dependencies):
            raise ServiceRegistrationError("dependencies must be unique")
        self._services[service_name] = ServiceRegistration(
            service_name=service_name,
            service=service,
            dependencies=dependencies,
        )

    async def start(self) -> None:
        async with self._lock:
            if self._phase is not RuntimePhase.INITIALIZED:
                raise LifecycleStateError(f"cannot start runtime from phase {self._phase.value}")
            self._phase = RuntimePhase.STARTING
            started_services: list[str] = []
            started_timestamp = _utc_now()
            try:
                await self._event_bus.publish(
                    event_type=OrchestrationEventType.RUNTIME_STARTING,
                    source="main_orchestrator",
                    payload={"service_count": len(self._services)},
                    timestamp=started_timestamp,
                )
                self._service_order = self._resolve_start_order()
                for service_name in self._service_order:
                    await self._start_service(service_name=service_name)
                    started_services.append(service_name)
                self._phase = RuntimePhase.RUNNING
                self._runtime_started_at = started_timestamp
                await self._event_bus.publish(
                    event_type=OrchestrationEventType.RUNTIME_STARTED,
                    source="main_orchestrator",
                    payload={"service_order": list(self._service_order)},
                )
            except Exception as exc:
                self._phase = RuntimePhase.FAILED
                await self._event_bus.publish(
                    event_type=OrchestrationEventType.RUNTIME_FAILURE,
                    source="main_orchestrator",
                    payload={
                        "error": str(exc),
                        "started_services": started_services,
                    },
                )
                await self._rollback_started_services(started_services)
                raise

    async def stop(self) -> None:
        async with self._lock:
            if self._phase not in {RuntimePhase.RUNNING, RuntimePhase.FAILED, RuntimePhase.STARTING}:
                if self._phase is RuntimePhase.STOPPED:
                    return
                raise LifecycleStateError(f"cannot stop runtime from phase {self._phase.value}")
            self._phase = RuntimePhase.STOPPING
            await self._event_bus.publish(
                event_type=OrchestrationEventType.RUNTIME_STOPPING,
                source="main_orchestrator",
                payload={"service_order": list(self._service_order)},
            )
            for service_name in reversed(self._service_order):
                registration = self._services[service_name]
                if not registration.started:
                    continue
                await self._event_bus.publish(
                    event_type=OrchestrationEventType.SERVICE_STOPPING,
                    source=service_name,
                    payload={"service_name": service_name},
                )
                await registration.service.stop()
                registration.started = False
                await self._event_bus.publish(
                    event_type=OrchestrationEventType.SERVICE_STOPPED,
                    source=service_name,
                    payload={"service_name": service_name},
                )
            self._phase = RuntimePhase.STOPPED
            self._runtime_stopped_at = _utc_now()
            self._stop_event.set()
            await self._event_bus.publish(
                event_type=OrchestrationEventType.RUNTIME_STOPPED,
                source="main_orchestrator",
                payload={"service_count": len(self._services)},
            )

    async def run_forever(self) -> None:
        if self._phase is RuntimePhase.INITIALIZED:
            await self.start()
        if self._phase is not RuntimePhase.RUNNING:
            raise LifecycleStateError(f"runtime must be RUNNING to enter wait loop, got {self._phase.value}")
        await self._stop_event.wait()

    async def publish_user_event(
        self,
        *,
        source: str,
        payload: dict[str, JsonValue],
        timestamp: datetime | None = None,
    ) -> OrchestrationEvent:
        return await self._event_bus.publish(
            event_type=OrchestrationEventType.USER_EVENT,
            source=source,
            payload=payload,
            timestamp=timestamp,
        )

    async def health_snapshot(self) -> dict[str, JsonValue]:
        snapshots: list[dict[str, JsonValue]] = []
        for service_name in self._service_order or tuple(self._services.keys()):
            registration = self._services[service_name]
            health = await self._collect_health(registration)
            snapshots.append(health.to_dict())
            await self._event_bus.publish(
                event_type=OrchestrationEventType.SERVICE_HEALTH,
                source=service_name,
                payload=health.to_dict(),
            )
        return {
            "phase": self._phase.value,
            "runtime_started_at": _optional_iso(self._runtime_started_at),
            "runtime_stopped_at": _optional_iso(self._runtime_stopped_at),
            "services": snapshots,
            "events": self._event_bus.audit_snapshot(),
        }

    def registry_snapshot(self) -> dict[str, JsonValue]:
        return {
            "phase": self._phase.value,
            "service_order": list(self._service_order),
            "services": {
                service_name: registration.to_dict()
                for service_name, registration in self._services.items()
            },
        }

    async def _start_service(self, *, service_name: str) -> None:
        registration = self._services[service_name]
        await self._event_bus.publish(
            event_type=OrchestrationEventType.SERVICE_STARTING,
            source=service_name,
            payload={
                "service_name": service_name,
                "dependencies": list(registration.dependencies),
            },
        )
        await registration.service.start()
        registration.started = True
        await self._event_bus.publish(
            event_type=OrchestrationEventType.SERVICE_STARTED,
            source=service_name,
            payload={"service_name": service_name},
        )

    async def _rollback_started_services(self, started_services: list[str]) -> None:
        for service_name in reversed(started_services):
            registration = self._services[service_name]
            if not registration.started:
                continue
            try:
                await registration.service.stop()
                registration.started = False
            except Exception:
                self._phase = RuntimePhase.FAILED

    async def _collect_health(self, registration: ServiceRegistration) -> ServiceHealthSnapshot:
        raw_health = await registration.service.health_check()
        if raw_health is None:
            snapshot = ServiceHealthSnapshot(
                service_name=registration.service_name,
                status=ServiceHealthStatus.UNKNOWN,
                timestamp=_utc_now(),
                details={},
            )
        elif isinstance(raw_health, ServiceHealthSnapshot):
            snapshot = raw_health
        elif isinstance(raw_health, dict):
            status_raw = str(raw_health.get("status", ServiceHealthStatus.UNKNOWN.value)).upper()
            try:
                status = ServiceHealthStatus(status_raw)
            except ValueError as exc:
                raise LifecycleStateError(
                    f"invalid health status from service {registration.service_name}: {status_raw}"
                ) from exc
            details = {
                str(key): _to_json_value(value)
                for key, value in raw_health.items()
                if key != "status"
            }
            snapshot = ServiceHealthSnapshot(
                service_name=registration.service_name,
                status=status,
                timestamp=_utc_now(),
                details=details,
            )
        else:
            raise LifecycleStateError(
                f"unsupported health response type from service {registration.service_name}: {type(raw_health)!r}"
            )
        registration.last_health = snapshot
        return snapshot

    def _resolve_start_order(self) -> tuple[str, ...]:
        missing_dependencies: list[str] = []
        for service_name, registration in self._services.items():
            for dependency in registration.dependencies:
                if dependency not in self._services:
                    missing_dependencies.append(f"{service_name}->{dependency}")
        if missing_dependencies:
            raise DependencyResolutionError(
                "missing dependencies: " + ", ".join(sorted(missing_dependencies))
            )

        remaining_dependencies: dict[str, set[str]] = {
            service_name: set(registration.dependencies)
            for service_name, registration in self._services.items()
        }
        resolved_order: list[str] = []
        ready = sorted(
            service_name
            for service_name, dependencies in remaining_dependencies.items()
            if not dependencies
        )
        while ready:
            service_name = ready.pop(0)
            if service_name in resolved_order:
                continue
            resolved_order.append(service_name)
            for candidate_name, dependencies in remaining_dependencies.items():
                if service_name in dependencies:
                    dependencies.remove(service_name)
                    if not dependencies and candidate_name not in resolved_order and candidate_name not in ready:
                        ready.append(candidate_name)
                        ready.sort()
        if len(resolved_order) != len(self._services):
            unresolved = sorted(set(self._services) - set(resolved_order))
            raise DependencyResolutionError(
                "cyclic or unresolved dependencies among services: " + ", ".join(unresolved)
            )
        return tuple(resolved_order)



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



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "AsyncEventBus",
    "DependencyResolutionError",
    "EventBusError",
    "LifecycleStateError",
    "MainOrchestrator",
    "OrchestratedService",
    "OrchestrationEvent",
    "OrchestrationEventType",
    "OrchestratorError",
    "RuntimePhase",
    "ServiceHealthSnapshot",
    "ServiceHealthStatus",
    "ServiceRegistration",
    "ServiceRegistrationError",
]
