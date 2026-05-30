# FILE: platform_core/testing/simulation_framework.py
"""
platform_core.testing.simulation_framework
═══════════════════════════════════════════════════════════════════════
Production-grade deterministic simulation framework for the institutional
Indian index futures and options derivatives intelligence and execution
platform.

This module provides a replay-safe, event-driven simulation substrate for unit,
integration, and validation workloads. It preserves causal ordering, explicit
simulated time progression, configurable latency injection, event-bus
compatibility, and structured execution auditing without introducing live-trade
side effects.

Scope:
- Deterministic event queue with causal timestamp ordering.
- Simulated clock with explicit monotonic advancement.
- Async-capable event handlers and publish hooks.
- Configurable synthetic latency and bounded audit retention.
- Scenario execution records with JSON-safe snapshots.
- Foundation for replay, stress, slippage, and walk-forward layers.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
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
SimulationHandler = Callable[["SimulationContext", "SimulationEvent"], Awaitable[list["SimulationEvent"] | None] | list["SimulationEvent"] | None]
SimulationPublisher = Callable[..., Awaitable[Any]]


class SimulationFrameworkError(RuntimeError):
    """Base exception for simulation framework failures."""


class SimulationConfigurationError(SimulationFrameworkError):
    """Raised when a simulation scenario or registration is invalid."""


class SimulationExecutionError(SimulationFrameworkError):
    """Raised when deterministic simulation execution fails."""


class SimulationStatus(str, Enum):
    """Explicit scenario execution statuses."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class SimulationEvent:
    """Serializable simulation event envelope."""

    event_type: str
    event_time: datetime
    source: str
    payload: dict[str, JsonValue] = field(default_factory=dict)
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must be non-empty")
        if not self.source.strip():
            raise ValueError("source must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_type": self.event_type,
            "event_time": _ensure_utc(self.event_time).isoformat(),
            "source": self.source,
            "payload": self.payload,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class SimulationAuditRecord:
    """Serializable audit record for one processed event."""

    sequence_number: int
    event: SimulationEvent
    processed_at: datetime
    handler_count: int
    emitted_count: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "sequence_number": self.sequence_number,
            "event": self.event.to_dict(),
            "processed_at": _ensure_utc(self.processed_at).isoformat(),
            "handler_count": self.handler_count,
            "emitted_count": self.emitted_count,
        }


@dataclass(frozen=True, slots=True)
class SimulationScenarioRecord:
    """Serializable scenario execution result."""

    scenario_name: str
    status: SimulationStatus
    started_at: datetime
    finished_at: datetime
    processed_events: int
    emitted_events: int
    final_simulation_time: datetime
    audit_records: tuple[SimulationAuditRecord, ...]
    error_message: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "scenario_name": self.scenario_name,
            "status": self.status.value,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "processed_events": self.processed_events,
            "emitted_events": self.emitted_events,
            "final_simulation_time": _ensure_utc(self.final_simulation_time).isoformat(),
            "audit_records": [item.to_dict() for item in self.audit_records],
            "error_message": self.error_message,
        }


@dataclass(slots=True)
class SimulationContext:
    """Mutable deterministic execution context for one scenario run."""

    scenario_name: str
    simulation_time: datetime
    processed_events: int = 0
    emitted_events: int = 0
    state: dict[str, JsonValue] = field(default_factory=dict)

    def advance_time(self, new_time: datetime) -> None:
        normalized = _ensure_utc(new_time)
        if normalized < self.simulation_time:
            raise SimulationExecutionError("simulation time cannot move backwards")
        self.simulation_time = normalized

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "scenario_name": self.scenario_name,
            "simulation_time": _ensure_utc(self.simulation_time).isoformat(),
            "processed_events": self.processed_events,
            "emitted_events": self.emitted_events,
            "state": self.state,
        }


@dataclass(order=True, slots=True)
class _ScheduledEvent:
    scheduled_time: datetime
    priority: int
    sequence_number: int
    event: SimulationEvent = field(compare=False)


class SimulationFramework:
    """Deterministic async event-driven simulation framework."""

    def __init__(
        self,
        *,
        scenario_name: str,
        start_time: datetime,
        latency_milliseconds: int = 0,
        max_audit_records: int = 100_000,
        event_publisher: SimulationPublisher | None = None,
    ) -> None:
        if not scenario_name.strip():
            raise ValueError("scenario_name must be non-empty")
        if latency_milliseconds < 0:
            raise ValueError("latency_milliseconds must be non-negative")
        if max_audit_records <= 0:
            raise ValueError("max_audit_records must be positive")
        self._scenario_name = scenario_name
        self._start_time = _ensure_utc(start_time)
        self._latency = timedelta(milliseconds=latency_milliseconds)
        self._max_audit_records = max_audit_records
        self._event_publisher = event_publisher
        self._status = SimulationStatus.CREATED
        self._handlers: dict[str, list[SimulationHandler]] = {}
        self._queue: list[_ScheduledEvent] = []
        self._audit_records: list[SimulationAuditRecord] = []
        self._execution_history: list[SimulationScenarioRecord] = []
        self._counter = itertools.count(start=1)
        self._processed_sequence = 0
        self._last_wall_clock_timestamp: datetime | None = None

    @property
    def scenario_name(self) -> str:
        return self._scenario_name

    @property
    def status(self) -> SimulationStatus:
        return self._status

    def register_handler(self, *, event_type: str, handler: SimulationHandler) -> None:
        normalized_event_type = _normalize_non_empty("event_type", event_type)
        self._handlers.setdefault(normalized_event_type, []).append(handler)

    def schedule_event(
        self,
        *,
        event_type: str,
        event_time: datetime,
        source: str,
        payload: dict[str, JsonValue] | None = None,
        metadata: dict[str, JsonValue] | None = None,
        priority: int = 0,
    ) -> None:
        normalized_time = _ensure_utc(event_time)
        if normalized_time < self._start_time:
            raise SimulationConfigurationError("scheduled event time cannot precede scenario start_time")
        event = SimulationEvent(
            event_type=_normalize_non_empty("event_type", event_type),
            event_time=normalized_time,
            source=_normalize_non_empty("source", source),
            payload=payload or {},
            metadata=metadata or {},
        )
        heapq.heappush(
            self._queue,
            _ScheduledEvent(
                scheduled_time=normalized_time,
                priority=priority,
                sequence_number=next(self._counter),
                event=event,
            ),
        )

    async def run(self, *, until_time: datetime | None = None, max_events: int | None = None) -> SimulationScenarioRecord:
        if max_events is not None and max_events <= 0:
            raise SimulationConfigurationError("max_events must be positive when provided")
        context = SimulationContext(
            scenario_name=self._scenario_name,
            simulation_time=self._start_time,
        )
        execution_started_at = _monotonic_now(self._last_wall_clock_timestamp)
        self._status = SimulationStatus.RUNNING
        emitted_events = len(self._queue)
        error_message: str | None = None
        normalized_until_time = _ensure_utc(until_time) if until_time is not None else None
        try:
            while self._queue:
                if max_events is not None and context.processed_events >= max_events:
                    break
                scheduled = heapq.heappop(self._queue)
                if normalized_until_time is not None and scheduled.scheduled_time > normalized_until_time:
                    heapq.heappush(self._queue, scheduled)
                    break
                context.advance_time(scheduled.scheduled_time)
                handler_list = list(self._handlers.get(scheduled.event.event_type, []))
                await self._publish_lifecycle_event(
                    event_type="SIMULATION_EVENT_DISPATCH",
                    payload={
                        "scenario_name": self._scenario_name,
                        "event": scheduled.event.to_dict(),
                        "simulation_time": _ensure_utc(context.simulation_time).isoformat(),
                    },
                    timestamp=context.simulation_time,
                )
                emitted_from_handlers = 0
                for handler in handler_list:
                    result = handler(context, scheduled.event)
                    produced = await result if asyncio.iscoroutine(result) else result
                    if produced is None:
                        continue
                    for new_event in produced:
                        if _ensure_utc(new_event.event_time) < context.simulation_time:
                            raise SimulationExecutionError(
                                "handler emitted event earlier than current simulation time"
                            )
                        self._schedule_emitted_event(new_event)
                        emitted_from_handlers += 1
                        emitted_events += 1
                context.processed_events += 1
                context.emitted_events = emitted_events
                self._processed_sequence += 1
                audit_record = SimulationAuditRecord(
                    sequence_number=self._processed_sequence,
                    event=scheduled.event,
                    processed_at=context.simulation_time,
                    handler_count=len(handler_list),
                    emitted_count=emitted_from_handlers,
                )
                self._audit_records.append(audit_record)
                if len(self._audit_records) > self._max_audit_records:
                    self._audit_records.pop(0)
            self._status = SimulationStatus.COMPLETED
        except Exception as exc:
            self._status = SimulationStatus.FAILED
            error_message = str(exc)
        execution_finished_at = _monotonic_now(execution_started_at)
        self._last_wall_clock_timestamp = execution_finished_at
        scenario_record = SimulationScenarioRecord(
            scenario_name=self._scenario_name,
            status=self._status,
            started_at=execution_started_at,
            finished_at=execution_finished_at,
            processed_events=context.processed_events,
            emitted_events=context.emitted_events,
            final_simulation_time=context.simulation_time,
            audit_records=tuple(self._audit_records),
            error_message=error_message,
        )
        self._execution_history.append(scenario_record)
        await self._publish_lifecycle_event(
            event_type=(
                "SIMULATION_COMPLETED" if self._status is SimulationStatus.COMPLETED else "SIMULATION_FAILED"
            ),
            payload=scenario_record.to_dict(),
            timestamp=context.simulation_time,
        )
        if error_message is not None:
            raise SimulationExecutionError(error_message)
        return scenario_record

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "scenario_name": self._scenario_name,
            "status": self._status.value,
            "start_time": _ensure_utc(self._start_time).isoformat(),
            "latency_milliseconds": int(self._latency.total_seconds() * 1000),
            "queued_events": [item.event.to_dict() for item in sorted(self._queue)],
            "registered_handlers": {
                event_type: len(handlers) for event_type, handlers in sorted(self._handlers.items())
            },
            "audit_records": [item.to_dict() for item in self._audit_records],
            "execution_history": [item.to_dict() for item in self._execution_history],
            "last_wall_clock_timestamp": _optional_iso(self._last_wall_clock_timestamp),
        }

    def _schedule_emitted_event(self, event: SimulationEvent) -> None:
        scheduled_time = _ensure_utc(event.event_time) + self._latency
        heapq.heappush(
            self._queue,
            _ScheduledEvent(
                scheduled_time=scheduled_time,
                priority=0,
                sequence_number=next(self._counter),
                event=SimulationEvent(
                    event_type=event.event_type,
                    event_time=scheduled_time,
                    source=event.source,
                    payload=event.payload,
                    metadata=event.metadata,
                ),
            ),
        )

    async def _publish_lifecycle_event(
        self,
        *,
        event_type: str,
        payload: dict[str, JsonValue],
        timestamp: datetime,
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher(
            event_type=event_type,
            source=f"simulation_framework.{self._scenario_name}",
            payload=payload,
            timestamp=timestamp,
        )



def _normalize_non_empty(field_name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SimulationConfigurationError(f"{field_name} must be non-empty")
    return normalized



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _monotonic_now(reference: datetime | None) -> datetime:
    candidate = _utc_now()
    if reference is None:
        return candidate
    normalized_reference = _ensure_utc(reference)
    normalized_candidate = _ensure_utc(candidate)
    return normalized_candidate if normalized_candidate >= normalized_reference else normalized_reference



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()


__all__ = [
    "SimulationAuditRecord",
    "SimulationConfigurationError",
    "SimulationContext",
    "SimulationEvent",
    "SimulationExecutionError",
    "SimulationFramework",
    "SimulationFrameworkError",
    "SimulationScenarioRecord",
    "SimulationStatus",
]
