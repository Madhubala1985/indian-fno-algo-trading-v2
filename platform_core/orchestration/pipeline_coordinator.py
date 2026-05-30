# FILE: platform_core/orchestration/pipeline_coordinator.py
"""
platform_core.orchestration.pipeline_coordinator
═══════════════════════════════════════════════════════════════════════
Production-grade async pipeline coordinator for the institutional derivatives
intelligence and automated execution platform specialized for Indian index
futures and options markets.

This module coordinates deterministic stage execution across event-driven
services while preserving validated architecture assumptions around async
processing, replay safety, causal ordering, dependency-aware orchestration,
and JSON-safe runtime state capture.

Scope:
- DAG-based stage registration and validation.
- Async pipeline execution with explicit execution context.
- Deterministic dependency ordering and causal stage progression.
- Publish hooks for pipeline lifecycle events.
- Failure propagation and stage-level result capture.
- Replay-safe execution snapshots for audit and testing.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Awaitable
from typing import Callable


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
StageHandler = Callable[["PipelineExecutionContext"], Awaitable[dict[str, JsonValue] | None]]
EventPublisher = Callable[..., Awaitable[Any]]


class PipelineCoordinatorError(RuntimeError):
    """Base exception for pipeline coordination failures."""


class PipelineRegistrationError(PipelineCoordinatorError):
    """Raised when stage registration is invalid."""


class PipelineExecutionError(PipelineCoordinatorError):
    """Raised when pipeline execution fails."""


class PipelineDependencyError(PipelineCoordinatorError):
    """Raised when pipeline dependencies are invalid or cyclic."""


class PipelineStatus(str, Enum):
    """Pipeline execution lifecycle status."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class StageStatus(str, Enum):
    """Individual stage execution status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """Serializable pipeline stage definition."""

    stage_name: str
    dependencies: tuple[str, ...]
    timeout_seconds: float | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stage_name.strip():
            raise ValueError("stage_name must be non-empty")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must be unique")
        if self.stage_name in self.dependencies:
            raise ValueError("stage cannot depend on itself")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "stage_name": self.stage_name,
            "dependencies": list(self.dependencies),
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class StageExecutionResult:
    """Serializable stage execution result."""

    stage_name: str
    status: StageStatus
    started_at: datetime | None
    finished_at: datetime | None
    output: dict[str, JsonValue]
    error: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "stage_name": self.stage_name,
            "status": self.status.value,
            "started_at": _optional_iso(self.started_at),
            "finished_at": _optional_iso(self.finished_at),
            "output": self.output,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PipelineExecutionRecord:
    """Serializable pipeline execution record."""

    pipeline_name: str
    run_id: str
    status: PipelineStatus
    started_at: datetime
    finished_at: datetime | None
    stage_results: tuple[StageExecutionResult, ...]
    input_payload: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "pipeline_name": self.pipeline_name,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _optional_iso(self.finished_at),
            "stage_results": [result.to_dict() for result in self.stage_results],
            "input_payload": self.input_payload,
        }


@dataclass(slots=True)
class PipelineExecutionContext:
    """Mutable execution context shared across pipeline stages."""

    pipeline_name: str
    run_id: str
    started_at: datetime
    input_payload: dict[str, JsonValue]
    stage_outputs: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    stage_statuses: dict[str, StageStatus] = field(default_factory=dict)

    def get_stage_output(self, stage_name: str) -> dict[str, JsonValue]:
        return dict(self.stage_outputs.get(stage_name, {}))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "pipeline_name": self.pipeline_name,
            "run_id": self.run_id,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "input_payload": self.input_payload,
            "stage_outputs": {stage: payload for stage, payload in sorted(self.stage_outputs.items())},
            "stage_statuses": {stage: status.value for stage, status in sorted(self.stage_statuses.items())},
        }


@dataclass(slots=True)
class _RegisteredStage:
    definition: StageDefinition
    handler: StageHandler


class PipelineCoordinator:
    """Dependency-aware async pipeline coordinator."""

    def __init__(
        self,
        *,
        pipeline_name: str,
        event_publisher: EventPublisher | None = None,
        fail_fast: bool = True,
    ) -> None:
        if not pipeline_name.strip():
            raise ValueError("pipeline_name must be non-empty")
        self._pipeline_name = pipeline_name
        self._event_publisher = event_publisher
        self._fail_fast = fail_fast
        self._registered_stages: dict[str, _RegisteredStage] = {}
        self._status = PipelineStatus.IDLE
        self._execution_history: list[PipelineExecutionRecord] = []
        self._run_sequence = 0
        self._last_timestamp: datetime | None = None

    @property
    def pipeline_name(self) -> str:
        return self._pipeline_name

    @property
    def status(self) -> PipelineStatus:
        return self._status

    def register_stage(
        self,
        *,
        stage_name: str,
        handler: StageHandler,
        dependencies: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> None:
        if stage_name in self._registered_stages:
            raise PipelineRegistrationError(f"stage already registered: {stage_name}")
        definition = StageDefinition(
            stage_name=stage_name,
            dependencies=dependencies,
            timeout_seconds=timeout_seconds,
            metadata=metadata or {},
        )
        self._registered_stages[stage_name] = _RegisteredStage(definition=definition, handler=handler)
        self.validate()

    def validate(self) -> None:
        graph = self._dependency_graph()
        for stage_name, dependencies in graph.items():
            for dependency in dependencies:
                if dependency not in self._registered_stages:
                    raise PipelineDependencyError(
                        f"stage {stage_name} depends on unknown stage {dependency}"
                    )
        _topological_order(graph)

    async def execute(
        self,
        *,
        input_payload: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> PipelineExecutionRecord:
        if not self._registered_stages:
            raise PipelineExecutionError("no stages registered for execution")
        self.validate()
        run_timestamp = _ensure_utc(timestamp or _utc_now())
        self._validate_timestamp(run_timestamp)
        self._run_sequence += 1
        run_id = f"{self._pipeline_name}-run-{self._run_sequence}"
        context = PipelineExecutionContext(
            pipeline_name=self._pipeline_name,
            run_id=run_id,
            started_at=run_timestamp,
            input_payload=input_payload or {},
        )
        self._status = PipelineStatus.RUNNING
        await self._publish_pipeline_event(
            event_type="PIPELINE_EXECUTION_STARTED",
            payload={
                "pipeline_name": self._pipeline_name,
                "run_id": run_id,
                "input_payload": context.input_payload,
            },
            timestamp=run_timestamp,
            correlation_id=run_id,
        )
        stage_results: list[StageExecutionResult] = []
        final_status = PipelineStatus.SUCCEEDED
        failure_detected = False
        for stage_name in _topological_order(self._dependency_graph()):
            registered_stage = self._registered_stages[stage_name]
            if failure_detected and self._fail_fast:
                skipped_result = StageExecutionResult(
                    stage_name=stage_name,
                    status=StageStatus.SKIPPED,
                    started_at=None,
                    finished_at=None,
                    output={},
                    error="skipped due to fail-fast after upstream failure",
                )
                context.stage_statuses[stage_name] = StageStatus.SKIPPED
                stage_results.append(skipped_result)
                continue
            dependency_failed = any(
                context.stage_statuses.get(dependency) not in {StageStatus.SUCCEEDED}
                for dependency in registered_stage.definition.dependencies
            )
            if dependency_failed:
                skipped_result = StageExecutionResult(
                    stage_name=stage_name,
                    status=StageStatus.SKIPPED,
                    started_at=None,
                    finished_at=None,
                    output={},
                    error="skipped because one or more dependencies did not succeed",
                )
                context.stage_statuses[stage_name] = StageStatus.SKIPPED
                stage_results.append(skipped_result)
                failure_detected = True
                final_status = PipelineStatus.FAILED
                continue
            stage_started_at = _monotonic_timestamp(self._last_timestamp, _utc_now())
            context.stage_statuses[stage_name] = StageStatus.RUNNING
            await self._publish_pipeline_event(
                event_type="PIPELINE_STAGE_STARTED",
                payload={
                    "pipeline_name": self._pipeline_name,
                    "run_id": run_id,
                    "stage_name": stage_name,
                    "context": context.to_dict(),
                },
                timestamp=stage_started_at,
                correlation_id=run_id,
            )
            try:
                output = await _execute_stage(
                    handler=registered_stage.handler,
                    context=context,
                    timeout_seconds=registered_stage.definition.timeout_seconds,
                )
                stage_finished_at = _monotonic_timestamp(stage_started_at, _utc_now())
                stage_result = StageExecutionResult(
                    stage_name=stage_name,
                    status=StageStatus.SUCCEEDED,
                    started_at=stage_started_at,
                    finished_at=stage_finished_at,
                    output=output,
                )
                context.stage_outputs[stage_name] = output
                context.stage_statuses[stage_name] = StageStatus.SUCCEEDED
                self._last_timestamp = stage_finished_at
                await self._publish_pipeline_event(
                    event_type="PIPELINE_STAGE_SUCCEEDED",
                    payload={
                        "pipeline_name": self._pipeline_name,
                        "run_id": run_id,
                        "stage_name": stage_name,
                        "output": output,
                    },
                    timestamp=stage_finished_at,
                    correlation_id=run_id,
                )
                stage_results.append(stage_result)
            except Exception as exc:
                stage_finished_at = _monotonic_timestamp(stage_started_at, _utc_now())
                stage_result = StageExecutionResult(
                    stage_name=stage_name,
                    status=StageStatus.FAILED,
                    started_at=stage_started_at,
                    finished_at=stage_finished_at,
                    output={},
                    error=str(exc),
                )
                context.stage_statuses[stage_name] = StageStatus.FAILED
                stage_results.append(stage_result)
                self._last_timestamp = stage_finished_at
                failure_detected = True
                final_status = PipelineStatus.FAILED
                await self._publish_pipeline_event(
                    event_type="PIPELINE_STAGE_FAILED",
                    payload={
                        "pipeline_name": self._pipeline_name,
                        "run_id": run_id,
                        "stage_name": stage_name,
                        "error": str(exc),
                    },
                    timestamp=stage_finished_at,
                    correlation_id=run_id,
                )
                if self._fail_fast:
                    continue
        finished_at = _monotonic_timestamp(self._last_timestamp or run_timestamp, _utc_now())
        self._status = final_status
        execution_record = PipelineExecutionRecord(
            pipeline_name=self._pipeline_name,
            run_id=run_id,
            status=final_status,
            started_at=run_timestamp,
            finished_at=finished_at,
            stage_results=tuple(stage_results),
            input_payload=context.input_payload,
        )
        self._execution_history.append(execution_record)
        await self._publish_pipeline_event(
            event_type=(
                "PIPELINE_EXECUTION_SUCCEEDED" if final_status is PipelineStatus.SUCCEEDED else "PIPELINE_EXECUTION_FAILED"
            ),
            payload=execution_record.to_dict(),
            timestamp=finished_at,
            correlation_id=run_id,
        )
        self._status = PipelineStatus.IDLE
        self._last_timestamp = finished_at
        return execution_record

    def snapshot(self) -> dict[str, JsonValue]:
        self.validate()
        return {
            "pipeline_name": self._pipeline_name,
            "status": self._status.value,
            "fail_fast": self._fail_fast,
            "registered_stages": {
                stage_name: registered.definition.to_dict()
                for stage_name, registered in sorted(self._registered_stages.items())
            },
            "execution_order": list(_topological_order(self._dependency_graph())),
            "history": [record.to_dict() for record in self._execution_history],
            "last_timestamp": _optional_iso(self._last_timestamp),
        }

    async def _publish_pipeline_event(
        self,
        *,
        event_type: str,
        payload: dict[str, JsonValue],
        timestamp: datetime,
        correlation_id: str,
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher(
            event_type=event_type,
            source=f"pipeline_coordinator.{self._pipeline_name}",
            payload=payload,
            timestamp=timestamp,
            correlation_id=correlation_id,
        )

    def _dependency_graph(self) -> dict[str, set[str]]:
        return {
            stage_name: set(registered.definition.dependencies)
            for stage_name, registered in self._registered_stages.items()
        }

    def _validate_timestamp(self, value: datetime) -> None:
        if self._last_timestamp is not None and value < self._last_timestamp:
            raise PipelineExecutionError("pipeline timestamps must be non-decreasing for replay safety")


async def _execute_stage(
    *,
    handler: StageHandler,
    context: PipelineExecutionContext,
    timeout_seconds: float | None,
) -> dict[str, JsonValue]:
    coroutine = handler(context)
    result = await asyncio.wait_for(coroutine, timeout=timeout_seconds) if timeout_seconds is not None else await coroutine
    if result is None:
        return {}
    return dict(result)



def _topological_order(graph: dict[str, set[str]]) -> tuple[str, ...]:
    remaining = {node: set(edges) for node, edges in graph.items()}
    ordered: list[str] = []
    ready = sorted(node for node, edges in remaining.items() if not edges)
    while ready:
        node = ready.pop(0)
        if node in ordered:
            continue
        ordered.append(node)
        for candidate, dependencies in remaining.items():
            if node in dependencies:
                dependencies.remove(node)
                if not dependencies and candidate not in ready and candidate not in ordered:
                    ready.append(candidate)
                    ready.sort()
    if len(ordered) != len(remaining):
        unresolved = sorted(set(remaining) - set(ordered))
        raise PipelineDependencyError("cyclic stage dependencies: " + ", ".join(unresolved))
    return tuple(ordered)



def _monotonic_timestamp(reference: datetime, candidate: datetime) -> datetime:
    normalized_reference = _ensure_utc(reference)
    normalized_candidate = _ensure_utc(candidate)
    return normalized_candidate if normalized_candidate >= normalized_reference else normalized_reference



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _optional_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat()



def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "PipelineCoordinator",
    "PipelineCoordinatorError",
    "PipelineDependencyError",
    "PipelineExecutionContext",
    "PipelineExecutionError",
    "PipelineExecutionRecord",
    "PipelineRegistrationError",
    "PipelineStatus",
    "StageDefinition",
    "StageExecutionResult",
    "StageStatus",
]
