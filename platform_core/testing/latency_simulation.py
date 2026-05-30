# FILE: platform_core/testing/latency_simulation.py
"""
platform_core.testing.latency_simulation
═══════════════════════════════════════════════════════════════════════
Production-grade deterministic latency simulation framework for the
institutional Indian index futures and options derivatives intelligence and
automated execution platform.

This module provides replay-safe latency estimation across the event-driven
trading path by decomposing end-to-end delay into deterministic network,
serialization, broker, exchange, and queueing components. It is designed for
validation workloads, not for live execution.

Scope:
- Deterministic latency path decomposition.
- Configurable venue-safe latency profiles without exchange hardcoding.
- Queueing and burst-pressure adjustments.
- JSON-safe latency records for audit and replay.
- Tail-latency summaries for validation pipelines.
- Compatibility with replay, execution, and slippage testing layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
from decimal import getcontext
from enum import Enum


getcontext().prec = 28


class LatencySimulationError(RuntimeError):
    """Base exception for latency simulation failures."""


class LatencyConfigurationError(LatencySimulationError):
    """Raised when latency configuration or inputs are invalid."""


class LatencyPath(str, Enum):
    """Explicit simulated latency paths."""

    MARKET_DATA_INGEST = "MARKET_DATA_INGEST"
    SIGNAL_TO_ORDER = "SIGNAL_TO_ORDER"
    ORDER_TO_BROKER = "ORDER_TO_BROKER"
    BROKER_TO_EXCHANGE = "BROKER_TO_EXCHANGE"
    ORDER_ROUND_TRIP = "ORDER_ROUND_TRIP"


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    """Deterministic base latency profile in milliseconds."""

    network_ms: Decimal
    serialization_ms: Decimal
    application_ms: Decimal
    broker_ms: Decimal
    exchange_ms: Decimal
    queue_base_ms: Decimal = Decimal("0")
    burst_sensitivity: Decimal = Decimal("0.50")
    throughput_capacity_per_second: int = 1000

    def __post_init__(self) -> None:
        components = (
            self.network_ms,
            self.serialization_ms,
            self.application_ms,
            self.broker_ms,
            self.exchange_ms,
            self.queue_base_ms,
            self.burst_sensitivity,
        )
        if any(value < 0 for value in components):
            raise ValueError("latency profile components must be non-negative")
        if self.throughput_capacity_per_second <= 0:
            raise ValueError("throughput_capacity_per_second must be positive")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "network_ms": str(self.network_ms),
            "serialization_ms": str(self.serialization_ms),
            "application_ms": str(self.application_ms),
            "broker_ms": str(self.broker_ms),
            "exchange_ms": str(self.exchange_ms),
            "queue_base_ms": str(self.queue_base_ms),
            "burst_sensitivity": str(self.burst_sensitivity),
            "throughput_capacity_per_second": self.throughput_capacity_per_second,
        }


@dataclass(frozen=True, slots=True)
class LatencyContext:
    """Replay-safe contextual inputs for one latency estimate."""

    timestamp: datetime
    path: LatencyPath
    message_count: int
    payload_bytes: int
    queue_depth: int
    observed_rate_per_second: int
    burst_factor: Decimal = Decimal("1")
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.message_count <= 0:
            raise ValueError("message_count must be positive")
        if self.payload_bytes < 0:
            raise ValueError("payload_bytes cannot be negative")
        if self.queue_depth < 0:
            raise ValueError("queue_depth cannot be negative")
        if self.observed_rate_per_second < 0:
            raise ValueError("observed_rate_per_second cannot be negative")
        if self.burst_factor <= 0:
            raise ValueError("burst_factor must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "path": self.path.value,
            "message_count": self.message_count,
            "payload_bytes": self.payload_bytes,
            "queue_depth": self.queue_depth,
            "observed_rate_per_second": self.observed_rate_per_second,
            "burst_factor": str(self.burst_factor),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class LatencyBreakdown:
    """Component-wise latency decomposition."""

    network_ms: Decimal
    serialization_ms: Decimal
    application_ms: Decimal
    broker_ms: Decimal
    exchange_ms: Decimal
    queue_ms: Decimal
    total_ms: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "network_ms": str(self.network_ms),
            "serialization_ms": str(self.serialization_ms),
            "application_ms": str(self.application_ms),
            "broker_ms": str(self.broker_ms),
            "exchange_ms": str(self.exchange_ms),
            "queue_ms": str(self.queue_ms),
            "total_ms": str(self.total_ms),
        }


@dataclass(frozen=True, slots=True)
class LatencyEstimate:
    """Serializable latency estimate record."""

    context: LatencyContext
    breakdown: LatencyBreakdown

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context.to_dict(),
            "breakdown": self.breakdown.to_dict(),
        }


class LatencySimulator:
    """Deterministic latency simulator with replay-safe audit history."""

    def __init__(self, *, profile: LatencyProfile) -> None:
        self._profile = profile
        self._history: list[LatencyEstimate] = []

    @property
    def profile(self) -> LatencyProfile:
        return self._profile

    @property
    def history(self) -> tuple[LatencyEstimate, ...]:
        return tuple(self._history)

    def simulate(self, *, context: LatencyContext) -> LatencyEstimate:
        pressure_ratio = Decimal(context.observed_rate_per_second) / Decimal(self._profile.throughput_capacity_per_second)
        queue_multiplier = max(pressure_ratio - Decimal("1"), Decimal("0"))
        queue_ms = self._profile.queue_base_ms + (
            Decimal(context.queue_depth) * self._profile.burst_sensitivity * max(context.burst_factor, Decimal("1"))
        ) + (queue_multiplier * self._profile.queue_base_ms)
        serialization_ms = self._profile.serialization_ms + (
            Decimal(context.payload_bytes) / Decimal("1024") * Decimal("0.01")
        )
        application_ms = self._profile.application_ms + (Decimal(context.message_count - 1) * Decimal("0.02"))
        network_ms = self._profile.network_ms
        broker_ms = self._profile.broker_ms
        exchange_ms = self._profile.exchange_ms
        if context.path is LatencyPath.MARKET_DATA_INGEST:
            broker_ms = Decimal("0")
        elif context.path is LatencyPath.SIGNAL_TO_ORDER:
            exchange_ms = Decimal("0")
        elif context.path is LatencyPath.ORDER_TO_BROKER:
            exchange_ms = Decimal("0")
        elif context.path is LatencyPath.BROKER_TO_EXCHANGE:
            application_ms = Decimal("0")
        total_ms = network_ms + serialization_ms + application_ms + broker_ms + exchange_ms + queue_ms
        estimate = LatencyEstimate(
            context=context,
            breakdown=LatencyBreakdown(
                network_ms=_quantize_ms(network_ms),
                serialization_ms=_quantize_ms(serialization_ms),
                application_ms=_quantize_ms(application_ms),
                broker_ms=_quantize_ms(broker_ms),
                exchange_ms=_quantize_ms(exchange_ms),
                queue_ms=_quantize_ms(queue_ms),
                total_ms=_quantize_ms(total_ms),
            ),
        )
        self._history.append(estimate)
        return estimate

    def percentile_summary(self) -> dict[str, str]:
        if not self._history:
            return {"p50_ms": "0.000", "p95_ms": "0.000", "p99_ms": "0.000", "count": "0"}
        totals = sorted(item.breakdown.total_ms for item in self._history)
        return {
            "p50_ms": str(_percentile(totals, Decimal("0.50"))),
            "p95_ms": str(_percentile(totals, Decimal("0.95"))),
            "p99_ms": str(_percentile(totals, Decimal("0.99"))),
            "count": str(len(totals)),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "profile": self._profile.to_dict(),
            "history": [item.to_dict() for item in self._history],
            "summary": self.percentile_summary(),
        }



def _percentile(sorted_values: list[Decimal], quantile: Decimal) -> Decimal:
    if not sorted_values:
        raise LatencyConfigurationError("cannot compute percentile for empty series")
    if not Decimal("0") <= quantile <= Decimal("1"):
        raise LatencyConfigurationError("quantile must be within [0, 1]")
    if len(sorted_values) == 1:
        return _quantize_ms(sorted_values[0])
    position = (Decimal(len(sorted_values) - 1) * quantile)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = position - Decimal(lower_index)
    value = (sorted_values[lower_index] * (Decimal("1") - weight)) + (sorted_values[upper_index] * weight)
    return _quantize_ms(value)



def _quantize_ms(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "LatencyBreakdown",
    "LatencyConfigurationError",
    "LatencyContext",
    "LatencyEstimate",
    "LatencyPath",
    "LatencyProfile",
    "LatencySimulationError",
    "LatencySimulator",
]
