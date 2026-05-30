# FILE: platform_core/risk/drawdown_protection.py
"""
platform_core.risk.drawdown_protection
═══════════════════════════════════════════════════════════════════════
Production-grade institutional drawdown protection engine specialized for
Indian index futures and options workflows.

The engine evaluates equity-curve deterioration using only finalized, causal
portfolio snapshots. It supports:
- Peak-to-current drawdown monitoring.
- Daily and rolling loss-limit enforcement.
- Progressive risk throttling based on drawdown severity.
- Consecutive-loss streak protection.
- Cooldown gating after hard breaches.
- Replay-safe serializable outputs for orchestration and risk controls.

This module intentionally avoids execution and order-management logic. It is a
safety-first control component for portfolio throttling, trading pauses, and
kill-switch escalation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class DrawdownProtectionError(RuntimeError):
    """Base exception for drawdown protection failures."""


class DrawdownProtectionInputError(DrawdownProtectionError):
    """Raised when drawdown protection inputs are invalid or causally unsafe."""


class DrawdownProtectionComputationError(DrawdownProtectionError):
    """Raised when drawdown protection evaluation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class DrawdownProtectionConfig:
    """Configuration for drawdown and equity-curve protection."""

    warning_drawdown_fraction: float = 0.05
    risk_reduce_drawdown_fraction: float = 0.08
    pause_drawdown_fraction: float = 0.12
    daily_loss_limit_fraction: float = 0.03
    rolling_loss_limit_fraction: float = 0.06
    rolling_window_days: int = 5
    max_consecutive_losses: int = 4
    cooldown_minutes_after_pause: int = 60
    min_risk_scale: float = 0.10
    reduced_risk_scale: float = 0.50
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        bounded_fields = {
            "warning_drawdown_fraction": self.warning_drawdown_fraction,
            "risk_reduce_drawdown_fraction": self.risk_reduce_drawdown_fraction,
            "pause_drawdown_fraction": self.pause_drawdown_fraction,
            "daily_loss_limit_fraction": self.daily_loss_limit_fraction,
            "rolling_loss_limit_fraction": self.rolling_loss_limit_fraction,
            "min_risk_scale": self.min_risk_scale,
            "reduced_risk_scale": self.reduced_risk_scale,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not self.warning_drawdown_fraction <= self.risk_reduce_drawdown_fraction <= self.pause_drawdown_fraction:
            raise ValueError("drawdown thresholds must satisfy warning <= risk_reduce <= pause")
        if self.rolling_window_days <= 0:
            raise ValueError("rolling_window_days must be positive")
        if self.max_consecutive_losses <= 0:
            raise ValueError("max_consecutive_losses must be positive")
        if self.cooldown_minutes_after_pause <= 0:
            raise ValueError("cooldown_minutes_after_pause must be positive")
        if self.reduced_risk_scale < self.min_risk_scale:
            raise ValueError("reduced_risk_scale must be greater than or equal to min_risk_scale")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """Serializable finalized portfolio equity snapshot."""

    timestamp: datetime
    equity: float
    realized_pnl_day: float
    realized_pnl_rolling_window: float
    consecutive_losses: int
    open_risk_fraction: float

    def __post_init__(self) -> None:
        numeric_values = {
            "equity": self.equity,
            "realized_pnl_day": self.realized_pnl_day,
            "realized_pnl_rolling_window": self.realized_pnl_rolling_window,
            "open_risk_fraction": self.open_risk_fraction,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.equity <= 0.0:
            raise ValueError("equity must be positive")
        if self.consecutive_losses < 0:
            raise ValueError("consecutive_losses must be non-negative")
        if not 0.0 <= self.open_risk_fraction <= 1.0:
            raise ValueError("open_risk_fraction must be between 0 and 1")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "equity": self.equity,
            "realized_pnl_day": self.realized_pnl_day,
            "realized_pnl_rolling_window": self.realized_pnl_rolling_window,
            "consecutive_losses": self.consecutive_losses,
            "open_risk_fraction": self.open_risk_fraction,
        }


@dataclass(frozen=True, slots=True)
class DrawdownProtectionBreakdown:
    """Interpretable decomposition of drawdown protection state."""

    peak_equity: float
    drawdown_fraction: float
    intraday_loss_fraction: float
    rolling_loss_fraction: float
    loss_streak_fraction: float
    open_risk_penalty_fraction: float
    recommended_risk_scale: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "peak_equity": self.peak_equity,
            "drawdown_fraction": self.drawdown_fraction,
            "intraday_loss_fraction": self.intraday_loss_fraction,
            "rolling_loss_fraction": self.rolling_loss_fraction,
            "loss_streak_fraction": self.loss_streak_fraction,
            "open_risk_penalty_fraction": self.open_risk_penalty_fraction,
            "recommended_risk_scale": self.recommended_risk_scale,
        }


@dataclass(frozen=True, slots=True)
class DrawdownProtectionResult:
    """Deterministic drawdown protection evaluation result."""

    timestamp: datetime
    status: str
    trading_allowed: bool
    hard_pause: bool
    cooldown_until: datetime | None
    breakdown: DrawdownProtectionBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "status": self.status,
            "trading_allowed": self.trading_allowed,
            "hard_pause": self.hard_pause,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class DrawdownProtectionState:
    """Serializable engine state for replay persistence."""

    processed_snapshots: int
    peak_equity: float | None
    last_timestamp: datetime | None
    last_status: str | None
    cooldown_until: datetime | None
    current_trade_day: str | None
    day_start_equity: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_snapshots": self.processed_snapshots,
            "peak_equity": self.peak_equity,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_status": self.last_status,
            "cooldown_until": _ensure_utc(self.cooldown_until).isoformat() if self.cooldown_until else None,
            "current_trade_day": self.current_trade_day,
            "day_start_equity": self.day_start_equity,
        }


class DrawdownProtectionEngine:
    """Institutional-grade deterministic drawdown protection engine."""

    def __init__(self, *, config: DrawdownProtectionConfig | None = None) -> None:
        self._config = config or DrawdownProtectionConfig()
        self._processed_snapshots = 0
        self._peak_equity: float | None = None
        self._last_timestamp: datetime | None = None
        self._last_status: str | None = None
        self._cooldown_until: datetime | None = None
        self._current_trade_day: date | None = None
        self._day_start_equity: float | None = None
        self._last_result: DrawdownProtectionResult | None = None

    @property
    def state(self) -> DrawdownProtectionState:
        return DrawdownProtectionState(
            processed_snapshots=self._processed_snapshots,
            peak_equity=self._peak_equity,
            last_timestamp=self._last_timestamp,
            last_status=self._last_status,
            cooldown_until=self._cooldown_until,
            current_trade_day=None if self._current_trade_day is None else self._current_trade_day.isoformat(),
            day_start_equity=self._day_start_equity,
        )

    def reset(self) -> None:
        self._processed_snapshots = 0
        self._peak_equity = None
        self._last_timestamp = None
        self._last_status = None
        self._cooldown_until = None
        self._current_trade_day = None
        self._day_start_equity = None
        self._last_result = None

    def evaluate(
        self,
        *,
        snapshot: EquitySnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> DrawdownProtectionResult:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)
        self._roll_trade_day_if_needed(snapshot=snapshot)
        self._peak_equity = snapshot.equity if self._peak_equity is None else max(self._peak_equity, snapshot.equity)
        if self._day_start_equity is None:
            self._day_start_equity = snapshot.equity

        peak_equity = self._peak_equity
        if peak_equity is None or peak_equity <= 0.0:
            raise DrawdownProtectionComputationError("peak_equity is unavailable or invalid")

        drawdown_fraction = max(0.0, (peak_equity - snapshot.equity) / peak_equity)
        intraday_reference_equity = self._day_start_equity if self._day_start_equity is not None and self._day_start_equity > 0.0 else peak_equity
        intraday_loss_fraction = max(0.0, -snapshot.realized_pnl_day / intraday_reference_equity)
        rolling_loss_fraction = max(0.0, -snapshot.realized_pnl_rolling_window / peak_equity)
        loss_streak_fraction = min(snapshot.consecutive_losses / self._config.max_consecutive_losses, 1.0)
        open_risk_penalty_fraction = snapshot.open_risk_fraction
        recommended_risk_scale = self._recommended_risk_scale(
            drawdown_fraction=drawdown_fraction,
            intraday_loss_fraction=intraday_loss_fraction,
            rolling_loss_fraction=rolling_loss_fraction,
            snapshot=snapshot,
        )
        hard_pause = self._hard_pause_required(
            drawdown_fraction=drawdown_fraction,
            intraday_loss_fraction=intraday_loss_fraction,
            rolling_loss_fraction=rolling_loss_fraction,
            snapshot=snapshot,
        )
        cooldown_until = self._cooldown_until
        if hard_pause:
            cooldown_until = normalized_timestamp + timedelta(minutes=self._config.cooldown_minutes_after_pause)
            self._cooldown_until = cooldown_until
        trading_allowed = not hard_pause and not self._in_cooldown(normalized_timestamp)
        status = self._status(
            drawdown_fraction=drawdown_fraction,
            intraday_loss_fraction=intraday_loss_fraction,
            rolling_loss_fraction=rolling_loss_fraction,
            snapshot=snapshot,
            trading_allowed=trading_allowed,
        )
        result = DrawdownProtectionResult(
            timestamp=normalized_timestamp,
            status=status,
            trading_allowed=trading_allowed,
            hard_pause=hard_pause,
            cooldown_until=cooldown_until if not trading_allowed else None,
            breakdown=DrawdownProtectionBreakdown(
                peak_equity=peak_equity,
                drawdown_fraction=drawdown_fraction,
                intraday_loss_fraction=intraday_loss_fraction,
                rolling_loss_fraction=rolling_loss_fraction,
                loss_streak_fraction=loss_streak_fraction,
                open_risk_penalty_fraction=open_risk_penalty_fraction,
                recommended_risk_scale=recommended_risk_scale,
            ),
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "consecutive_losses": snapshot.consecutive_losses,
                "trade_day": self._current_trade_day.isoformat() if self._current_trade_day else None,
            },
        )
        self._processed_snapshots += 1
        self._last_timestamp = normalized_timestamp
        self._last_status = status
        self._last_result = result
        return result

    def get_last_result(self) -> DrawdownProtectionResult:
        if self._last_result is None:
            raise DrawdownProtectionComputationError("no drawdown protection result is available")
        return self._last_result

    def _validate_snapshot(self, *, snapshot: EquitySnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise DrawdownProtectionInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise DrawdownProtectionInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise DrawdownProtectionInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _roll_trade_day_if_needed(self, *, snapshot: EquitySnapshot) -> None:
        snapshot_day = _ensure_utc(snapshot.timestamp).date()
        if self._current_trade_day is None or snapshot_day != self._current_trade_day:
            self._current_trade_day = snapshot_day
            self._day_start_equity = snapshot.equity

    def _recommended_risk_scale(
        self,
        *,
        drawdown_fraction: float,
        intraday_loss_fraction: float,
        rolling_loss_fraction: float,
        snapshot: EquitySnapshot,
    ) -> float:
        if self._hard_pause_required(
            drawdown_fraction=drawdown_fraction,
            intraday_loss_fraction=intraday_loss_fraction,
            rolling_loss_fraction=rolling_loss_fraction,
            snapshot=snapshot,
        ):
            return self._config.min_risk_scale
        if (
            drawdown_fraction >= self._config.risk_reduce_drawdown_fraction
            or intraday_loss_fraction >= 0.75 * self._config.daily_loss_limit_fraction
            or rolling_loss_fraction >= 0.75 * self._config.rolling_loss_limit_fraction
            or snapshot.consecutive_losses >= max(1, self._config.max_consecutive_losses - 1)
        ):
            return self._config.reduced_risk_scale
        if drawdown_fraction >= self._config.warning_drawdown_fraction:
            return max(self._config.reduced_risk_scale, 0.75)
        return 1.0

    def _hard_pause_required(
        self,
        *,
        drawdown_fraction: float,
        intraday_loss_fraction: float,
        rolling_loss_fraction: float,
        snapshot: EquitySnapshot,
    ) -> bool:
        return (
            drawdown_fraction >= self._config.pause_drawdown_fraction
            or intraday_loss_fraction >= self._config.daily_loss_limit_fraction
            or rolling_loss_fraction >= self._config.rolling_loss_limit_fraction
            or snapshot.consecutive_losses >= self._config.max_consecutive_losses
        )

    def _in_cooldown(self, timestamp: datetime) -> bool:
        return self._cooldown_until is not None and timestamp < self._cooldown_until

    def _status(
        self,
        *,
        drawdown_fraction: float,
        intraday_loss_fraction: float,
        rolling_loss_fraction: float,
        snapshot: EquitySnapshot,
        trading_allowed: bool,
    ) -> str:
        if not trading_allowed:
            return "PAUSED"
        if (
            drawdown_fraction >= self._config.risk_reduce_drawdown_fraction
            or intraday_loss_fraction >= 0.75 * self._config.daily_loss_limit_fraction
            or rolling_loss_fraction >= 0.75 * self._config.rolling_loss_limit_fraction
            or snapshot.consecutive_losses >= max(1, self._config.max_consecutive_losses - 1)
        ):
            return "RISK_REDUCED"
        if drawdown_fraction >= self._config.warning_drawdown_fraction:
            return "WARNING"
        return "NORMAL"



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DrawdownProtectionBreakdown",
    "DrawdownProtectionComputationError",
    "DrawdownProtectionConfig",
    "DrawdownProtectionEngine",
    "DrawdownProtectionError",
    "DrawdownProtectionInputError",
    "DrawdownProtectionResult",
    "DrawdownProtectionState",
    "EquitySnapshot",
]
