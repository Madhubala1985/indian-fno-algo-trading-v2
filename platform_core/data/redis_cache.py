# FILE: platform_core/data/redis_cache.py
"""
platform_core.data.redis_cache
═══════════════════════════════════════════════════════════════════════
Production-grade async Redis cache foundation for Indian Index Futures &
Options market data workflows.

This module is designed to be:
- async-first,
- snapshot-compatible,
- pubsub-compatible,
- stream-compatible,
- replay-compatible,
- serialization-safe,
- backend-agnostic,
- dependency-clean.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


class RedisCacheError(RuntimeError):
    """Base exception for Redis cache failures."""


class RedisCacheSerializationError(RedisCacheError):
    """Raised when a value cannot be serialized or deserialized safely."""


class RedisCacheConnectionError(RedisCacheError):
    """Raised when the Redis backend is unavailable."""


@dataclass(frozen=True, slots=True)
class RedisCacheConfig:
    """Operational configuration for Redis cache usage."""

    key_prefix: str = "platform_core"
    default_ttl_seconds: int | None = 300
    command_timeout_seconds: float = 2.0
    publish_timeout_seconds: float = 1.0
    stream_maxlen: int | None = 100_000
    enable_pubsub: bool = True
    enable_streams: bool = True

    def __post_init__(self) -> None:
        if not self.key_prefix.strip():
            raise ValueError("key_prefix must be non-empty")
        if self.default_ttl_seconds is not None and self.default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive when provided")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if self.publish_timeout_seconds <= 0:
            raise ValueError("publish_timeout_seconds must be positive")
        if self.stream_maxlen is not None and self.stream_maxlen <= 0:
            raise ValueError("stream_maxlen must be positive when provided")


@dataclass(frozen=True, slots=True)
class RedisCacheStats:
    """Operational cache metrics."""

    set_operations: int = 0
    get_operations: int = 0
    delete_operations: int = 0
    publish_operations: int = 0
    stream_append_operations: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    last_error_timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class CacheMessage:
    """Canonical pubsub or stream message payload."""

    channel: str
    message_type: str
    payload: dict[str, JsonValue]
    published_timestamp: datetime
    source: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "channel": self.channel,
            "message_type": self.message_type,
            "payload": self.payload,
            "published_timestamp": self.published_timestamp.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """Canonical stream entry for durable event fan-out."""

    stream: str
    entry_id: str
    payload: dict[str, JsonValue]
    published_timestamp: datetime


class RedisBackend(ABC):
    """Abstract async Redis backend contract."""

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Set a Redis key."""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Get a Redis key."""

    @abstractmethod
    async def delete(self, key: str) -> int:
        """Delete a Redis key and return deletion count."""

    @abstractmethod
    async def publish(self, channel: str, message: str) -> int:
        """Publish a message to a Redis pubsub channel."""

    @abstractmethod
    async def xadd(self, stream: str, fields: Mapping[str, str], maxlen: int | None = None) -> str:
        """Append a message to a Redis stream and return the entry id."""

    @abstractmethod
    async def xread(self, streams: Mapping[str, str], block_milliseconds: int | None = None, count: int | None = None) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        """Read from Redis streams."""


class InMemoryRedisBackend(RedisBackend):
    """Deterministic in-memory backend for testing and replay-safe validation."""

    def __init__(self) -> None:
        self._kv_store: dict[str, tuple[str, datetime | None]] = {}
        self._pubsub_messages: dict[str, list[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._stream_sequence = 0

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        expiry = None
        if ttl_seconds is not None:
            expiry = _utc_now() + timedelta(seconds=ttl_seconds)
        self._kv_store[key] = (value, expiry)

    async def get(self, key: str) -> str | None:
        item = self._kv_store.get(key)
        if item is None:
            return None
        value, expiry = item
        if expiry is not None and _utc_now() >= expiry:
            del self._kv_store[key]
            return None
        return value

    async def delete(self, key: str) -> int:
        if key in self._kv_store:
            del self._kv_store[key]
            return 1
        return 0

    async def publish(self, channel: str, message: str) -> int:
        self._pubsub_messages.setdefault(channel, []).append(message)
        return 1

    async def xadd(self, stream: str, fields: Mapping[str, str], maxlen: int | None = None) -> str:
        self._stream_sequence += 1
        entry_id = f"{int(_utc_now().timestamp() * 1000)}-{self._stream_sequence}"
        self._streams.setdefault(stream, []).append((entry_id, dict(fields)))
        if maxlen is not None and len(self._streams[stream]) > maxlen:
            self._streams[stream] = self._streams[stream][-maxlen:]
        return entry_id

    async def xread(self, streams: Mapping[str, str], block_milliseconds: int | None = None, count: int | None = None) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream_name, last_seen_id in streams.items():
            stream_entries = self._streams.get(stream_name, [])
            selected = [entry for entry in stream_entries if _compare_stream_ids(entry[0], last_seen_id) > 0]
            if count is not None:
                selected = selected[:count]
            if selected:
                results.append((stream_name, selected))
        if results:
            return results
        if block_milliseconds is not None and block_milliseconds > 0:
            await asyncio.sleep(block_milliseconds / 1000.0)
        return []

    def snapshot_messages(self, channel: str) -> tuple[str, ...]:
        return tuple(self._pubsub_messages.get(channel, []))


class RedisKeyBuilder:
    """Centralized typed key construction for Redis access."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix.strip()

    def build(self, *parts: str) -> str:
        normalized = [self._prefix]
        for part in parts:
            text = part.strip()
            if not text:
                raise ValueError("Redis key parts must be non-empty")
            normalized.append(text.replace(" ", "_"))
        return ":".join(normalized)


class RedisJsonCodec:
    """Deterministic JSON codec for Redis values and messages."""

    @staticmethod
    def dumps(value: Mapping[str, JsonValue]) -> str:
        try:
            return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise RedisCacheSerializationError("failed to serialize Redis payload") from exc

    @staticmethod
    def loads(raw_value: str) -> dict[str, JsonValue]:
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RedisCacheSerializationError("failed to deserialize Redis payload") from exc
        if not isinstance(decoded, dict):
            raise RedisCacheSerializationError("Redis payload must decode to a dictionary")
        return decoded


class RedisCacheRepository:
    """Typed Redis repository for snapshot state, pubsub, and streams."""

    def __init__(
        self,
        *,
        backend: RedisBackend,
        config: RedisCacheConfig,
        source_name: str,
    ) -> None:
        if not source_name.strip():
            raise ValueError("source_name must be non-empty")
        self._backend = backend
        self._config = config
        self._source_name = source_name.strip()
        self._keys = RedisKeyBuilder(config.key_prefix)
        self._stats = RedisCacheStats()

    @property
    def stats(self) -> RedisCacheStats:
        return self._stats

    async def set_snapshot(
        self,
        *,
        namespace: str,
        entity_id: str,
        value: Mapping[str, JsonValue],
        ttl_seconds: int | None = None,
    ) -> str:
        key = self._keys.build("snapshot", namespace, entity_id)
        payload = RedisJsonCodec.dumps(dict(value))
        try:
            await asyncio.wait_for(
                self._backend.set(key, payload, ttl_seconds or self._config.default_ttl_seconds),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to set Redis snapshot") from exc
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations + 1,
            get_operations=self._stats.get_operations,
            delete_operations=self._stats.delete_operations,
            publish_operations=self._stats.publish_operations,
            stream_append_operations=self._stats.stream_append_operations,
            cache_hits=self._stats.cache_hits,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return key

    async def get_snapshot(self, *, namespace: str, entity_id: str) -> dict[str, JsonValue] | None:
        key = self._keys.build("snapshot", namespace, entity_id)
        try:
            raw_value = await asyncio.wait_for(
                self._backend.get(key),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to get Redis snapshot") from exc
        if raw_value is None:
            self._stats = RedisCacheStats(
                set_operations=self._stats.set_operations,
                get_operations=self._stats.get_operations + 1,
                delete_operations=self._stats.delete_operations,
                publish_operations=self._stats.publish_operations,
                stream_append_operations=self._stats.stream_append_operations,
                cache_hits=self._stats.cache_hits,
                cache_misses=self._stats.cache_misses + 1,
                last_error_timestamp=self._stats.last_error_timestamp,
            )
            return None
        decoded = RedisJsonCodec.loads(raw_value)
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations,
            get_operations=self._stats.get_operations + 1,
            delete_operations=self._stats.delete_operations,
            publish_operations=self._stats.publish_operations,
            stream_append_operations=self._stats.stream_append_operations,
            cache_hits=self._stats.cache_hits + 1,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return decoded

    async def delete_snapshot(self, *, namespace: str, entity_id: str) -> int:
        key = self._keys.build("snapshot", namespace, entity_id)
        try:
            deleted = await asyncio.wait_for(
                self._backend.delete(key),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to delete Redis snapshot") from exc
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations,
            get_operations=self._stats.get_operations,
            delete_operations=self._stats.delete_operations + 1,
            publish_operations=self._stats.publish_operations,
            stream_append_operations=self._stats.stream_append_operations,
            cache_hits=self._stats.cache_hits,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return deleted

    async def publish_message(
        self,
        *,
        channel: str,
        message_type: str,
        payload: Mapping[str, JsonValue],
    ) -> CacheMessage:
        if not self._config.enable_pubsub:
            raise RedisCacheError("pubsub is disabled in RedisCacheConfig")
        message = CacheMessage(
            channel=self._keys.build("channel", channel),
            message_type=message_type.strip(),
            payload=dict(payload),
            published_timestamp=_utc_now(),
            source=self._source_name,
        )
        if not message.message_type:
            raise ValueError("message_type must be non-empty")
        encoded = RedisJsonCodec.dumps(message.to_dict())
        try:
            await asyncio.wait_for(
                self._backend.publish(message.channel, encoded),
                timeout=self._config.publish_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to publish Redis message") from exc
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations,
            get_operations=self._stats.get_operations,
            delete_operations=self._stats.delete_operations,
            publish_operations=self._stats.publish_operations + 1,
            stream_append_operations=self._stats.stream_append_operations,
            cache_hits=self._stats.cache_hits,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return message

    async def append_stream_event(
        self,
        *,
        stream_name: str,
        payload: Mapping[str, JsonValue],
    ) -> StreamEntry:
        if not self._config.enable_streams:
            raise RedisCacheError("streams are disabled in RedisCacheConfig")
        stream_key = self._keys.build("stream", stream_name)
        published_timestamp = _utc_now()
        encoded_fields = {
            "payload": RedisJsonCodec.dumps(dict(payload)),
            "published_timestamp": published_timestamp.isoformat(),
            "source": self._source_name,
        }
        try:
            entry_id = await asyncio.wait_for(
                self._backend.xadd(stream_key, encoded_fields, maxlen=self._config.stream_maxlen),
                timeout=self._config.command_timeout_seconds,
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to append Redis stream event") from exc
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations,
            get_operations=self._stats.get_operations,
            delete_operations=self._stats.delete_operations,
            publish_operations=self._stats.publish_operations,
            stream_append_operations=self._stats.stream_append_operations + 1,
            cache_hits=self._stats.cache_hits,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=self._stats.last_error_timestamp,
        )
        return StreamEntry(
            stream=stream_key,
            entry_id=entry_id,
            payload=dict(payload),
            published_timestamp=published_timestamp,
        )

    async def read_stream(
        self,
        *,
        stream_name: str,
        last_seen_id: str = "0-0",
        count: int | None = None,
        block_milliseconds: int | None = None,
    ) -> list[StreamEntry]:
        stream_key = self._keys.build("stream", stream_name)
        try:
            rows = await asyncio.wait_for(
                self._backend.xread({stream_key: last_seen_id}, block_milliseconds=block_milliseconds, count=count),
                timeout=self._config.command_timeout_seconds + ((block_milliseconds or 0) / 1000.0),
            )
        except Exception as exc:
            self._mark_error()
            raise RedisCacheConnectionError("failed to read Redis stream") from exc
        entries: list[StreamEntry] = []
        for stream, stream_rows in rows:
            for entry_id, fields in stream_rows:
                payload_raw = fields.get("payload", "{}")
                timestamp_raw = fields.get("published_timestamp")
                entries.append(
                    StreamEntry(
                        stream=stream,
                        entry_id=entry_id,
                        payload=RedisJsonCodec.loads(payload_raw),
                        published_timestamp=_coerce_datetime(timestamp_raw) or _utc_now(),
                    )
                )
        return entries

    def _mark_error(self) -> None:
        self._stats = RedisCacheStats(
            set_operations=self._stats.set_operations,
            get_operations=self._stats.get_operations,
            delete_operations=self._stats.delete_operations,
            publish_operations=self._stats.publish_operations,
            stream_append_operations=self._stats.stream_append_operations,
            cache_hits=self._stats.cache_hits,
            cache_misses=self._stats.cache_misses,
            last_error_timestamp=_utc_now(),
        )


class SnapshotAndSubscribeCoordinator:
    """Coordinates snapshot-first then stream consumption semantics."""

    def __init__(self, repository: RedisCacheRepository) -> None:
        self._repository = repository

    async def load_snapshot_and_stream(
        self,
        *,
        namespace: str,
        entity_id: str,
        stream_name: str,
        last_seen_id: str = "0-0",
        count: int | None = None,
    ) -> tuple[dict[str, JsonValue] | None, list[StreamEntry]]:
        snapshot = await self._repository.get_snapshot(namespace=namespace, entity_id=entity_id)
        events = await self._repository.read_stream(
            stream_name=stream_name,
            last_seen_id=last_seen_id,
            count=count,
            block_milliseconds=0,
        )
        return snapshot, events


class RedisStreamTailer:
    """Async stream tailer for continuous event consumption."""

    def __init__(
        self,
        *,
        repository: RedisCacheRepository,
        stream_name: str,
        start_id: str = "0-0",
        block_milliseconds: int = 1_000,
        count: int = 100,
    ) -> None:
        self._repository = repository
        self._stream_name = stream_name
        self._last_seen_id = start_id
        self._block_milliseconds = block_milliseconds
        self._count = count
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        self._stop_event.set()

    async def __aiter__(self) -> AsyncIterator[StreamEntry]:
        while not self._stop_event.is_set():
            entries = await self._repository.read_stream(
                stream_name=self._stream_name,
                last_seen_id=self._last_seen_id,
                count=self._count,
                block_milliseconds=self._block_milliseconds,
            )
            for entry in entries:
                self._last_seen_id = entry.entry_id
                yield entry



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None



def _compare_stream_ids(left: str, right: str) -> int:
    left_major, left_minor = _split_stream_id(left)
    right_major, right_minor = _split_stream_id(right)
    if left_major != right_major:
        return 1 if left_major > right_major else -1
    if left_minor != right_minor:
        return 1 if left_minor > right_minor else -1
    return 0



def _split_stream_id(stream_id: str) -> tuple[int, int]:
    if stream_id == "$":
        return (2**63 - 1, 2**63 - 1)
    major_text, minor_text = stream_id.split("-", maxsplit=1)
    return int(major_text), int(minor_text)


__all__ = [
    "CacheMessage",
    "InMemoryRedisBackend",
    "JsonPrimitive",
    "JsonValue",
    "RedisBackend",
    "RedisCacheConfig",
    "RedisCacheConnectionError",
    "RedisCacheError",
    "RedisCacheRepository",
    "RedisCacheSerializationError",
    "RedisCacheStats",
    "RedisJsonCodec",
    "RedisKeyBuilder",
    "RedisStreamTailer",
    "SnapshotAndSubscribeCoordinator",
    "StreamEntry",
]
