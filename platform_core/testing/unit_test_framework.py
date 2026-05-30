# FILE: platform_core/testing/unit_test_framework.py
"""
platform_core.testing.unit_test_framework
═══════════════════════════════════════════════════════════════════════
Production-grade unit testing framework for the institutional derivatives
intelligence and automated execution platform specialized for Indian index
futures and options markets.

This module provides deterministic, replay-aware, async-compatible unit-test
harness primitives that integrate with event-driven services without imposing
hidden dependencies on pytest or unittest internals. It is intended to serve as
an internal testing substrate for module-level validation, fixture control,
message capture, dependency overrides, and structured assertion outcomes.

Scope:
- Deterministic fixture registry with immutable test context snapshots.
- Async-safe event capture harness for event-driven unit verification.
- Dependency override container for isolated test wiring.
- Structured assertion helpers with rich failure metadata.
- Replay-safe test execution records and summary reporting.
- Standard-library-only implementation for portability.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Generic
from typing import TypeVar


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
TestCallable = Callable[["UnitTestContext"], Awaitable[None] | None]
FixtureFactory = Callable[[], Any]
AssertionValue = TypeVar("AssertionValue")


class UnitTestFrameworkError(RuntimeError):
    """Base exception for the internal unit test framework."""


class FixtureRegistrationError(UnitTestFrameworkError):
    """Raised when fixture registration is invalid."""


class DependencyOverrideError(UnitTestFrameworkError):
    """Raised when dependency override operations fail."""


class AssertionFailure(UnitTestFrameworkError):
    """Raised when an assertion helper detects a failure."""


class TestExecutionError(UnitTestFrameworkError):
    """Raised when a test case cannot be executed safely."""


class TestOutcome(str, Enum):
    """Explicit unit test outcomes."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class CapturedEvent:
    """Serializable event captured by the test event sink."""

    event_type: str
    source: str
    timestamp: datetime
    payload: dict[str, JsonValue]
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_type": self.event_type,
            "source": self.source,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "payload": self.payload,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class AssertionRecord:
    """Serializable structured assertion record."""

    assertion_name: str
    passed: bool
    message: str
    details: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "assertion_name": self.assertion_name,
            "passed": self.passed,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class TestExecutionRecord:
    """Serializable test execution record."""

    test_name: str
    outcome: TestOutcome
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    assertion_records: tuple[AssertionRecord, ...]
    captured_events: tuple[CapturedEvent, ...]
    error_message: str | None = None
    error_traceback: str | None = None
    context_snapshot: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "test_name": self.test_name,
            "outcome": self.outcome.value,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "duration_ms": self.duration_ms,
            "assertion_records": [item.to_dict() for item in self.assertion_records],
            "captured_events": [item.to_dict() for item in self.captured_events],
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "context_snapshot": self.context_snapshot,
        }


@dataclass(slots=True)
class _FixtureRegistration:
    name: str
    factory: FixtureFactory
    cache_per_test: bool


class DependencyOverrideRegistry:
    """Deterministic dependency override storage for isolated unit wiring."""

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def set(self, *, dependency_name: str, dependency: Any) -> None:
        if not dependency_name.strip():
            raise DependencyOverrideError("dependency_name must be non-empty")
        self._overrides[dependency_name] = dependency

    def get(self, *, dependency_name: str) -> Any:
        if dependency_name not in self._overrides:
            raise DependencyOverrideError(f"unknown dependency override: {dependency_name}")
        return self._overrides[dependency_name]

    def has(self, *, dependency_name: str) -> bool:
        return dependency_name in self._overrides

    def snapshot(self) -> dict[str, JsonValue]:
        return {name: _safe_repr(value) for name, value in sorted(self._overrides.items())}


class EventCaptureSink:
    """Async-safe event sink for verifying event-driven outcomes."""

    def __init__(self) -> None:
        self._events: list[CapturedEvent] = []
        self._last_timestamp: datetime | None = None
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        event_type: str,
        source: str,
        payload: dict[str, JsonValue] | None = None,
        metadata: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        event_timestamp = _ensure_utc(timestamp or _utc_now())
        async with self._lock:
            if self._last_timestamp is not None and event_timestamp < self._last_timestamp:
                raise TestExecutionError("captured event timestamps must be non-decreasing for replay safety")
            event = CapturedEvent(
                event_type=event_type,
                source=source,
                timestamp=event_timestamp,
                payload=payload or {},
                metadata=metadata or {},
            )
            self._events.append(event)
            self._last_timestamp = event_timestamp

    def events(self) -> tuple[CapturedEvent, ...]:
        return tuple(self._events)

    def snapshot(self) -> list[dict[str, JsonValue]]:
        return [event.to_dict() for event in self._events]


class AssertionRecorder:
    """Structured assertion recorder with explicit helper methods."""

    def __init__(self) -> None:
        self._records: list[AssertionRecord] = []

    def equal(self, left: Any, right: Any, message: str = "values are not equal") -> None:
        passed = left == right
        record = AssertionRecord(
            assertion_name="equal",
            passed=passed,
            message=message,
            details={"left": _safe_repr(left), "right": _safe_repr(right)},
        )
        self._records.append(record)
        if not passed:
            raise AssertionFailure(message)

    def true(self, value: Any, message: str = "expected value to be truthy") -> None:
        passed = bool(value)
        record = AssertionRecord(
            assertion_name="true",
            passed=passed,
            message=message,
            details={"value": _safe_repr(value)},
        )
        self._records.append(record)
        if not passed:
            raise AssertionFailure(message)

    def false(self, value: Any, message: str = "expected value to be falsy") -> None:
        passed = not bool(value)
        record = AssertionRecord(
            assertion_name="false",
            passed=passed,
            message=message,
            details={"value": _safe_repr(value)},
        )
        self._records.append(record)
        if not passed:
            raise AssertionFailure(message)

    def almost_equal(
        self,
        left: float,
        right: float,
        tolerance: float = 1e-9,
        message: str = "values are not within tolerance",
    ) -> None:
        passed = abs(left - right) <= tolerance
        record = AssertionRecord(
            assertion_name="almost_equal",
            passed=passed,
            message=message,
            details={"left": left, "right": right, "tolerance": tolerance},
        )
        self._records.append(record)
        if not passed:
            raise AssertionFailure(message)

    def records(self) -> tuple[AssertionRecord, ...]:
        return tuple(self._records)


class FixtureRegistry:
    """Deterministic fixture registry with per-test caching semantics."""

    def __init__(self) -> None:
        self._registrations: dict[str, _FixtureRegistration] = {}

    def register(
        self,
        *,
        fixture_name: str,
        factory: FixtureFactory,
        cache_per_test: bool = True,
    ) -> None:
        if not fixture_name.strip():
            raise FixtureRegistrationError("fixture_name must be non-empty")
        if fixture_name in self._registrations:
            raise FixtureRegistrationError(f"fixture already registered: {fixture_name}")
        self._registrations[fixture_name] = _FixtureRegistration(
            name=fixture_name,
            factory=factory,
            cache_per_test=cache_per_test,
        )

    def create_context_cache(self) -> dict[str, Any]:
        return {}

    def resolve(self, *, fixture_name: str, cache: dict[str, Any]) -> Any:
        registration = self._registrations.get(fixture_name)
        if registration is None:
            raise FixtureRegistrationError(f"unknown fixture: {fixture_name}")
        if registration.cache_per_test and fixture_name in cache:
            return cache[fixture_name]
        instance = registration.factory()
        if registration.cache_per_test:
            cache[fixture_name] = instance
        return instance

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            name: {
                "cache_per_test": registration.cache_per_test,
                "factory": _safe_repr(registration.factory),
            }
            for name, registration in sorted(self._registrations.items())
        }


class UnitTestContext:
    """Per-test deterministic execution context."""

    def __init__(
        self,
        *,
        test_name: str,
        fixture_registry: FixtureRegistry,
        dependency_overrides: DependencyOverrideRegistry,
        event_sink: EventCaptureSink,
        assertion_recorder: AssertionRecorder,
        started_at: datetime,
    ) -> None:
        self._test_name = test_name
        self._fixture_registry = fixture_registry
        self._dependency_overrides = dependency_overrides
        self._event_sink = event_sink
        self._assertion_recorder = assertion_recorder
        self._started_at = started_at
        self._fixture_cache = fixture_registry.create_context_cache()

    @property
    def test_name(self) -> str:
        return self._test_name

    @property
    def assertions(self) -> AssertionRecorder:
        return self._assertion_recorder

    @property
    def events(self) -> EventCaptureSink:
        return self._event_sink

    def fixture(self, fixture_name: str) -> Any:
        return self._fixture_registry.resolve(fixture_name=fixture_name, cache=self._fixture_cache)

    def dependency(self, dependency_name: str) -> Any:
        return self._dependency_overrides.get(dependency_name=dependency_name)

    def has_dependency(self, dependency_name: str) -> bool:
        return self._dependency_overrides.has(dependency_name=dependency_name)

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "test_name": self._test_name,
            "started_at": _ensure_utc(self._started_at).isoformat(),
            "fixtures": {name: _safe_repr(value) for name, value in sorted(self._fixture_cache.items())},
            "dependency_overrides": self._dependency_overrides.snapshot(),
            "captured_events": self._event_sink.snapshot(),
        }


@dataclass(frozen=True, slots=True)
class UnitTestCase:
    """Immutable test case definition."""

    test_name: str
    test_callable: TestCallable
    skip: bool = False
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.test_name.strip():
            raise ValueError("test_name must be non-empty")
        if self.skip and (self.skip_reason is None or not self.skip_reason.strip()):
            raise ValueError("skip_reason must be provided when skip is True")


class UnitTestFramework:
    """Deterministic async-compatible unit testing harness."""

    def __init__(self) -> None:
        self._fixture_registry = FixtureRegistry()
        self._dependency_overrides = DependencyOverrideRegistry()
        self._test_cases: list[UnitTestCase] = []
        self._execution_history: list[TestExecutionRecord] = []
        self._last_timestamp: datetime | None = None

    @property
    def fixtures(self) -> FixtureRegistry:
        return self._fixture_registry

    @property
    def dependency_overrides(self) -> DependencyOverrideRegistry:
        return self._dependency_overrides

    def register_test(self, *, test_name: str, test_callable: TestCallable) -> None:
        if any(item.test_name == test_name for item in self._test_cases):
            raise TestExecutionError(f"duplicate test registration: {test_name}")
        self._test_cases.append(UnitTestCase(test_name=test_name, test_callable=test_callable))

    def register_skipped_test(self, *, test_name: str, skip_reason: str) -> None:
        if any(item.test_name == test_name for item in self._test_cases):
            raise TestExecutionError(f"duplicate test registration: {test_name}")
        self._test_cases.append(
            UnitTestCase(
                test_name=test_name,
                test_callable=lambda _context: None,
                skip=True,
                skip_reason=skip_reason,
            )
        )

    async def run_all(self) -> tuple[TestExecutionRecord, ...]:
        results: list[TestExecutionRecord] = []
        for test_case in self._test_cases:
            results.append(await self.run_test(test_name=test_case.test_name))
        return tuple(results)

    async def run_test(self, *, test_name: str) -> TestExecutionRecord:
        test_case = next((item for item in self._test_cases if item.test_name == test_name), None)
        if test_case is None:
            raise TestExecutionError(f"unknown test: {test_name}")
        started_at = _monotonic_now(self._last_timestamp)
        assertion_recorder = AssertionRecorder()
        event_sink = EventCaptureSink()
        context = UnitTestContext(
            test_name=test_case.test_name,
            fixture_registry=self._fixture_registry,
            dependency_overrides=self._dependency_overrides,
            event_sink=event_sink,
            assertion_recorder=assertion_recorder,
            started_at=started_at,
        )
        if test_case.skip:
            finished_at = _monotonic_now(started_at)
            record = TestExecutionRecord(
                test_name=test_case.test_name,
                outcome=TestOutcome.SKIPPED,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=max((finished_at - started_at).total_seconds() * 1000.0, 0.0),
                assertion_records=(),
                captured_events=(),
                error_message=test_case.skip_reason,
                context_snapshot=context.snapshot(),
            )
            self._execution_history.append(record)
            self._last_timestamp = finished_at
            return record
        outcome = TestOutcome.PASSED
        error_message: str | None = None
        error_traceback: str | None = None
        try:
            result = test_case.test_callable(context)
            if asyncio.iscoroutine(result):
                await result
        except AssertionFailure as exc:
            outcome = TestOutcome.FAILED
            error_message = str(exc)
            error_traceback = traceback.format_exc()
        except Exception as exc:
            outcome = TestOutcome.ERROR
            error_message = str(exc)
            error_traceback = traceback.format_exc()
        finished_at = _monotonic_now(started_at)
        record = TestExecutionRecord(
            test_name=test_case.test_name,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max((finished_at - started_at).total_seconds() * 1000.0, 0.0),
            assertion_records=assertion_recorder.records(),
            captured_events=event_sink.events(),
            error_message=error_message,
            error_traceback=error_traceback,
            context_snapshot=context.snapshot(),
        )
        self._execution_history.append(record)
        self._last_timestamp = finished_at
        return record

    def summary(self) -> dict[str, JsonValue]:
        counts: dict[TestOutcome, int] = {outcome: 0 for outcome in TestOutcome}
        for record in self._execution_history:
            counts[record.outcome] += 1
        return {
            "registered_tests": len(self._test_cases),
            "executed_tests": len(self._execution_history),
            "counts": {outcome.value: count for outcome, count in counts.items()},
            "history": [record.to_dict() for record in self._execution_history],
            "fixtures": self._fixture_registry.snapshot(),
            "dependency_overrides": self._dependency_overrides.snapshot(),
            "last_timestamp": _optional_iso(self._last_timestamp),
        }



def _safe_repr(value: Any) -> JsonValue:
    text = repr(value)
    return text if len(text) <= 500 else text[:497] + "..."



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
    "AssertionFailure",
    "AssertionRecord",
    "AssertionRecorder",
    "CapturedEvent",
    "DependencyOverrideError",
    "DependencyOverrideRegistry",
    "EventCaptureSink",
    "FixtureRegistrationError",
    "FixtureRegistry",
    "TestExecutionError",
    "TestExecutionRecord",
    "TestOutcome",
    "UnitTestCase",
    "UnitTestContext",
    "UnitTestFramework",
    "UnitTestFrameworkError",
]
