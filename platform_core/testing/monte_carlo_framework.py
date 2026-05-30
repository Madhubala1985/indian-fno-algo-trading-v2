# FILE: platform_core/testing/monte_carlo_framework.py
"""
platform_core.testing.monte_carlo_framework
═══════════════════════════════════════════════════════════════════════
Production-grade deterministic Monte Carlo framework for the institutional
Indian index futures and options derivatives intelligence and automated
execution platform.

This module provides reproducible Monte Carlo scenario generation and path-level
metric aggregation for validation, stress, and risk-analysis workloads. It is
explicitly designed for testing and research pipelines and does not depend on
live market connectivity.

Scope:
- Deterministic pseudo-random path generation via explicit seeds.
- Configurable Gaussian return-path simulation using supplied drift/volatility.
- Async-compatible path evaluators for downstream analytics.
- Serializable scenario, path, and summary records.
- Percentile and drawdown-style aggregation support.
- Replay-safe auditability for testing and model validation.
"""
from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
from decimal import getcontext
from typing import Awaitable
from typing import Callable


getcontext().prec = 28


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
PathEvaluator = Callable[["MonteCarloPath"], Awaitable[dict[str, JsonValue]] | dict[str, JsonValue]]


class MonteCarloFrameworkError(RuntimeError):
    """Base exception for Monte Carlo framework failures."""


class MonteCarloConfigurationError(MonteCarloFrameworkError):
    """Raised when Monte Carlo configuration is invalid."""


class MonteCarloExecutionError(MonteCarloFrameworkError):
    """Raised when path evaluation or simulation execution fails."""


@dataclass(frozen=True, slots=True)
class MonteCarloConfiguration:
    """Deterministic Monte Carlo simulation settings."""

    path_count: int
    horizon_steps: int
    initial_value: Decimal
    drift_per_step: Decimal
    volatility_per_step: Decimal
    deterministic_seed: int = 42
    clamp_floor: Decimal = Decimal("0.000001")

    def __post_init__(self) -> None:
        if self.path_count <= 0:
            raise ValueError("path_count must be positive")
        if self.horizon_steps <= 0:
            raise ValueError("horizon_steps must be positive")
        if self.initial_value <= 0:
            raise ValueError("initial_value must be positive")
        if self.volatility_per_step < 0:
            raise ValueError("volatility_per_step must be non-negative")
        if self.clamp_floor <= 0:
            raise ValueError("clamp_floor must be positive")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path_count": self.path_count,
            "horizon_steps": self.horizon_steps,
            "initial_value": str(self.initial_value),
            "drift_per_step": str(self.drift_per_step),
            "volatility_per_step": str(self.volatility_per_step),
            "deterministic_seed": self.deterministic_seed,
            "clamp_floor": str(self.clamp_floor),
        }


@dataclass(frozen=True, slots=True)
class MonteCarloPoint:
    """One simulated point along a Monte Carlo path."""

    step_index: int
    value: Decimal
    return_value: Decimal

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "step_index": self.step_index,
            "value": str(self.value),
            "return_value": str(self.return_value),
        }


@dataclass(frozen=True, slots=True)
class MonteCarloPath:
    """Serializable deterministic Monte Carlo path."""

    path_index: int
    seed: int
    points: tuple[MonteCarloPoint, ...]

    def terminal_value(self) -> Decimal:
        return self.points[-1].value

    def max_drawdown(self) -> Decimal:
        peak = self.points[0].value
        worst = Decimal("0")
        for point in self.points:
            if point.value > peak:
                peak = point.value
            drawdown = (peak - point.value) / peak if peak > 0 else Decimal("0")
            if drawdown > worst:
                worst = drawdown
        return worst.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path_index": self.path_index,
            "seed": self.seed,
            "points": [point.to_dict() for point in self.points],
            "terminal_value": str(self.terminal_value()),
            "max_drawdown": str(self.max_drawdown()),
        }


@dataclass(frozen=True, slots=True)
class MonteCarloPathRecord:
    """Serializable path evaluation record."""

    path_index: int
    seed: int
    terminal_value: Decimal
    max_drawdown: Decimal
    metrics: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path_index": self.path_index,
            "seed": self.seed,
            "terminal_value": str(self.terminal_value),
            "max_drawdown": str(self.max_drawdown),
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class MonteCarloRunRecord:
    """Serializable aggregate Monte Carlo run record."""

    run_name: str
    configuration: MonteCarloConfiguration
    started_at: datetime
    finished_at: datetime
    path_records: tuple[MonteCarloPathRecord, ...]
    aggregate_metrics: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_name": self.run_name,
            "configuration": self.configuration.to_dict(),
            "started_at": _ensure_utc(self.started_at).isoformat(),
            "finished_at": _ensure_utc(self.finished_at).isoformat(),
            "path_records": [record.to_dict() for record in self.path_records],
            "aggregate_metrics": self.aggregate_metrics,
        }


class MonteCarloFramework:
    """Deterministic Monte Carlo scenario generator and evaluator."""

    def __init__(
        self,
        *,
        run_name: str,
        configuration: MonteCarloConfiguration,
        evaluator: PathEvaluator | None = None,
    ) -> None:
        if not run_name.strip():
            raise ValueError("run_name must be non-empty")
        self._run_name = run_name
        self._configuration = configuration
        self._evaluator = evaluator
        self._history: list[MonteCarloRunRecord] = []
        self._last_timestamp: datetime | None = None

    @property
    def configuration(self) -> MonteCarloConfiguration:
        return self._configuration

    def generate_paths(self) -> tuple[MonteCarloPath, ...]:
        paths: list[MonteCarloPath] = []
        for path_index in range(self._configuration.path_count):
            seed = self._configuration.deterministic_seed + path_index
            rng = random.Random(seed)
            current_value = self._configuration.initial_value
            points: list[MonteCarloPoint] = [
                MonteCarloPoint(step_index=0, value=_quantize_value(current_value), return_value=Decimal("0"))
            ]
            for step_index in range(1, self._configuration.horizon_steps + 1):
                shock = Decimal(str(rng.gauss(0.0, 1.0)))
                step_return = self._configuration.drift_per_step + (self._configuration.volatility_per_step * shock)
                growth_factor = Decimal(str(math.exp(float(step_return))))
                current_value = max(
                    current_value * growth_factor,
                    self._configuration.clamp_floor,
                )
                points.append(
                    MonteCarloPoint(
                        step_index=step_index,
                        value=_quantize_value(current_value),
                        return_value=_quantize_ratio(step_return),
                    )
                )
            paths.append(MonteCarloPath(path_index=path_index, seed=seed, points=tuple(points)))
        return tuple(paths)

    async def run(self) -> MonteCarloRunRecord:
        started_at = _monotonic_now(self._last_timestamp)
        path_records: list[MonteCarloPathRecord] = []
        try:
            for path in self.generate_paths():
                metrics = await self._evaluate_path(path)
                path_records.append(
                    MonteCarloPathRecord(
                        path_index=path.path_index,
                        seed=path.seed,
                        terminal_value=_quantize_value(path.terminal_value()),
                        max_drawdown=_quantize_ratio(path.max_drawdown()),
                        metrics=metrics,
                    )
                )
        except Exception as exc:
            raise MonteCarloExecutionError(str(exc)) from exc
        finished_at = _monotonic_now(started_at)
        aggregate_metrics = _aggregate_path_metrics(tuple(path_records))
        run_record = MonteCarloRunRecord(
            run_name=self._run_name,
            configuration=self._configuration,
            started_at=started_at,
            finished_at=finished_at,
            path_records=tuple(path_records),
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

    async def _evaluate_path(self, path: MonteCarloPath) -> dict[str, JsonValue]:
        if self._evaluator is None:
            return {}
        result = self._evaluator(path)
        return await result if asyncio.iscoroutine(result) else result



def _aggregate_path_metrics(path_records: tuple[MonteCarloPathRecord, ...]) -> dict[str, JsonValue]:
    if not path_records:
        raise MonteCarloConfigurationError("path_records cannot be empty")
    terminal_values = sorted(record.terminal_value for record in path_records)
    drawdowns = sorted(record.max_drawdown for record in path_records)
    numeric_metrics: dict[str, list[float]] = {}
    for record in path_records:
        for key, value in record.metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_metrics.setdefault(key, []).append(float(value))
    return {
        "path_count": len(path_records),
        "terminal_value": {
            "mean": str(_mean_decimal(terminal_values)),
            "p05": str(_percentile_decimal(terminal_values, Decimal("0.05"))),
            "p50": str(_percentile_decimal(terminal_values, Decimal("0.50"))),
            "p95": str(_percentile_decimal(terminal_values, Decimal("0.95"))),
        },
        "max_drawdown": {
            "mean": str(_mean_decimal(drawdowns)),
            "p50": str(_percentile_decimal(drawdowns, Decimal("0.50"))),
            "p95": str(_percentile_decimal(drawdowns, Decimal("0.95"))),
        },
        "custom_numeric_metrics": {
            key: {
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
                "count": len(values),
            }
            for key, values in sorted(numeric_metrics.items())
            if values
        },
    }



def _mean_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        raise MonteCarloConfigurationError("values cannot be empty")
    return _quantize_value(sum(values) / Decimal(len(values)))



def _percentile_decimal(sorted_values: list[Decimal], quantile: Decimal) -> Decimal:
    if not sorted_values:
        raise MonteCarloConfigurationError("cannot compute percentile for empty series")
    if not Decimal("0") <= quantile <= Decimal("1"):
        raise MonteCarloConfigurationError("quantile must be within [0, 1]")
    if len(sorted_values) == 1:
        return _quantize_value(sorted_values[0])
    position = Decimal(len(sorted_values) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = position - Decimal(lower_index)
    value = (sorted_values[lower_index] * (Decimal("1") - weight)) + (sorted_values[upper_index] * weight)
    return _quantize_value(value)



def _quantize_value(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)



def _quantize_ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)



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
    "MonteCarloConfiguration",
    "MonteCarloConfigurationError",
    "MonteCarloExecutionError",
    "MonteCarloFramework",
    "MonteCarloFrameworkError",
    "MonteCarloPath",
    "MonteCarloPathRecord",
    "MonteCarloPoint",
    "MonteCarloRunRecord",
]
