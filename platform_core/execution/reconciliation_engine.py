# FILE: platform_core/execution/reconciliation_engine.py
"""
platform_core.execution.reconciliation_engine
═══════════════════════════════════════════════════════════════════════
Production-grade reconciliation engine for institutional execution workflows
specialized for Indian index futures and options markets.

The engine compares internal order/position state with broker-observed state,
detects discrepancies deterministically, and emits JSON-serializable breaks,
resolution hints, and reconciliation snapshots suitable for replay, audit,
Redis caching, and TimescaleDB/PostgreSQL persistence.

Scope:
- Order-level lifecycle reconciliation.
- Fill quantity and average-price reconciliation.
- Missing-order detection across internal and broker books.
- Position reconciliation at tradingsymbol level.
- Controlled severity classification for operational escalation.
- No automatic state mutation; this engine diagnoses only.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ReconciliationError(RuntimeError):
    """Base exception for reconciliation failures."""


class ReconciliationInputError(ReconciliationError):
    """Raised when reconciliation inputs are invalid."""


class ReconciliationStateError(ReconciliationError):
    """Raised when reconciliation chronology or assumptions are invalid."""


class BreakSeverity(str, Enum):
    """Explicit severity levels for reconciliation breaks."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class BreakType(str, Enum):
    """Explicit break taxonomy."""

    MISSING_INTERNAL_ORDER = "MISSING_INTERNAL_ORDER"
    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"
    STATE_MISMATCH = "STATE_MISMATCH"
    FILLED_QUANTITY_MISMATCH = "FILLED_QUANTITY_MISMATCH"
    AVERAGE_PRICE_MISMATCH = "AVERAGE_PRICE_MISMATCH"
    BROKER_ORDER_LINK_MISMATCH = "BROKER_ORDER_LINK_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    """Configuration for deterministic reconciliation thresholds."""

    average_price_tolerance_fraction: float = 0.0005
    max_position_quantity_difference_for_medium: int = 0
    max_filled_quantity_difference_for_medium: int = 0
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.average_price_tolerance_fraction <= 1.0:
            raise ValueError("average_price_tolerance_fraction must be between 0 and 1")
        if self.max_position_quantity_difference_for_medium < 0:
            raise ValueError("max_position_quantity_difference_for_medium must be >= 0")
        if self.max_filled_quantity_difference_for_medium < 0:
            raise ValueError("max_filled_quantity_difference_for_medium must be >= 0")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class InternalOrderRecord:
    """Serializable internal order state."""

    client_order_id: str
    broker_order_id: str | None
    tradingsymbol: str
    state: str
    quantity: int
    filled_quantity: int
    average_price: float | None

    def __post_init__(self) -> None:
        if not self.client_order_id.strip() or not self.tradingsymbol.strip() or not self.state.strip():
            raise ValueError("client_order_id, tradingsymbol, and state must be non-empty")
        if self.broker_order_id is not None and not self.broker_order_id.strip():
            raise ValueError("broker_order_id must be non-empty when provided")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be between 0 and quantity")
        if self.average_price is not None and (self.average_price < 0.0 or not isfinite(self.average_price)):
            raise ValueError("average_price must be non-negative and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "tradingsymbol": self.tradingsymbol,
            "state": self.state,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderRecord:
    """Serializable broker order state."""

    broker_order_id: str
    tradingsymbol: str
    status: str
    quantity: int
    filled_quantity: int
    average_price: float | None

    def __post_init__(self) -> None:
        if not self.broker_order_id.strip() or not self.tradingsymbol.strip() or not self.status.strip():
            raise ValueError("broker_order_id, tradingsymbol, and status must be non-empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must be between 0 and quantity")
        if self.average_price is not None and (self.average_price < 0.0 or not isfinite(self.average_price)):
            raise ValueError("average_price must be non-negative and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "broker_order_id": self.broker_order_id,
            "tradingsymbol": self.tradingsymbol,
            "status": self.status,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
        }


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """Serializable position state for internal or broker books."""

    tradingsymbol: str
    net_quantity: int
    average_price: float | None = None

    def __post_init__(self) -> None:
        if not self.tradingsymbol.strip():
            raise ValueError("tradingsymbol must be non-empty")
        if self.average_price is not None and (self.average_price < 0.0 or not isfinite(self.average_price)):
            raise ValueError("average_price must be non-negative and finite when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "tradingsymbol": self.tradingsymbol,
            "net_quantity": self.net_quantity,
            "average_price": self.average_price,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshotInput:
    """Serializable input snapshot for reconciliation."""

    timestamp: datetime
    internal_orders: tuple[InternalOrderRecord, ...]
    broker_orders: tuple[BrokerOrderRecord, ...]
    internal_positions: tuple[PositionRecord, ...]
    broker_positions: tuple[PositionRecord, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "internal_orders": [record.to_dict() for record in self.internal_orders],
            "broker_orders": [record.to_dict() for record in self.broker_orders],
            "internal_positions": [record.to_dict() for record in self.internal_positions],
            "broker_positions": [record.to_dict() for record in self.broker_positions],
        }


@dataclass(frozen=True, slots=True)
class ReconciliationBreak:
    """Serializable reconciliation discrepancy."""

    break_type: BreakType
    severity: BreakSeverity
    entity_key: str
    message: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "break_type": self.break_type.value,
            "severity": self.severity.value,
            "entity_key": self.entity_key,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Serializable reconciliation result."""

    timestamp: datetime
    break_count: int
    highest_severity: BreakSeverity | None
    breaks: tuple[ReconciliationBreak, ...]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "break_count": self.break_count,
            "highest_severity": self.highest_severity.value if self.highest_severity is not None else None,
            "breaks": [item.to_dict() for item in self.breaks],
            "metadata": self.metadata,
        }


class ReconciliationEngine:
    """Deterministic order and position reconciliation engine."""

    def __init__(self, *, config: ReconciliationConfig | None = None) -> None:
        self._config = config or ReconciliationConfig()
        self._last_timestamp: datetime | None = None
        self._processed_snapshots = 0
        self._last_result: ReconciliationResult | None = None

    def reset(self) -> None:
        self._last_timestamp = None
        self._processed_snapshots = 0
        self._last_result = None

    def reconcile(
        self,
        *,
        snapshot: ReconciliationSnapshotInput,
        valuation_timestamp: datetime | None = None,
    ) -> ReconciliationResult:
        timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        breaks: list[ReconciliationBreak] = []
        internal_by_client = {record.client_order_id: record for record in snapshot.internal_orders}
        internal_by_broker = {
            record.broker_order_id: record
            for record in snapshot.internal_orders
            if record.broker_order_id is not None
        }
        broker_by_id = {record.broker_order_id: record for record in snapshot.broker_orders}

        for broker_order_id, broker_record in broker_by_id.items():
            internal_record = internal_by_broker.get(broker_order_id)
            if internal_record is None:
                breaks.append(
                    ReconciliationBreak(
                        break_type=BreakType.MISSING_INTERNAL_ORDER,
                        severity=BreakSeverity.HIGH,
                        entity_key=broker_order_id,
                        message="broker order exists without linked internal record",
                        metadata={"broker_order": broker_record.to_dict()},
                    )
                )
                continue
            breaks.extend(self._reconcile_order_pair(internal_record=internal_record, broker_record=broker_record))

        for client_order_id, internal_record in internal_by_client.items():
            if internal_record.broker_order_id is None:
                breaks.append(
                    ReconciliationBreak(
                        break_type=BreakType.BROKER_ORDER_LINK_MISMATCH,
                        severity=BreakSeverity.MEDIUM,
                        entity_key=client_order_id,
                        message="internal order has no broker_order_id for reconciliation",
                        metadata={"internal_order": internal_record.to_dict()},
                    )
                )
                continue
            if internal_record.broker_order_id not in broker_by_id:
                breaks.append(
                    ReconciliationBreak(
                        break_type=BreakType.MISSING_BROKER_ORDER,
                        severity=BreakSeverity.HIGH,
                        entity_key=client_order_id,
                        message="internal order link not present in broker book",
                        metadata={"internal_order": internal_record.to_dict()},
                    )
                )

        breaks.extend(
            self._reconcile_positions(
                internal_positions=snapshot.internal_positions,
                broker_positions=snapshot.broker_positions,
            )
        )

        highest_severity = _highest_severity(breaks)
        result = ReconciliationResult(
            timestamp=timestamp,
            break_count=len(breaks),
            highest_severity=highest_severity,
            breaks=tuple(breaks),
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "internal_order_count": len(snapshot.internal_orders),
                "broker_order_count": len(snapshot.broker_orders),
                "internal_position_count": len(snapshot.internal_positions),
                "broker_position_count": len(snapshot.broker_positions),
            },
        )
        self._processed_snapshots += 1
        self._last_timestamp = timestamp
        self._last_result = result
        return result

    def get_last_result(self) -> ReconciliationResult:
        if self._last_result is None:
            raise ReconciliationStateError("no reconciliation result available")
        return self._last_result

    def _validate_snapshot(
        self,
        *,
        snapshot: ReconciliationSnapshotInput,
        valuation_timestamp: datetime | None,
    ) -> None:
        timestamp = _ensure_utc(snapshot.timestamp)
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ReconciliationInputError("snapshot timestamp must be non-decreasing for replay safety")
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if timestamp > normalized_valuation_timestamp:
                raise ReconciliationInputError("snapshot timestamp cannot exceed valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise ReconciliationInputError("snapshot is stale relative to valuation_timestamp")

    def _reconcile_order_pair(
        self,
        *,
        internal_record: InternalOrderRecord,
        broker_record: BrokerOrderRecord,
    ) -> list[ReconciliationBreak]:
        breaks: list[ReconciliationBreak] = []
        normalized_internal_state = _normalize_internal_state(internal_record.state)
        normalized_broker_state = _normalize_broker_state(broker_record.status)
        if normalized_internal_state != normalized_broker_state:
            breaks.append(
                ReconciliationBreak(
                    break_type=BreakType.STATE_MISMATCH,
                    severity=BreakSeverity.MEDIUM,
                    entity_key=internal_record.client_order_id,
                    message="internal and broker order states do not align",
                    metadata={
                        "internal_state": internal_record.state,
                        "broker_status": broker_record.status,
                    },
                )
            )
        filled_quantity_difference = abs(internal_record.filled_quantity - broker_record.filled_quantity)
        if filled_quantity_difference > self._config.max_filled_quantity_difference_for_medium:
            breaks.append(
                ReconciliationBreak(
                    break_type=BreakType.FILLED_QUANTITY_MISMATCH,
                    severity=BreakSeverity.HIGH,
                    entity_key=internal_record.client_order_id,
                    message="filled quantity differs between internal and broker records",
                    metadata={
                        "internal_filled_quantity": internal_record.filled_quantity,
                        "broker_filled_quantity": broker_record.filled_quantity,
                    },
                )
            )
        if _average_price_mismatch(
            internal_price=internal_record.average_price,
            broker_price=broker_record.average_price,
            tolerance_fraction=self._config.average_price_tolerance_fraction,
        ):
            breaks.append(
                ReconciliationBreak(
                    break_type=BreakType.AVERAGE_PRICE_MISMATCH,
                    severity=BreakSeverity.MEDIUM,
                    entity_key=internal_record.client_order_id,
                    message="average execution price differs beyond tolerance",
                    metadata={
                        "internal_average_price": internal_record.average_price,
                        "broker_average_price": broker_record.average_price,
                        "tolerance_fraction": self._config.average_price_tolerance_fraction,
                    },
                )
            )
        return breaks

    def _reconcile_positions(
        self,
        *,
        internal_positions: tuple[PositionRecord, ...],
        broker_positions: tuple[PositionRecord, ...],
    ) -> list[ReconciliationBreak]:
        breaks: list[ReconciliationBreak] = []
        internal_by_symbol = {record.tradingsymbol: record for record in internal_positions}
        broker_by_symbol = {record.tradingsymbol: record for record in broker_positions}
        all_symbols = sorted(set(internal_by_symbol) | set(broker_by_symbol))
        for tradingsymbol in all_symbols:
            internal_record = internal_by_symbol.get(tradingsymbol)
            broker_record = broker_by_symbol.get(tradingsymbol)
            if internal_record is None or broker_record is None:
                breaks.append(
                    ReconciliationBreak(
                        break_type=BreakType.POSITION_MISMATCH,
                        severity=BreakSeverity.HIGH,
                        entity_key=tradingsymbol,
                        message="position missing from one side of reconciliation",
                        metadata={
                            "internal_position": internal_record.to_dict() if internal_record is not None else None,
                            "broker_position": broker_record.to_dict() if broker_record is not None else None,
                        },
                    )
                )
                continue
            quantity_difference = abs(internal_record.net_quantity - broker_record.net_quantity)
            if quantity_difference > self._config.max_position_quantity_difference_for_medium:
                breaks.append(
                    ReconciliationBreak(
                        break_type=BreakType.POSITION_MISMATCH,
                        severity=BreakSeverity.CRITICAL,
                        entity_key=tradingsymbol,
                        message="position quantity differs between internal and broker books",
                        metadata={
                            "internal_net_quantity": internal_record.net_quantity,
                            "broker_net_quantity": broker_record.net_quantity,
                        },
                    )
                )
        return breaks



def _normalize_internal_state(state: str) -> str:
    normalized = state.strip().upper()
    mapping = {
        "CREATED": "PENDING",
        "SUBMITTING": "PENDING",
        "ACKNOWLEDGED": "OPEN",
        "PARTIALLY_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCEL_PENDING": "CANCEL_PENDING",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "FAILED": "FAILED",
    }
    return mapping.get(normalized, normalized)



def _normalize_broker_state(status: str) -> str:
    normalized = status.strip().upper()
    mapping = {
        "OPEN": "OPEN",
        "COMPLETE": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "CANCEL PENDING": "CANCEL_PENDING",
        "MODIFY PENDING": "OPEN",
        "TRIGGER PENDING": "OPEN",
        "PUT ORDER REQ RECEIVED": "PENDING",
        "VALIDATION PENDING": "PENDING",
        "MODIFY VALIDATION PENDING": "PENDING",
    }
    return mapping.get(normalized, normalized)



def _average_price_mismatch(*, internal_price: float | None, broker_price: float | None, tolerance_fraction: float) -> bool:
    if internal_price is None and broker_price is None:
        return False
    if internal_price is None or broker_price is None:
        return True
    if internal_price == broker_price:
        return False
    reference = max(abs(internal_price), abs(broker_price), 1e-12)
    return abs(internal_price - broker_price) / reference > tolerance_fraction



def _highest_severity(breaks: list[ReconciliationBreak]) -> BreakSeverity | None:
    if not breaks:
        return None
    rank = {
        BreakSeverity.LOW: 1,
        BreakSeverity.MEDIUM: 2,
        BreakSeverity.HIGH: 3,
        BreakSeverity.CRITICAL: 4,
    }
    return max(breaks, key=lambda item: rank[item.severity]).severity



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "BreakSeverity",
    "BreakType",
    "BrokerOrderRecord",
    "InternalOrderRecord",
    "PositionRecord",
    "ReconciliationBreak",
    "ReconciliationConfig",
    "ReconciliationEngine",
    "ReconciliationError",
    "ReconciliationInputError",
    "ReconciliationResult",
    "ReconciliationSnapshotInput",
    "ReconciliationStateError",
]
