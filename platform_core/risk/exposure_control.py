# FILE: platform_core/risk/exposure_control.py
"""
platform_core.risk.exposure_control
═══════════════════════════════════════════════════════════════════════
Production-grade institutional exposure control engine specialized for Indian
index futures and options workflows.

The engine evaluates portfolio-level exposure against deterministic risk limits
using only finalized, causal inputs. It supports:
- Gross and net notional exposure controls.
- Delta-adjusted and gamma-aware options exposure controls.
- Directional long/short exposure decomposition.
- Underlying concentration and expiry concentration checks.
- Correlation-aware aggregated exposure penalties.
- Replay-safe serializable outputs for orchestration and kill-switch layers.

This module does not compute Greeks internally and does not manage orders.
Upstream services must provide finalized position-level exposures and Greeks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import fabs
from math import isfinite
from typing import Sequence


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ExposureControlError(RuntimeError):
    """Base exception for exposure control failures."""


class ExposureControlInputError(ExposureControlError):
    """Raised when exposure control inputs are invalid or causally unsafe."""


class ExposureControlComputationError(ExposureControlError):
    """Raised when exposure control computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class ExposureControlConfig:
    """Configuration for institutional exposure monitoring."""

    max_gross_exposure_fraction: float = 3.00
    max_net_exposure_fraction: float = 1.25
    max_delta_adjusted_exposure_fraction: float = 1.50
    max_gamma_exposure_fraction: float = 0.35
    warning_utilization_fraction: float = 0.70
    critical_utilization_fraction: float = 0.90
    max_underlying_concentration_fraction: float = 0.55
    max_expiry_concentration_fraction: float = 0.60
    correlation_penalty_weight: float = 0.55
    liquidity_penalty_weight: float = 0.30
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "max_gross_exposure_fraction": self.max_gross_exposure_fraction,
            "max_net_exposure_fraction": self.max_net_exposure_fraction,
            "max_delta_adjusted_exposure_fraction": self.max_delta_adjusted_exposure_fraction,
            "max_gamma_exposure_fraction": self.max_gamma_exposure_fraction,
            "warning_utilization_fraction": self.warning_utilization_fraction,
            "critical_utilization_fraction": self.critical_utilization_fraction,
            "max_underlying_concentration_fraction": self.max_underlying_concentration_fraction,
            "max_expiry_concentration_fraction": self.max_expiry_concentration_fraction,
            "correlation_penalty_weight": self.correlation_penalty_weight,
            "liquidity_penalty_weight": self.liquidity_penalty_weight,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if self.warning_utilization_fraction >= self.critical_utilization_fraction:
            raise ValueError("warning_utilization_fraction must be less than critical_utilization_fraction")
        if self.max_underlying_concentration_fraction > 1.0 or self.max_expiry_concentration_fraction > 1.0:
            raise ValueError("concentration fractions must not exceed 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class ExposurePortfolioContext:
    """Portfolio-level capital context for exposure calculations."""

    equity: float
    available_cash: float

    def __post_init__(self) -> None:
        numeric_values = {
            "equity": self.equity,
            "available_cash": self.available_cash,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.equity <= 0.0:
            raise ValueError("equity must be positive")
        if self.available_cash < 0.0:
            raise ValueError("available_cash must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "equity": self.equity,
            "available_cash": self.available_cash,
        }


@dataclass(frozen=True, slots=True)
class ExposurePosition:
    """Serializable finalized exposure snapshot for a single position."""

    symbol: str
    underlying_symbol: str
    expiry_code: str
    timestamp: datetime
    direction: str
    instrument_type: str
    quantity: int
    gross_notional: float
    net_notional: float
    delta_adjusted_notional: float
    gamma_exposure: float
    correlation_to_book: float
    liquidity_score: float

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if not self.expiry_code.strip():
            raise ValueError("expiry_code must be non-empty")
        direction = self.direction.upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if not self.instrument_type.strip():
            raise ValueError("instrument_type must be non-empty")
        numeric_values = {
            "gross_notional": self.gross_notional,
            "net_notional": self.net_notional,
            "delta_adjusted_notional": self.delta_adjusted_notional,
            "gamma_exposure": self.gamma_exposure,
            "correlation_to_book": self.correlation_to_book,
            "liquidity_score": self.liquidity_score,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero")
        if self.gross_notional < 0.0:
            raise ValueError("gross_notional must be non-negative")
        if not 0.0 <= self.correlation_to_book <= 1.0:
            raise ValueError("correlation_to_book must be between 0 and 1")
        if not 0.0 <= self.liquidity_score <= 1.0:
            raise ValueError("liquidity_score must be between 0 and 1")

    @property
    def normalized_direction(self) -> str:
        return self.direction.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "symbol": self.symbol,
            "underlying_symbol": self.underlying_symbol,
            "expiry_code": self.expiry_code,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "direction": self.normalized_direction,
            "instrument_type": self.instrument_type,
            "quantity": self.quantity,
            "gross_notional": self.gross_notional,
            "net_notional": self.net_notional,
            "delta_adjusted_notional": self.delta_adjusted_notional,
            "gamma_exposure": self.gamma_exposure,
            "correlation_to_book": self.correlation_to_book,
            "liquidity_score": self.liquidity_score,
        }


@dataclass(frozen=True, slots=True)
class ExposureControlBreakdown:
    """Interpretable decomposition of portfolio exposure controls."""

    gross_exposure_fraction: float
    net_exposure_fraction: float
    delta_adjusted_exposure_fraction: float
    gamma_exposure_fraction: float
    long_notional_fraction: float
    short_notional_fraction: float
    underlying_concentration_fraction: float
    expiry_concentration_fraction: float
    correlation_penalty_fraction: float
    liquidity_penalty_fraction: float
    adjusted_utilization_fraction: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gross_exposure_fraction": self.gross_exposure_fraction,
            "net_exposure_fraction": self.net_exposure_fraction,
            "delta_adjusted_exposure_fraction": self.delta_adjusted_exposure_fraction,
            "gamma_exposure_fraction": self.gamma_exposure_fraction,
            "long_notional_fraction": self.long_notional_fraction,
            "short_notional_fraction": self.short_notional_fraction,
            "underlying_concentration_fraction": self.underlying_concentration_fraction,
            "expiry_concentration_fraction": self.expiry_concentration_fraction,
            "correlation_penalty_fraction": self.correlation_penalty_fraction,
            "liquidity_penalty_fraction": self.liquidity_penalty_fraction,
            "adjusted_utilization_fraction": self.adjusted_utilization_fraction,
        }


@dataclass(frozen=True, slots=True)
class ExposureLimitStatus:
    """Normalized limit-state output for a monitored exposure dimension."""

    name: str
    current_fraction: float
    limit_fraction: float
    utilization_fraction: float
    breach: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "current_fraction": self.current_fraction,
            "limit_fraction": self.limit_fraction,
            "utilization_fraction": self.utilization_fraction,
            "breach": self.breach,
        }


@dataclass(frozen=True, slots=True)
class ExposureControlResult:
    """Deterministic exposure control evaluation result."""

    timestamp: datetime
    total_positions: int
    status: str
    hard_breach: bool
    breakdown: ExposureControlBreakdown
    limits: dict[str, ExposureLimitStatus]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "total_positions": self.total_positions,
            "status": self.status,
            "hard_breach": self.hard_breach,
            "breakdown": self.breakdown.to_dict(),
            "limits": {name: value.to_dict() for name, value in self.limits.items()},
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ExposureControlState:
    """Serializable engine state for replay persistence."""

    processed_snapshots: int
    last_timestamp: datetime | None
    last_status: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_snapshots": self.processed_snapshots,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_status": self.last_status,
        }


class ExposureControlEngine:
    """Institutional-grade deterministic exposure control engine."""

    def __init__(self, *, config: ExposureControlConfig | None = None) -> None:
        self._config = config or ExposureControlConfig()
        self._processed_snapshots = 0
        self._last_timestamp: datetime | None = None
        self._last_result: ExposureControlResult | None = None

    @property
    def state(self) -> ExposureControlState:
        return ExposureControlState(
            processed_snapshots=self._processed_snapshots,
            last_timestamp=self._last_timestamp,
            last_status=None if self._last_result is None else self._last_result.status,
        )

    def reset(self) -> None:
        self._processed_snapshots = 0
        self._last_timestamp = None
        self._last_result = None

    def evaluate(
        self,
        *,
        timestamp: datetime,
        portfolio: ExposurePortfolioContext,
        positions: Sequence[ExposurePosition],
        valuation_timestamp: datetime | None = None,
    ) -> ExposureControlResult:
        normalized_timestamp = _ensure_utc(timestamp)
        self._validate_timestamp(timestamp=normalized_timestamp, valuation_timestamp=valuation_timestamp)
        gross_notional = sum(position.gross_notional for position in positions)
        net_notional = sum(position.net_notional for position in positions)
        delta_adjusted_notional = sum(position.delta_adjusted_notional for position in positions)
        gamma_exposure = sum(position.gamma_exposure for position in positions)
        long_notional = sum(position.gross_notional for position in positions if position.normalized_direction == "LONG")
        short_notional = sum(position.gross_notional for position in positions if position.normalized_direction == "SHORT")
        gross_exposure_fraction = self._safe_ratio(gross_notional, portfolio.equity)
        net_exposure_fraction = self._safe_ratio(fabs(net_notional), portfolio.equity)
        delta_adjusted_exposure_fraction = self._safe_ratio(fabs(delta_adjusted_notional), portfolio.equity)
        gamma_exposure_fraction = self._safe_ratio(fabs(gamma_exposure), portfolio.equity)
        long_notional_fraction = self._safe_ratio(long_notional, portfolio.equity)
        short_notional_fraction = self._safe_ratio(short_notional, portfolio.equity)
        underlying_concentration_fraction = self._underlying_concentration_fraction(positions=positions, gross_notional=gross_notional)
        expiry_concentration_fraction = self._expiry_concentration_fraction(positions=positions, gross_notional=gross_notional)
        correlation_penalty_fraction = self._correlation_penalty_fraction(positions=positions)
        liquidity_penalty_fraction = self._liquidity_penalty_fraction(positions=positions)
        base_utilization = max(
            self._safe_ratio(gross_exposure_fraction, self._config.max_gross_exposure_fraction),
            self._safe_ratio(net_exposure_fraction, self._config.max_net_exposure_fraction),
            self._safe_ratio(delta_adjusted_exposure_fraction, self._config.max_delta_adjusted_exposure_fraction),
            self._safe_ratio(gamma_exposure_fraction, self._config.max_gamma_exposure_fraction),
            self._safe_ratio(underlying_concentration_fraction, self._config.max_underlying_concentration_fraction),
            self._safe_ratio(expiry_concentration_fraction, self._config.max_expiry_concentration_fraction),
        )
        adjusted_utilization_fraction = base_utilization + correlation_penalty_fraction + liquidity_penalty_fraction

        limits = {
            "gross_exposure": self._limit_status(
                name="gross_exposure",
                current_fraction=gross_exposure_fraction,
                limit_fraction=self._config.max_gross_exposure_fraction,
            ),
            "net_exposure": self._limit_status(
                name="net_exposure",
                current_fraction=net_exposure_fraction,
                limit_fraction=self._config.max_net_exposure_fraction,
            ),
            "delta_adjusted_exposure": self._limit_status(
                name="delta_adjusted_exposure",
                current_fraction=delta_adjusted_exposure_fraction,
                limit_fraction=self._config.max_delta_adjusted_exposure_fraction,
            ),
            "gamma_exposure": self._limit_status(
                name="gamma_exposure",
                current_fraction=gamma_exposure_fraction,
                limit_fraction=self._config.max_gamma_exposure_fraction,
            ),
            "underlying_concentration": self._limit_status(
                name="underlying_concentration",
                current_fraction=underlying_concentration_fraction,
                limit_fraction=self._config.max_underlying_concentration_fraction,
            ),
            "expiry_concentration": self._limit_status(
                name="expiry_concentration",
                current_fraction=expiry_concentration_fraction,
                limit_fraction=self._config.max_expiry_concentration_fraction,
            ),
        }
        hard_breach = any(limit.breach for limit in limits.values())
        status = self._status(adjusted_utilization_fraction=adjusted_utilization_fraction, hard_breach=hard_breach)
        result = ExposureControlResult(
            timestamp=normalized_timestamp,
            total_positions=len(positions),
            status=status,
            hard_breach=hard_breach,
            breakdown=ExposureControlBreakdown(
                gross_exposure_fraction=gross_exposure_fraction,
                net_exposure_fraction=net_exposure_fraction,
                delta_adjusted_exposure_fraction=delta_adjusted_exposure_fraction,
                gamma_exposure_fraction=gamma_exposure_fraction,
                long_notional_fraction=long_notional_fraction,
                short_notional_fraction=short_notional_fraction,
                underlying_concentration_fraction=underlying_concentration_fraction,
                expiry_concentration_fraction=expiry_concentration_fraction,
                correlation_penalty_fraction=correlation_penalty_fraction,
                liquidity_penalty_fraction=liquidity_penalty_fraction,
                adjusted_utilization_fraction=adjusted_utilization_fraction,
            ),
            limits=limits,
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "equity": portfolio.equity,
                "available_cash": portfolio.available_cash,
                "gross_notional": gross_notional,
                "net_notional": net_notional,
            },
        )
        self._processed_snapshots += 1
        self._last_timestamp = normalized_timestamp
        self._last_result = result
        return result

    def get_last_result(self) -> ExposureControlResult:
        if self._last_result is None:
            raise ExposureControlComputationError("no exposure control result is available")
        return self._last_result

    def _validate_timestamp(self, *, timestamp: datetime, valuation_timestamp: datetime | None) -> None:
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if timestamp > normalized_valuation_timestamp:
                raise ExposureControlInputError("timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise ExposureControlInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ExposureControlInputError("timestamp must be non-decreasing for replay determinism")

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0.0:
            return 0.0
        return numerator / denominator

    def _underlying_concentration_fraction(self, *, positions: Sequence[ExposurePosition], gross_notional: float) -> float:
        if gross_notional <= 0.0 or not positions:
            return 0.0
        totals: dict[str, float] = {}
        for position in positions:
            totals[position.underlying_symbol] = totals.get(position.underlying_symbol, 0.0) + position.gross_notional
        return max(totals.values()) / gross_notional

    def _expiry_concentration_fraction(self, *, positions: Sequence[ExposurePosition], gross_notional: float) -> float:
        if gross_notional <= 0.0 or not positions:
            return 0.0
        totals: dict[str, float] = {}
        for position in positions:
            totals[position.expiry_code] = totals.get(position.expiry_code, 0.0) + position.gross_notional
        return max(totals.values()) / gross_notional

    def _correlation_penalty_fraction(self, *, positions: Sequence[ExposurePosition]) -> float:
        if not positions:
            return 0.0
        average_correlation = sum(position.correlation_to_book for position in positions) / len(positions)
        return average_correlation * self._config.correlation_penalty_weight

    def _liquidity_penalty_fraction(self, *, positions: Sequence[ExposurePosition]) -> float:
        if not positions:
            return 0.0
        average_illiquidity = sum(1.0 - position.liquidity_score for position in positions) / len(positions)
        return average_illiquidity * self._config.liquidity_penalty_weight

    def _limit_status(self, *, name: str, current_fraction: float, limit_fraction: float) -> ExposureLimitStatus:
        utilization_fraction = self._safe_ratio(current_fraction, limit_fraction)
        return ExposureLimitStatus(
            name=name,
            current_fraction=current_fraction,
            limit_fraction=limit_fraction,
            utilization_fraction=utilization_fraction,
            breach=current_fraction > limit_fraction,
        )

    def _status(self, *, adjusted_utilization_fraction: float, hard_breach: bool) -> str:
        if hard_breach:
            return "BREACH"
        if adjusted_utilization_fraction >= self._config.critical_utilization_fraction:
            return "CRITICAL"
        if adjusted_utilization_fraction >= self._config.warning_utilization_fraction:
            return "WARNING"
        return "NORMAL"



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ExposureControlBreakdown",
    "ExposureControlComputationError",
    "ExposureControlConfig",
    "ExposureControlEngine",
    "ExposureControlError",
    "ExposureControlInputError",
    "ExposureControlResult",
    "ExposureControlState",
    "ExposureLimitStatus",
    "ExposurePortfolioContext",
    "ExposurePosition",
]
