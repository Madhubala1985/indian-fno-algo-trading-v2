# FILE: platform_core/orchestration/service_registry.py
"""
platform_core.orchestration.service_registry
═══════════════════════════════════════════════════════════════════════
Production-grade runtime service registry for the institutional derivatives
intelligence and automated execution platform specialized for Indian index
futures and options markets.

The registry provides deterministic service metadata management, dependency
validation, typed capability lookup, lifecycle state tracking, health snapshot
storage, and replay-safe JSON serialization without coupling unrelated domains
or redesigning the validated orchestration architecture.

Scope:
- In-process runtime service registration and deregistration.
- Dependency-aware service metadata tracking.
- Capability and tag-based discovery.
- Service health snapshot capture.
- Deterministic snapshots for replay, Redis, PostgreSQL, and TimescaleDB.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Protocol
from typing import TypeVar
from typing import runtime_checkable


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ServiceType = TypeVar("ServiceType")


class ServiceRegistryError(RuntimeError):
    """Base exception for service registry failures."""


class ServiceRegistrationError(ServiceRegistryError):
    """Raised when service registration fails validation."""


class ServiceLookupError(ServiceRegistryError):
    """Raised when a service lookup cannot be satisfied."""


class ServiceDependencyError(ServiceRegistryError):
    """Raised when declared dependencies are inconsistent or cyclic."""


class ServiceLifecycleState(str, Enum):
    """Explicit service lifecycle states."""

    REGISTERED = "REGISTERED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    DEREGISTERED = "DEREGISTERED"


class ServiceHealthStatus(str, Enum):
    """Explicit health states for registry snapshots."""

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@runtime_checkable
class RegistryManagedService(Protocol):
    """Minimal protocol for services stored in the runtime registry."""

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ServiceHealthSnapshot:
    """Serializable health state for a registered service."""

    service_name: str
    status: ServiceHealthStatus
    timestamp: datetime
    details: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.service_name.strip():
            raise ValueError("service_name must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "service_name": self.service_name,
            "status": self.status.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "details": self.details,
        }


@dataclass(slots=True)
class ServiceRegistrationRecord:
    """Registry metadata for one service instance."""

    service_name: str
    service: RegistryManagedService
    service_type_name: str
    lifecycle_state: ServiceLifecycleState
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    registered_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    last_health_snapshot: ServiceHealthSnapshot | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "service_name": self.service_name,
            "service_type_name": self.service_type_name,
            "lifecycle_state": self.lifecycle_state.value,
            "dependencies": list(self.dependencies),
            "capabilities": list(self.capabilities),
            "tags": list(self.tags),
            "metadata": self.metadata,
            "registered_at": _ensure_utc(self.registered_at).isoformat(),
            "updated_at": _ensure_utc(self.updated_at).isoformat(),
            "last_health_snapshot": self.last_health_snapshot.to_dict() if self.last_health_snapshot is not None else None,
        }


class ServiceRegistry:
    """Deterministic in-process runtime service registry."""

    def __init__(self) -> None:
        self._records: dict[str, ServiceRegistrationRecord] = {}
        self._capability_index: dict[str, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._last_timestamp: datetime | None = None

    def register(
        self,
        *,
        service_name: str,
        service: RegistryManagedService,
        dependencies: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
        tags: tuple[str, ...] = (),
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        if not service_name.strip():
            raise ServiceRegistrationError("service_name must be non-empty")
        if service_name in self._records and self._records[service_name].lifecycle_state is not ServiceLifecycleState.DEREGISTERED:
            raise ServiceRegistrationError(f"service already registered: {service_name}")
        _validate_unique_tuple(name="dependencies", values=dependencies)
        _validate_unique_tuple(name="capabilities", values=capabilities)
        _validate_unique_tuple(name="tags", values=tags)
        if service_name in dependencies:
            raise ServiceRegistrationError("service cannot depend on itself")
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        self._validate_timestamp(event_timestamp)
        record = ServiceRegistrationRecord(
            service_name=service_name,
            service=service,
            service_type_name=f"{service.__class__.__module__}.{service.__class__.__qualname__}",
            lifecycle_state=ServiceLifecycleState.REGISTERED,
            dependencies=dependencies,
            capabilities=capabilities,
            tags=tags,
            metadata=metadata or {},
            registered_at=event_timestamp,
            updated_at=event_timestamp,
        )
        self._records[service_name] = record
        self._index_record(record)
        self._validate_dependency_graph()
        self._last_timestamp = event_timestamp

    def deregister(self, *, service_name: str, timestamp: datetime | None = None) -> None:
        record = self._require_record(service_name)
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        self._validate_timestamp(event_timestamp)
        self._deindex_record(record)
        record.lifecycle_state = ServiceLifecycleState.DEREGISTERED
        record.updated_at = event_timestamp
        self._last_timestamp = event_timestamp

    def update_lifecycle(
        self,
        *,
        service_name: str,
        lifecycle_state: ServiceLifecycleState,
        timestamp: datetime | None = None,
    ) -> None:
        record = self._require_record(service_name)
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        self._validate_timestamp(event_timestamp)
        record.lifecycle_state = lifecycle_state
        record.updated_at = event_timestamp
        self._last_timestamp = event_timestamp

    def update_health(
        self,
        *,
        service_name: str,
        health_snapshot: ServiceHealthSnapshot,
        timestamp: datetime | None = None,
    ) -> None:
        record = self._require_record(service_name)
        event_timestamp = _ensure_utc(timestamp or health_snapshot.timestamp)
        self._validate_timestamp(event_timestamp)
        record.last_health_snapshot = ServiceHealthSnapshot(
            service_name=service_name,
            status=health_snapshot.status,
            timestamp=event_timestamp,
            details=health_snapshot.details,
        )
        if health_snapshot.status is ServiceHealthStatus.HEALTHY and record.lifecycle_state is ServiceLifecycleState.RUNNING:
            record.lifecycle_state = ServiceLifecycleState.RUNNING
        elif health_snapshot.status is ServiceHealthStatus.DEGRADED:
            record.lifecycle_state = ServiceLifecycleState.DEGRADED
        elif health_snapshot.status is ServiceHealthStatus.UNHEALTHY:
            record.lifecycle_state = ServiceLifecycleState.FAILED
        record.updated_at = event_timestamp
        self._last_timestamp = event_timestamp

    def get(self, *, service_name: str) -> RegistryManagedService:
        record = self._require_record(service_name)
        return record.service

    def get_typed(self, *, service_name: str, expected_type: type[ServiceType]) -> ServiceType:
        service = self.get(service_name=service_name)
        if not isinstance(service, expected_type):
            raise ServiceLookupError(
                f"service {service_name} is not of expected type {expected_type.__module__}.{expected_type.__qualname__}"
            )
        return service

    def find_by_capability(self, *, capability: str) -> tuple[str, ...]:
        if not capability.strip():
            raise ServiceLookupError("capability must be non-empty")
        return tuple(sorted(self._capability_index.get(capability, set())))

    def find_by_tag(self, *, tag: str) -> tuple[str, ...]:
        if not tag.strip():
            raise ServiceLookupError("tag must be non-empty")
        return tuple(sorted(self._tag_index.get(tag, set())))

    def dependency_order(self) -> tuple[str, ...]:
        return self._topological_order(active_only=True)

    def reverse_dependency_order(self) -> tuple[str, ...]:
        ordered = self._topological_order(active_only=True)
        return tuple(reversed(ordered))

    def dependents_of(self, *, service_name: str) -> tuple[str, ...]:
        self._require_record(service_name)
        dependents = [
            candidate_name
            for candidate_name, record in self._records.items()
            if record.lifecycle_state is not ServiceLifecycleState.DEREGISTERED and service_name in record.dependencies
        ]
        return tuple(sorted(dependents))

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "service_count": len([record for record in self._records.values() if record.lifecycle_state is not ServiceLifecycleState.DEREGISTERED]),
            "dependency_order": list(self.dependency_order()),
            "services": {
                service_name: record.to_dict()
                for service_name, record in sorted(self._records.items())
            },
            "capability_index": {
                capability: sorted(service_names)
                for capability, service_names in sorted(self._capability_index.items())
            },
            "tag_index": {
                tag: sorted(service_names)
                for tag, service_names in sorted(self._tag_index.items())
            },
            "last_timestamp": _optional_iso(self._last_timestamp),
        }

    def service_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                service_name
                for service_name, record in self._records.items()
                if record.lifecycle_state is not ServiceLifecycleState.DEREGISTERED
            )
        )

    def _require_record(self, service_name: str) -> ServiceRegistrationRecord:
        record = self._records.get(service_name)
        if record is None or record.lifecycle_state is ServiceLifecycleState.DEREGISTERED:
            raise ServiceLookupError(f"unknown service: {service_name}")
        return record

    def _validate_dependency_graph(self) -> None:
        for service_name, record in self._records.items():
            if record.lifecycle_state is ServiceLifecycleState.DEREGISTERED:
                continue
            for dependency in record.dependencies:
                dependency_record = self._records.get(dependency)
                if dependency_record is None or dependency_record.lifecycle_state is ServiceLifecycleState.DEREGISTERED:
                    raise ServiceDependencyError(
                        f"service {service_name} depends on unregistered dependency {dependency}"
                    )
        self._topological_order(active_only=True)

    def _topological_order(self, *, active_only: bool) -> tuple[str, ...]:
        graph: dict[str, set[str]] = {}
        for service_name, record in self._records.items():
            if active_only and record.lifecycle_state is ServiceLifecycleState.DEREGISTERED:
                continue
            graph[service_name] = set(record.dependencies)
        ordered: list[str] = []
        ready = sorted(name for name, dependencies in graph.items() if not dependencies)
        while ready:
            node = ready.pop(0)
            if node in ordered:
                continue
            ordered.append(node)
            for candidate, dependencies in graph.items():
                if node in dependencies:
                    dependencies.remove(node)
                    if not dependencies and candidate not in ordered and candidate not in ready:
                        ready.append(candidate)
                        ready.sort()
        if len(ordered) != len(graph):
            unresolved = sorted(set(graph) - set(ordered))
            raise ServiceDependencyError(
                "cyclic or unresolved service dependencies: " + ", ".join(unresolved)
            )
        return tuple(ordered)

    def _index_record(self, record: ServiceRegistrationRecord) -> None:
        for capability in record.capabilities:
            self._capability_index.setdefault(capability, set()).add(record.service_name)
        for tag in record.tags:
            self._tag_index.setdefault(tag, set()).add(record.service_name)

    def _deindex_record(self, record: ServiceRegistrationRecord) -> None:
        for capability in record.capabilities:
            service_names = self._capability_index.get(capability)
            if service_names is None:
                continue
            service_names.discard(record.service_name)
            if not service_names:
                del self._capability_index[capability]
        for tag in record.tags:
            service_names = self._tag_index.get(tag)
            if service_names is None:
                continue
            service_names.discard(record.service_name)
            if not service_names:
                del self._tag_index[tag]

    def _validate_timestamp(self, timestamp: datetime) -> None:
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ServiceRegistryError("timestamps must be non-decreasing for replay safety")



def _validate_unique_tuple(*, name: str, values: tuple[str, ...]) -> None:
    normalized: list[str] = []
    for value in values:
        if not value.strip():
            raise ServiceRegistrationError(f"{name} entries must be non-empty")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ServiceRegistrationError(f"{name} entries must be unique")



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
    "RegistryManagedService",
    "ServiceDependencyError",
    "ServiceHealthSnapshot",
    "ServiceHealthStatus",
    "ServiceLifecycleState",
    "ServiceLookupError",
    "ServiceRegistrationError",
    "ServiceRegistrationRecord",
    "ServiceRegistry",
    "ServiceRegistryError",
]
