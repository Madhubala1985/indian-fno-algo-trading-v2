# FILE: platform_core/ml/regime_classifier.py
"""
platform_core.ml.regime_classifier
═══════════════════════════════════════════════════════════════════════
Production-grade causal regime classifier for institutional derivatives
intelligence workflows.

The classifier is designed for replay-safe, walk-forward-safe market regime
inference. It consumes finalized bar observations one bar at a time, updates
incremental state without future leakage, and outputs interpretable regime
labels using deterministic feature calculations based on trend efficiency,
realized volatility, range compression, and directional persistence.

The module intentionally avoids model fitting or hidden state inference at this
layer. It provides a robust rule-based classification backbone that can be used
standalone or as a feature/labeling primitive for downstream ML pipelines.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from enum import Enum
from math import fabs
from math import isfinite
from math import log
from statistics import fmean
from typing import Deque


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class RegimeClassifierError(RuntimeError):
    """Base exception for regime classifier failures."""


class RegimeClassifierInputError(RegimeClassifierError):
    """Raised when input data violates causal or structural assumptions."""


class RegimeClassifierComputationError(RegimeClassifierError):
    """Raised when regime computation cannot be completed safely."""


class MarketRegime(str, Enum):
    """Primary market regime labels for downstream orchestration."""

    UNKNOWN = "UNKNOWN"
    TREND_UP_LOW_VOL = "TREND_UP_LOW_VOL"
    TREND_UP_HIGH_VOL = "TREND_UP_HIGH_VOL"
    TREND_DOWN_LOW_VOL = "TREND_DOWN_LOW_VOL"
    TREND_DOWN_HIGH_VOL = "TREND_DOWN_HIGH_VOL"
    RANGE_LOW_VOL = "RANGE_LOW_VOL"
    RANGE_HIGH_VOL = "RANGE_HIGH_VOL"
    TRANSITION = "TRANSITION"


class DirectionState(str, Enum):
    """Directional state inferred from trend score."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class VolatilityState(str, Enum):
    """Volatility state inferred from realized volatility ratios."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class StructureState(str, Enum):
    """Structure state inferred from efficiency and compression."""

    TREND = "TREND"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"


@dataclass(frozen=True, slots=True)
class RegimeClassifierConfig:
    """Configuration for the causal regime classifier."""

    short_vol_window: int = 12
    long_vol_window: int = 48
    trend_window: int = 20
    persistence_window: int = 12
    compression_window: int = 20
    min_history_bars: int = 24
    trend_efficiency_threshold: float = 0.42
    range_efficiency_threshold: float = 0.20
    bullish_trend_score_threshold: float = 0.0015
    bearish_trend_score_threshold: float = -0.0015
    high_vol_ratio_threshold: float = 1.25
    low_vol_ratio_threshold: float = 0.80
    compression_threshold: float = 0.0125
    minimum_price: float = 1.0e-9
    stale_bar_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        integer_fields = {
            "short_vol_window": self.short_vol_window,
            "long_vol_window": self.long_vol_window,
            "trend_window": self.trend_window,
            "persistence_window": self.persistence_window,
            "compression_window": self.compression_window,
            "min_history_bars": self.min_history_bars,
        }
        for field_name, field_value in integer_fields.items():
            if field_value <= 1:
                raise ValueError(f"{field_name} must be greater than 1")
        if self.long_vol_window < self.short_vol_window:
            raise ValueError("long_vol_window must be greater than or equal to short_vol_window")
        if self.min_history_bars < max(self.short_vol_window, self.trend_window, self.compression_window):
            raise ValueError("min_history_bars must cover required lookback windows")
        positive_fields = {
            "trend_efficiency_threshold": self.trend_efficiency_threshold,
            "range_efficiency_threshold": self.range_efficiency_threshold,
            "high_vol_ratio_threshold": self.high_vol_ratio_threshold,
            "low_vol_ratio_threshold": self.low_vol_ratio_threshold,
            "compression_threshold": self.compression_threshold,
            "minimum_price": self.minimum_price,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if self.range_efficiency_threshold >= self.trend_efficiency_threshold:
            raise ValueError("range_efficiency_threshold must be less than trend_efficiency_threshold")
        if self.low_vol_ratio_threshold >= self.high_vol_ratio_threshold:
            raise ValueError("low_vol_ratio_threshold must be less than high_vol_ratio_threshold")
        if self.stale_bar_max_age_seconds is not None and self.stale_bar_max_age_seconds <= 0.0:
            raise ValueError("stale_bar_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class FinalizedBar:
    """Finalized causal bar input for regime classification."""

    underlying_symbol: str
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        numeric_values = {
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
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

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class RegimeFeatures:
    """Interpretable causal features used for regime assignment."""

    log_return: float
    short_realized_volatility: float
    long_realized_volatility: float
    volatility_ratio: float
    trend_efficiency: float
    trend_score: float
    directional_persistence: float
    range_compression: float
    close_location_value: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "log_return": self.log_return,
            "short_realized_volatility": self.short_realized_volatility,
            "long_realized_volatility": self.long_realized_volatility,
            "volatility_ratio": self.volatility_ratio,
            "trend_efficiency": self.trend_efficiency,
            "trend_score": self.trend_score,
            "directional_persistence": self.directional_persistence,
            "range_compression": self.range_compression,
            "close_location_value": self.close_location_value,
        }


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """Single classification result for a finalized bar."""

    underlying_symbol: str
    timestamp: datetime
    regime: MarketRegime
    direction_state: DirectionState
    volatility_state: VolatilityState
    structure_state: StructureState
    confidence: float
    features: RegimeFeatures
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "regime": self.regime.value,
            "direction_state": self.direction_state.value,
            "volatility_state": self.volatility_state.value,
            "structure_state": self.structure_state.value,
            "confidence": self.confidence,
            "features": self.features.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class RegimeClassifierState:
    """Serializable classifier state for replay persistence."""

    underlying_symbol: str | None
    processed_bars: int
    last_timestamp: datetime | None
    last_regime: MarketRegime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "processed_bars": self.processed_bars,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_regime": self.last_regime.value if self.last_regime else None,
        }


class RegimeClassifier:
    """Incremental, causal, deterministic regime classifier."""

    def __init__(self, *, config: RegimeClassifierConfig | None = None) -> None:
        self._config = config or RegimeClassifierConfig()
        maxlen = max(
            self._config.long_vol_window + 1,
            self._config.trend_window + 1,
            self._config.persistence_window + 1,
            self._config.compression_window,
            self._config.min_history_bars + 1,
        )
        self._bars: Deque[FinalizedBar] = deque(maxlen=maxlen)
        self._log_returns: Deque[float] = deque(maxlen=self._config.long_vol_window)
        self._direction_signs: Deque[int] = deque(maxlen=self._config.persistence_window)
        self._underlying_symbol: str | None = None
        self._processed_bars = 0
        self._last_timestamp: datetime | None = None
        self._last_classification: RegimeClassification | None = None

    @property
    def state(self) -> RegimeClassifierState:
        return RegimeClassifierState(
            underlying_symbol=self._underlying_symbol,
            processed_bars=self._processed_bars,
            last_timestamp=self._last_timestamp,
            last_regime=None if self._last_classification is None else self._last_classification.regime,
        )

    def reset(self) -> None:
        self._bars.clear()
        self._log_returns.clear()
        self._direction_signs.clear()
        self._underlying_symbol = None
        self._processed_bars = 0
        self._last_timestamp = None
        self._last_classification = None

    def update(self, *, bar: FinalizedBar, valuation_timestamp: datetime | None = None) -> RegimeClassification:
        self._validate_bar(bar=bar, valuation_timestamp=valuation_timestamp)
        normalized_bar = FinalizedBar(
            underlying_symbol=bar.underlying_symbol,
            timestamp=_ensure_utc(bar.timestamp),
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
            volume=bar.volume,
        )
        previous_close = None if not self._bars else self._bars[-1].close_price
        self._bars.append(normalized_bar)
        self._processed_bars += 1
        self._underlying_symbol = normalized_bar.underlying_symbol
        self._last_timestamp = normalized_bar.timestamp
        if previous_close is not None:
            log_return = log(max(normalized_bar.close_price, self._config.minimum_price) / max(previous_close, self._config.minimum_price))
            self._log_returns.append(log_return)
            self._direction_signs.append(_sign(log_return))
        else:
            log_return = 0.0
        if len(self._bars) < self._config.min_history_bars or len(self._log_returns) < self._config.short_vol_window:
            classification = self._unknown_classification(bar=normalized_bar, log_return=log_return)
            self._last_classification = classification
            return classification
        features = self._compute_features(current_log_return=log_return)
        direction_state = self._infer_direction_state(features=features)
        volatility_state = self._infer_volatility_state(features=features)
        structure_state = self._infer_structure_state(features=features, direction_state=direction_state)
        regime = self._infer_regime(
            direction_state=direction_state,
            volatility_state=volatility_state,
            structure_state=structure_state,
        )
        confidence = self._compute_confidence(
            features=features,
            direction_state=direction_state,
            volatility_state=volatility_state,
            structure_state=structure_state,
        )
        classification = RegimeClassification(
            underlying_symbol=normalized_bar.underlying_symbol,
            timestamp=normalized_bar.timestamp,
            regime=regime,
            direction_state=direction_state,
            volatility_state=volatility_state,
            structure_state=structure_state,
            confidence=confidence,
            features=features,
            metadata={
                "processed_bars": self._processed_bars,
                "history_ready": True,
                "short_vol_window": self._config.short_vol_window,
                "long_vol_window": self._config.long_vol_window,
                "trend_window": self._config.trend_window,
                "compression_window": self._config.compression_window,
            },
        )
        self._last_classification = classification
        return classification

    def get_last_classification(self) -> RegimeClassification:
        if self._last_classification is None:
            raise RegimeClassifierComputationError("no regime classification is available")
        return self._last_classification

    def _validate_bar(self, *, bar: FinalizedBar, valuation_timestamp: datetime | None) -> None:
        normalized_bar_timestamp = _ensure_utc(bar.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_bar_timestamp > normalized_valuation_timestamp:
                raise RegimeClassifierInputError("bar timestamp cannot be later than valuation_timestamp")
            if self._config.stale_bar_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_bar_timestamp).total_seconds()
                if age_seconds > self._config.stale_bar_max_age_seconds:
                    raise RegimeClassifierInputError("bar is stale relative to valuation_timestamp")
        if self._underlying_symbol is not None and bar.underlying_symbol != self._underlying_symbol:
            raise RegimeClassifierInputError("classifier instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_bar_timestamp <= self._last_timestamp:
            raise RegimeClassifierInputError("bar timestamp must be strictly increasing for replay determinism")

    def _unknown_classification(self, *, bar: FinalizedBar, log_return: float) -> RegimeClassification:
        features = RegimeFeatures(
            log_return=log_return,
            short_realized_volatility=0.0,
            long_realized_volatility=0.0,
            volatility_ratio=1.0,
            trend_efficiency=0.0,
            trend_score=0.0,
            directional_persistence=0.0,
            range_compression=0.0,
            close_location_value=0.0,
        )
        return RegimeClassification(
            underlying_symbol=bar.underlying_symbol,
            timestamp=bar.timestamp,
            regime=MarketRegime.UNKNOWN,
            direction_state=DirectionState.NEUTRAL,
            volatility_state=VolatilityState.NORMAL,
            structure_state=StructureState.TRANSITION,
            confidence=0.0,
            features=features,
            metadata={
                "processed_bars": self._processed_bars,
                "history_ready": False,
                "required_bars": self._config.min_history_bars,
            },
        )

    def _compute_features(self, *, current_log_return: float) -> RegimeFeatures:
        short_returns = list(self._slice_tail(self._log_returns, self._config.short_vol_window))
        long_returns = list(self._slice_tail(self._log_returns, self._config.long_vol_window))
        trend_bars = list(self._slice_tail(self._bars, self._config.trend_window + 1))
        compression_bars = list(self._slice_tail(self._bars, self._config.compression_window))
        short_realized_volatility = self._realized_volatility(short_returns)
        long_realized_volatility = self._realized_volatility(long_returns)
        volatility_ratio = self._safe_ratio(short_realized_volatility, max(long_realized_volatility, 1.0e-12))
        trend_efficiency = self._trend_efficiency(trend_bars=trend_bars)
        trend_score = self._trend_score(trend_bars=trend_bars)
        directional_persistence = self._directional_persistence()
        range_compression = self._range_compression(compression_bars=compression_bars)
        close_location_value = self._close_location_value(trend_bars[-1])
        return RegimeFeatures(
            log_return=current_log_return,
            short_realized_volatility=short_realized_volatility,
            long_realized_volatility=long_realized_volatility,
            volatility_ratio=volatility_ratio,
            trend_efficiency=trend_efficiency,
            trend_score=trend_score,
            directional_persistence=directional_persistence,
            range_compression=range_compression,
            close_location_value=close_location_value,
        )

    def _infer_direction_state(self, *, features: RegimeFeatures) -> DirectionState:
        if features.trend_score >= self._config.bullish_trend_score_threshold:
            return DirectionState.BULLISH
        if features.trend_score <= self._config.bearish_trend_score_threshold:
            return DirectionState.BEARISH
        return DirectionState.NEUTRAL

    def _infer_volatility_state(self, *, features: RegimeFeatures) -> VolatilityState:
        if features.volatility_ratio >= self._config.high_vol_ratio_threshold:
            return VolatilityState.HIGH
        if features.volatility_ratio <= self._config.low_vol_ratio_threshold:
            return VolatilityState.LOW
        return VolatilityState.NORMAL

    def _infer_structure_state(self, *, features: RegimeFeatures, direction_state: DirectionState) -> StructureState:
        if (
            features.trend_efficiency >= self._config.trend_efficiency_threshold
            and direction_state is not DirectionState.NEUTRAL
            and features.directional_persistence >= 0.55
        ):
            return StructureState.TREND
        if (
            features.trend_efficiency <= self._config.range_efficiency_threshold
            and features.range_compression <= self._config.compression_threshold
        ):
            return StructureState.RANGE
        return StructureState.TRANSITION

    def _infer_regime(
        self,
        *,
        direction_state: DirectionState,
        volatility_state: VolatilityState,
        structure_state: StructureState,
    ) -> MarketRegime:
        if structure_state is StructureState.RANGE:
            if volatility_state is VolatilityState.HIGH:
                return MarketRegime.RANGE_HIGH_VOL
            return MarketRegime.RANGE_LOW_VOL
        if structure_state is StructureState.TREND:
            if direction_state is DirectionState.BULLISH:
                if volatility_state is VolatilityState.HIGH:
                    return MarketRegime.TREND_UP_HIGH_VOL
                return MarketRegime.TREND_UP_LOW_VOL
            if direction_state is DirectionState.BEARISH:
                if volatility_state is VolatilityState.HIGH:
                    return MarketRegime.TREND_DOWN_HIGH_VOL
                return MarketRegime.TREND_DOWN_LOW_VOL
        return MarketRegime.TRANSITION

    def _compute_confidence(
        self,
        *,
        features: RegimeFeatures,
        direction_state: DirectionState,
        volatility_state: VolatilityState,
        structure_state: StructureState,
    ) -> float:
        trend_component = min(features.trend_efficiency / max(self._config.trend_efficiency_threshold, 1.0e-12), 2.0) / 2.0
        persistence_component = min(features.directional_persistence / 0.75, 1.0)
        volatility_component = min(fabs(features.volatility_ratio - 1.0) / max(self._config.high_vol_ratio_threshold - 1.0, 1.0e-12), 1.0)
        compression_component = 1.0 - min(features.range_compression / max(self._config.compression_threshold * 2.0, 1.0e-12), 1.0)
        if structure_state is StructureState.TREND:
            base_score = 0.45 * trend_component + 0.35 * persistence_component + 0.20 * volatility_component
        elif structure_state is StructureState.RANGE:
            base_score = 0.50 * (1.0 - trend_component) + 0.30 * compression_component + 0.20 * (1.0 - persistence_component)
        else:
            neutrality_penalty = 0.1 if direction_state is DirectionState.NEUTRAL and volatility_state is VolatilityState.NORMAL else 0.0
            base_score = max(0.20, 0.45 - neutrality_penalty)
        if base_score < 0.0:
            return 0.0
        if base_score > 1.0:
            return 1.0
        return base_score

    def _realized_volatility(self, returns: list[float]) -> float:
        if not returns:
            return 0.0
        variance = fmean(value * value for value in returns)
        if variance <= 0.0:
            return 0.0
        return variance ** 0.5

    def _trend_efficiency(self, *, trend_bars: list[FinalizedBar]) -> float:
        if len(trend_bars) < 2:
            return 0.0
        net_move = fabs(trend_bars[-1].close_price - trend_bars[0].close_price)
        path_length = 0.0
        for index in range(1, len(trend_bars)):
            path_length += fabs(trend_bars[index].close_price - trend_bars[index - 1].close_price)
        return self._safe_ratio(net_move, path_length)

    def _trend_score(self, *, trend_bars: list[FinalizedBar]) -> float:
        if len(trend_bars) < 2:
            return 0.0
        first_close = trend_bars[0].close_price
        last_close = trend_bars[-1].close_price
        net_return = self._safe_ratio(last_close - first_close, max(first_close, self._config.minimum_price))
        efficiency = self._trend_efficiency(trend_bars=trend_bars)
        return net_return * max(efficiency, 0.0)

    def _directional_persistence(self) -> float:
        if not self._direction_signs:
            return 0.0
        non_zero_signs = [value for value in self._direction_signs if value != 0]
        if not non_zero_signs:
            return 0.0
        agreement = fabs(sum(non_zero_signs))
        return self._safe_ratio(agreement, len(non_zero_signs))

    def _range_compression(self, *, compression_bars: list[FinalizedBar]) -> float:
        if not compression_bars:
            return 0.0
        highest_high = max(bar.high_price for bar in compression_bars)
        lowest_low = min(bar.low_price for bar in compression_bars)
        latest_close = compression_bars[-1].close_price
        return self._safe_ratio(highest_high - lowest_low, max(latest_close, self._config.minimum_price))

    @staticmethod
    def _close_location_value(bar: FinalizedBar) -> float:
        bar_range = bar.high_price - bar.low_price
        if bar_range <= 0.0:
            return 0.0
        midpoint = bar.low_price + bar_range / 2.0
        return (bar.close_price - midpoint) / (bar_range / 2.0)

    @staticmethod
    def _slice_tail(values: Deque[float] | Deque[FinalizedBar], count: int) -> list[float] | list[FinalizedBar]:
        sequence = list(values)
        if count >= len(sequence):
            return sequence
        return sequence[-count:]

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0.0:
            return 0.0
        return numerator / denominator



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
    "DirectionState",
    "FinalizedBar",
    "MarketRegime",
    "RegimeClassification",
    "RegimeClassifier",
    "RegimeClassifierComputationError",
    "RegimeClassifierConfig",
    "RegimeClassifierError",
    "RegimeClassifierInputError",
    "RegimeClassifierState",
    "RegimeFeatures",
    "StructureState",
    "VolatilityState",
]
