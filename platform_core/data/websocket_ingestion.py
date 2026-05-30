# FILE: platform_core/data/websocket_ingestion.py
"""
platform_core.data.websocket_ingestion
═══════════════════════════════════════════════════════════════════════
Production-grade async websocket ingestion foundation for Indian Index
Futures & Options market data workflows.

This module is designed to be:
- async-first,
- streaming-compatible,
- replay-compatible,
- event-driven compatible,
- no-hindsight safe,
- backpressure-aware,
- reconnection-safe,
- dependency-clean.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

from platform_core.core.utils import ensure_utc

LOGGER = logging.getLogger(__name__)

JsonMap = dict[str, Any]
MessageHandler = Callable[["InboundMessageEnvelope"], Awaitable[None]]


class WebsocketConnectionError(RuntimeError):
    """Raised when websocket connection establishment or streaming fails."""


class WebsocketIdleTimeoutError(WebsocketConnectionError):
    """Raised when no data arrives within the configured idle timeout."""


class WebsocketBackpressureError(WebsocketConnectionError):
    """Raised when inbound backpressure exceeds queue capacity."""


class WebsocketTransport(ABC):
    """Abstract websocket transport to preserve dependency cleanliness."""

    @abstractmethod
    async def connect(self, url: str, headers: Mapping[str, str], config: "WebsocketIngestionConfig") -> None:
        """Establish a websocket connection."""

    @abstractmethod
    async def send_text(self, payload: str) -> None:
        """Send a text payload over the active websocket session."""

    @abstractmethod
    async def receive_text(self) -> str:
        """Receive the next text payload from the active websocket session."""

    @abstractmethod
    async def close(self) -> None:
        """Close the active websocket session."""


class UnsupportedWebsocketTransport(WebsocketTransport):
    """Fallback transport that fails explicitly until a concrete transport is wired."""

    async def connect(self, url: str, headers: Mapping[str, str], config: "WebsocketIngestionConfig") -> None:
        raise WebsocketConnectionError(
            "No websocket transport implementation is configured. "
            "Inject a concrete WebsocketTransport compatible with the target broker or exchange."
        )

    async def send_text(self, payload: str) -> None:
        raise WebsocketConnectionError("websocket transport is not connected")

    async def receive_text(self) -> str:
        raise WebsocketConnectionError("websocket transport is not connected")

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class WebsocketAuthConfig:
    """Authentication inputs required for websocket session establishment."""

    api_key: str | None = None
    access_token: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    query_params: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebsocketSubscription:
    """Canonical subscription request for a websocket stream."""

    subscription_id: str
    payload: JsonMap
    requires_ack: bool = True
    ack_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.subscription_id.strip():
            raise ValueError("subscription_id must be non-empty")
        if self.ack_timeout_seconds <= 0:
            raise ValueError("ack_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class WebsocketIngestionConfig:
    """Operational configuration for resilient websocket ingestion."""

    url: str
    connect_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 10.0
    ping_interval_seconds: float = 20.0
    ping_timeout_seconds: float = 20.0
    heartbeat_interval_seconds: float | None = None
    idle_timeout_seconds: float = 60.0
    max_reconnect_delay_seconds: float = 30.0
    reconnect_base_delay_seconds: float = 1.0
    reconnect_jitter_seconds: float = 0.5
    outbound_queue_maxsize: int = 1_024
    inbound_queue_maxsize: int = 16_384
    max_message_size_bytes: int = 2 * 1024 * 1024
    max_consecutive_failures: int | None = None
    extra_connect_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("url must be non-empty")
        numeric_fields = {
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "close_timeout_seconds": self.close_timeout_seconds,
            "ping_interval_seconds": self.ping_interval_seconds,
            "ping_timeout_seconds": self.ping_timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_reconnect_delay_seconds": self.max_reconnect_delay_seconds,
            "reconnect_base_delay_seconds": self.reconnect_base_delay_seconds,
            "reconnect_jitter_seconds": self.reconnect_jitter_seconds,
        }
        for name, value in numeric_fields.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.heartbeat_interval_seconds is not None and self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive when provided")
        if self.outbound_queue_maxsize <= 0 or self.inbound_queue_maxsize <= 0:
            raise ValueError("queue sizes must be positive")
        if self.max_message_size_bytes <= 0:
            raise ValueError("max_message_size_bytes must be positive")


@dataclass(frozen=True, slots=True)
class InboundMessageEnvelope:
    """Canonical inbound websocket payload with causal timestamps."""

    connection_id: str
    sequence_number: int
    received_timestamp: datetime
    exchange_timestamp: datetime | None
    raw_message: str
    parsed_message: JsonMap | list[Any] | str
    message_type: str
    is_replay: bool = False
    source: str = "websocket"


@dataclass(frozen=True, slots=True)
class OutboundMessageEnvelope:
    """Canonical outbound websocket payload."""

    message_id: str
    payload: JsonMap
    created_timestamp: datetime
    requires_ack: bool = False


@dataclass(frozen=True, slots=True)
class WebsocketConnectionStats:
    """Snapshot of runtime ingestion health metrics."""

    successful_connections: int = 0
    reconnect_attempts: int = 0
    consecutive_failures: int = 0
    inbound_messages: int = 0
    outbound_messages: int = 0
    parse_failures: int = 0
    dropped_messages: int = 0
    last_message_timestamp: datetime | None = None
    last_connect_timestamp: datetime | None = None
    last_disconnect_timestamp: datetime | None = None
    latency_seconds: float | None = None


class WebsocketMessageParser(Protocol):
    """Protocol for exchange-specific websocket payload parsing."""

    def parse(self, raw_message: str) -> JsonMap | list[Any] | str:
        ...

    def classify(self, parsed_message: JsonMap | list[Any] | str) -> str:
        ...

    def extract_exchange_timestamp(self, parsed_message: JsonMap | list[Any] | str) -> datetime | None:
        ...

    def is_heartbeat_message(self, parsed_message: JsonMap | list[Any] | str) -> bool:
        ...

    def is_subscription_ack(self, parsed_message: JsonMap | list[Any] | str, subscription: WebsocketSubscription) -> bool:
        ...


class JsonWebsocketMessageParser:
    """Default JSON parser for generic exchange websocket streams."""

    def parse(self, raw_message: str) -> JsonMap | list[Any] | str:
        return json.loads(raw_message)

    def classify(self, parsed_message: JsonMap | list[Any] | str) -> str:
        if isinstance(parsed_message, dict):
            for key in ("type", "event", "channel", "topic"):
                value = parsed_message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return "dict"
        if isinstance(parsed_message, list):
            return "list"
        return "text"

    def extract_exchange_timestamp(self, parsed_message: JsonMap | list[Any] | str) -> datetime | None:
        if not isinstance(parsed_message, dict):
            return None
        for key in ("exchange_timestamp", "timestamp", "ts", "time"):
            raw_value = parsed_message.get(key)
            if raw_value is None:
                continue
            parsed = _coerce_timestamp(raw_value)
            if parsed is not None:
                return parsed
        return None

    def is_heartbeat_message(self, parsed_message: JsonMap | list[Any] | str) -> bool:
        if not isinstance(parsed_message, dict):
            return False
        message_type = str(parsed_message.get("type", "")).lower()
        event = str(parsed_message.get("event", "")).lower()
        return message_type in {"heartbeat", "ping", "pong"} or event in {"heartbeat", "ping", "pong"}

    def is_subscription_ack(self, parsed_message: JsonMap | list[Any] | str, subscription: WebsocketSubscription) -> bool:
        if not isinstance(parsed_message, dict):
            return False
        values = {str(value) for value in parsed_message.values() if isinstance(value, (str, int, float))}
        return subscription.subscription_id in values


class WebsocketAuthProvider(ABC):
    """Abstract authentication provider for websocket sessions."""

    @abstractmethod
    async def build_auth_config(self) -> WebsocketAuthConfig:
        """Build auth configuration before each connection attempt."""


class StaticWebsocketAuthProvider(WebsocketAuthProvider):
    """Static auth provider suitable for config-driven brokers or exchanges."""

    def __init__(self, auth_config: WebsocketAuthConfig) -> None:
        self._auth_config = auth_config

    async def build_auth_config(self) -> WebsocketAuthConfig:
        return self._auth_config


class WebsocketLifecycleHooks(ABC):
    """Optional lifecycle hooks for exchange-specific orchestration logic."""

    async def on_connect(self, transport: WebsocketTransport) -> None:
        return None

    async def on_disconnect(self) -> None:
        return None

    async def build_heartbeat_payload(self) -> JsonMap | None:
        return None


class NullLifecycleHooks(WebsocketLifecycleHooks):
    """No-op lifecycle hooks implementation."""


class WebsocketIngestionClient:
    """Resilient async websocket ingestion client with causal processing guarantees."""

    def __init__(
        self,
        *,
        config: WebsocketIngestionConfig,
        parser: WebsocketMessageParser | None = None,
        auth_provider: WebsocketAuthProvider | None = None,
        lifecycle_hooks: WebsocketLifecycleHooks | None = None,
        transport: WebsocketTransport | None = None,
        subscriptions: Iterable[WebsocketSubscription] = (),
        message_handler: MessageHandler | None = None,
        source_name: str,
    ) -> None:
        if not source_name.strip():
            raise ValueError("source_name must be non-empty")
        self._config = config
        self._parser = parser or JsonWebsocketMessageParser()
        self._auth_provider = auth_provider or StaticWebsocketAuthProvider(WebsocketAuthConfig())
        self._lifecycle_hooks = lifecycle_hooks or NullLifecycleHooks()
        self._transport = transport or UnsupportedWebsocketTransport()
        self._subscriptions = tuple(subscriptions)
        self._message_handler = message_handler
        self._source_name = source_name.strip()

        self._connection_id = ""
        self._sequence_number = 0
        self._stop_event = asyncio.Event()
        self._outbound_queue: asyncio.Queue[OutboundMessageEnvelope] = asyncio.Queue(
            maxsize=self._config.outbound_queue_maxsize
        )
        self._inbound_queue: asyncio.Queue[InboundMessageEnvelope] = asyncio.Queue(
            maxsize=self._config.inbound_queue_maxsize
        )
        self._stats = WebsocketConnectionStats()
        self._runner_task: asyncio.Task[None] | None = None

    @property
    def stats(self) -> WebsocketConnectionStats:
        return self._stats

    @property
    def inbound_queue(self) -> asyncio.Queue[InboundMessageEnvelope]:
        return self._inbound_queue

    async def start(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            raise RuntimeError("websocket ingestion client is already running")
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._run(), name=f"{self._source_name}-websocket-runner")

    async def stop(self) -> None:
        self._stop_event.set()
        await self._transport.close()
        if self._runner_task is not None:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None

    async def send(self, message_id: str, payload: JsonMap, *, requires_ack: bool = False) -> None:
        if not message_id.strip():
            raise ValueError("message_id must be non-empty")
        envelope = OutboundMessageEnvelope(
            message_id=message_id.strip(),
            payload=dict(payload),
            created_timestamp=_utc_now(),
            requires_ack=requires_ack,
        )
        await self._outbound_queue.put(envelope)

    async def drain_messages(self) -> AsyncIterator[InboundMessageEnvelope]:
        while not self._stop_event.is_set() or not self._inbound_queue.empty():
            try:
                envelope = await asyncio.wait_for(self._inbound_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield envelope
            self._inbound_queue.task_done()

    async def _run(self) -> None:
        reconnect_attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._run_single_connection()
                reconnect_attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reconnect_attempt += 1
                self._stats = WebsocketConnectionStats(
                    successful_connections=self._stats.successful_connections,
                    reconnect_attempts=self._stats.reconnect_attempts + 1,
                    consecutive_failures=self._stats.consecutive_failures + 1,
                    inbound_messages=self._stats.inbound_messages,
                    outbound_messages=self._stats.outbound_messages,
                    parse_failures=self._stats.parse_failures,
                    dropped_messages=self._stats.dropped_messages,
                    last_message_timestamp=self._stats.last_message_timestamp,
                    last_connect_timestamp=self._stats.last_connect_timestamp,
                    last_disconnect_timestamp=_utc_now(),
                    latency_seconds=self._stats.latency_seconds,
                )
                LOGGER.warning("Websocket ingestion failure on %s: %s", self._source_name, exc, exc_info=True)
                if self._config.max_consecutive_failures is not None and self._stats.consecutive_failures >= self._config.max_consecutive_failures:
                    raise RuntimeError("maximum consecutive websocket failures exceeded") from exc
                await asyncio.sleep(self._compute_reconnect_delay(reconnect_attempt))

    async def _run_single_connection(self) -> None:
        auth_config = await self._auth_provider.build_auth_config()
        headers, url = self._build_connection_parameters(auth_config)
        await self._transport.connect(url=url, headers=headers, config=self._config)
        self._connection_id = _build_connection_id(self._source_name)
        self._sequence_number = 0
        self._stats = WebsocketConnectionStats(
            successful_connections=self._stats.successful_connections + 1,
            reconnect_attempts=self._stats.reconnect_attempts,
            consecutive_failures=0,
            inbound_messages=self._stats.inbound_messages,
            outbound_messages=self._stats.outbound_messages,
            parse_failures=self._stats.parse_failures,
            dropped_messages=self._stats.dropped_messages,
            last_message_timestamp=self._stats.last_message_timestamp,
            last_connect_timestamp=_utc_now(),
            last_disconnect_timestamp=self._stats.last_disconnect_timestamp,
            latency_seconds=self._stats.latency_seconds,
        )

        await self._lifecycle_hooks.on_connect(self._transport)
        await self._subscribe()

        tasks = [
            asyncio.create_task(self._reader_loop(), name=f"{self._source_name}-reader"),
            asyncio.create_task(self._writer_loop(), name=f"{self._source_name}-writer"),
        ]
        if self._message_handler is not None:
            tasks.append(asyncio.create_task(self._dispatch_loop(), name=f"{self._source_name}-dispatcher"))
        if self._config.heartbeat_interval_seconds is not None:
            tasks.append(asyncio.create_task(self._heartbeat_loop(), name=f"{self._source_name}-heartbeat"))

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._lifecycle_hooks.on_disconnect()
            await self._transport.close()
            self._stats = WebsocketConnectionStats(
                successful_connections=self._stats.successful_connections,
                reconnect_attempts=self._stats.reconnect_attempts,
                consecutive_failures=self._stats.consecutive_failures,
                inbound_messages=self._stats.inbound_messages,
                outbound_messages=self._stats.outbound_messages,
                parse_failures=self._stats.parse_failures,
                dropped_messages=self._stats.dropped_messages,
                last_message_timestamp=self._stats.last_message_timestamp,
                last_connect_timestamp=self._stats.last_connect_timestamp,
                last_disconnect_timestamp=_utc_now(),
                latency_seconds=self._stats.latency_seconds,
            )

    async def _subscribe(self) -> None:
        for subscription in self._subscriptions:
            await self._transport.send_text(json.dumps(subscription.payload, separators=(",", ":"), ensure_ascii=False))
            if subscription.requires_ack:
                await self._await_subscription_ack(subscription)

    async def _await_subscription_ack(self, subscription: WebsocketSubscription) -> None:
        deadline = time.monotonic() + subscription.ack_timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.001)
            raw_message = await asyncio.wait_for(self._transport.receive_text(), timeout=remaining)
            received_timestamp = _utc_now()
            parsed_message = self._parse_message(raw_message)
            if self._parser.is_subscription_ack(parsed_message, subscription):
                return
            await self._queue_inbound_message(raw_message, parsed_message, received_timestamp)
        raise TimeoutError(f"subscription ack timeout for {subscription.subscription_id}")

    async def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                raw_message = await asyncio.wait_for(
                    self._transport.receive_text(),
                    timeout=self._config.idle_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise WebsocketIdleTimeoutError("websocket idle timeout exceeded") from exc
            except Exception as exc:
                raise WebsocketConnectionError("websocket transport receive failure") from exc
            received_timestamp = _utc_now()
            parsed_message = self._parse_message(raw_message)
            if self._parser.is_heartbeat_message(parsed_message):
                continue
            await self._queue_inbound_message(raw_message, parsed_message, received_timestamp)

    async def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            envelope = await self._outbound_queue.get()
            try:
                await self._transport.send_text(json.dumps(envelope.payload, separators=(",", ":"), ensure_ascii=False))
                self._stats = WebsocketConnectionStats(
                    successful_connections=self._stats.successful_connections,
                    reconnect_attempts=self._stats.reconnect_attempts,
                    consecutive_failures=self._stats.consecutive_failures,
                    inbound_messages=self._stats.inbound_messages,
                    outbound_messages=self._stats.outbound_messages + 1,
                    parse_failures=self._stats.parse_failures,
                    dropped_messages=self._stats.dropped_messages,
                    last_message_timestamp=self._stats.last_message_timestamp,
                    last_connect_timestamp=self._stats.last_connect_timestamp,
                    last_disconnect_timestamp=self._stats.last_disconnect_timestamp,
                    latency_seconds=self._stats.latency_seconds,
                )
            finally:
                self._outbound_queue.task_done()

    async def _dispatch_loop(self) -> None:
        if self._message_handler is None:
            return
        while not self._stop_event.is_set():
            envelope = await self._inbound_queue.get()
            try:
                await self._message_handler(envelope)
            finally:
                self._inbound_queue.task_done()

    async def _heartbeat_loop(self) -> None:
        interval = self._config.heartbeat_interval_seconds
        if interval is None:
            return
        while not self._stop_event.is_set():
            await asyncio.sleep(interval)
            payload = await self._lifecycle_hooks.build_heartbeat_payload()
            if payload is None:
                continue
            await self._transport.send_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

    def _build_connection_parameters(self, auth_config: WebsocketAuthConfig) -> tuple[dict[str, str], str]:
        headers = dict(auth_config.headers)
        if auth_config.api_key is not None:
            headers.setdefault("X-API-Key", auth_config.api_key)
        if auth_config.access_token is not None:
            headers.setdefault("Authorization", f"Bearer {auth_config.access_token}")

        url = self._config.url
        if auth_config.query_params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(dict(auth_config.query_params))}"
        return headers, url

    def _parse_message(self, raw_message: str) -> JsonMap | list[Any] | str:
        try:
            return self._parser.parse(raw_message)
        except Exception as exc:
            self._stats = WebsocketConnectionStats(
                successful_connections=self._stats.successful_connections,
                reconnect_attempts=self._stats.reconnect_attempts,
                consecutive_failures=self._stats.consecutive_failures,
                inbound_messages=self._stats.inbound_messages,
                outbound_messages=self._stats.outbound_messages,
                parse_failures=self._stats.parse_failures + 1,
                dropped_messages=self._stats.dropped_messages,
                last_message_timestamp=self._stats.last_message_timestamp,
                last_connect_timestamp=self._stats.last_connect_timestamp,
                last_disconnect_timestamp=self._stats.last_disconnect_timestamp,
                latency_seconds=self._stats.latency_seconds,
            )
            raise ValueError(f"failed to parse websocket message: {exc}") from exc

    async def _queue_inbound_message(
        self,
        raw_message: str,
        parsed_message: JsonMap | list[Any] | str,
        received_timestamp: datetime,
    ) -> None:
        self._sequence_number += 1
        exchange_timestamp = self._parser.extract_exchange_timestamp(parsed_message)
        envelope = InboundMessageEnvelope(
            connection_id=self._connection_id,
            sequence_number=self._sequence_number,
            received_timestamp=received_timestamp,
            exchange_timestamp=exchange_timestamp,
            raw_message=raw_message,
            parsed_message=parsed_message,
            message_type=self._parser.classify(parsed_message),
            is_replay=False,
            source=self._source_name,
        )
        try:
            self._inbound_queue.put_nowait(envelope)
        except asyncio.QueueFull as exc:
            self._stats = WebsocketConnectionStats(
                successful_connections=self._stats.successful_connections,
                reconnect_attempts=self._stats.reconnect_attempts,
                consecutive_failures=self._stats.consecutive_failures,
                inbound_messages=self._stats.inbound_messages,
                outbound_messages=self._stats.outbound_messages,
                parse_failures=self._stats.parse_failures,
                dropped_messages=self._stats.dropped_messages + 1,
                last_message_timestamp=self._stats.last_message_timestamp,
                last_connect_timestamp=self._stats.last_connect_timestamp,
                last_disconnect_timestamp=self._stats.last_disconnect_timestamp,
                latency_seconds=self._stats.latency_seconds,
            )
            raise WebsocketBackpressureError("inbound queue is full; backpressure threshold exceeded") from exc
        latency_seconds = None
        if exchange_timestamp is not None:
            latency_seconds = max((received_timestamp - exchange_timestamp).total_seconds(), 0.0)
        self._stats = WebsocketConnectionStats(
            successful_connections=self._stats.successful_connections,
            reconnect_attempts=self._stats.reconnect_attempts,
            consecutive_failures=self._stats.consecutive_failures,
            inbound_messages=self._stats.inbound_messages + 1,
            outbound_messages=self._stats.outbound_messages,
            parse_failures=self._stats.parse_failures,
            dropped_messages=self._stats.dropped_messages,
            last_message_timestamp=received_timestamp,
            last_connect_timestamp=self._stats.last_connect_timestamp,
            last_disconnect_timestamp=self._stats.last_disconnect_timestamp,
            latency_seconds=latency_seconds,
        )

    def _compute_reconnect_delay(self, reconnect_attempt: int) -> float:
        exponential = self._config.reconnect_base_delay_seconds * (2 ** max(reconnect_attempt - 1, 0))
        bounded = min(exponential, self._config.max_reconnect_delay_seconds)
        jitter = random.uniform(0.0, self._config.reconnect_jitter_seconds)
        return bounded + jitter


class MemoryWebsocketTransport(WebsocketTransport):
    """In-memory transport for deterministic unit and replay-aligned tests."""

    def __init__(self) -> None:
        self._connected = False
        self._inbound: asyncio.Queue[str] = asyncio.Queue()
        self._outbound: asyncio.Queue[str] = asyncio.Queue()

    async def connect(self, url: str, headers: Mapping[str, str], config: WebsocketIngestionConfig) -> None:
        self._connected = True

    async def send_text(self, payload: str) -> None:
        if not self._connected:
            raise WebsocketConnectionError("memory transport is not connected")
        await self._outbound.put(payload)

    async def receive_text(self) -> str:
        if not self._connected:
            raise WebsocketConnectionError("memory transport is not connected")
        return await self._inbound.get()

    async def close(self) -> None:
        self._connected = False

    async def inject_inbound(self, payload: str) -> None:
        await self._inbound.put(payload)

    async def drain_outbound(self) -> list[str]:
        items: list[str] = []
        while not self._outbound.empty():
            items.append(self._outbound.get_nowait())
        return items



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def _build_connection_id(source_name: str) -> str:
    timestamp_ns = time.time_ns()
    random_component = random.randint(1000, 9999)
    return f"{source_name}-{timestamp_ns}-{random_component}"



def _coerce_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            return datetime.fromtimestamp(value / 1_000.0, tz=timezone.utc)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        with contextlib.suppress(ValueError):
            return ensure_utc(datetime.fromisoformat(stripped.replace("Z", "+00:00")))
    return None


__all__ = [
    "InboundMessageEnvelope",
    "JsonWebsocketMessageParser",
    "MemoryWebsocketTransport",
    "MessageHandler",
    "NullLifecycleHooks",
    "OutboundMessageEnvelope",
    "StaticWebsocketAuthProvider",
    "UnsupportedWebsocketTransport",
    "WebsocketAuthConfig",
    "WebsocketAuthProvider",
    "WebsocketBackpressureError",
    "WebsocketConnectionError",
    "WebsocketConnectionStats",
    "WebsocketIdleTimeoutError",
    "WebsocketIngestionClient",
    "WebsocketIngestionConfig",
    "WebsocketLifecycleHooks",
    "WebsocketMessageParser",
    "WebsocketSubscription",
    "WebsocketTransport",
]
