# FILE: platform_core/testing/stress_testing.py
"""
platform_core.testing.stress_testing
═══════════════════════════════════════════════════════════════════════
Production-grade stress testing framework for the institutional Indian index
futures and options derivatives intelligence and automated execution platform.

This module provides deterministic, replay-safe stress scenario orchestration
for validating service resilience, event-driven throughput limits, latency
breach behavior, recovery assumptions, and failure threshold detection under
adverse synthetic workloads.

Scope:
- Deterministic stress profile generation.
- Async-compatible scenario executor wiring.
- Threshold-based health evaluation and failure classification.
- Reproducible sequence generation for adverse workload waves.
- Structured result, summary, and audit records.
- Foundation for system resilience validation without live side effects.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Awaitable
from typing import Callable


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
StressExecutor = Callable[["StressStep"], Awaitable["StressMeasurement"] | "StressMeasurement"]


class StressTestingError(RuntimeError):
    """Base exception for stress testing failures."""


class StressConfigurationError(StressTestingError):
    """Raised when a stress configuration is invalid."""


class StressExecutionError(StressTestingError):
    """Raised when a stress scenario fails to execute."""


class StressOutcome(str, Enum):
    """Scenario-level stress outcomes."""

    PASSED = "PASSED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class StressFailureMode(str, Enum):
    """Explicit failure classifications for adverse stress conditions."""

    NONE = "NONE"
    LATENCY_BREACH = "LATENCY_BREACH"
    ERROR_RATE_BREACH = "ERROR_RATE_BREACH"
    THROUGHPUT_COLLAPSE = "THROUGHPUT_COLLAPSE"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    EXECUTION_EXCEPTION = "EXECUTION_EXCEPTION"


@dataclass(frozen=True, slots=True)
class StressThresholds:
    """Acceptable operating boundaries during stress."""

    max_latency_ms: float
    max_error_rate: float
    min_throughput_per_second: float
    max_cpu_utilization: float = 1.0
    max_memory_utilization: float = 1.0

    def __post_init__(self) -> None:
        if self.max_latency_ms <= 0.0:
            raise ValueError("max_latency_ms must be positive")
        if not 0.0 <= self.max_error_rate <= 1.0:
            raise ValueError("max_error_rate must be within [0, 1]")
        if self.min_throughput_per_second <= 0.0:
            raise ValueError("min_throughput_per_second must be positive")
        if not 0.0 <= self.max_cpu_utilization <= 1.0:
            raise ValueError("max_cpu_utilization must be within [0, 1]")
        if not 0.0 <= self.max_memory_utilization <= 1.0:
            raise ValueError("max_memory_utilization must be within [0, 1]")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "max_latency_ms": self.max_latency_ms,
            "max_error_rate": self.max_error_rate,
            "min_throughput_per_second": self.min_throughput_per_second,
            "max_cpu_utilization": self.max_cpu_utilization,
            "max_memory_utilization": self.max_memory_utilization,
        }


@dataclass(frozen=True, slots=True)
class StressProfile:
    """Deterministic workload progression policy."""

    start_rate_per_second: float
    step_rate_per_second: float
    step_count: int
    burst_multiplier: float = 1.0
    deterministic_seed: int = 42

    def __post_init__(self) -> None:
        if self.start_rate_per_second <= 0.0:
            raise ValueError("start_rate_per_second must be positive")
        if self.step_rate_per_second <= 0.0:
            raise ValueError("step_rate_per_second must be positive")
        if self.step_count <= 0:
            raise ValueError("step_count must be positive")
        if self.burst_multiplier <= 0.0:
            raise ValueError("burst_multiplier must be positive")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "start_rate_per_second": self.start_rate_per_second,
            "step_rate_per_second": self.step_rate_per_second,
            "step_count": self.step_count,
            "burst_multiplier": self.burst_multiplier,
            "deterministic_seed": self.deterministic_seed,
        }


@dataclass(frozen=True, slots=True)
class StressStep:
    """One deterministic stress workload step."""

    step_index: int
    target_rate_per_second: float
    burst_factor: float
    expected_events: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step_index": self.step_index,
            "target_rate_per_second": self.target_rate_per_second,
            "burst_factor": self.burst_factor,
            "expected_events": self.expected_events,
        }


@dataclass(frozen=True, slots=True)
class StressMeasurement:
    """Executor-returned measurement for one stress step."""

    observed_latency_ms: float
    observed_error_rate: float
    observed_throughput_per_second: float
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    notes: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_latency_ms < 0.0:
            raise ValueError("observed_latency_ms must be non-negative")
        if not 0.0 <= self.observed_error_rate <= 1.0:
            raise ValueError("observed_error_rate must be within [0, 1]")
        if self.observed_throughput_per_second < 0.0:
            raise ValueError("observed_throughput_per_second must be non-negative")
        if not 0.0 <= self.cpu_utilization <= 1.0:
            raise ValueError("cpu_utilization must be within [0, 1]")
        if not 0.0 <= self.memory_utilization <= 1.0:
            raise ValueError("memory_utilization must be within [0, 1]")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "observed_latency_ms": self.observed_latency_ms,
            "observed_error_rate": self.observed_error_rate,
            "observed_throughput_per_second": self.observed_throughput_per_second,
            "cpu_utilization": self.cpu_utilization,
            "memory_utilization": self.memory_utilization,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class StressStepRecord:
    """Serializable stress-step evaluation record."""

    step: StressStep
    measurement: StressMeasurement
    failure_mode: StressFailureMode
    passed: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step": self.step.to_dict(),
            "measurement": self.measurement.to_dict(),
            "failure_mode": self.failure_mode.value,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class StressScenarioRecord:
    """Serializable scenario result."""

    scenario_name: str
    outcome: StressOutcome
    started_at: datetime
    finished_at: datetime
    thresholds: StressThresholds
    profile: StressProfile
    step_records: tuple[StressStepRecord, ...]
    worst_failure_mode: StressFailureMode
    error_message: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "scenario_name": self.scenario_name,
            "outcome": self.outcome.value,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "thresholds": self.thresholds.to_dict(),
            "profile": self.profile.to_dict(),
            "step_records": [record.to_dict() for record in self.step_records],
            "worst_failure_mode": self.worst_failure_mode.value,
            "error_message": self.error_message,
        }


class StressTestingFramework:
    """Deterministic stress-testing framework for replay-safe resilience validation."""

    def __init__(
        self,
        *,
        scenario_name: str,
        thresholds: StressThresholds,
        profile: StressProfile,
        executor: StressExecutor,
    ) -> None:
        if not scenario_name.strip():
            raise ValueError("scenario_name must be non-empty")
        self._scenario_name = scenario_name
        self._thresholds = thresholds
        self._profile = profile
        self._executor = executor
        self._history: list[StressScenarioRecord] = []
        self._last_timestamp: datetime | None = None

    def generate_steps(self) -> tuple[StressStep, ...]:
        generator = random.Random(self._profile.deterministic_seed)
        steps: list[StressStep] = []
        for step_index in range(self._profile.step_count):
            target_rate = self._profile.start_rate_per_second + (step_index * self._profile.step_rate_per_second)
            burst_factor = 1.0 + ((self._profile.burst_multiplier - 1.0) * generator.random())
            expected_events = max(int(round(target_rate * burst_factor)), 1)
            steps.append(
                StressStep(
                    step_index=step_index,
                    target_rate_per_second=target_rate,
                    burst_factor=burst_factor,
                    expected_events=expected_events,
                )
            )
        return tuple(steps)

    async def run(self) -> StressScenarioRecord:
        started_at = _monotonic_now(self._last_timestamp)
        step_records: list[StressStepRecord] = []
        error_message: str | None = None
        try:
            for step in self.generate_steps():
                result = self._executor(step)
                measurement = await result if asyncio.iscoroutine(result) else result
                step_records.append(
                    StressStepRecord(
                        step=step,
                        measurement=measurement,
                        failure_mode=_classify_failure(measurement=measurement, thresholds=self._thresholds),
                        passed=_classify_failure(measurement=measurement, thresholds=self._thresholds)
                        is StressFailureMode.NONE,
                    )
                )
        except Exception as exc:
            error_message = str(exc)
            finished_at = _monotonic_now(started_at)
            record = StressScenarioRecord(
                scenario_name=self._scenario_name,
                outcome=StressOutcome.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                thresholds=self._thresholds,
                profile=self._profile,
                step_records=tuple(step_records),
                worst_failure_mode=StressFailureMode.EXECUTION_EXCEPTION,
                error_message=error_message,
            )
            self._history.append(record)
            self._last_timestamp = finished_at
            raise StressExecutionError(error_message) from exc
        finished_at = _monotonic_now(started_at)
        worst_failure_mode = _worst_failure(step_records)
        outcome = _derive_outcome(step_records=tuple(step_records), worst_failure_mode=worst_failure_mode)
        record = StressScenarioRecord(
            scenario_name=self._scenario_name,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            thresholds=self._thresholds,
            profile=self._profile,
            step_records=tuple(step_records),
            worst_failure_mode=worst_failure_mode,
            error_message=error_message,
        )
        self._history.append(record)
        self._last_timestamp = finished_at
        return record

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "scenario_name": self._scenario_name,
            "thresholds": self._thresholds.to_dict(),
            "profile": self._profile.to_dict(),
            "history": [record.to_dict() for record in self._history],
            "last_timestamp": _optional_iso(self._last_timestamp),
        }



def _classify_failure(*, measurement: StressMeasurement, thresholds: StressThresholds) -> StressFailureMode:
    if measurement.cpu_utilization > thresholds.max_cpu_utilization:
        return StressFailureMode.RESOURCE_EXHAUSTION
    if measurement.memory_utilization > thresholds.max_memory_utilization:
        return StressFailureMode.RESOURCE_EXHAUSTION
    if measurement.observed_error_rate > thresholds.max_error_rate:
        return StressFailureMode.ERROR_RATE_BREACH
    if measurement.observed_latency_ms > thresholds.max_latency_ms:
        return StressFailureMode.LATENCY_BREACH
    if measurement.observed_throughput_per_second < thresholds.min_throughput_per_second:
        return StressFailureMode.THROUGHPUT_COLLAPSE
    return StressFailureMode.NONE



def _worst_failure(step_records: list[StressStepRecord]) -> StressFailureMode:
    severity_order = {
        StressFailureMode.NONE: 0,
        StressFailureMode.LATENCY_BREACH: 1,
        StressFailureMode.ERROR_RATE_BREACH: 2,
        StressFailureMode.THROUGHPUT_COLLAPSE: 3,
        StressFailureMode.RESOURCE_EXHAUSTION: 4,
        StressFailureMode.EXECUTION_EXCEPTION: 5,
    }
    return max((record.failure_mode for record in step_records), key=severity_order.get, default=StressFailureMode.NONE)



def _derive_outcome(
    *,
    step_records: tuple[StressStepRecord, ...],
    worst_failure_mode: StressFailureMode,
) -> StressOutcome:
    if not step_records:
        return StressOutcome.FAILED
    failing_steps = [record for record in step_records if not record.passed]
    if not failing_steps:
        return StressOutcome.PASSED
    if worst_failure_mode in {StressFailureMode.RESOURCE_EXHAUSTION, StressFailureMode.THROUGHPUT_COLLAPSE}:
        return StressOutcome.FAILED
    return StressOutcome.DEGRADED



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)



def _monotonic_now(reference: datetime | None) -> datetime:
    candidate = _utc_now()
    if reference is None:
        return candidate
    normalized_reference = _ensure_utc(reference)
    normalized_candidate = _ensure_utc(candidate)
    return normalized_candidate if normalized_candidate >= normalized_reference else normalized_reference



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()


__all__ = [
    "StressConfigurationError",
    "StressExecutionError",
    "StressFailureMode",
    "StressMeasurement",
    "StressOutcome",
    "StressProfile",
    "StressScenarioRecord",
    "StressStep",
    "StressStepRecord",
    "StressTestingError",
    "StressTestingFramework",
    "StressThresholds",
]
