# FILE: platform_core/core/events.py
"""
platform_core.core.events
═══════════════════════════════════════════════════════════════════════
Production-grade foundational event system for the institutional derivatives
intelligence and automated execution platform.

Design guarantees preserved:
- Full package imports only.
- Event-driven, async-first, streaming-safe, replay-safe architecture.
- JSON / Redis / PostgreSQL / TimescaleDB compatible event envelopes.
- No future leakage assumptions preserved through explicit event timestamps.
- Correlation and causation identifiers supported for deterministic tracing.
- Modular event-domain compatibility for future split into platform_core/events/*.
- No transport coupling and no business logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from platform_core.core.constants import EVENT_ID_PREFIX, EVENT_SCHEMA_VERSION, UTC
from platform_core.core.enums import EventDomain, EventPriority, EventType


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
EventPayload = dict[str, JsonValue]

PayloadModelT = TypeVar("PayloadModelT", bound="EventPayloadModel")


class EventPayloadModel(BaseModel):
    """
    Base model for strongly typed event payloads.

    Downstream event payload schemas may inherit from this class while keeping
    strict serialization guarantees and immutable validated payload contracts.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        populate_by_name=True,
    )

    def to_payload(self) -> EventPayload:
        """Return a JSON-compatible payload dictionary."""
        return self.model_dump(mode="json")


class EventMetadata(BaseModel):
    """
    Metadata envelope attached to every platform event.

    This model is intentionally transport-agnostic and can be serialized to:
    - JSON for event buses,
    - Redis strings,
    - PostgreSQL JSONB,
    - TimescaleDB JSONB columns,
    - replay artifacts.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        populate_by_name=True,
    )

    event_id: str = Field(default_factory=lambda: f"{EVENT_ID_PREFIX}_{uuid4().hex}")
    schema_version: str = EVENT_SCHEMA_VERSION
    correlation_id: str = Field(default_factory=lambda: uuid4().hex)
    causation_id: str | None = None
    producer: str
    partition_key: str | None = None
    replay: bool = False
    sequence_number: int | None = None
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("event_id", "correlation_id", "producer")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must be non-empty")
        return cleaned

    @field_validator("sequence_number")
    @classmethod
    def non_negative_sequence(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("sequence_number must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_temporal_order(self) -> "EventMetadata":
        if self.observed_at < self.emitted_at:
            raise ValueError("observed_at cannot precede emitted_at")
        return self

    def to_safe_dict(self) -> dict[str, JsonValue]:
        """Return JSON-compatible metadata."""
        return self.model_dump(mode="json")


class EventEnvelope(BaseModel):
    """
    Canonical foundational event envelope.

    The envelope is the only cross-service contract required in Phase 9A.
    It is purposely transport-agnostic and replay-compatible.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        populate_by_name=True,
    )

    domain: EventDomain
    event_type: EventType
    priority: EventPriority = EventPriority.NORMAL
    metadata: EventMetadata
    payload: EventPayload

    @field_validator("payload")
    @classmethod
    def validate_payload_is_mapping(cls, value: EventPayload) -> EventPayload:
        if not isinstance(value, dict):
            raise ValueError("payload must be a dictionary")
        return value

    def to_json_dict(self) -> dict[str, JsonValue]:
        """Return a JSON-compatible representation of the full event envelope."""
        return self.model_dump(mode="json")

    def topic_name(self) -> str:
        """Return the canonical topic token used by future event-bus adapters."""
        return f"{self.domain.value}.{self.event_type.value}"

    def with_payload_update(self, **payload_updates: JsonValue) -> "EventEnvelope":
        """Return a new envelope with updated payload values."""
        updated_payload = dict(self.payload)
        updated_payload.update(payload_updates)
        return self.model_copy(update={"payload": updated_payload})


def build_event_metadata(
    *,
    producer: str,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    partition_key: str | None = None,
    replay: bool = False,
    sequence_number: int | None = None,
    emitted_at: datetime | None = None,
    observed_at: datetime | None = None,
    tags: Mapping[str, str] | None = None,
) -> EventMetadata:
    """Create validated event metadata with sane defaults."""
    effective_emitted_at = emitted_at or datetime.now(UTC)
    effective_observed_at = observed_at or effective_emitted_at
    return EventMetadata(
        producer=producer,
        correlation_id=correlation_id or uuid4().hex,
        causation_id=causation_id,
        partition_key=partition_key,
        replay=replay,
        sequence_number=sequence_number,
        emitted_at=effective_emitted_at,
        observed_at=effective_observed_at,
        tags=dict(tags or {}),
    )


def build_event_envelope(
    *,
    domain: EventDomain,
    event_type: EventType,
    producer: str,
    payload: Mapping[str, JsonValue] | EventPayloadModel,
    priority: EventPriority = EventPriority.NORMAL,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    partition_key: str | None = None,
    replay: bool = False,
    sequence_number: int | None = None,
    emitted_at: datetime | None = None,
    observed_at: datetime | None = None,
    tags: Mapping[str, str] | None = None,
) -> EventEnvelope:
    """
    Build a validated event envelope from a payload mapping or payload model.
    """
    normalized_payload: EventPayload
    if isinstance(payload, EventPayloadModel):
        normalized_payload = payload.to_payload()
    else:
        normalized_payload = dict(payload)

    metadata = build_event_metadata(
        producer=producer,
        correlation_id=correlation_id,
        causation_id=causation_id,
        partition_key=partition_key,
        replay=replay,
        sequence_number=sequence_number,
        emitted_at=emitted_at,
        observed_at=observed_at,
        tags=tags,
    )
    return EventEnvelope(
        domain=domain,
        event_type=event_type,
        priority=priority,
        metadata=metadata,
        payload=normalized_payload,
    )


def clone_event_envelope(
    envelope: EventEnvelope,
    *,
    producer: str | None = None,
    event_type: EventType | None = None,
    domain: EventDomain | None = None,
    priority: EventPriority | None = None,
    payload_updates: Mapping[str, JsonValue] | None = None,
    causation_id: str | None = None,
    emitted_at: datetime | None = None,
    observed_at: datetime | None = None,
    replay: bool | None = None,
    sequence_number: int | None = None,
    tags: Mapping[str, str] | None = None,
) -> EventEnvelope:
    """
    Create a new event derived from an existing envelope.

    Correlation id is preserved by default. Event id is regenerated. The source
    event id becomes the default causation id unless an explicit value is passed.
    """
    merged_tags = dict(envelope.metadata.tags)
    if tags:
        merged_tags.update(tags)

    updated_payload = dict(envelope.payload)
    if payload_updates:
        updated_payload.update(dict(payload_updates))

    return build_event_envelope(
        domain=domain or envelope.domain,
        event_type=event_type or envelope.event_type,
        producer=producer or envelope.metadata.producer,
        payload=updated_payload,
        priority=priority or envelope.priority,
        correlation_id=envelope.metadata.correlation_id,
        causation_id=causation_id or envelope.metadata.event_id,
        partition_key=envelope.metadata.partition_key,
        replay=envelope.metadata.replay if replay is None else replay,
        sequence_number=sequence_number,
        emitted_at=emitted_at,
        observed_at=observed_at,
        tags=merged_tags,
    )


def event_from_dict(data: Mapping[str, Any]) -> EventEnvelope:
    """Validate and reconstruct an event envelope from a mapping."""
    return EventEnvelope.model_validate(dict(data))


def payload_model_from_dict(
    model_type: type[PayloadModelT],
    data: Mapping[str, Any],
) -> PayloadModelT:
    """Validate and reconstruct a typed payload model from a mapping."""
    return model_type.model_validate(dict(data))


def is_replay_event(envelope: EventEnvelope) -> bool:
    """Return true if the envelope is flagged as replay-originated."""
    return envelope.metadata.replay


def is_high_priority_event(envelope: EventEnvelope) -> bool:
    """Return true for HIGH or CRITICAL priority events."""
    return envelope.priority in {EventPriority.HIGH, EventPriority.CRITICAL}


__all__ = [
    "EventEnvelope",
    "EventMetadata",
    "EventPayload",
    "EventPayloadModel",
    "JsonScalar",
    "JsonValue",
    "PayloadModelT",
    "build_event_envelope",
    "build_event_metadata",
    "clone_event_envelope",
    "event_from_dict",
    "is_high_priority_event",
    "is_replay_event",
    "payload_model_from_dict",
]
