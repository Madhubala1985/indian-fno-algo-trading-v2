# FILE: platform_core/data/replay_engine.py
"""
platform_core.data.replay_engine
═══════════════════════════════════════════════════════════════════════
Production-grade deterministic replay engine for Indian Index Futures &
Options market-data workflows.

This module replays historical events in causal order with configurable pacing,
replay-speed scaling, bounded asynchronous fan-out, deterministic sequencing,
and idempotent delivery metadata suitable for storage, analytics, orchestration,
and strategy validation pipelines.
"""
from __future__ import annotations

import asyncio
import heapq
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
ReplayHandler = Callable[["ReplayEnvelope"], Awaitable[None] | None]


class ReplayMode(str, Enum):
    """Replay pacing behavior."""

    IMMEDIATE = "immediate"
    REALTIME = "realtime"
    ACCELERATED = "accelerated"


class ReplayLifecycleState(str, Enum):
    """Replay engine lifecycle state."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"


class ReplayEngineError(RuntimeError):
    """Base exception for replay engine failures."""


class ReplayConfigurationError(ReplayEngineError):
    """Raised when replay configuration is invalid."""


class ReplayOrderingError(ReplayEngineError):
    """Raised when replay input violates deterministic ordering requirements."""


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """Canonical replay event."""

    event_id: str
    event_timestamp: datetime
    event_type: str
    source: str
    payload: dict[str, JsonValue]
    sequence_key: str | None = None
    sequence_number: int | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must be non-empty")
        if not self.event_type.strip():
            raise ValueError("event_type must be non-empty")
        if not self.source.strip():
            raise ValueError("source must be non-empty")

    @property
    def event_timestamp_utc(self) -> datetime:
        return _ensure_utc(self.event_timestamp)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "event_timestamp": self.event_timestamp_utc.isoformat(),
            "event_type": self.event_type,
            "source": self.source,
            "payload": self.payload,
            "sequence_key": self.sequence_key,
            "sequence_number": self.sequence_number,
        }


@dataclass(frozen=True, slots=True)
class ReplayEnvelope:
    """Delivery envelope emitted by the replay engine."""

    replay_id: str
    event: ReplayEvent
    replay_timestamp: datetime
    delivery_index: int
    relative_offset_seconds: float
    speed_multiplier: float
    is_replay: bool = True

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "replay_id": self.replay_id,
            "event": self.event.to_dict(),
            "replay_timestamp": self.replay_timestamp.isoformat(),
            "delivery_index": self.delivery_index,
            "relative_offset_seconds": self.relative_offset_seconds,
            "speed_multiplier": self.speed_multiplier,
            "is_replay": self.is_replay,
        }


@dataclass(frozen=True, slots=True)
class ReplayStats:
    """Operational replay metrics."""

    emitted_events: int = 0
    handler_invocations: int = 0
    dropped_events: int = 0
    paused_count: int = 0
    replay_started_timestamp: datetime | None = None
    replay_completed_timestamp: datetime | None = None
    last_emitted_event_timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Replay engine operational configuration."""

    replay_id: str
    mode: ReplayMode = ReplayMode.REALTIME
    speed_multiplier: float = 1.0
    max_queue_size: int = 10_000
    sleep_resolution_seconds: float = 0.001
    enforce_global_ordering: bool = True
    enforce_per_sequence_monotonicity: bool = True
    emit_clock_events: bool = False
    clock_event_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.replay_id.strip():
            raise ReplayConfigurationError("replay_id must be non-empty")
        if self.speed_multiplier <= 0:
            raise ReplayConfigurationError("speed_multiplier must be positive")
        if self.max_queue_size <= 0:
            raise ReplayConfigurationError("max_queue_size must be positive")
        if self.sleep_resolution_seconds <= 0:
            raise ReplayConfigurationError("sleep_resolution_seconds must be positive")
        if self.emit_clock_events:
            if self.clock_event_interval_seconds is None or self.clock_event_interval_seconds <= 0:
                raise ReplayConfigurationError(
                    "clock_event_interval_seconds must be positive when emit_clock_events is enabled"
                )


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    """Serializable checkpoint for deterministic resume."""

    replay_id: str
    delivery_index: int
    last_event_id: str | None
    last_event_timestamp: datetime | None
    lifecycle_state: ReplayLifecycleState

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "replay_id": self.replay_id,
            "delivery_index": self.delivery_index,
            "last_event_id": self.last_event_id,
            "last_event_timestamp": self.last_event_timestamp.isoformat() if self.last_event_timestamp else None,
            "lifecycle_state": self.lifecycle_state.value,
        }


@dataclass(order=True, slots=True)
class _ScheduledReplayItem:
    """Internal heap item for deterministic replay scheduling."""

    sort_key: tuple[float, int, str] = field(init=False, repr=False)
    event_timestamp_unix: float
    delivery_index: int
    event_id: str
    event: ReplayEvent = field(compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sort_key",
            (self.event_timestamp_unix, self.delivery_index, self.event_id),
        )


class ReplayEventSource:
    """Async-capable replay event source wrapper."""

    def __init__(self, events: Sequence[ReplayEvent] | AsyncIterator[ReplayEvent]) -> None:
        self._sequence = events if not hasattr(events, "__anext__") else None
        self._async_iterator = events if hasattr(events, "__anext__") else None

    async def load(self) -> list[ReplayEvent]:
        if self._sequence is not None:
            return list(self._sequence)
        assert self._async_iterator is not None
        loaded: list[ReplayEvent] = []
        async for event in self._async_iterator:
            loaded.append(event)
        return loaded


class ReplayEngine:
    """Deterministic asynchronous replay engine."""

    def __init__(
        self,
        *,
        config: ReplayConfig,
        source: ReplayEventSource,
        handlers: Sequence[ReplayHandler] = (),
    ) -> None:
        self._config = config
        self._source = source
        self._handlers = tuple(handlers)
        self._state = ReplayLifecycleState.IDLE
        self._stats = ReplayStats()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_requested = False
        self._checkpoint = ReplayCheckpoint(
            replay_id=config.replay_id,
            delivery_index=0,
            last_event_id=None,
            last_event_timestamp=None,
            lifecycle_state=ReplayLifecycleState.IDLE,
        )

    @property
    def state(self) -> ReplayLifecycleState:
        return self._state

    @property
    def stats(self) -> ReplayStats:
        return self._stats

    @property
    def checkpoint(self) -> ReplayCheckpoint:
        return self._checkpoint

    async def run(self) -> None:
        if self._state == ReplayLifecycleState.RUNNING:
            raise ReplayEngineError("replay engine is already running")
        self._state = ReplayLifecycleState.RUNNING
        self._stop_requested = False
        self._pause_event.set()
        events = await self._source.load()
        ordered_events = self._prepare_events(events)
        self._stats = ReplayStats(
            emitted_events=self._stats.emitted_events,
            handler_invocations=self._stats.handler_invocations,
            dropped_events=self._stats.dropped_events,
            paused_count=self._stats.paused_count,
            replay_started_timestamp=_utc_now(),
            replay_completed_timestamp=None,
            last_emitted_event_timestamp=self._stats.last_emitted_event_timestamp,
        )
        if not ordered_events:
            self._state = ReplayLifecycleState.COMPLETED
            self._checkpoint = ReplayCheckpoint(
                replay_id=self._config.replay_id,
                delivery_index=0,
                last_event_id=None,
                last_event_timestamp=None,
                lifecycle_state=self._state,
            )
            self._stats = ReplayStats(
                emitted_events=self._stats.emitted_events,
                handler_invocations=self._stats.handler_invocations,
                dropped_events=self._stats.dropped_events,
                paused_count=self._stats.paused_count,
                replay_started_timestamp=self._stats.replay_started_timestamp,
                replay_completed_timestamp=_utc_now(),
                last_emitted_event_timestamp=self._stats.last_emitted_event_timestamp,
            )
            return
        scheduled = self._build_schedule(ordered_events)
        start_monotonic = time.monotonic()
        base_timestamp = ordered_events[0].event_timestamp_utc
        next_clock_offset = self._config.clock_event_interval_seconds if self._config.emit_clock_events else None
        while scheduled and not self._stop_requested:
            await self._pause_event.wait()
            current_item = heapq.heappop(scheduled)
            event = current_item.event
            event_offset_seconds = (event.event_timestamp_utc - base_timestamp).total_seconds()
            if self._config.mode != ReplayMode.IMMEDIATE:
                target_elapsed = event_offset_seconds / self._config.speed_multiplier
                await self._sleep_until(start_monotonic, target_elapsed)
            while next_clock_offset is not None and event_offset_seconds >= next_clock_offset and not self._stop_requested:
                await self._emit_clock_event(relative_offset_seconds=next_clock_offset)
                next_clock_offset += self._config.clock_event_interval_seconds or 0.0
            envelope = ReplayEnvelope(
                replay_id=self._config.replay_id,
                event=event,
                replay_timestamp=_utc_now(),
                delivery_index=current_item.delivery_index,
                relative_offset_seconds=event_offset_seconds,
                speed_multiplier=self._config.speed_multiplier,
            )
            await self._dispatch(envelope)
            self._checkpoint = ReplayCheckpoint(
                replay_id=self._config.replay_id,
                delivery_index=current_item.delivery_index,
                last_event_id=event.event_id,
                last_event_timestamp=event.event_timestamp_utc,
                lifecycle_state=self._state,
            )
            self._stats = ReplayStats(
                emitted_events=self._stats.emitted_events + 1,
                handler_invocations=self._stats.handler_invocations,
                dropped_events=self._stats.dropped_events,
                paused_count=self._stats.paused_count,
                replay_started_timestamp=self._stats.replay_started_timestamp,
                replay_completed_timestamp=None,
                last_emitted_event_timestamp=event.event_timestamp_utc,
            )
        self._state = ReplayLifecycleState.STOPPED if self._stop_requested else ReplayLifecycleState.COMPLETED
        self._checkpoint = ReplayCheckpoint(
            replay_id=self._config.replay_id,
            delivery_index=self._checkpoint.delivery_index,
            last_event_id=self._checkpoint.last_event_id,
            last_event_timestamp=self._checkpoint.last_event_timestamp,
            lifecycle_state=self._state,
        )
        self._stats = ReplayStats(
            emitted_events=self._stats.emitted_events,
            handler_invocations=self._stats.handler_invocations,
            dropped_events=self._stats.dropped_events,
            paused_count=self._stats.paused_count,
            replay_started_timestamp=self._stats.replay_started_timestamp,
            replay_completed_timestamp=_utc_now(),
            last_emitted_event_timestamp=self._stats.last_emitted_event_timestamp,
        )

    async def pause(self) -> None:
        if self._state != ReplayLifecycleState.RUNNING:
            return
        self._state = ReplayLifecycleState.PAUSED
        self._pause_event.clear()
        self._stats = ReplayStats(
            emitted_events=self._stats.emitted_events,
            handler_invocations=self._stats.handler_invocations,
            dropped_events=self._stats.dropped_events,
            paused_count=self._stats.paused_count + 1,
            replay_started_timestamp=self._stats.replay_started_timestamp,
            replay_completed_timestamp=self._stats.replay_completed_timestamp,
            last_emitted_event_timestamp=self._stats.last_emitted_event_timestamp,
        )
        self._checkpoint = ReplayCheckpoint(
            replay_id=self._checkpoint.replay_id,
            delivery_index=self._checkpoint.delivery_index,
            last_event_id=self._checkpoint.last_event_id,
            last_event_timestamp=self._checkpoint.last_event_timestamp,
            lifecycle_state=self._state,
        )

    async def resume(self) -> None:
        if self._state != ReplayLifecycleState.PAUSED:
            return
        self._state = ReplayLifecycleState.RUNNING
        self._pause_event.set()
        self._checkpoint = ReplayCheckpoint(
            replay_id=self._checkpoint.replay_id,
            delivery_index=self._checkpoint.delivery_index,
            last_event_id=self._checkpoint.last_event_id,
            last_event_timestamp=self._checkpoint.last_event_timestamp,
            lifecycle_state=self._state,
        )

    async def stop(self) -> None:
        self._stop_requested = True
        self._pause_event.set()

    async def preview(self) -> list[ReplayEvent]:
        events = await self._source.load()
        return self._prepare_events(events)

    async def stream(self) -> AsyncIterator[ReplayEnvelope]:
        events = await self._source.load()
        ordered_events = self._prepare_events(events)
        if not ordered_events:
            return
        base_timestamp = ordered_events[0].event_timestamp_utc
        for index, event in enumerate(ordered_events, start=1):
            yield ReplayEnvelope(
                replay_id=self._config.replay_id,
                event=event,
                replay_timestamp=_utc_now(),
                delivery_index=index,
                relative_offset_seconds=(event.event_timestamp_utc - base_timestamp).total_seconds(),
                speed_multiplier=self._config.speed_multiplier,
            )

    def _prepare_events(self, events: Sequence[ReplayEvent]) -> list[ReplayEvent]:
        if not events:
            return []
        normalized = [
            ReplayEvent(
                event_id=event.event_id,
                event_timestamp=event.event_timestamp_utc,
                event_type=event.event_type,
                source=event.source,
                payload=dict(event.payload),
                sequence_key=event.sequence_key,
                sequence_number=event.sequence_number,
            )
            for event in events
        ]
        if self._config.enforce_global_ordering:
            normalized = sorted(
                normalized,
                key=lambda item: (
                    item.event_timestamp_utc,
                    item.sequence_number if item.sequence_number is not None else math.inf,
                    item.event_id,
                ),
            )
        self._validate_ordering(normalized)
        return normalized

    def _validate_ordering(self, events: Sequence[ReplayEvent]) -> None:
        previous_timestamp: datetime | None = None
        sequence_timestamps: dict[str, datetime] = {}
        sequence_numbers: dict[str, int] = {}
        for event in events:
            current_timestamp = event.event_timestamp_utc
            if previous_timestamp is not None and current_timestamp < previous_timestamp:
                raise ReplayOrderingError("global event timestamps are not monotonic")
            previous_timestamp = current_timestamp
            if self._config.enforce_per_sequence_monotonicity and event.sequence_key is not None:
                last_timestamp = sequence_timestamps.get(event.sequence_key)
                if last_timestamp is not None and current_timestamp < last_timestamp:
                    raise ReplayOrderingError("per-sequence event timestamps are not monotonic")
                sequence_timestamps[event.sequence_key] = current_timestamp
                if event.sequence_number is not None:
                    last_sequence_number = sequence_numbers.get(event.sequence_key)
                    if last_sequence_number is not None and event.sequence_number < last_sequence_number:
                        raise ReplayOrderingError("per-sequence event numbers are not monotonic")
                    sequence_numbers[event.sequence_key] = event.sequence_number

    def _build_schedule(self, events: Sequence[ReplayEvent]) -> list[_ScheduledReplayItem]:
        schedule: list[_ScheduledReplayItem] = []
        for index, event in enumerate(events, start=1):
            schedule.append(
                _ScheduledReplayItem(
                    event_timestamp_unix=event.event_timestamp_utc.timestamp(),
                    delivery_index=index,
                    event_id=event.event_id,
                    event=event,
                )
            )
        heapq.heapify(schedule)
        return schedule

    async def _sleep_until(self, start_monotonic: float, target_elapsed_seconds: float) -> None:
        while True:
            current_elapsed = time.monotonic() - start_monotonic
            remaining = target_elapsed_seconds - current_elapsed
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, self._config.sleep_resolution_seconds))

    async def _dispatch(self, envelope: ReplayEnvelope) -> None:
        if not self._handlers:
            return
        tasks: list[Awaitable[None]] = []
        for handler in self._handlers:
            result = handler(envelope)
            if asyncio.iscoroutine(result):
                tasks.append(result)
        if tasks:
            await asyncio.gather(*tasks)
        self._stats = ReplayStats(
            emitted_events=self._stats.emitted_events,
            handler_invocations=self._stats.handler_invocations + len(self._handlers),
            dropped_events=self._stats.dropped_events,
            paused_count=self._stats.paused_count,
            replay_started_timestamp=self._stats.replay_started_timestamp,
            replay_completed_timestamp=self._stats.replay_completed_timestamp,
            last_emitted_event_timestamp=self._stats.last_emitted_event_timestamp,
        )

    async def _emit_clock_event(self, *, relative_offset_seconds: float) -> None:
        clock_event = ReplayEvent(
            event_id=f"{self._config.replay_id}:clock:{relative_offset_seconds:.6f}",
            event_timestamp=_utc_now(),
            event_type="replay_clock",
            source="replay_engine",
            payload={
                "relative_offset_seconds": relative_offset_seconds,
                "replay_id": self._config.replay_id,
            },
            sequence_key="replay_clock",
            sequence_number=None,
        )
        envelope = ReplayEnvelope(
            replay_id=self._config.replay_id,
            event=clock_event,
            replay_timestamp=_utc_now(),
            delivery_index=self._checkpoint.delivery_index,
            relative_offset_seconds=relative_offset_seconds,
            speed_multiplier=self._config.speed_multiplier,
        )
        await self._dispatch(envelope)


class ReplayCollector:
    """Utility handler that collects emitted replay envelopes."""

    def __init__(self) -> None:
        self._envelopes: list[ReplayEnvelope] = []

    @property
    def envelopes(self) -> tuple[ReplayEnvelope, ...]:
        return tuple(self._envelopes)

    async def __call__(self, envelope: ReplayEnvelope) -> None:
        self._envelopes.append(envelope)


class ReplayEventBuilder:
    """Factory helpers for building canonical replay events from mappings."""

    @staticmethod
    def from_mapping(payload: Mapping[str, Any]) -> ReplayEvent:
        event_id = str(payload["event_id"])
        event_type = str(payload["event_type"])
        source = str(payload["source"])
        event_timestamp = _coerce_datetime(payload["event_timestamp"])
        if event_timestamp is None:
            raise ReplayConfigurationError("event_timestamp could not be coerced to datetime")
        raw_payload = payload.get("payload", {})
        if not isinstance(raw_payload, Mapping):
            raise ReplayConfigurationError("payload field must be a mapping")
        sequence_key = payload.get("sequence_key")
        raw_sequence_number = payload.get("sequence_number")
        sequence_number = int(raw_sequence_number) if raw_sequence_number is not None else None
        return ReplayEvent(
            event_id=event_id,
            event_timestamp=event_timestamp,
            event_type=event_type,
            source=source,
            payload=_json_safe_mapping(raw_payload),
            sequence_key=str(sequence_key) if sequence_key is not None else None,
            sequence_number=sequence_number,
        )

    @staticmethod
    def from_mappings(payloads: Iterable[Mapping[str, Any]]) -> list[ReplayEvent]:
        return [ReplayEventBuilder.from_mapping(payload) for payload in payloads]



def _json_safe_mapping(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {str(key): _json_safe_value(value) for key, value in payload.items()}



def _json_safe_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe_value(item) for item in value]
    return str(value)



def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "JsonPrimitive",
    "JsonValue",
    "ReplayCheckpoint",
    "ReplayCollector",
    "ReplayConfig",
    "ReplayEngine",
    "ReplayEngineError",
    "ReplayEnvelope",
    "ReplayEvent",
    "ReplayEventBuilder",
    "ReplayEventSource",
    "ReplayHandler",
    "ReplayLifecycleState",
    "ReplayMode",
    "ReplayOrderingError",
    "ReplayConfigurationError",
    "ReplayStats",
]
