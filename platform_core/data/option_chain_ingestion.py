# FILE: platform_core/data/option_chain_ingestion.py
"""
platform_core.data.option_chain_ingestion
═══════════════════════════════════════════════════════════════════════
Production-grade async option chain ingestion foundation for Indian Index
Futures & Options market data workflows.

This module is designed to be:
- async-first,
- snapshot-and-delta compatible,
- replay-compatible,
- event-driven compatible,
- no-hindsight safe,
- exchange-normalization aware,
- incremental-state safe,
- dependency-clean.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

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

ChainBatchHandler = Callable[[Sequence["OptionChainUpdateEnvelope"]], Awaitable[None]]
ChainUpdateKind = Literal["SNAPSHOT", "DELTA"]
OptionSide = Literal["CALL", "PUT"]


class OptionChainIngestionError(RuntimeError):
    """Base exception for option chain ingestion failures."""


class OptionChainValidationError(OptionChainIngestionError):
    """Raised when an option chain payload violates invariants."""


class OptionChainBackpressureError(OptionChainIngestionError):
    """Raised when the option chain queue exceeds configured capacity."""


@dataclass(frozen=True, slots=True)
class OptionGreeksSnapshot:
    """Canonical snapshot of contract-level option greeks."""

    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "delta": None if self.delta is None else format_decimal(self.delta),
            "gamma": None if self.gamma is None else format_decimal(self.gamma),
            "theta": None if self.theta is None else format_decimal(self.theta),
            "vega": None if self.vega is None else format_decimal(self.vega),
            "rho": None if self.rho is None else format_decimal(self.rho),
        }


@dataclass(frozen=True, slots=True)
class OptionContractSnapshot:
    """Canonical top-of-book and state snapshot for one option contract."""

    contract_symbol: str
    underlying_symbol: str
    exchange: str
    expiry_date: date
    strike_price: Decimal
    option_side: OptionSide
    event_timestamp: datetime
    received_timestamp: datetime
    last_traded_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    traded_volume: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    greeks: OptionGreeksSnapshot = field(default_factory=OptionGreeksSnapshot)
    is_tradable: bool = True
    source: str = "option_chain"
    raw_payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.contract_symbol.strip():
            raise OptionChainValidationError("contract_symbol must be non-empty")
        if not self.underlying_symbol.strip():
            raise OptionChainValidationError("underlying_symbol must be non-empty")
        if not self.exchange.strip():
            raise OptionChainValidationError("exchange must be non-empty")
        if self.option_side not in {"CALL", "PUT"}:
            raise OptionChainValidationError("option_side must be CALL or PUT")
        if self.strike_price <= 0:
            raise OptionChainValidationError("strike_price must be positive")
        if self.event_timestamp.tzinfo is None or self.received_timestamp.tzinfo is None:
            raise OptionChainValidationError("timestamps must be timezone-aware")
        if self.received_timestamp < self.event_timestamp:
            raise OptionChainValidationError("received_timestamp cannot precede event_timestamp")
        for name, value in (
            ("last_traded_price", self.last_traded_price),
            ("bid_price", self.bid_price),
            ("ask_price", self.ask_price),
            ("implied_volatility", self.implied_volatility),
        ):
            if value is not None and value < 0:
                raise OptionChainValidationError(f"{name} cannot be negative")
        for name, value in (
            ("bid_quantity", self.bid_quantity),
            ("ask_quantity", self.ask_quantity),
            ("traded_volume", self.traded_volume),
            ("open_interest", self.open_interest),
        ):
            if value is not None and value < 0:
                raise OptionChainValidationError(f"{name} cannot be negative")
        if self.bid_price is not None and self.ask_price is not None and self.ask_price < self.bid_price:
            raise OptionChainValidationError("ask_price cannot be lower than bid_price")

    @property
    def contract_key(self) -> str:
        return f"{self.underlying_symbol}|{self.expiry_date.isoformat()}|{format_decimal(self.strike_price)}|{self.option_side}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_symbol": self.contract_symbol,
            "underlying_symbol": self.underlying_symbol,
            "exchange": self.exchange,
            "expiry_date": self.expiry_date.isoformat(),
            "strike_price": format_decimal(self.strike_price),
            "option_side": self.option_side,
            "event_timestamp": self.event_timestamp.isoformat(),
            "received_timestamp": self.received_timestamp.isoformat(),
            "last_traded_price": None if self.last_traded_price is None else format_decimal(self.last_traded_price),
            "bid_price": None if self.bid_price is None else format_decimal(self.bid_price),
            "ask_price": None if self.ask_price is None else format_decimal(self.ask_price),
            "bid_quantity": self.bid_quantity,
            "ask_quantity": self.ask_quantity,
            "traded_volume": self.traded_volume,
            "open_interest": self.open_interest,
            "implied_volatility": None if self.implied_volatility is None else format_decimal(self.implied_volatility),
            "greeks": self.greeks.to_dict(),
            "is_tradable": self.is_tradable,
            "source": self.source,
            "raw_payload": self.raw_payload,
            "contract_key": self.contract_key,
        }


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """Canonical option chain state for a single underlying and expiry."""

    chain_id: str
    underlying_symbol: str
    expiry_date: date
    exchange: str
    event_timestamp: datetime
    received_timestamp: datetime
    contracts: tuple[OptionContractSnapshot, ...]
    sequence_number: int
    update_kind: ChainUpdateKind
    is_replay: bool = False
    source: str = "option_chain"

    def __post_init__(self) -> None:
        if not self.chain_id.strip():
            raise OptionChainValidationError("chain_id must be non-empty")
        if not self.underlying_symbol.strip():
            raise OptionChainValidationError("underlying_symbol must be non-empty")
        if not self.exchange.strip():
            raise OptionChainValidationError("exchange must be non-empty")
        if self.event_timestamp.tzinfo is None or self.received_timestamp.tzinfo is None:
            raise OptionChainValidationError("timestamps must be timezone-aware")
        if self.received_timestamp < self.event_timestamp:
            raise OptionChainValidationError("received_timestamp cannot precede event_timestamp")
        if self.sequence_number < 0:
            raise OptionChainValidationError("sequence_number cannot be negative")
        if self.update_kind not in {"SNAPSHOT", "DELTA"}:
            raise OptionChainValidationError("update_kind must be SNAPSHOT or DELTA")
        if not self.contracts:
            raise OptionChainValidationError("contracts cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "exchange": self.exchange,
            "event_timestamp": self.event_timestamp.isoformat(),
            "received_timestamp": self.received_timestamp.isoformat(),
            "contracts": [contract.to_dict() for contract in self.contracts],
            "sequence_number": self.sequence_number,
            "update_kind": self.update_kind,
            "is_replay": self.is_replay,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class OptionChainUpdateEnvelope:
    """Canonical event envelope emitted by the ingestion pipeline."""

    connection_id: str
    sequence_number: int
    chain_snapshot: OptionChainSnapshot
    received_timestamp: datetime
    source: str


@dataclass(frozen=True, slots=True)
class OptionChainIngestionConfig:
    """Operational configuration for option chain ingestion."""

    queue_maxsize: int = 8_192
    drain_batch_size: int = 128
    flush_interval_seconds: float = 0.25
    max_tracked_chains: int = 256
    drop_on_backpressure: bool = False

    def __post_init__(self) -> None:
        if self.queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be positive")
        if self.drain_batch_size <= 0:
            raise ValueError("drain_batch_size must be positive")
        if self.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if self.max_tracked_chains <= 0:
            raise ValueError("max_tracked_chains must be positive")


@dataclass(frozen=True, slots=True)
class OptionChainIngestionStats:
    """Operational metrics for option chain ingestion health."""

    accepted_updates: int = 0
    emitted_updates: int = 0
    dropped_updates: int = 0
    parse_failures: int = 0
    queue_size: int = 0
    tracked_chains: int = 0
    last_update_timestamp: datetime | None = None


class OptionChainParser(ABC):
    """Abstract parser that normalizes broker/exchange option chain payloads."""

    @abstractmethod
    def parse(self, payload: Mapping[str, Any]) -> OptionChainSnapshot:
        """Normalize an external payload into a canonical option chain snapshot."""


class DefaultOptionChainParser(OptionChainParser):
    """Generic parser for normalized snapshot/delta payloads."""

    def parse(self, payload: Mapping[str, Any]) -> OptionChainSnapshot:
        event_timestamp = _coerce_datetime(payload.get("event_timestamp"))
        received_timestamp = _coerce_datetime(payload.get("received_timestamp"))
        if event_timestamp is None or received_timestamp is None:
            raise OptionChainValidationError("event_timestamp and received_timestamp are required")

        contracts_payload = payload.get("contracts")
        if not isinstance(contracts_payload, Sequence) or isinstance(contracts_payload, (str, bytes, bytearray)):
            raise OptionChainValidationError("contracts must be a sequence of mappings")

        contracts = tuple(self._parse_contract(contract_payload) for contract_payload in contracts_payload)
        return OptionChainSnapshot(
            chain_id=_require_text(payload.get("chain_id"), "chain_id"),
            underlying_symbol=_require_text(payload.get("underlying_symbol"), "underlying_symbol"),
            expiry_date=_coerce_date(payload.get("expiry_date"), "expiry_date"),
            exchange=_require_text(payload.get("exchange"), "exchange"),
            event_timestamp=event_timestamp,
            received_timestamp=received_timestamp,
            contracts=contracts,
            sequence_number=_coerce_non_negative_int(payload.get("sequence_number"), "sequence_number"),
            update_kind=_coerce_update_kind(payload.get("update_kind", "SNAPSHOT")),
            is_replay=bool(payload.get("is_replay", False)),
            source=_require_text(payload.get("source", "option_chain"), "source"),
        )

    def _parse_contract(self, payload: Any) -> OptionContractSnapshot:
        if not isinstance(payload, Mapping):
            raise OptionChainValidationError("contract payload must be a mapping")
        event_timestamp = _coerce_datetime(payload.get("event_timestamp"))
        received_timestamp = _coerce_datetime(payload.get("received_timestamp"))
        if event_timestamp is None or received_timestamp is None:
            raise OptionChainValidationError("contract timestamps are required")
        greeks_payload = payload.get("greeks")
        greeks_mapping = greeks_payload if isinstance(greeks_payload, Mapping) else {}
        return OptionContractSnapshot(
            contract_symbol=_require_text(payload.get("contract_symbol"), "contract_symbol"),
            underlying_symbol=_require_text(payload.get("underlying_symbol"), "underlying_symbol"),
            exchange=_require_text(payload.get("exchange"), "exchange"),
            expiry_date=_coerce_date(payload.get("expiry_date"), "expiry_date"),
            strike_price=_coerce_positive_decimal(payload.get("strike_price"), "strike_price"),
            option_side=_coerce_option_side(payload.get("option_side")),
            event_timestamp=event_timestamp,
            received_timestamp=received_timestamp,
            last_traded_price=_coerce_optional_non_negative_decimal(payload.get("last_traded_price"), "last_traded_price"),
            bid_price=_coerce_optional_non_negative_decimal(payload.get("bid_price"), "bid_price"),
            ask_price=_coerce_optional_non_negative_decimal(payload.get("ask_price"), "ask_price"),
            bid_quantity=_coerce_optional_non_negative_int(payload.get("bid_quantity"), "bid_quantity"),
            ask_quantity=_coerce_optional_non_negative_int(payload.get("ask_quantity"), "ask_quantity"),
            traded_volume=_coerce_optional_non_negative_int(payload.get("traded_volume"), "traded_volume"),
            open_interest=_coerce_optional_non_negative_int(payload.get("open_interest"), "open_interest"),
            implied_volatility=_coerce_optional_non_negative_decimal(payload.get("implied_volatility"), "implied_volatility"),
            greeks=OptionGreeksSnapshot(
                delta=_coerce_optional_decimal(greeks_mapping.get("delta"), "delta"),
                gamma=_coerce_optional_non_negative_decimal(greeks_mapping.get("gamma"), "gamma"),
                theta=_coerce_optional_decimal(greeks_mapping.get("theta"), "theta"),
                vega=_coerce_optional_non_negative_decimal(greeks_mapping.get("vega"), "vega"),
                rho=_coerce_optional_decimal(greeks_mapping.get("rho"), "rho"),
            ),
            is_tradable=bool(payload.get("is_tradable", True)),
            source=_require_text(payload.get("source", "option_chain"), "source"),
            raw_payload=dict(payload),
        )


class OptionChainStateStore:
    """Incremental state store for latest option chain snapshots."""

    def __init__(self, *, max_tracked_chains: int) -> None:
        self._max_tracked_chains = max_tracked_chains
        self._chains: dict[str, OptionChainSnapshot] = {}

    def upsert(self, snapshot: OptionChainSnapshot) -> None:
        if snapshot.chain_id not in self._chains and len(self._chains) >= self._max_tracked_chains:
            oldest_key = min(
                self._chains,
                key=lambda key: self._chains[key].received_timestamp,
            )
            del self._chains[oldest_key]
        if snapshot.update_kind == "SNAPSHOT" or snapshot.chain_id not in self._chains:
            self._chains[snapshot.chain_id] = snapshot
            return
        previous = self._chains[snapshot.chain_id]
        merged_contracts = _merge_contracts(previous.contracts, snapshot.contracts)
        self._chains[snapshot.chain_id] = OptionChainSnapshot(
            chain_id=snapshot.chain_id,
            underlying_symbol=snapshot.underlying_symbol,
            expiry_date=snapshot.expiry_date,
            exchange=snapshot.exchange,
            event_timestamp=snapshot.event_timestamp,
            received_timestamp=snapshot.received_timestamp,
            contracts=merged_contracts,
            sequence_number=snapshot.sequence_number,
            update_kind=snapshot.update_kind,
            is_replay=snapshot.is_replay,
            source=snapshot.source,
        )

    def get(self, chain_id: str) -> OptionChainSnapshot | None:
        return self._chains.get(chain_id)

    def snapshot(self) -> dict[str, OptionChainSnapshot]:
        return dict(self._chains)

    @property
    def size(self) -> int:
        return len(self._chains)


class OptionChainIngestionEngine:
    """Async option chain ingestion engine with bounded queue and incremental state."""

    def __init__(
        self,
        *,
        config: OptionChainIngestionConfig,
        parser: OptionChainParser | None = None,
        batch_handler: ChainBatchHandler | None = None,
        source_name: str,
    ) -> None:
        if not source_name.strip():
            raise ValueError("source_name must be non-empty")
        self._config = config
        self._parser = parser or DefaultOptionChainParser()
        self._batch_handler = batch_handler
        self._source_name = source_name.strip()
        self._state_store = OptionChainStateStore(max_tracked_chains=self._config.max_tracked_chains)
        self._queue: asyncio.Queue[OptionChainUpdateEnvelope] = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._stats = OptionChainIngestionStats()
        self._runner_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connection_id = f"{self._source_name}-option-chain"
        self._sequence_number = 0

    @property
    def stats(self) -> OptionChainIngestionStats:
        return self._stats

    @property
    def state_store(self) -> OptionChainStateStore:
        return self._state_store

    async def start(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            raise RuntimeError("option chain ingestion engine is already running")
        self._stop_event.clear()
        self._runner_task = asyncio.create_task(self._run(), name=f"{self._source_name}-option-chain-runner")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._runner_task is not None:
            self._runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None

    async def ingest_mapping(self, payload: Mapping[str, Any]) -> None:
        try:
            snapshot = self._parser.parse(payload)
        except Exception as exc:
            self._stats = OptionChainIngestionStats(
                accepted_updates=self._stats.accepted_updates,
                emitted_updates=self._stats.emitted_updates,
                dropped_updates=self._stats.dropped_updates,
                parse_failures=self._stats.parse_failures + 1,
                queue_size=self._queue.qsize(),
                tracked_chains=self._state_store.size,
                last_update_timestamp=self._stats.last_update_timestamp,
            )
            raise OptionChainValidationError(f"failed to parse option chain payload: {exc}") from exc

        self._sequence_number += 1
        self._state_store.upsert(snapshot)
        envelope = OptionChainUpdateEnvelope(
            connection_id=self._connection_id,
            sequence_number=self._sequence_number,
            chain_snapshot=self._state_store.get(snapshot.chain_id) or snapshot,
            received_timestamp=_utc_now(),
            source=self._source_name,
        )
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull as exc:
            self._stats = OptionChainIngestionStats(
                accepted_updates=self._stats.accepted_updates,
                emitted_updates=self._stats.emitted_updates,
                dropped_updates=self._stats.dropped_updates + 1,
                parse_failures=self._stats.parse_failures,
                queue_size=self._queue.qsize(),
                tracked_chains=self._state_store.size,
                last_update_timestamp=self._stats.last_update_timestamp,
            )
            if self._config.drop_on_backpressure:
                LOGGER.warning("Dropping option chain update due to queue backpressure: %s", snapshot.chain_id)
                return
            raise OptionChainBackpressureError("option chain ingestion queue is full") from exc

        self._stats = OptionChainIngestionStats(
            accepted_updates=self._stats.accepted_updates + 1,
            emitted_updates=self._stats.emitted_updates,
            dropped_updates=self._stats.dropped_updates,
            parse_failures=self._stats.parse_failures,
            queue_size=self._queue.qsize(),
            tracked_chains=self._state_store.size,
            last_update_timestamp=envelope.received_timestamp,
        )

    async def drain_updates(self) -> AsyncIterator[OptionChainUpdateEnvelope]:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield envelope
            self._queue.task_done()

    async def _run(self) -> None:
        if self._batch_handler is None:
            await self._idle_loop()
            return
        while not self._stop_event.is_set():
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=self._config.flush_interval_seconds)
            except asyncio.TimeoutError:
                continue
            batch = [first]
            while len(batch) < self._config.drain_batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._batch_handler(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()
            self._stats = OptionChainIngestionStats(
                accepted_updates=self._stats.accepted_updates,
                emitted_updates=self._stats.emitted_updates + len(batch),
                dropped_updates=self._stats.dropped_updates,
                parse_failures=self._stats.parse_failures,
                queue_size=self._queue.qsize(),
                tracked_chains=self._state_store.size,
                last_update_timestamp=self._stats.last_update_timestamp,
            )

    async def _idle_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self._config.flush_interval_seconds)



def _merge_contracts(
    base_contracts: Sequence[OptionContractSnapshot],
    delta_contracts: Sequence[OptionContractSnapshot],
) -> tuple[OptionContractSnapshot, ...]:
    merged: dict[str, OptionContractSnapshot] = {contract.contract_key: contract for contract in base_contracts}
    for contract in delta_contracts:
        merged[contract.contract_key] = contract
    ordered = tuple(sorted(merged.values(), key=lambda item: (item.expiry_date, item.strike_price, item.option_side)))
    return ordered



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
            raise OptionChainValidationError("invalid datetime value") from exc
    raise OptionChainValidationError("invalid datetime value")



def _coerce_date(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise OptionChainValidationError(f"{field_name} must be non-empty")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise OptionChainValidationError(f"{field_name} must be ISO date") from exc
    raise OptionChainValidationError(f"{field_name} must be a date")



def _require_text(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise OptionChainValidationError(f"{field_name} must be non-empty")
    return text



def _coerce_non_negative_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise OptionChainValidationError(f"{field_name} must be an integer") from exc
    if normalized < 0:
        raise OptionChainValidationError(f"{field_name} cannot be negative")
    return normalized



def _coerce_optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _coerce_non_negative_int(value, field_name)



def _coerce_positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        normalized = safe_decimal(value)
    except Exception as exc:
        raise OptionChainValidationError(f"{field_name} must be decimal-compatible") from exc
    if normalized <= 0:
        raise OptionChainValidationError(f"{field_name} must be positive")
    return normalized



def _coerce_optional_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return safe_decimal(value)
    except Exception as exc:
        raise OptionChainValidationError(f"{field_name} must be decimal-compatible") from exc



def _coerce_optional_non_negative_decimal(value: Any, field_name: str) -> Decimal | None:
    decimal_value = _coerce_optional_decimal(value, field_name)
    if decimal_value is not None and decimal_value < 0:
        raise OptionChainValidationError(f"{field_name} cannot be negative")
    return decimal_value



def _coerce_option_side(value: Any) -> OptionSide:
    normalized = _require_text(value, "option_side").upper()
    if normalized not in {"CALL", "PUT"}:
        raise OptionChainValidationError("option_side must be CALL or PUT")
    return normalized  # type: ignore[return-value]



def _coerce_update_kind(value: Any) -> ChainUpdateKind:
    normalized = _require_text(value, "update_kind").upper()
    if normalized not in {"SNAPSHOT", "DELTA"}:
        raise OptionChainValidationError("update_kind must be SNAPSHOT or DELTA")
    return normalized  # type: ignore[return-value]


__all__ = [
    "ChainBatchHandler",
    "ChainUpdateKind",
    "DefaultOptionChainParser",
    "OptionChainBackpressureError",
    "OptionChainIngestionConfig",
    "OptionChainIngestionEngine",
    "OptionChainIngestionError",
    "OptionChainIngestionStats",
    "OptionChainParser",
    "OptionChainSnapshot",
    "OptionChainStateStore",
    "OptionChainUpdateEnvelope",
    "OptionChainValidationError",
    "OptionContractSnapshot",
    "OptionGreeksSnapshot",
    "OptionSide",
]
