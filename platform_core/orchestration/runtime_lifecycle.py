# FILE: platform_core/orchestration/runtime_lifecycle.py
"""
platform_core.orchestration.runtime_lifecycle
═══════════════════════════════════════════════════════════════════════
Production-grade runtime lifecycle management for the institutional derivatives
intelligence and automated execution platform specialized for Indian index
futures and options markets.

This module coordinates application-level lifecycle transitions, readiness and
liveness semantics, graceful startup and shutdown ordering, service draining,
health aggregation, emergency stop signaling, and replay-safe audit capture.

Scope:
- Explicit runtime state machine.
- Ordered startup, readiness transition, drain, shutdown, and emergency stop.
- Readiness/liveness/health snapshots.
- Async-safe runtime operation serialization.
- Event publication hooks for orchestration observability.
- Deterministic lifecycle audit history suitable for replay and persistence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Awaitable
from typing import Callable


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
LifecycleEventPublisher = Callable[..., Awaitable[Any]]
DrainCallback = Callable[[], Awaitable[None] | None]
HealthProvider = Callable[[], Awaitable[dict[str, JsonValue]] | dict[str, JsonValue]]


class RuntimeLifecycleError(RuntimeError):
    """Base exception for runtime lifecycle failures."""


class RuntimeTransitionError(RuntimeLifecycleError):
    """Raised when a requested runtime transition is invalid."""


class RuntimeHealthError(RuntimeLifecycleError):
    """Raised when runtime health operations fail."""


class RuntimeState(str, Enum):
    """Explicit application runtime states."""

    CREATED = "CREATED"
    STARTING = "STARTING"
    WARMING = "WARMING"
    READY = "READY"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    FAILED = "FAILED"


class ReadinessState(str, Enum):
    """Explicit readiness states for traffic and workflow admission."""

    NOT_READY = "NOT_READY"
    READY = "READY"
    REFUSING_TRAFFIC = "REFUSING_TRAFFIC"


class LivenessState(str, Enum):
    """Explicit liveness states."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class LifecycleTransitionRecord:
    """Serializable runtime transition audit record."""

    previous_state: RuntimeState
    new_state: RuntimeState
    timestamp: datetime
    reason: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "previous_state": self.previous_state.value,
            "new_state": self.new_state.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RuntimeHealthSnapshot:
    """Serializable runtime health snapshot."""

    runtime_state: RuntimeState
    readiness_state: ReadinessState
    liveness_state: LivenessState
    timestamp: datetime
    uptime_seconds: float
    details: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "runtime_state": self.runtime_state.value,
            "readiness_state": self.readiness_state.value,
            "liveness_state": self.liveness_state.value,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "uptime_seconds": self.uptime_seconds,
            "details": self.details,
        }


class RuntimeLifecycleManager:
    """Async-safe runtime lifecycle manager."""

    def __init__(
        self,
        *,
        runtime_name: str,
        startup_callback: DrainCallback | None = None,
        shutdown_callback: DrainCallback | None = None,
        drain_callback: DrainCallback | None = None,
        emergency_callback: DrainCallback | None = None,
        health_provider: HealthProvider | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
        max_history: int = 10_000,
    ) -> None:
        if not runtime_name.strip():
            raise ValueError("runtime_name must be non-empty")
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        self._runtime_name = runtime_name
        self._startup_callback = startup_callback
        self._shutdown_callback = shutdown_callback
        self._drain_callback = drain_callback
        self._emergency_callback = emergency_callback
        self._health_provider = health_provider
        self._event_publisher = event_publisher
        self._max_history = max_history
        self._runtime_state = RuntimeState.CREATED
        self._readiness_state = ReadinessState.NOT_READY
        self._liveness_state = LivenessState.ALIVE
        self._created_at = _utc_now()
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_timestamp: datetime = self._created_at
        self._history: list[LifecycleTransitionRecord] = []
        self._latest_health: RuntimeHealthSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def runtime_name(self) -> str:
        return self._runtime_name

    @property
    def runtime_state(self) -> RuntimeState:
        return self._runtime_state

    @property
    def readiness_state(self) -> ReadinessState:
        return self._readiness_state

    @property
    def liveness_state(self) -> LivenessState:
        return self._liveness_state

    async def start(self, *, timestamp: datetime | None = None, reason: str = "startup requested") -> None:
        async with self._lock:
            if self._runtime_state not in {RuntimeState.CREATED, RuntimeState.STOPPED}:
                raise RuntimeTransitionError(
                    f"cannot start runtime from state {self._runtime_state.value}"
                )
            transition_timestamp = self._next_timestamp(timestamp)
            await self._transition_to(RuntimeState.STARTING, transition_timestamp, reason)
            self._readiness_state = ReadinessState.NOT_READY
            self._liveness_state = LivenessState.ALIVE
            self._started_at = transition_timestamp
        await _invoke_callback(self._startup_callback)
        async with self._lock:
            warming_timestamp = self._next_timestamp()
            await self._transition_to(RuntimeState.WARMING, warming_timestamp, "startup callback completed")
            self._readiness_state = ReadinessState.NOT_READY
            ready_timestamp = self._next_timestamp()
            await self._transition_to(RuntimeState.READY, ready_timestamp, "runtime warmup completed")
            self._readiness_state = ReadinessState.READY
            self._stopped_at = None

    async def begin_drain(self, *, timestamp: datetime | None = None, reason: str = "drain requested") -> None:
        async with self._lock:
            if self._runtime_state is not RuntimeState.READY:
                raise RuntimeTransitionError(
                    f"cannot drain runtime from state {self._runtime_state.value}"
                )
            drain_timestamp = self._next_timestamp(timestamp)
            await self._transition_to(RuntimeState.DRAINING, drain_timestamp, reason)
            self._readiness_state = ReadinessState.REFUSING_TRAFFIC
        await _invoke_callback(self._drain_callback)

    async def stop(self, *, timestamp: datetime | None = None, reason: str = "shutdown requested") -> None:
        async with self._lock:
            if self._runtime_state in {RuntimeState.STOPPED, RuntimeState.CREATED}:
                raise RuntimeTransitionError(
                    f"cannot stop runtime from state {self._runtime_state.value}"
                )
            if self._runtime_state is RuntimeState.READY:
                drain_timestamp = self._next_timestamp(timestamp)
                await self._transition_to(RuntimeState.DRAINING, drain_timestamp, "implicit drain before shutdown")
                self._readiness_state = ReadinessState.REFUSING_TRAFFIC
            elif self._runtime_state is RuntimeState.DRAINING:
                drain_timestamp = self._next_timestamp(timestamp)
            else:
                drain_timestamp = self._next_timestamp(timestamp)
        if self._runtime_state is RuntimeState.DRAINING and self._drain_callback is not None:
            await _invoke_callback(self._drain_callback)
        async with self._lock:
            stopping_timestamp = self._next_timestamp(drain_timestamp)
            await self._transition_to(RuntimeState.STOPPING, stopping_timestamp, reason)
        await _invoke_callback(self._shutdown_callback)
        async with self._lock:
            stopped_timestamp = self._next_timestamp()
            await self._transition_to(RuntimeState.STOPPED, stopped_timestamp, "shutdown callback completed")
            self._readiness_state = ReadinessState.NOT_READY
            self._liveness_state = LivenessState.DEAD
            self._stopped_at = stopped_timestamp

    async def emergency_stop(
        self,
        *,
        timestamp: datetime | None = None,
        reason: str = "emergency stop requested",
    ) -> None:
        async with self._lock:
            if self._runtime_state is RuntimeState.EMERGENCY_STOP:
                return
            emergency_timestamp = self._next_timestamp(timestamp)
            await self._transition_to(RuntimeState.EMERGENCY_STOP, emergency_timestamp, reason)
            self._readiness_state = ReadinessState.REFUSING_TRAFFIC
            self._liveness_state = LivenessState.ALIVE
        await _invoke_callback(self._emergency_callback)
        async with self._lock:
            failed_timestamp = self._next_timestamp()
            await self._transition_to(RuntimeState.FAILED, failed_timestamp, "emergency callback completed")
            self._readiness_state = ReadinessState.NOT_READY
            self._liveness_state = LivenessState.DEAD
            self._stopped_at = failed_timestamp

    async def health(self, *, timestamp: datetime | None = None) -> RuntimeHealthSnapshot:
        async with self._lock:
            snapshot_timestamp = self._next_timestamp(timestamp)
            runtime_state = self._runtime_state
            readiness_state = self._readiness_state
            liveness_state = self._liveness_state
            started_at = self._started_at or self._created_at
        details: dict[str, JsonValue] = {}
        if self._health_provider is not None:
            try:
                provided = self._health_provider()
                details = await provided if asyncio.iscoroutine(provided) else provided
            except Exception as exc:
                raise RuntimeHealthError(str(exc)) from exc
        uptime_seconds = max((snapshot_timestamp - started_at).total_seconds(), 0.0)
        snapshot = RuntimeHealthSnapshot(
            runtime_state=runtime_state,
            readiness_state=readiness_state,
            liveness_state=liveness_state,
            timestamp=snapshot_timestamp,
            uptime_seconds=uptime_seconds,
            details=details,
        )
        async with self._lock:
            self._latest_health = snapshot
            self._last_timestamp = snapshot_timestamp
        return snapshot

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "runtime_name": self._runtime_name,
            "runtime_state": self._runtime_state.value,
            "readiness_state": self._readiness_state.value,
            "liveness_state": self._liveness_state.value,
            "created_at": _ensure_utc(self._created_at).isoformat(),
            "started_at": _optional_iso(self._started_at),
            "stopped_at": _optional_iso(self._stopped_at),
            "last_timestamp": _ensure_utc(self._last_timestamp).isoformat(),
            "latest_health": self._latest_health.to_dict() if self._latest_health is not None else None,
            "history": [record.to_dict() for record in self._history],
        }

    async def _transition_to(self, new_state: RuntimeState, timestamp: datetime, reason: str) -> None:
        previous_state = self._runtime_state
        self._runtime_state = new_state
        self._last_timestamp = timestamp
        record = LifecycleTransitionRecord(
            previous_state=previous_state,
            new_state=new_state,
            timestamp=timestamp,
            reason=reason,
        )
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        await self._publish_event(record=record)

    async def _publish_event(self, *, record: LifecycleTransitionRecord) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher(
            event_type="RUNTIME_STATE_TRANSITION",
            source=f"runtime_lifecycle.{self._runtime_name}",
            payload={
                "runtime_name": self._runtime_name,
                "transition": record.to_dict(),
                "readiness_state": self._readiness_state.value,
                "liveness_state": self._liveness_state.value,
            },
            timestamp=record.timestamp,
        )

    def _next_timestamp(self, explicit: datetime | None = None) -> datetime:
        candidate = _ensure_utc(explicit or _utc_now())
        if candidate < self._last_timestamp:
            return self._last_timestamp
        return candidate


async def _invoke_callback(callback: DrainCallback | None) -> None:
    if callback is None:
        return
    result = callback()
    if asyncio.iscoroutine(result):
        await result



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
    "LifecycleTransitionRecord",
    "LivenessState",
    "ReadinessState",
    "RuntimeHealthError",
    "RuntimeHealthSnapshot",
    "RuntimeLifecycleError",
    "RuntimeLifecycleManager",
    "RuntimeState",
    "RuntimeTransitionError",
]
