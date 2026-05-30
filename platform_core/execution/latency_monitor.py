# FILE: platform_core/execution/latency_monitor.py
"""
platform_core.execution.latency_monitor
═══════════════════════════════════════════════════════════════════════
Production-grade execution latency monitor for institutional derivatives
execution workflows specialized for Indian index futures and options.

The monitor records causal execution lifecycle timestamps, derives deterministic
latency measurements, aggregates rolling statistics, and emits JSON-safe
snapshots suitable for real-time monitoring, replay, TimescaleDB persistence,
and broker quality diagnostics.

Key properties:
- No-hindsight and replay-safe timestamp handling.
- Stage-wise latency measurements for order lifecycle events.
- Rolling percentile and jitter computation from causal observations only.
- Alert generation for threshold breaches and stale acknowledgements.
- Broker- and route-level extensibility through structured dimensions.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class LatencyMonitorError(RuntimeError):
    """Base exception for latency monitor errors."""


class LatencyValidationError(LatencyMonitorError):
    """Raised when timestamps or inputs violate causal assumptions."""


class LatencyStateError(LatencyMonitorError):
    """Raised when lifecycle events are incomplete or inconsistent."""


class LatencyStage(str, Enum):
    """Execution lifecycle stages used for latency breakdowns."""

    SIGNAL_TO_SUBMIT = "SIGNAL_TO_SUBMIT"
    SUBMIT_TO_ACK = "SUBMIT_TO_ACK"
    ACK_TO_FIRST_FILL = "ACK_TO_FIRST_FILL"
    SUBMIT_TO_FIRST_FILL = "SUBMIT_TO_FIRST_FILL"
    SUBMIT_TO_FINAL = "SUBMIT_TO_FINAL"
    CANCEL_REQUEST_TO_CONFIRM = "CANCEL_REQUEST_TO_CONFIRM"


class LatencyAlertSeverity(str, Enum):
    """Explicit alert severities."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class LatencyThresholds:
    """Deterministic latency thresholds in milliseconds."""

    submit_to_ack_warning_ms: float = 250.0
    submit_to_ack_critical_ms: float = 500.0
    submit_to_final_warning_ms: float = 1500.0
    submit_to_final_critical_ms: float = 3000.0
    cancel_to_confirm_warning_ms: float = 500.0
    cancel_to_confirm_critical_ms: float = 1000.0
    stale_ack_after_ms: float = 2000.0

    def __post_init__(self) -> None:
        for field_name in (
            "submit_to_ack_warning_ms",
            "submit_to_ack_critical_ms",
            "submit_to_final_warning_ms",
            "submit_to_final_critical_ms",
            "cancel_to_confirm_warning_ms",
            "cancel_to_confirm_critical_ms",
            "stale_ack_after_ms",
        ):
            value = getattr(self, field_name)
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{field_name} must be positive and finite")
        if self.submit_to_ack_warning_ms > self.submit_to_ack_critical_ms:
            raise ValueError("submit_to_ack_warning_ms must be <= submit_to_ack_critical_ms")
        if self.submit_to_final_warning_ms > self.submit_to_final_critical_ms:
            raise ValueError("submit_to_final_warning_ms must be <= submit_to_final_critical_ms")
        if self.cancel_to_confirm_warning_ms > self.cancel_to_confirm_critical_ms:
            raise ValueError("cancel_to_confirm_warning_ms must be <= cancel_to_confirm_critical_ms")


@dataclass(frozen=True, slots=True)
class LatencyMonitorConfig:
    """Configuration for rolling latency monitoring."""

    rolling_window_size: int = 500
    thresholds: LatencyThresholds = field(default_factory=LatencyThresholds)

    def __post_init__(self) -> None:
        if self.rolling_window_size <= 0:
            raise ValueError("rolling_window_size must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionTimestamps:
    """Serializable causal timestamps for one order lifecycle."""

    order_id: str
    broker_name: str
    route_name: str
    signal_timestamp: datetime | None = None
    submit_timestamp: datetime | None = None
    ack_timestamp: datetime | None = None
    first_fill_timestamp: datetime | None = None
    final_timestamp: datetime | None = None
    cancel_request_timestamp: datetime | None = None
    cancel_confirm_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ValueError("order_id must be non-empty")
        if not self.broker_name.strip():
            raise ValueError("broker_name must be non-empty")
        if not self.route_name.strip():
            raise ValueError("route_name must be non-empty")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "order_id": self.order_id,
            "broker_name": self.broker_name,
            "route_name": self.route_name,
            "signal_timestamp": _optional_iso(self.signal_timestamp),
            "submit_timestamp": _optional_iso(self.submit_timestamp),
            "ack_timestamp": _optional_iso(self.ack_timestamp),
            "first_fill_timestamp": _optional_iso(self.first_fill_timestamp),
            "final_timestamp": _optional_iso(self.final_timestamp),
            "cancel_request_timestamp": _optional_iso(self.cancel_request_timestamp),
            "cancel_confirm_timestamp": _optional_iso(self.cancel_confirm_timestamp),
        }


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    """Serializable stage measurement."""

    order_id: str
    broker_name: str
    route_name: str
    stage: LatencyStage
    latency_ms: float
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.latency_ms < 0.0 or not isfinite(self.latency_ms):
            raise ValueError("latency_ms must be non-negative and finite")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "order_id": self.order_id,
            "broker_name": self.broker_name,
            "route_name": self.route_name,
            "stage": self.stage.value,
            "latency_ms": self.latency_ms,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LatencyAlert:
    """Serializable latency alert."""

    timestamp: datetime
    order_id: str
    broker_name: str
    route_name: str
    severity: LatencyAlertSeverity
    message: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "order_id": self.order_id,
            "broker_name": self.broker_name,
            "route_name": self.route_name,
            "severity": self.severity.value,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class LatencyStatsSnapshot:
    """Serializable rolling statistics snapshot."""

    stage: LatencyStage
    count: int
    min_latency_ms: float | None
    max_latency_ms: float | None
    mean_latency_ms: float | None
    p50_latency_ms: float | None
    p90_latency_ms: float | None
    p99_latency_ms: float | None
    jitter_ms: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "stage": self.stage.value,
            "count": self.count,
            "min_latency_ms": self.min_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "mean_latency_ms": self.mean_latency_ms,
            "p50_latency_ms": self.p50_latency_ms,
            "p90_latency_ms": self.p90_latency_ms,
            "p99_latency_ms": self.p99_latency_ms,
            "jitter_ms": self.jitter_ms,
        }


class LatencyMonitor:
    """Deterministic rolling latency monitor for execution workflows."""

    def __init__(self, *, config: LatencyMonitorConfig | None = None) -> None:
        self._config = config or LatencyMonitorConfig()
        self._measurements_by_stage: dict[LatencyStage, deque[float]] = {
            stage: deque(maxlen=self._config.rolling_window_size) for stage in LatencyStage
        }
        self._last_observed_timestamp: datetime | None = None
        self._order_timestamps: dict[str, ExecutionTimestamps] = {}
        self._latest_measurements: dict[str, list[LatencyMeasurement]] = {}
        self._latest_alerts: dict[str, list[LatencyAlert]] = {}

    def record(self, *, timestamps: ExecutionTimestamps, valuation_timestamp: datetime | None = None) -> tuple[LatencyMeasurement, ...]:
        normalized = self._normalize_timestamps(timestamps)
        self._validate_timestamps(normalized=normalized, valuation_timestamp=valuation_timestamp)
        measurements = self._derive_measurements(normalized)
        alerts = self._derive_alerts(normalized, measurements, valuation_timestamp=valuation_timestamp)
        self._order_timestamps[normalized.order_id] = normalized
        self._latest_measurements[normalized.order_id] = list(measurements)
        self._latest_alerts[normalized.order_id] = list(alerts)
        for measurement in measurements:
            self._measurements_by_stage[measurement.stage].append(measurement.latency_ms)
            self._last_observed_timestamp = measurement.timestamp
        if not measurements:
            candidate_timestamps = [
                ts
                for ts in (
                    normalized.signal_timestamp,
                    normalized.submit_timestamp,
                    normalized.ack_timestamp,
                    normalized.first_fill_timestamp,
                    normalized.final_timestamp,
                    normalized.cancel_request_timestamp,
                    normalized.cancel_confirm_timestamp,
                )
                if ts is not None
            ]
            if candidate_timestamps:
                latest_candidate = max(candidate_timestamps)
                if self._last_observed_timestamp is None or latest_candidate >= self._last_observed_timestamp:
                    self._last_observed_timestamp = latest_candidate
        return tuple(measurements)

    def get_alerts(self, *, order_id: str) -> tuple[LatencyAlert, ...]:
        return tuple(self._latest_alerts.get(order_id, []))

    def get_measurements(self, *, order_id: str) -> tuple[LatencyMeasurement, ...]:
        return tuple(self._latest_measurements.get(order_id, []))

    def get_stats_snapshot(self) -> tuple[LatencyStatsSnapshot, ...]:
        snapshots: list[LatencyStatsSnapshot] = []
        for stage in LatencyStage:
            values = list(self._measurements_by_stage[stage])
            snapshots.append(
                LatencyStatsSnapshot(
                    stage=stage,
                    count=len(values),
                    min_latency_ms=min(values) if values else None,
                    max_latency_ms=max(values) if values else None,
                    mean_latency_ms=(sum(values) / len(values)) if values else None,
                    p50_latency_ms=_percentile(values, 50.0) if values else None,
                    p90_latency_ms=_percentile(values, 90.0) if values else None,
                    p99_latency_ms=_percentile(values, 99.0) if values else None,
                    jitter_ms=_sample_standard_deviation(values) if len(values) >= 2 else None,
                )
            )
        return tuple(snapshots)

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "last_observed_timestamp": _optional_iso(self._last_observed_timestamp),
            "tracked_orders": len(self._order_timestamps),
            "stats": [item.to_dict() for item in self.get_stats_snapshot()],
            "latest_measurements": {
                order_id: [measurement.to_dict() for measurement in measurements]
                for order_id, measurements in self._latest_measurements.items()
            },
            "latest_alerts": {
                order_id: [alert.to_dict() for alert in alerts]
                for order_id, alerts in self._latest_alerts.items()
            },
        }

    def _normalize_timestamps(self, timestamps: ExecutionTimestamps) -> ExecutionTimestamps:
        return ExecutionTimestamps(
            order_id=timestamps.order_id,
            broker_name=timestamps.broker_name,
            route_name=timestamps.route_name,
            signal_timestamp=_optional_ensure_utc(timestamps.signal_timestamp),
            submit_timestamp=_optional_ensure_utc(timestamps.submit_timestamp),
            ack_timestamp=_optional_ensure_utc(timestamps.ack_timestamp),
            first_fill_timestamp=_optional_ensure_utc(timestamps.first_fill_timestamp),
            final_timestamp=_optional_ensure_utc(timestamps.final_timestamp),
            cancel_request_timestamp=_optional_ensure_utc(timestamps.cancel_request_timestamp),
            cancel_confirm_timestamp=_optional_ensure_utc(timestamps.cancel_confirm_timestamp),
        )

    def _validate_timestamps(self, *, normalized: ExecutionTimestamps, valuation_timestamp: datetime | None) -> None:
        ordered_points = [
            ("signal_timestamp", normalized.signal_timestamp),
            ("submit_timestamp", normalized.submit_timestamp),
            ("ack_timestamp", normalized.ack_timestamp),
            ("first_fill_timestamp", normalized.first_fill_timestamp),
            ("final_timestamp", normalized.final_timestamp),
            ("cancel_request_timestamp", normalized.cancel_request_timestamp),
            ("cancel_confirm_timestamp", normalized.cancel_confirm_timestamp),
        ]
        present_points = [(name, value) for name, value in ordered_points if value is not None]
        for index in range(1, len(present_points)):
            previous_name, previous_value = present_points[index - 1]
            current_name, current_value = present_points[index]
            if current_value < previous_value:
                raise LatencyValidationError(
                    f"timestamp ordering violated: {current_name} < {previous_name} for order {normalized.order_id}"
                )
        if self._last_observed_timestamp is not None and present_points:
            earliest_present = min(value for _, value in present_points)
            if earliest_present < self._last_observed_timestamp:
                raise LatencyValidationError("timestamps must be globally non-decreasing for replay safety")
        if normalized.submit_timestamp is None and (
            normalized.ack_timestamp is not None
            or normalized.first_fill_timestamp is not None
            or normalized.final_timestamp is not None
        ):
            raise LatencyValidationError("submit_timestamp is required for downstream lifecycle latency measurement")
        if normalized.cancel_confirm_timestamp is not None and normalized.cancel_request_timestamp is None:
            raise LatencyValidationError("cancel_request_timestamp is required when cancel_confirm_timestamp is provided")
        if valuation_timestamp is not None:
            normalized_valuation = _ensure_utc(valuation_timestamp)
            for name, value in present_points:
                if value > normalized_valuation:
                    raise LatencyValidationError(f"{name} exceeds valuation_timestamp")

    def _derive_measurements(self, timestamps: ExecutionTimestamps) -> list[LatencyMeasurement]:
        measurements: list[LatencyMeasurement] = []
        if timestamps.signal_timestamp is not None and timestamps.submit_timestamp is not None:
            measurements.append(self._build_measurement(timestamps, LatencyStage.SIGNAL_TO_SUBMIT, timestamps.signal_timestamp, timestamps.submit_timestamp))
        if timestamps.submit_timestamp is not None and timestamps.ack_timestamp is not None:
            measurements.append(self._build_measurement(timestamps, LatencyStage.SUBMIT_TO_ACK, timestamps.submit_timestamp, timestamps.ack_timestamp))
        if timestamps.ack_timestamp is not None and timestamps.first_fill_timestamp is not None:
            measurements.append(self._build_measurement(timestamps, LatencyStage.ACK_TO_FIRST_FILL, timestamps.ack_timestamp, timestamps.first_fill_timestamp))
        if timestamps.submit_timestamp is not None and timestamps.first_fill_timestamp is not None:
            measurements.append(self._build_measurement(timestamps, LatencyStage.SUBMIT_TO_FIRST_FILL, timestamps.submit_timestamp, timestamps.first_fill_timestamp))
        if timestamps.submit_timestamp is not None and timestamps.final_timestamp is not None:
            measurements.append(self._build_measurement(timestamps, LatencyStage.SUBMIT_TO_FINAL, timestamps.submit_timestamp, timestamps.final_timestamp))
        if timestamps.cancel_request_timestamp is not None and timestamps.cancel_confirm_timestamp is not None:
            measurements.append(
                self._build_measurement(
                    timestamps,
                    LatencyStage.CANCEL_REQUEST_TO_CONFIRM,
                    timestamps.cancel_request_timestamp,
                    timestamps.cancel_confirm_timestamp,
                )
            )
        return measurements

    def _build_measurement(
        self,
        timestamps: ExecutionTimestamps,
        stage: LatencyStage,
        start_timestamp: datetime,
        end_timestamp: datetime,
    ) -> LatencyMeasurement:
        latency_ms = max(0.0, (end_timestamp - start_timestamp).total_seconds() * 1000.0)
        return LatencyMeasurement(
            order_id=timestamps.order_id,
            broker_name=timestamps.broker_name,
            route_name=timestamps.route_name,
            stage=stage,
            latency_ms=latency_ms,
            timestamp=end_timestamp,
        )

    def _derive_alerts(
        self,
        timestamps: ExecutionTimestamps,
        measurements: list[LatencyMeasurement],
        *,
        valuation_timestamp: datetime | None,
    ) -> list[LatencyAlert]:
        alerts: list[LatencyAlert] = []
        for measurement in measurements:
            if measurement.stage is LatencyStage.SUBMIT_TO_ACK:
                severity = _severity_from_thresholds(
                    measurement.latency_ms,
                    warning_ms=self._config.thresholds.submit_to_ack_warning_ms,
                    critical_ms=self._config.thresholds.submit_to_ack_critical_ms,
                )
                if severity is not None:
                    alerts.append(
                        self._build_alert(
                            timestamps=timestamps,
                            severity=severity,
                            message="submit-to-ack latency threshold breached",
                            metadata={"stage": measurement.stage.value, "latency_ms": measurement.latency_ms},
                            timestamp=measurement.timestamp,
                        )
                    )
            if measurement.stage is LatencyStage.SUBMIT_TO_FINAL:
                severity = _severity_from_thresholds(
                    measurement.latency_ms,
                    warning_ms=self._config.thresholds.submit_to_final_warning_ms,
                    critical_ms=self._config.thresholds.submit_to_final_critical_ms,
                )
                if severity is not None:
                    alerts.append(
                        self._build_alert(
                            timestamps=timestamps,
                            severity=severity,
                            message="submit-to-final latency threshold breached",
                            metadata={"stage": measurement.stage.value, "latency_ms": measurement.latency_ms},
                            timestamp=measurement.timestamp,
                        )
                    )
            if measurement.stage is LatencyStage.CANCEL_REQUEST_TO_CONFIRM:
                severity = _severity_from_thresholds(
                    measurement.latency_ms,
                    warning_ms=self._config.thresholds.cancel_to_confirm_warning_ms,
                    critical_ms=self._config.thresholds.cancel_to_confirm_critical_ms,
                )
                if severity is not None:
                    alerts.append(
                        self._build_alert(
                            timestamps=timestamps,
                            severity=severity,
                            message="cancel-to-confirm latency threshold breached",
                            metadata={"stage": measurement.stage.value, "latency_ms": measurement.latency_ms},
                            timestamp=measurement.timestamp,
                        )
                    )
        if timestamps.submit_timestamp is not None and timestamps.ack_timestamp is None and valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            stale_ms = (normalized_valuation_timestamp - timestamps.submit_timestamp).total_seconds() * 1000.0
            if stale_ms >= self._config.thresholds.stale_ack_after_ms:
                alerts.append(
                    self._build_alert(
                        timestamps=timestamps,
                        severity=LatencyAlertSeverity.CRITICAL,
                        message="stale acknowledgement detected after submission",
                        metadata={
                            "stage": LatencyStage.SUBMIT_TO_ACK.value,
                            "stale_ms": stale_ms,
                            "threshold_ms": self._config.thresholds.stale_ack_after_ms,
                        },
                        timestamp=normalized_valuation_timestamp,
                    )
                )
        return alerts

    def _build_alert(
        self,
        *,
        timestamps: ExecutionTimestamps,
        severity: LatencyAlertSeverity,
        message: str,
        metadata: dict[str, JsonValue],
        timestamp: datetime,
    ) -> LatencyAlert:
        return LatencyAlert(
            timestamp=timestamp,
            order_id=timestamps.order_id,
            broker_name=timestamps.broker_name,
            route_name=timestamps.route_name,
            severity=severity,
            message=message,
            metadata=metadata,
        )



def _severity_from_thresholds(
    latency_ms: float,
    *,
    warning_ms: float,
    critical_ms: float,
) -> LatencyAlertSeverity | None:
    if latency_ms >= critical_ms:
        return LatencyAlertSeverity.CRITICAL
    if latency_ms >= warning_ms:
        return LatencyAlertSeverity.WARNING
    return None



def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    if percentile <= 0.0:
        return min(values)
    if percentile >= 100.0:
        return max(values)
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100.0
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight



def _sample_standard_deviation(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / (len(values) - 1)
    return variance ** 0.5



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _optional_ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()


__all__ = [
    "ExecutionTimestamps",
    "LatencyAlert",
    "LatencyAlertSeverity",
    "LatencyMeasurement",
    "LatencyMonitor",
    "LatencyMonitorConfig",
    "LatencyMonitorError",
    "LatencyStage",
    "LatencyStatsSnapshot",
    "LatencyThresholds",
    "LatencyStateError",
    "LatencyValidationError",
]
