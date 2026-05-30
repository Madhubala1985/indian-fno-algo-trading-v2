# FILE: platform_core/ml/feature_pipeline.py
"""
platform_core.ml.feature_pipeline
═══════════════════════════════════════════════════════════════════════
Production-grade causal feature engineering pipeline for institutional
Indian index futures and options intelligence workflows.

The pipeline consumes finalized market snapshots one observation at a time and
emits deterministic, replay-safe feature vectors suitable for walk-forward ML
training, inference, and validation. It intentionally avoids centered windows,
forward references, and hidden state that cannot be serialized.

Feature groups implemented in this module:
- Price and return dynamics.
- Realized volatility and range statistics.
- Trend efficiency and directional persistence.
- Volume normalization and participation proxies.
- Option-chain/open-interest analytics inputs.
- Greeks and premium behavior contextual features.
- Regime-state passthrough for downstream models.

The module is standalone and depends only on the Python standard library.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timezone
from math import fabs
from math import isfinite
from math import log
from statistics import fmean
from typing import Deque
from typing import Mapping


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class FeaturePipelineError(RuntimeError):
    """Base exception for causal feature pipeline failures."""


class FeaturePipelineInputError(FeaturePipelineError):
    """Raised when input payloads violate pipeline assumptions."""


class FeaturePipelineComputationError(FeaturePipelineError):
    """Raised when feature computation cannot complete safely."""


@dataclass(frozen=True, slots=True)
class FeaturePipelineConfig:
    """Configuration controlling feature lookbacks and safeguards."""

    short_return_window: int = 12
    medium_return_window: int = 24
    long_return_window: int = 48
    short_volume_window: int = 12
    medium_volume_window: int = 24
    trend_window: int = 20
    persistence_window: int = 12
    range_window: int = 20
    min_history_points: int = 24
    minimum_price: float = 1.0e-9
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        integer_fields = {
            "short_return_window": self.short_return_window,
            "medium_return_window": self.medium_return_window,
            "long_return_window": self.long_return_window,
            "short_volume_window": self.short_volume_window,
            "medium_volume_window": self.medium_volume_window,
            "trend_window": self.trend_window,
            "persistence_window": self.persistence_window,
            "range_window": self.range_window,
            "min_history_points": self.min_history_points,
        }
        for field_name, field_value in integer_fields.items():
            if field_value <= 1:
                raise ValueError(f"{field_name} must be greater than 1")
        if not (self.short_return_window <= self.medium_return_window <= self.long_return_window):
            raise ValueError("return windows must be ordered short <= medium <= long")
        if not (self.short_volume_window <= self.medium_volume_window):
            raise ValueError("volume windows must be ordered short <= medium")
        if self.min_history_points < max(self.medium_return_window, self.trend_window, self.range_window):
            raise ValueError("min_history_points must cover required windows")
        if self.minimum_price <= 0.0:
            raise ValueError("minimum_price must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class MarketFeatureSnapshot:
    """Finalized causal observation for feature generation."""

    underlying_symbol: str
    timestamp: datetime
    trading_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    call_open_interest: int = 0
    put_open_interest: int = 0
    put_call_ratio: float = 0.0
    atm_implied_volatility: float = 0.0
    implied_volatility_skew: float = 0.0
    net_gamma_exposure: float = 0.0
    net_delta_exposure: float = 0.0
    net_vanna_exposure: float = 0.0
    net_charm_exposure: float = 0.0
    total_premium_oi_notional: float = 0.0
    total_extrinsic_value: float = 0.0
    net_time_decay_per_day: float = 0.0
    regime_label: str | None = None
    regime_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        numeric_values = {
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
            "put_call_ratio": self.put_call_ratio,
            "atm_implied_volatility": self.atm_implied_volatility,
            "implied_volatility_skew": self.implied_volatility_skew,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "total_extrinsic_value": self.total_extrinsic_value,
            "net_time_decay_per_day": self.net_time_decay_per_day,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.open_price <= 0.0 or self.high_price <= 0.0 or self.low_price <= 0.0 or self.close_price <= 0.0:
            raise ValueError("prices must be positive")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("high_price must be greater than or equal to open_price and close_price")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("low_price must be less than or equal to open_price and close_price")
        if self.high_price < self.low_price:
            raise ValueError("high_price must be greater than or equal to low_price")
        if self.volume < 0.0:
            raise ValueError("volume must be non-negative")
        if self.call_open_interest < 0 or self.put_open_interest < 0:
            raise ValueError("open interest values must be non-negative")
        if self.atm_implied_volatility < 0.0:
            raise ValueError("atm_implied_volatility must be non-negative")
        if self.total_premium_oi_notional < 0.0 or self.total_extrinsic_value < 0.0:
            raise ValueError("premium notionals must be non-negative")
        if self.regime_confidence is not None and not 0.0 <= self.regime_confidence <= 1.0:
            raise ValueError("regime_confidence must be between 0 and 1 when provided")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
            "call_open_interest": self.call_open_interest,
            "put_open_interest": self.put_open_interest,
            "put_call_ratio": self.put_call_ratio,
            "atm_implied_volatility": self.atm_implied_volatility,
            "implied_volatility_skew": self.implied_volatility_skew,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "total_extrinsic_value": self.total_extrinsic_value,
            "net_time_decay_per_day": self.net_time_decay_per_day,
            "regime_label": self.regime_label,
            "regime_confidence": self.regime_confidence,
        }


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """Deterministic feature vector output for a single snapshot."""

    underlying_symbol: str
    timestamp: datetime
    trading_date: date
    features: dict[str, float]
    categorical_features: dict[str, str]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "features": dict(self.features),
            "categorical_features": dict(self.categorical_features),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class FeaturePipelineState:
    """Serializable pipeline state for replay persistence."""

    underlying_symbol: str | None
    processed_points: int
    last_timestamp: datetime | None
    last_trading_date: date | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "processed_points": self.processed_points,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_trading_date": self.last_trading_date.isoformat() if self.last_trading_date else None,
        }


class FeaturePipeline:
    """Replay-safe incremental feature engineering pipeline."""

    def __init__(self, *, config: FeaturePipelineConfig | None = None) -> None:
        self._config = config or FeaturePipelineConfig()
        maxlen = max(
            self._config.long_return_window + 1,
            self._config.medium_volume_window + 1,
            self._config.trend_window + 1,
            self._config.persistence_window + 1,
            self._config.range_window + 1,
            self._config.min_history_points + 1,
        )
        self._snapshots: Deque[MarketFeatureSnapshot] = deque(maxlen=maxlen)
        self._log_returns: Deque[float] = deque(maxlen=self._config.long_return_window)
        self._direction_signs: Deque[int] = deque(maxlen=self._config.persistence_window)
        self._volumes: Deque[float] = deque(maxlen=self._config.medium_volume_window)
        self._underlying_symbol: str | None = None
        self._processed_points = 0
        self._last_timestamp: datetime | None = None
        self._last_trading_date: date | None = None
        self._last_vector: FeatureVector | None = None

    @property
    def state(self) -> FeaturePipelineState:
        return FeaturePipelineState(
            underlying_symbol=self._underlying_symbol,
            processed_points=self._processed_points,
            last_timestamp=self._last_timestamp,
            last_trading_date=self._last_trading_date,
        )

    def reset(self) -> None:
        self._snapshots.clear()
        self._log_returns.clear()
        self._direction_signs.clear()
        self._volumes.clear()
        self._underlying_symbol = None
        self._processed_points = 0
        self._last_timestamp = None
        self._last_trading_date = None
        self._last_vector = None

    def update(
        self,
        *,
        snapshot: MarketFeatureSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> FeatureVector:
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)
        normalized_snapshot = MarketFeatureSnapshot(
            underlying_symbol=snapshot.underlying_symbol,
            timestamp=_ensure_utc(snapshot.timestamp),
            trading_date=snapshot.trading_date,
            open_price=snapshot.open_price,
            high_price=snapshot.high_price,
            low_price=snapshot.low_price,
            close_price=snapshot.close_price,
            volume=snapshot.volume,
            call_open_interest=snapshot.call_open_interest,
            put_open_interest=snapshot.put_open_interest,
            put_call_ratio=snapshot.put_call_ratio,
            atm_implied_volatility=snapshot.atm_implied_volatility,
            implied_volatility_skew=snapshot.implied_volatility_skew,
            net_gamma_exposure=snapshot.net_gamma_exposure,
            net_delta_exposure=snapshot.net_delta_exposure,
            net_vanna_exposure=snapshot.net_vanna_exposure,
            net_charm_exposure=snapshot.net_charm_exposure,
            total_premium_oi_notional=snapshot.total_premium_oi_notional,
            total_extrinsic_value=snapshot.total_extrinsic_value,
            net_time_decay_per_day=snapshot.net_time_decay_per_day,
            regime_label=snapshot.regime_label,
            regime_confidence=snapshot.regime_confidence,
        )
        previous_close = None if not self._snapshots else self._snapshots[-1].close_price
        self._snapshots.append(normalized_snapshot)
        self._volumes.append(normalized_snapshot.volume)
        self._processed_points += 1
        self._underlying_symbol = normalized_snapshot.underlying_symbol
        self._last_timestamp = normalized_snapshot.timestamp
        self._last_trading_date = normalized_snapshot.trading_date
        if previous_close is not None:
            log_return = log(max(normalized_snapshot.close_price, self._config.minimum_price) / max(previous_close, self._config.minimum_price))
            self._log_returns.append(log_return)
            self._direction_signs.append(_sign(log_return))
        else:
            log_return = 0.0
        vector = self._build_feature_vector(snapshot=normalized_snapshot, log_return=log_return)
        self._last_vector = vector
        return vector

    def get_last_vector(self) -> FeatureVector:
        if self._last_vector is None:
            raise FeaturePipelineComputationError("no feature vector is available")
        return self._last_vector

    def _validate_snapshot(self, *, snapshot: MarketFeatureSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise FeaturePipelineInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise FeaturePipelineInputError("snapshot is stale relative to valuation_timestamp")
        if self._underlying_symbol is not None and snapshot.underlying_symbol != self._underlying_symbol:
            raise FeaturePipelineInputError("pipeline instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp <= self._last_timestamp:
            raise FeaturePipelineInputError("snapshot timestamp must be strictly increasing for replay determinism")

    def _build_feature_vector(self, *, snapshot: MarketFeatureSnapshot, log_return: float) -> FeatureVector:
        features: dict[str, float] = {}
        features["price_close"] = snapshot.close_price
        features["price_range_pct"] = self._safe_ratio(snapshot.high_price - snapshot.low_price, snapshot.close_price)
        features["candle_body_pct"] = self._safe_ratio(snapshot.close_price - snapshot.open_price, snapshot.open_price)
        features["close_location_value"] = self._close_location_value(snapshot)
        features["log_return_1"] = log_return
        features["return_1"] = self._simple_return_from_log(log_return)
        return_windows: Mapping[str, int] = {
            "short": self._config.short_return_window,
            "medium": self._config.medium_return_window,
            "long": self._config.long_return_window,
        }
        for label, window in return_windows.items():
            returns = self._tail_list(self._log_returns, window)
            features[f"return_sum_{label}"] = sum(returns)
            features[f"return_mean_{label}"] = fmean(returns) if returns else 0.0
            features[f"realized_vol_{label}"] = self._realized_volatility(returns)
            features[f"up_move_share_{label}"] = self._up_move_share(returns)
        trend_snapshots = self._tail_list(self._snapshots, self._config.trend_window + 1)
        range_snapshots = self._tail_list(self._snapshots, self._config.range_window)
        features["trend_efficiency"] = self._trend_efficiency(trend_snapshots)
        features["trend_score"] = self._trend_score(trend_snapshots)
        features["directional_persistence"] = self._directional_persistence()
        features["range_compression"] = self._range_compression(range_snapshots)
        features["distance_from_range_mid"] = self._distance_from_range_mid(range_snapshots)
        short_volume = self._tail_list(self._volumes, self._config.short_volume_window)
        medium_volume = self._tail_list(self._volumes, self._config.medium_volume_window)
        features["volume_raw"] = snapshot.volume
        features["volume_zscore_short"] = self._zscore(snapshot.volume, short_volume)
        features["volume_zscore_medium"] = self._zscore(snapshot.volume, medium_volume)
        features["volume_ratio_short"] = self._safe_ratio(snapshot.volume, fmean(short_volume) if short_volume else 0.0)
        features["volume_ratio_medium"] = self._safe_ratio(snapshot.volume, fmean(medium_volume) if medium_volume else 0.0)
        total_open_interest = float(snapshot.call_open_interest + snapshot.put_open_interest)
        features["call_open_interest"] = float(snapshot.call_open_interest)
        features["put_open_interest"] = float(snapshot.put_open_interest)
        features["total_open_interest"] = total_open_interest
        features["put_call_ratio"] = snapshot.put_call_ratio
        features["put_open_interest_share"] = self._safe_ratio(float(snapshot.put_open_interest), total_open_interest)
        features["call_open_interest_share"] = self._safe_ratio(float(snapshot.call_open_interest), total_open_interest)
        features["atm_implied_volatility"] = snapshot.atm_implied_volatility
        features["implied_volatility_skew"] = snapshot.implied_volatility_skew
        features["net_gamma_exposure"] = snapshot.net_gamma_exposure
        features["net_delta_exposure"] = snapshot.net_delta_exposure
        features["net_vanna_exposure"] = snapshot.net_vanna_exposure
        features["net_charm_exposure"] = snapshot.net_charm_exposure
        features["gamma_exposure_to_oi"] = self._safe_ratio(snapshot.net_gamma_exposure, max(total_open_interest, 1.0))
        features["delta_exposure_to_oi"] = self._safe_ratio(snapshot.net_delta_exposure, max(total_open_interest, 1.0))
        features["premium_oi_notional"] = snapshot.total_premium_oi_notional
        features["extrinsic_value_total"] = snapshot.total_extrinsic_value
        features["extrinsic_value_to_oi"] = self._safe_ratio(snapshot.total_extrinsic_value, max(total_open_interest, 1.0))
        features["time_decay_per_day"] = snapshot.net_time_decay_per_day
        features["time_decay_to_premium"] = self._safe_ratio(snapshot.net_time_decay_per_day, max(snapshot.total_premium_oi_notional, 1.0e-12))
        features["overnight_gap_proxy"] = self._overnight_gap_proxy()
        features["intraday_range_change"] = self._intraday_range_change(range_snapshots)
        features["history_ready"] = 1.0 if len(self._snapshots) >= self._config.min_history_points else 0.0
        categorical_features = {
            "underlying_symbol": snapshot.underlying_symbol,
            "regime_label": snapshot.regime_label or "UNKNOWN",
            "session_phase": self._session_phase(snapshot.timestamp),
            "day_of_week": str(snapshot.timestamp.weekday()),
        }
        features["regime_confidence"] = 0.0 if snapshot.regime_confidence is None else snapshot.regime_confidence
        features["regime_known"] = 0.0 if snapshot.regime_label is None else 1.0
        metadata: dict[str, JsonValue] = {
            "processed_points": self._processed_points,
            "history_depth": len(self._snapshots),
            "log_return_depth": len(self._log_returns),
            "volume_depth": len(self._volumes),
        }
        self._assert_feature_finiteness(features)
        return FeatureVector(
            underlying_symbol=snapshot.underlying_symbol,
            timestamp=snapshot.timestamp,
            trading_date=snapshot.trading_date,
            features=features,
            categorical_features=categorical_features,
            metadata=metadata,
        )

    def _overnight_gap_proxy(self) -> float:
        if len(self._snapshots) < 2:
            return 0.0
        current_snapshot = self._snapshots[-1]
        previous_snapshot = self._snapshots[-2]
        if current_snapshot.trading_date == previous_snapshot.trading_date:
            return 0.0
        return self._safe_ratio(current_snapshot.open_price - previous_snapshot.close_price, previous_snapshot.close_price)

    def _intraday_range_change(self, snapshots: list[MarketFeatureSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        current_range_pct = self._safe_ratio(snapshots[-1].high_price - snapshots[-1].low_price, snapshots[-1].close_price)
        previous_range_pct = self._safe_ratio(snapshots[-2].high_price - snapshots[-2].low_price, snapshots[-2].close_price)
        return current_range_pct - previous_range_pct

    def _trend_efficiency(self, snapshots: list[MarketFeatureSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        net_move = fabs(snapshots[-1].close_price - snapshots[0].close_price)
        path_length = 0.0
        for index in range(1, len(snapshots)):
            path_length += fabs(snapshots[index].close_price - snapshots[index - 1].close_price)
        return self._safe_ratio(net_move, path_length)

    def _trend_score(self, snapshots: list[MarketFeatureSnapshot]) -> float:
        if len(snapshots) < 2:
            return 0.0
        first_close = snapshots[0].close_price
        last_close = snapshots[-1].close_price
        net_return = self._safe_ratio(last_close - first_close, first_close)
        return net_return * self._trend_efficiency(snapshots)

    def _directional_persistence(self) -> float:
        if not self._direction_signs:
            return 0.0
        non_zero_signs = [value for value in self._direction_signs if value != 0]
        if not non_zero_signs:
            return 0.0
        return self._safe_ratio(fabs(sum(non_zero_signs)), len(non_zero_signs))

    def _range_compression(self, snapshots: list[MarketFeatureSnapshot]) -> float:
        if not snapshots:
            return 0.0
        highest_high = max(snapshot.high_price for snapshot in snapshots)
        lowest_low = min(snapshot.low_price for snapshot in snapshots)
        latest_close = snapshots[-1].close_price
        return self._safe_ratio(highest_high - lowest_low, latest_close)

    def _distance_from_range_mid(self, snapshots: list[MarketFeatureSnapshot]) -> float:
        if not snapshots:
            return 0.0
        highest_high = max(snapshot.high_price for snapshot in snapshots)
        lowest_low = min(snapshot.low_price for snapshot in snapshots)
        midpoint = lowest_low + (highest_high - lowest_low) / 2.0
        latest_close = snapshots[-1].close_price
        return self._safe_ratio(latest_close - midpoint, latest_close)

    def _realized_volatility(self, returns: list[float]) -> float:
        if not returns:
            return 0.0
        variance = fmean(value * value for value in returns)
        if variance <= 0.0:
            return 0.0
        return variance ** 0.5

    def _up_move_share(self, returns: list[float]) -> float:
        if not returns:
            return 0.0
        up_moves = sum(1 for value in returns if value > 0.0)
        return self._safe_ratio(up_moves, len(returns))

    def _zscore(self, current_value: float, history: list[float]) -> float:
        if len(history) < 2:
            return 0.0
        mean_value = fmean(history)
        variance = fmean((value - mean_value) * (value - mean_value) for value in history)
        if variance <= 0.0:
            return 0.0
        std_dev = variance ** 0.5
        if std_dev <= 0.0:
            return 0.0
        return (current_value - mean_value) / std_dev

    @staticmethod
    def _close_location_value(snapshot: MarketFeatureSnapshot) -> float:
        bar_range = snapshot.high_price - snapshot.low_price
        if bar_range <= 0.0:
            return 0.0
        midpoint = snapshot.low_price + bar_range / 2.0
        return (snapshot.close_price - midpoint) / (bar_range / 2.0)

    @staticmethod
    def _simple_return_from_log(log_return: float) -> float:
        if log_return == 0.0:
            return 0.0
        return (2.718281828459045 ** log_return) - 1.0

    @staticmethod
    def _tail_list(values: Deque[float] | Deque[MarketFeatureSnapshot], count: int) -> list[float] | list[MarketFeatureSnapshot]:
        sequence = list(values)
        if count >= len(sequence):
            return sequence
        return sequence[-count:]

    @staticmethod
    def _session_phase(timestamp: datetime) -> str:
        hour = _ensure_utc(timestamp).hour
        minute = _ensure_utc(timestamp).minute
        total_minutes = hour * 60 + minute
        if total_minutes < 300:
            return "PRE_OPEN_OR_OFF_HOURS"
        if total_minutes < 420:
            return "OPENING_PHASE"
        if total_minutes < 720:
            return "MID_SESSION"
        return "CLOSING_PHASE"

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0.0:
            return 0.0
        return numerator / denominator

    @staticmethod
    def _assert_feature_finiteness(features: Mapping[str, float]) -> None:
        invalid_names = [name for name, value in features.items() if not isfinite(value)]
        if invalid_names:
            raise FeaturePipelineComputationError(
                "non-finite feature values encountered: " + ", ".join(sorted(invalid_names))
            )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


__all__ = [
    "FeaturePipeline",
    "FeaturePipelineComputationError",
    "FeaturePipelineConfig",
    "FeaturePipelineError",
    "FeaturePipelineInputError",
    "FeaturePipelineState",
    "FeatureVector",
    "MarketFeatureSnapshot",
]
