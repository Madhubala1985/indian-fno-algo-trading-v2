# FILE: platform_core/testing/walkforward_framework.py
"""
platform_core.testing.walkforward_framework
═══════════════════════════════════════════════════════════════════════
Production-grade walk-forward validation framework for the institutional
Indian index futures and options derivatives intelligence and execution
platform.

This module provides deterministic rolling and expanding walk-forward splits,
strict temporal anti-leakage guarantees, fold-level execution orchestration,
replay-safe audit capture, and structured aggregation of fold results. It is
intended as the validation substrate for regime models, signal classifiers,
trap/reversal/continuation models, and future walk-forward-safe strategy
analysis.

Scope:
- Deterministic temporal fold generation.
- Rolling or expanding in-sample windows.
- Strict no-overlap / no-future-leakage split enforcement.
- Async-compatible fold executor integration.
- Serializable fold and run records for reproducibility.
- Framework-agnostic validation substrate.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Generic
from typing import Sequence
from typing import TypeVar


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
RecordType = TypeVar("RecordType")
FoldExecutor = Callable[["WalkForwardFold[RecordType]"], Awaitable[dict[str, JsonValue]] | dict[str, JsonValue]]


class WalkForwardFrameworkError(RuntimeError):
    """Base exception for walk-forward validation failures."""


class WalkForwardConfigurationError(WalkForwardFrameworkError):
    """Raised when walk-forward configuration is invalid."""


class WalkForwardExecutionError(WalkForwardFrameworkError):
    """Raised when walk-forward execution fails."""


class WindowMode(str, Enum):
    """Supported in-sample window expansion policies."""

    ROLLING = "ROLLING"
    EXPANDING = "EXPANDING"


class FoldOutcome(str, Enum):
    """Per-fold execution outcomes."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class WalkForwardConfiguration:
    """Deterministic walk-forward configuration."""

    train_size: int
    test_size: int
    step_size: int
    window_mode: WindowMode = WindowMode.ROLLING
    min_train_size: int | None = None
    gap_size: int = 0
    max_folds: int | None = None

    def __post_init__(self) -> None:
        if self.train_size <= 0:
            raise ValueError("train_size must be positive")
        if self.test_size <= 0:
            raise ValueError("test_size must be positive")
        if self.step_size <= 0:
            raise ValueError("step_size must be positive")
        if self.gap_size < 0:
            raise ValueError("gap_size must be non-negative")
        if self.min_train_size is not None and self.min_train_size <= 0:
            raise ValueError("min_train_size must be positive when provided")
        if self.max_folds is not None and self.max_folds <= 0:
            raise ValueError("max_folds must be positive when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "train_size": self.train_size,
            "test_size": self.test_size,
            "step_size": self.step_size,
            "window_mode": self.window_mode.value,
            "min_train_size": self.min_train_size,
            "gap_size": self.gap_size,
            "max_folds": self.max_folds,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardFold(Generic[RecordType]):
    """Immutable train/test split for one walk-forward fold."""

    fold_index: int
    train_start_index: int
    train_end_index: int
    test_start_index: int
    test_end_index: int
    train_records: tuple[RecordType, ...]
    test_records: tuple[RecordType, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fold_index": self.fold_index,
            "train_start_index": self.train_start_index,
            "train_end_index": self.train_end_index,
            "test_start_index": self.test_start_index,
            "test_end_index": self.test_end_index,
            "train_size": len(self.train_records),
            "test_size": len(self.test_records),
        }


@dataclass(frozen=True, slots=True)
class FoldExecutionRecord:
    """Serializable execution record for one fold."""

    fold_index: int
    outcome: FoldOutcome
    started_at: datetime
    finished_at: datetime
    metrics: dict[str, JsonValue]
    split: dict[str, JsonValue]
    error_message: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fold_index": self.fold_index,
            "outcome": self.outcome.value,
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "metrics": self.metrics,
            "split": self.split,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardRunRecord:
    """Serializable aggregate run record."""

    run_name: str
    configuration: WalkForwardConfiguration
    started_at: datetime
    finished_at: datetime
    fold_records: tuple[FoldExecutionRecord, ...]
    aggregate_metrics: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_name": self.run_name,
            "configuration": self.configuration.to_dict(),
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "fold_records": [record.to_dict() for record in self.fold_records],
            "aggregate_metrics": self.aggregate_metrics,
        }


class WalkForwardFramework(Generic[RecordType]):
    """Deterministic walk-forward validation engine."""

    def __init__(
        self,
        *,
        run_name: str,
        configuration: WalkForwardConfiguration,
        executor: FoldExecutor[RecordType],
    ) -> None:
        if not run_name.strip():
            raise ValueError("run_name must be non-empty")
        self._run_name = run_name
        self._configuration = configuration
        self._executor = executor
        self._history: list[WalkForwardRunRecord] = []
        self._last_timestamp: datetime | None = None

    @property
    def run_name(self) -> str:
        return self._run_name

    @property
    def configuration(self) -> WalkForwardConfiguration:
        return self._configuration

    def build_folds(self, records: Sequence[RecordType]) -> tuple[WalkForwardFold[RecordType], ...]:
        if len(records) < self._configuration.train_size + self._configuration.gap_size + self._configuration.test_size:
            raise WalkForwardConfigurationError(
                "insufficient records for requested train/gap/test configuration"
            )
        folds: list[WalkForwardFold[RecordType]] = []
        train_end_index = self._configuration.train_size
        fold_index = 0
        min_train_size = self._configuration.min_train_size or self._configuration.train_size
        while True:
            if self._configuration.window_mode is WindowMode.ROLLING:
                train_start_index = max(train_end_index - self._configuration.train_size, 0)
            else:
                train_start_index = 0
            effective_train_size = train_end_index - train_start_index
            if effective_train_size < min_train_size:
                break
            test_start_index = train_end_index + self._configuration.gap_size
            test_end_index = test_start_index + self._configuration.test_size
            if test_end_index > len(records):
                break
            fold = WalkForwardFold(
                fold_index=fold_index,
                train_start_index=train_start_index,
                train_end_index=train_end_index,
                test_start_index=test_start_index,
                test_end_index=test_end_index,
                train_records=tuple(records[train_start_index:train_end_index]),
                test_records=tuple(records[test_start_index:test_end_index]),
            )
            self._validate_fold(fold=fold)
            folds.append(fold)
            fold_index += 1
            if self._configuration.max_folds is not None and len(folds) >= self._configuration.max_folds:
                break
            train_end_index += self._configuration.step_size
        if not folds:
            raise WalkForwardConfigurationError("configuration produced zero walk-forward folds")
        return tuple(folds)

    async def run(self, records: Sequence[RecordType]) -> WalkForwardRunRecord:
        folds = self.build_folds(records)
        started_at = _monotonic_now(self._last_timestamp)
        fold_records: list[FoldExecutionRecord] = []
        for fold in folds:
            fold_started_at = _monotonic_now(self._last_timestamp or started_at)
            try:
                result = self._executor(fold)
                metrics = await result if asyncio.iscoroutine(result) else result
                fold_finished_at = _monotonic_now(fold_started_at)
                record = FoldExecutionRecord(
                    fold_index=fold.fold_index,
                    outcome=FoldOutcome.SUCCEEDED,
                    started_at=fold_started_at,
                    finished_at=fold_finished_at,
                    metrics=metrics,
                    split=fold.to_dict(),
                )
            except Exception as exc:
                fold_finished_at = _monotonic_now(fold_started_at)
                record = FoldExecutionRecord(
                    fold_index=fold.fold_index,
                    outcome=FoldOutcome.FAILED,
                    started_at=fold_started_at,
                    finished_at=fold_finished_at,
                    metrics={},
                    split=fold.to_dict(),
                    error_message=str(exc),
                )
                fold_records.append(record)
                self._last_timestamp = fold_finished_at
                raise WalkForwardExecutionError(str(exc)) from exc
            fold_records.append(record)
            self._last_timestamp = fold_finished_at
        finished_at = _monotonic_now(self._last_timestamp or started_at)
        aggregate_metrics = _aggregate_metrics(tuple(fold_records))
        run_record = WalkForwardRunRecord(
            run_name=self._run_name,
            configuration=self._configuration,
            started_at=started_at,
            finished_at=finished_at,
            fold_records=tuple(fold_records),
            aggregate_metrics=aggregate_metrics,
        )
        self._history.append(run_record)
        self._last_timestamp = finished_at
        return run_record

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "run_name": self._run_name,
            "configuration": self._configuration.to_dict(),
            "history": [record.to_dict() for record in self._history],
            "last_timestamp": _optional_iso(self._last_timestamp),
        }

    def _validate_fold(self, *, fold: WalkForwardFold[RecordType]) -> None:
        if fold.train_end_index > fold.test_start_index:
            raise WalkForwardConfigurationError("train window overlaps test window")
        if fold.train_start_index < 0 or fold.test_start_index < 0:
            raise WalkForwardConfigurationError("fold indices cannot be negative")
        if fold.train_end_index <= fold.train_start_index:
            raise WalkForwardConfigurationError("train window must be non-empty")
        if fold.test_end_index <= fold.test_start_index:
            raise WalkForwardConfigurationError("test window must be non-empty")
        if len(fold.train_records) != fold.train_end_index - fold.train_start_index:
            raise WalkForwardConfigurationError("train slice length mismatch")
        if len(fold.test_records) != fold.test_end_index - fold.test_start_index:
            raise WalkForwardConfigurationError("test slice length mismatch")



def _aggregate_metrics(fold_records: tuple[FoldExecutionRecord, ...]) -> dict[str, JsonValue]:
    succeeded = [record for record in fold_records if record.outcome is FoldOutcome.SUCCEEDED]
    failed = [record for record in fold_records if record.outcome is FoldOutcome.FAILED]
    numeric_accumulator: dict[str, list[float]] = {}
    for record in succeeded:
        for key, value in record.metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_accumulator.setdefault(key, []).append(float(value))
    aggregated_numeric = {
        key: {
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "count": len(values),
        }
        for key, values in sorted(numeric_accumulator.items())
        if values
    }
    return {
        "fold_count": len(fold_records),
        "succeeded_folds": len(succeeded),
        "failed_folds": len(failed),
        "numeric_metrics": aggregated_numeric,
    }



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
    "FoldExecutionRecord",
    "FoldOutcome",
    "WalkForwardConfiguration",
    "WalkForwardConfigurationError",
    "WalkForwardExecutionError",
    "WalkForwardFold",
    "WalkForwardFramework",
    "WalkForwardFrameworkError",
    "WalkForwardRunRecord",
    "WindowMode",
]
