# FILE: platform_core/data/tick_storage.py
"""
platform_core.data.tick_storage
═══════════════════════════════════════════════════════════════════════
Production-grade async tick storage foundation for Indian Index Futures &
Options market data workflows.

This module is designed to be:
- async-first,
- append-only,
- replay-compatible,
- event-driven compatible,
- no-hindsight safe,
- batch-write optimized,
- storage-backend agnostic,
- dependency-clean.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

try:
    from platform_core.core.utils import ensure_utc, format_decimal, safe_decimal
except ModuleNotFoundError:
    def ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def safe_decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    def format_decimal(value: Decimal) -> str:
        return format(value, 'f')

LOGGER = logging.getLogger(__name__)

StorageHook = Callable[[Sequence["TickStorageRecord"]], Awaitable[None]]


class TickStorageError(RuntimeError):
    """Base exception for tick storage failures."""


class TickValidationError(TickStorageError):
    """Raised when a tick record violates storage invariants."""


class TickStorageBackpressureError(TickStorageError):
    """Raised when the storage queue exceeds configured capacity."""


class TickStorageFlushError(TickStorageError):
    """Raised when a storage batch flush fails."""


@dataclass(frozen=True, slots=True)
class TickStorageConfig:
    """Operational configuration for append-only tick persistence."""

    batch_size: int = 1_000
    flush_interval_seconds: float = 0.5
    queue_maxsize: int = 100_000
    max_consecutive_failures: int | None = None
    drop_on_backpressure: bool = False

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if self.queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be positive")
        if self.max_consecutive_failures is not None and self.max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be positive when provided")


@dataclass(frozen=True, slots=True)
class TickStorageRecord:
    """Canonical append-only tick record for persistence and replay."""

    event_id: str
    exchange: str
    instrument_symbol: str
    underlying_symbol: str
    instrument_type: str
    exchange_timestamp: datetime
    received_timestamp: datetime
    sequence_number: int
    last_traded_price: Decimal
    last_traded_quantity: int
    cumulative_volume: int | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    is_trade_tick: bool = True
    is_quote_tick: bool = False
    is_replay: bool = False
    source: str = "market_data"
    raw_payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise TickValidationError("event_id must be non-empty")
        if not self.exchange.strip():
            raise TickValidationError("exchange must be non-empty")
        if not self.instrument_symbol.strip():
            raise TickValidationError("instrument_symbol must be non-empty")
        if not self.underlying_symbol.strip():
            raise TickValidationError("underlying_symbol must be non-empty")
        if not self.instrument_type.strip():
            raise TickValidationError("instrument_type must be non-empty")
        if not self.source.strip():
            raise TickValidationError("source must be non-empty")
        if self.sequence_number < 0:
            raise TickValidationError("sequence_number cannot be negative")
        if self.last_traded_quantity < 0:
            raise TickValidationError("last_traded_quantity cannot be negative")
        if self.cumulative_volume is not None and self.cumulative_volume < 0:
            raise TickValidationError("cumulative_volume cannot be negative")
        if self.bid_quantity is not None and self.bid_quantity < 0:
            raise TickValidationError("bid_quantity cannot be negative")
        if self.ask_quantity is not None and self.ask_quantity < 0:
            raise TickValidationError("ask_quantity cannot be negative")
        if self.open_interest is not None and self.open_interest < 0:
            raise TickValidationError("open_interest cannot be negative")
        if self.exchange_timestamp.tzinfo is None or self.received_timestamp.tzinfo is None:
            raise TickValidationError("timestamps must be timezone-aware")
        if self.received_timestamp < self.exchange_timestamp:
            raise TickValidationError("received_timestamp cannot precede exchange_timestamp")
        if self.last_traded_price <= 0:
            raise TickValidationError("last_traded_price must be positive")
        if self.bid_price is not None and self.bid_price <= 0:
            raise TickValidationError("bid_price must be positive when provided")
        if self.ask_price is not None and self.ask_price <= 0:
            raise TickValidationError("ask_price must be positive when provided")
        if self.implied_volatility is not None and self.implied_volatility < 0:
            raise TickValidationError("implied_volatility cannot be negative")
        if self.bid_price is not None and self.ask_price is not None and self.ask_price < self.bid_price:
            raise TickValidationError("ask_price cannot be less than bid_price")
        if not self.is_trade_tick and not self.is_quote_tick:
            raise TickValidationError("at least one of is_trade_tick or is_quote_tick must be true")

    @property
    def trading_day(self) -> date:
        return self.exchange_timestamp.date()

    @property
    def latency_seconds(self) -> float:
        return max((self.received_timestamp - self.exchange_timestamp).total_seconds(), 0.0)

    def to_storage_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "exchange": self.exchange,
            "instrument_symbol": self.instrument_symbol,
            "underlying_symbol": self.underlying_symbol,
            "instrument_type": self.instrument_type,
            "exchange_timestamp": self.exchange_timestamp,
            "received_timestamp": self.received_timestamp,
            "sequence_number": self.sequence_number,
            "last_traded_price": format_decimal(self.last_traded_price),
            "last_traded_quantity": self.last_traded_quantity,
            "cumulative_volume": self.cumulative_volume,
            "bid_price": None if self.bid_price is None else format_decimal(self.bid_price),
            "ask_price": None if self.ask_price is None else format_decimal(self.ask_price),
            "bid_quantity": self.bid_quantity,
            "ask_quantity": self.ask_quantity,
            "open_interest": self.open_interest,
            "implied_volatility": None if self.implied_volatility is None else format_decimal(self.implied_volatility),
            "is_trade_tick": self.is_trade_tick,
            "is_quote_tick": self.is_quote_tick,
            "is_replay": self.is_replay,
            "source": self.source,
            "raw_payload": self.raw_payload,
            "trading_day": self.trading_day.isoformat(),
            "latency_seconds": self.latency_seconds,
        }


@dataclass(frozen=True, slots=True)
class TickStorageStats:
    """Storage runtime health and throughput metrics."""

    enqueued_records: int = 0
    persisted_records: int = 0
    dropped_records: int = 0
    flush_count: int = 0
    consecutive_failures: int = 0
    queue_size: int = 0
    last_flush_timestamp: datetime | None = None
    last_failure_timestamp: datetime | None = None


class TickStorageBackend(ABC):
    """Abstract append-only tick persistence backend."""

    @abstractmethod
    async def write_batch(self, records: Sequence[TickStorageRecord]) -> None:
        """Persist a batch of tick records atomically from the writer perspective."""


class InMemoryTickStorageBackend(TickStorageBackend):
    """Deterministic in-memory backend for tests and replay validation."""

    def __init__(self) -> None:
        self._records: list[TickStorageRecord] = []

    async def write_batch(self, records: Sequence[TickStorageRecord]) -> None:
        self._records.extend(records)

    def snapshot(self) -> tuple[TickStorageRecord, ...]:
        return tuple(self._records)


class TickRecordNormalizer:
    """Normalize heterogeneous inbound tick payloads into canonical storage records."""

    @staticmethod
    def from_mapping(payload: dict[str, Any]) -> TickStorageRecord:
        exchange_timestamp = _coerce_datetime(payload.get("exchange_timestamp"))
        received_timestamp = _coerce_datetime(payload.get("received_timestamp"))
        if exchange_timestamp is None or received_timestamp is None:
            raise TickValidationError("exchange_timestamp and received_timestamp are required")

        return TickStorageRecord(
            event_id=_require_text(payload.get("event_id"), "event_id"),
            exchange=_require_text(payload.get("exchange"), "exchange"),
            instrument_symbol=_require_text(payload.get("instrument_symbol"), "instrument_symbol"),
            underlying_symbol=_require_text(payload.get("underlying_symbol"), "underlying_symbol"),
            instrument_type=_require_text(payload.get("instrument_type"), "instrument_type"),
            exchange_timestamp=exchange_timestamp,
            received_timestamp=received_timestamp,
            sequence_number=_coerce_non_negative_int(payload.get("sequence_number"), "sequence_number"),
            last_traded_price=_coerce_positive_decimal(payload.get("last_traded_price"), "last_traded_price"),
            last_traded_quantity=_coerce_non_negative_int(payload.get("last_traded_quantity", 0), "last_traded_quantity"),
            cumulative_volume=_coerce_optional_non_negative_int(payload.get("cumulative_volume"), "cumulative_volume"),
            bid_price=_coerce_optional_positive_decimal(payload.get("bid_price"), "bid_price"),
            ask_price=_coerce_optional_positive_decimal(payload.get("ask_price"), "ask_price"),
            bid_quantity=_coerce_optional_non_negative_int(payload.get("bid_quantity"), "bid_quantity"),
            ask_quantity=_coerce_optional_non_negative_int(payload.get("ask_quantity"), "ask_quantity"),
            open_interest=_coerce_optional_non_negative_int(payload.get("open_interest"), "open_interest"),
            implied_volatility=_coerce_optional_non_negative_decimal(payload.get("implied_volatility"), "implied_volatility"),
            is_trade_tick=bool(payload.get("is_trade_tick", True)),
            is_quote_tick=bool(payload.get("is_quote_tick", False)),
            is_replay=bool(payload.get("is_replay", False)),
            source=_require_text(payload.get("source", "market_data"), "source"),
            raw_payload=dict(payload),
        )


class TickStorageWriter:
    """Async append-only writer with bounded queue and periodic batch flushes."""

    def __init__(
        self,
        *,
        backend: TickStorageBackend,
        config: TickStorageConfig,
        on_before_flush: StorageHook | None = None,
        on_after_flush: StorageHook | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._on_before_flush = on_before_flush
        self._on_after_flush = on_after_flush
        self._queue: asyncio.Queue[TickStorageRecord] = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._stats = TickStorageStats()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def stats(self) -> TickStorageStats:
        return self._stats

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            raise RuntimeError("tick storage writer is already running")
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._run(), name="tick-storage-writer")

    async def stop(self, *, flush: bool = True) -> None:
        self._stop_event.set()
        if flush:
            await self.flush()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    async def enqueue(self, record: TickStorageRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull as exc:
            self._stats = TickStorageStats(
                enqueued_records=self._stats.enqueued_records,
                persisted_records=self._stats.persisted_records,
                dropped_records=self._stats.dropped_records + 1,
                flush_count=self._stats.flush_count,
                consecutive_failures=self._stats.consecutive_failures,
                queue_size=self._queue.qsize(),
                last_flush_timestamp=self._stats.last_flush_timestamp,
                last_failure_timestamp=self._stats.last_failure_timestamp,
            )
            if self._config.drop_on_backpressure:
                LOGGER.warning("Dropping tick record due to queue backpressure: %s", record.event_id)
                return
            raise TickStorageBackpressureError("tick storage queue is full") from exc
        self._stats = TickStorageStats(
            enqueued_records=self._stats.enqueued_records + 1,
            persisted_records=self._stats.persisted_records,
            dropped_records=self._stats.dropped_records,
            flush_count=self._stats.flush_count,
            consecutive_failures=self._stats.consecutive_failures,
            queue_size=self._queue.qsize(),
            last_flush_timestamp=self._stats.last_flush_timestamp,
            last_failure_timestamp=self._stats.last_failure_timestamp,
        )

    async def enqueue_mapping(self, payload: dict[str, Any]) -> None:
        await self.enqueue(TickRecordNormalizer.from_mapping(payload))

    async def flush(self) -> None:
        batch = self._drain_batch(limit=self._config.batch_size)
        if batch:
            await self._flush_batch(batch)

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                first_record = await asyncio.wait_for(self._queue.get(), timeout=self._config.flush_interval_seconds)
            except asyncio.TimeoutError:
                continue
            batch = [first_record]
            batch.extend(self._drain_batch(limit=self._config.batch_size - 1))
            await self._flush_batch(batch)

    def _drain_batch(self, *, limit: int) -> list[TickStorageRecord]:
        drained: list[TickStorageRecord] = []
        while len(drained) < limit and not self._queue.empty():
            drained.append(self._queue.get_nowait())
        return drained

    async def _flush_batch(self, batch: Sequence[TickStorageRecord]) -> None:
        if not batch:
            return
        try:
            if self._on_before_flush is not None:
                await self._on_before_flush(batch)
            await self._backend.write_batch(batch)
            if self._on_after_flush is not None:
                await self._on_after_flush(batch)
        except Exception as exc:
            self._stats = TickStorageStats(
                enqueued_records=self._stats.enqueued_records,
                persisted_records=self._stats.persisted_records,
                dropped_records=self._stats.dropped_records,
                flush_count=self._stats.flush_count,
                consecutive_failures=self._stats.consecutive_failures + 1,
                queue_size=self._queue.qsize(),
                last_flush_timestamp=self._stats.last_flush_timestamp,
                last_failure_timestamp=_utc_now(),
            )
            if self._config.max_consecutive_failures is not None and self._stats.consecutive_failures >= self._config.max_consecutive_failures:
                raise TickStorageFlushError("maximum consecutive flush failures exceeded") from exc
            raise TickStorageFlushError("tick storage batch flush failed") from exc
        finally:
            for _ in batch:
                with contextlib.suppress(ValueError):
                    self._queue.task_done()
        self._stats = TickStorageStats(
            enqueued_records=self._stats.enqueued_records,
            persisted_records=self._stats.persisted_records + len(batch),
            dropped_records=self._stats.dropped_records,
            flush_count=self._stats.flush_count + 1,
            consecutive_failures=0,
            queue_size=self._queue.qsize(),
            last_flush_timestamp=_utc_now(),
            last_failure_timestamp=self._stats.last_failure_timestamp,
        )



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError as exc:
            raise TickValidationError("invalid datetime value") from exc
    raise TickValidationError("invalid datetime value")



def _require_text(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise TickValidationError(f"{field_name} must be non-empty")
    return text



def _coerce_non_negative_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise TickValidationError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise TickValidationError(f"{field_name} cannot be negative")
    return normalized



def _coerce_optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _coerce_non_negative_int(value, field_name)



def _coerce_positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        normalized = safe_decimal(value)
    except Exception as exc:
        raise TickValidationError(f"{field_name} must be a decimal-compatible value") from exc
    if normalized <= 0:
        raise TickValidationError(f"{field_name} must be positive")
    return normalized



def _coerce_optional_positive_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _coerce_positive_decimal(value, field_name)



def _coerce_optional_non_negative_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = safe_decimal(value)
    except Exception as exc:
        raise TickValidationError(f"{field_name} must be a decimal-compatible value") from exc
    if normalized < 0:
        raise TickValidationError(f"{field_name} cannot be negative")
    return normalized


__all__ = [
    "InMemoryTickStorageBackend",
    "StorageHook",
    "TickRecordNormalizer",
    "TickStorageBackend",
    "TickStorageBackpressureError",
    "TickStorageConfig",
    "TickStorageError",
    "TickStorageFlushError",
    "TickStorageRecord",
    "TickStorageStats",
    "TickStorageWriter",
    "TickValidationError",
]
