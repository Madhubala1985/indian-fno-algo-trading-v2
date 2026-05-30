# FILE: platform_core/orchestration/dependency_injection.py
"""
platform_core.orchestration.dependency_injection
═══════════════════════════════════════════════════════════════════════
Production-grade dependency injection container for the institutional
Indian index futures and options derivatives platform.

This module provides deterministic dependency registration and resolution,
constructor-based wiring, scoped resolution, singleton/transient lifetimes,
factory registration, dependency graph validation, and replay-safe metadata
snapshots without introducing service-locator coupling into domain modules.

Design guarantees:
- Composition-root-friendly registration model.
- Explicit lifetime semantics.
- Constructor annotation driven auto-wiring.
- Scoped caching for per-resolution trees.
- Deterministic graph validation and JSON-safe container snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from inspect import Parameter
from inspect import signature
from typing import Any
from typing import Callable
from typing import Generic
from typing import TypeVar


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FactoryType = Callable[["ResolutionContext"], Any]
ProviderToken = type[Any] | str
ProvidedType = TypeVar("ProvidedType")


class DependencyInjectionError(RuntimeError):
    """Base exception for dependency injection failures."""


class DependencyRegistrationError(DependencyInjectionError):
    """Raised when service registration is invalid."""


class DependencyResolutionError(DependencyInjectionError):
    """Raised when dependency resolution fails."""


class DependencyGraphError(DependencyInjectionError):
    """Raised when dependency graphs are incomplete or cyclic."""


class ServiceLifetime(str, Enum):
    """Explicit dependency lifetimes."""

    SINGLETON = "SINGLETON"
    TRANSIENT = "TRANSIENT"
    SCOPED = "SCOPED"


@dataclass(frozen=True, slots=True)
class DependencyDescriptor:
    """Serializable registration metadata."""

    token_name: str
    implementation_name: str | None
    lifetime: ServiceLifetime
    dependency_tokens: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "token_name": self.token_name,
            "implementation_name": self.implementation_name,
            "lifetime": self.lifetime.value,
            "dependency_tokens": list(self.dependency_tokens),
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class _Registration:
    token: ProviderToken
    lifetime: ServiceLifetime
    descriptor: DependencyDescriptor
    factory: FactoryType
    singleton_instance: Any | None = None


class ResolutionContext:
    """Per-resolution scoped cache and recursion guard."""

    def __init__(self, *, container: DependencyInjectionContainer) -> None:
        self._container = container
        self._scoped_instances: dict[str, Any] = {}
        self._resolution_stack: list[str] = []

    def resolve(self, token: ProviderToken) -> Any:
        return self._container._resolve(token=token, context=self)

    def has_scoped_instance(self, token_name: str) -> bool:
        return token_name in self._scoped_instances

    def get_scoped_instance(self, token_name: str) -> Any:
        return self._scoped_instances[token_name]

    def set_scoped_instance(self, token_name: str, instance: Any) -> None:
        self._scoped_instances[token_name] = instance

    def push(self, token_name: str) -> None:
        if token_name in self._resolution_stack:
            chain = " -> ".join((*self._resolution_stack, token_name))
            raise DependencyResolutionError(f"cyclic resolution detected: {chain}")
        self._resolution_stack.append(token_name)

    def pop(self) -> None:
        if self._resolution_stack:
            self._resolution_stack.pop()


class DependencyInjectionContainer:
    """Deterministic constructor-wiring dependency injection container."""

    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}
        self._last_timestamp: datetime | None = None
        self._frozen: bool = False

    def register_instance(
        self,
        *,
        token: ProviderToken,
        instance: Any,
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        token_name = _token_name(token)
        self._validate_registration_allowed(token_name=token_name, timestamp=timestamp)
        descriptor = DependencyDescriptor(
            token_name=token_name,
            implementation_name=f"{instance.__class__.__module__}.{instance.__class__.__qualname__}",
            lifetime=ServiceLifetime.SINGLETON,
            dependency_tokens=(),
            metadata=metadata or {},
        )
        self._registrations[token_name] = _Registration(
            token=token,
            lifetime=ServiceLifetime.SINGLETON,
            descriptor=descriptor,
            factory=lambda _context: instance,
            singleton_instance=instance,
        )
        self._mark_timestamp(timestamp)

    def register_factory(
        self,
        *,
        token: ProviderToken,
        factory: FactoryType,
        lifetime: ServiceLifetime = ServiceLifetime.TRANSIENT,
        dependency_tokens: tuple[ProviderToken, ...] = (),
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        token_name = _token_name(token)
        self._validate_registration_allowed(token_name=token_name, timestamp=timestamp)
        dependency_token_names = tuple(_token_name(item) for item in dependency_tokens)
        descriptor = DependencyDescriptor(
            token_name=token_name,
            implementation_name=getattr(factory, "__qualname__", repr(factory)),
            lifetime=lifetime,
            dependency_tokens=dependency_token_names,
            metadata=metadata or {},
        )
        self._registrations[token_name] = _Registration(
            token=token,
            lifetime=lifetime,
            descriptor=descriptor,
            factory=factory,
        )
        self._mark_timestamp(timestamp)

    def register_type(
        self,
        *,
        token: ProviderToken,
        implementation: type[Any],
        lifetime: ServiceLifetime = ServiceLifetime.TRANSIENT,
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        token_name = _token_name(token)
        self._validate_registration_allowed(token_name=token_name, timestamp=timestamp)
        dependency_tokens = _constructor_dependency_tokens(implementation)
        descriptor = DependencyDescriptor(
            token_name=token_name,
            implementation_name=f"{implementation.__module__}.{implementation.__qualname__}",
            lifetime=lifetime,
            dependency_tokens=tuple(_token_name(item) for item in dependency_tokens),
            metadata=metadata or {},
        )

        def factory(context: ResolutionContext) -> Any:
            kwargs: dict[str, Any] = {}
            for parameter_name, dependency_token in dependency_tokens:
                kwargs[parameter_name] = context.resolve(dependency_token)
            return implementation(**kwargs)

        self._registrations[token_name] = _Registration(
            token=token,
            lifetime=lifetime,
            descriptor=descriptor,
            factory=factory,
        )
        self._mark_timestamp(timestamp)

    def freeze(self) -> None:
        self.validate_graph()
        self._frozen = True

    def resolve(self, token: ProviderToken) -> Any:
        context = ResolutionContext(container=self)
        return self._resolve(token=token, context=context)

    def resolve_typed(self, token: ProviderToken, expected_type: type[ProvidedType]) -> ProvidedType:
        instance = self.resolve(token)
        if not isinstance(instance, expected_type):
            raise DependencyResolutionError(
                f"resolved instance for { _token_name(token) } is not of expected type "
                f"{expected_type.__module__}.{expected_type.__qualname__}"
            )
        return instance

    def create_scope(self) -> "DependencyScope":
        return DependencyScope(container=self)

    def validate_graph(self) -> None:
        graph = {
            token_name: set(registration.descriptor.dependency_tokens)
            for token_name, registration in self._registrations.items()
        }
        missing_edges: list[str] = []
        for token_name, dependencies in graph.items():
            for dependency_token_name in dependencies:
                if dependency_token_name not in self._registrations:
                    missing_edges.append(f"{token_name}->{dependency_token_name}")
        if missing_edges:
            raise DependencyGraphError("missing dependencies: " + ", ".join(sorted(missing_edges)))
        _topological_order(graph)

    def snapshot(self) -> dict[str, JsonValue]:
        self.validate_graph()
        graph = {
            token_name: set(registration.descriptor.dependency_tokens)
            for token_name, registration in self._registrations.items()
        }
        return {
            "frozen": self._frozen,
            "registered_tokens": sorted(self._registrations.keys()),
            "resolution_order": list(_topological_order(graph)),
            "descriptors": {
                token_name: registration.descriptor.to_dict()
                for token_name, registration in sorted(self._registrations.items())
            },
            "last_timestamp": _optional_iso(self._last_timestamp),
        }

    def _resolve(self, *, token: ProviderToken, context: ResolutionContext) -> Any:
        token_name = _token_name(token)
        registration = self._registrations.get(token_name)
        if registration is None:
            raise DependencyResolutionError(f"unregistered dependency token: {token_name}")
        if registration.lifetime is ServiceLifetime.SINGLETON and registration.singleton_instance is not None:
            return registration.singleton_instance
        if registration.lifetime is ServiceLifetime.SCOPED and context.has_scoped_instance(token_name):
            return context.get_scoped_instance(token_name)
        context.push(token_name)
        try:
            instance = registration.factory(context)
        finally:
            context.pop()
        if registration.lifetime is ServiceLifetime.SINGLETON:
            registration.singleton_instance = instance
        elif registration.lifetime is ServiceLifetime.SCOPED:
            context.set_scoped_instance(token_name, instance)
        return instance

    def _validate_registration_allowed(self, *, token_name: str, timestamp: datetime | None) -> None:
        if not token_name.strip():
            raise DependencyRegistrationError("token name must be non-empty")
        if self._frozen:
            raise DependencyRegistrationError("container is frozen and cannot accept new registrations")
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        if self._last_timestamp is not None and event_timestamp < self._last_timestamp:
            raise DependencyRegistrationError("registration timestamps must be non-decreasing for replay safety")

    def _mark_timestamp(self, timestamp: datetime | None) -> None:
        self._last_timestamp = _ensure_utc(timestamp or _utc_now())


class DependencyScope(Generic[ProvidedType]):
    """Explicit scoped resolution context."""

    def __init__(self, *, container: DependencyInjectionContainer) -> None:
        self._container = container
        self._context = ResolutionContext(container=container)

    def resolve(self, token: ProviderToken) -> Any:
        return self._container._resolve(token=token, context=self._context)

    def resolve_typed(self, token: ProviderToken, expected_type: type[ProvidedType]) -> ProvidedType:
        instance = self.resolve(token)
        if not isinstance(instance, expected_type):
            raise DependencyResolutionError(
                f"resolved scoped instance for { _token_name(token) } is not of expected type "
                f"{expected_type.__module__}.{expected_type.__qualname__}"
            )
        return instance



def _constructor_dependency_tokens(implementation: type[Any]) -> list[tuple[str, ProviderToken]]:
    parameters = signature(implementation.__init__).parameters
    dependency_tokens: list[tuple[str, ProviderToken]] = []
    for parameter_name, parameter in parameters.items():
        if parameter_name == "self":
            continue
        if parameter.kind in {Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD}:
            continue
        if parameter.annotation is Parameter.empty:
            raise DependencyRegistrationError(
                f"constructor parameter {parameter_name} for {implementation.__module__}.{implementation.__qualname__} "
                "must have an annotation"
            )
        dependency_tokens.append((parameter_name, parameter.annotation))
    return dependency_tokens



def _topological_order(graph: dict[str, set[str]]) -> tuple[str, ...]:
    remaining = {node: set(edges) for node, edges in graph.items()}
    ordered: list[str] = []
    ready = sorted(node for node, edges in remaining.items() if not edges)
    while ready:
        node = ready.pop(0)
        if node in ordered:
            continue
        ordered.append(node)
        for candidate, edges in remaining.items():
            if node in edges:
                edges.remove(node)
                if not edges and candidate not in ordered and candidate not in ready:
                    ready.append(candidate)
                    ready.sort()
    if len(ordered) != len(remaining):
        unresolved = sorted(set(remaining) - set(ordered))
        raise DependencyGraphError("cyclic dependency graph: " + ", ".join(unresolved))
    return tuple(ordered)



def _token_name(token: ProviderToken) -> str:
    if isinstance(token, str):
        if not token.strip():
            raise DependencyRegistrationError("string token must be non-empty")
        return token
    return f"{token.__module__}.{token.__qualname__}"



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
    "DependencyDescriptor",
    "DependencyGraphError",
    "DependencyInjectionContainer",
    "DependencyInjectionError",
    "DependencyRegistrationError",
    "DependencyResolutionError",
    "DependencyScope",
    "ResolutionContext",
    "ServiceLifetime",
]
