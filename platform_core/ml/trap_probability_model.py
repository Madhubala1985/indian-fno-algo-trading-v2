# FILE: platform_core/ml/trap_probability_model.py
"""
platform_core.ml.trap_probability_model
═══════════════════════════════════════════════════════════════════════
Production-grade causal trap probability model for institutional derivatives
intelligence workflows.

The model estimates bull-trap and bear-trap probabilities from finalized,
causally observed breakout-attempt snapshots. It is deterministic,
serialization-safe, replay-compatible, and walk-forward-safe. The module does
not train parameters online and does not require external ML libraries.

Design goals:
- No future leakage.
- Interpretable probability decomposition.
- Stable, bounded outputs.
- Incremental state for replay and streaming workflows.
- Compatibility with downstream reversal/continuation classifiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import exp
from math import fabs
from math import isfinite
from typing import Mapping


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class TrapProbabilityModelError(RuntimeError):
    """Base exception for trap probability model failures."""


class TrapProbabilityModelInputError(TrapProbabilityModelError):
    """Raised when model inputs are invalid or causally unsafe."""


class TrapProbabilityModelComputationError(TrapProbabilityModelError):
    """Raised when probabilities cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class TrapProbabilityModelConfig:
    """Configuration for trap probability inference."""

    reclaim_strength_weight: float = 1.20
    wick_rejection_weight: float = 0.90
    structure_reentry_weight: float = 1.10
    volume_exhaustion_weight: float = 0.80
    gamma_pressure_weight: float = 0.75
    iv_shock_weight: float = 0.55
    theta_pressure_weight: float = 0.35
    trend_instability_weight: float = 0.70
    probability_floor: float = 0.01
    probability_ceiling: float = 0.99
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "reclaim_strength_weight": self.reclaim_strength_weight,
            "wick_rejection_weight": self.wick_rejection_weight,
            "structure_reentry_weight": self.structure_reentry_weight,
            "volume_exhaustion_weight": self.volume_exhaustion_weight,
            "gamma_pressure_weight": self.gamma_pressure_weight,
            "iv_shock_weight": self.iv_shock_weight,
            "theta_pressure_weight": self.theta_pressure_weight,
            "trend_instability_weight": self.trend_instability_weight,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not 0.0 <= self.probability_floor < self.probability_ceiling <= 1.0:
            raise ValueError("probability_floor and probability_ceiling must satisfy 0 <= floor < ceiling <= 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class TrapObservation:
    """Causal breakout-attempt snapshot for trap inference."""

    underlying_symbol: str
    timestamp: datetime
    attempted_direction: str
    reference_level: float
    breakout_extent_pct: float
    reclaim_distance_pct: float
    close_back_inside_range: bool
    wick_rejection_pct: float
    volume_ratio: float
    directional_persistence: float
    trend_efficiency: float
    realized_volatility_ratio: float
    range_compression: float
    close_location_value: float
    put_call_ratio: float
    atm_implied_volatility: float
    implied_volatility_change: float
    net_gamma_exposure: float
    net_delta_exposure: float
    net_vanna_exposure: float
    net_charm_exposure: float
    total_premium_oi_notional: float
    net_time_decay_per_day: float
    regime_label: str | None = None
    regime_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        direction = self.attempted_direction.upper()
        if direction not in {"UP", "DOWN"}:
            raise ValueError("attempted_direction must be UP or DOWN")
        numeric_values = {
            "reference_level": self.reference_level,
            "breakout_extent_pct": self.breakout_extent_pct,
            "reclaim_distance_pct": self.reclaim_distance_pct,
            "wick_rejection_pct": self.wick_rejection_pct,
            "volume_ratio": self.volume_ratio,
            "directional_persistence": self.directional_persistence,
            "trend_efficiency": self.trend_efficiency,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "range_compression": self.range_compression,
            "close_location_value": self.close_location_value,
            "put_call_ratio": self.put_call_ratio,
            "atm_implied_volatility": self.atm_implied_volatility,
            "implied_volatility_change": self.implied_volatility_change,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "net_time_decay_per_day": self.net_time_decay_per_day,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.reference_level <= 0.0:
            raise ValueError("reference_level must be positive")
        bounded_zero_one = {
            "directional_persistence": self.directional_persistence,
            "trend_efficiency": self.trend_efficiency,
        }
        for field_name, field_value in bounded_zero_one.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not -1.0 <= self.close_location_value <= 1.0:
            raise ValueError("close_location_value must be between -1 and 1")
        if self.volume_ratio < 0.0:
            raise ValueError("volume_ratio must be non-negative")
        if self.atm_implied_volatility < 0.0:
            raise ValueError("atm_implied_volatility must be non-negative")
        if self.total_premium_oi_notional < 0.0:
            raise ValueError("total_premium_oi_notional must be non-negative")
        if self.regime_confidence is not None and not 0.0 <= self.regime_confidence <= 1.0:
            raise ValueError("regime_confidence must be between 0 and 1 when provided")

    @property
    def normalized_direction(self) -> str:
        return self.attempted_direction.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "attempted_direction": self.normalized_direction,
            "reference_level": self.reference_level,
            "breakout_extent_pct": self.breakout_extent_pct,
            "reclaim_distance_pct": self.reclaim_distance_pct,
            "close_back_inside_range": self.close_back_inside_range,
            "wick_rejection_pct": self.wick_rejection_pct,
            "volume_ratio": self.volume_ratio,
            "directional_persistence": self.directional_persistence,
            "trend_efficiency": self.trend_efficiency,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "range_compression": self.range_compression,
            "close_location_value": self.close_location_value,
            "put_call_ratio": self.put_call_ratio,
            "atm_implied_volatility": self.atm_implied_volatility,
            "implied_volatility_change": self.implied_volatility_change,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "net_time_decay_per_day": self.net_time_decay_per_day,
            "regime_label": self.regime_label,
            "regime_confidence": self.regime_confidence,
        }


@dataclass(frozen=True, slots=True)
class TrapProbabilityBreakdown:
    """Interpretable component contributions for trap probability."""

    reclaim_component: float
    wick_component: float
    structure_component: float
    volume_component: float
    options_component: float
    instability_component: float
    raw_score: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "reclaim_component": self.reclaim_component,
            "wick_component": self.wick_component,
            "structure_component": self.structure_component,
            "volume_component": self.volume_component,
            "options_component": self.options_component,
            "instability_component": self.instability_component,
            "raw_score": self.raw_score,
        }


@dataclass(frozen=True, slots=True)
class TrapProbabilityResult:
    """Probability output for a bull or bear trap scenario."""

    underlying_symbol: str
    timestamp: datetime
    attempted_direction: str
    trap_side: str
    trap_probability: float
    continuation_probability: float
    confidence: float
    features: dict[str, float]
    breakdown: TrapProbabilityBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "attempted_direction": self.attempted_direction,
            "trap_side": self.trap_side,
            "trap_probability": self.trap_probability,
            "continuation_probability": self.continuation_probability,
            "confidence": self.confidence,
            "features": dict(self.features),
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class TrapProbabilityState:
    """Serializable model state for replay persistence."""

    processed_observations: int
    last_underlying_symbol: str | None
    last_timestamp: datetime | None
    last_trap_probability: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_observations": self.processed_observations,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_trap_probability": self.last_trap_probability,
        }


class TrapProbabilityModel:
    """Deterministic trap probability model for false breakout detection."""

    def __init__(self, *, config: TrapProbabilityModelConfig | None = None) -> None:
        self._config = config or TrapProbabilityModelConfig()
        self._processed_observations = 0
        self._last_underlying_symbol: str | None = None
        self._last_timestamp: datetime | None = None
        self._last_result: TrapProbabilityResult | None = None

    @property
    def state(self) -> TrapProbabilityState:
        return TrapProbabilityState(
            processed_observations=self._processed_observations,
            last_underlying_symbol=self._last_underlying_symbol,
            last_timestamp=self._last_timestamp,
            last_trap_probability=None if self._last_result is None else self._last_result.trap_probability,
        )

    def reset(self) -> None:
        self._processed_observations = 0
        self._last_underlying_symbol = None
        self._last_timestamp = None
        self._last_result = None

    def predict(
        self,
        *,
        observation: TrapObservation,
        valuation_timestamp: datetime | None = None,
    ) -> TrapProbabilityResult:
        self._validate_observation(observation=observation, valuation_timestamp=valuation_timestamp)
        normalized_timestamp = _ensure_utc(observation.timestamp)
        features = self._extract_features(observation=observation)
        breakdown = self._compute_breakdown(observation=observation, features=features)
        probability = self._bounded_probability(_sigmoid(breakdown.raw_score))
        continuation_probability = 1.0 - probability
        confidence = self._compute_confidence(features=features, breakdown=breakdown)
        trap_side = "BULL_TRAP" if observation.normalized_direction == "UP" else "BEAR_TRAP"
        result = TrapProbabilityResult(
            underlying_symbol=observation.underlying_symbol,
            timestamp=normalized_timestamp,
            attempted_direction=observation.normalized_direction,
            trap_side=trap_side,
            trap_probability=probability,
            continuation_probability=continuation_probability,
            confidence=confidence,
            features=features,
            breakdown=breakdown,
            metadata={
                "processed_observations": self._processed_observations + 1,
                "regime_label": observation.regime_label,
                "regime_confidence": observation.regime_confidence,
                "reference_level": observation.reference_level,
            },
        )
        self._processed_observations += 1
        self._last_underlying_symbol = observation.underlying_symbol
        self._last_timestamp = normalized_timestamp
        self._last_result = result
        return result

    def get_last_result(self) -> TrapProbabilityResult:
        if self._last_result is None:
            raise TrapProbabilityModelComputationError("no trap probability result is available")
        return self._last_result

    def _validate_observation(self, *, observation: TrapObservation, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise TrapProbabilityModelInputError("observation timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise TrapProbabilityModelInputError("observation is stale relative to valuation_timestamp")
        if self._last_underlying_symbol is not None and observation.underlying_symbol != self._last_underlying_symbol:
            raise TrapProbabilityModelInputError("model instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise TrapProbabilityModelInputError("observation timestamp must be non-decreasing for replay determinism")

    def _extract_features(self, *, observation: TrapObservation) -> dict[str, float]:
        reclaim_strength = _clip01(observation.reclaim_distance_pct / max(fabs(observation.breakout_extent_pct), 1.0e-12))
        wick_rejection_strength = _clip01(fabs(observation.wick_rejection_pct))
        structure_reentry = 1.0 if observation.close_back_inside_range else 0.0
        volume_exhaustion = _clip01(max(0.0, 1.5 - observation.volume_ratio) / 1.5)
        if observation.normalized_direction == "UP":
            gamma_pressure = _clip01(max(0.0, observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            options_skew_pressure = _clip01(max(0.0, observation.put_call_ratio - 1.0))
            delta_adverse = _clip01(max(0.0, -observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
        else:
            gamma_pressure = _clip01(max(0.0, -observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            options_skew_pressure = _clip01(max(0.0, 1.0 - observation.put_call_ratio))
            delta_adverse = _clip01(max(0.0, observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
        iv_shock = _clip01(max(0.0, fabs(observation.implied_volatility_change)))
        theta_pressure = _clip01(fabs(observation.net_time_decay_per_day) / max(observation.total_premium_oi_notional, 1.0))
        trend_instability = _clip01((1.0 - observation.trend_efficiency) * 0.5 + (1.0 - observation.directional_persistence) * 0.5)
        volatility_shock = _clip01(max(0.0, observation.realized_volatility_ratio - 1.0))
        compression_memory = _clip01(max(0.0, 0.05 - observation.range_compression) / 0.05)
        clv_reversal = _clip01(-observation.close_location_value if observation.normalized_direction == "UP" else observation.close_location_value)
        features = {
            "reclaim_strength": reclaim_strength,
            "wick_rejection_strength": wick_rejection_strength,
            "structure_reentry": structure_reentry,
            "volume_exhaustion": volume_exhaustion,
            "gamma_pressure": gamma_pressure,
            "options_skew_pressure": options_skew_pressure,
            "delta_adverse": delta_adverse,
            "iv_shock": iv_shock,
            "theta_pressure": theta_pressure,
            "trend_instability": trend_instability,
            "volatility_shock": volatility_shock,
            "compression_memory": compression_memory,
            "close_location_reversal": clv_reversal,
        }
        invalid_names = [name for name, value in features.items() if not isfinite(value)]
        if invalid_names:
            raise TrapProbabilityModelComputationError(
                "non-finite trap features encountered: " + ", ".join(sorted(invalid_names))
            )
        return features

    def _compute_breakdown(
        self,
        *,
        observation: TrapObservation,
        features: Mapping[str, float],
    ) -> TrapProbabilityBreakdown:
        reclaim_component = self._config.reclaim_strength_weight * (
            0.65 * features["reclaim_strength"] + 0.35 * features["close_location_reversal"]
        )
        wick_component = self._config.wick_rejection_weight * features["wick_rejection_strength"]
        structure_component = self._config.structure_reentry_weight * (
            0.70 * features["structure_reentry"] + 0.30 * features["compression_memory"]
        )
        volume_component = self._config.volume_exhaustion_weight * (
            0.75 * features["volume_exhaustion"] + 0.25 * features["volatility_shock"]
        )
        options_component = (
            self._config.gamma_pressure_weight * (0.50 * features["gamma_pressure"] + 0.25 * features["options_skew_pressure"] + 0.25 * features["delta_adverse"])
            + self._config.iv_shock_weight * features["iv_shock"]
            + self._config.theta_pressure_weight * features["theta_pressure"]
        )
        instability_component = self._config.trend_instability_weight * features["trend_instability"]
        directional_bias = -0.20 * observation.trend_efficiency - 0.15 * observation.directional_persistence
        raw_score = (
            -1.15
            + reclaim_component
            + wick_component
            + structure_component
            + volume_component
            + options_component
            + instability_component
            + directional_bias
        )
        if observation.regime_label is not None:
            regime_label = observation.regime_label.upper()
            if "RANGE" in regime_label:
                raw_score += 0.10
            if "HIGH_VOL" in regime_label:
                raw_score += 0.08
            if "TREND" in regime_label and (observation.regime_confidence or 0.0) >= 0.60:
                raw_score -= 0.18
        return TrapProbabilityBreakdown(
            reclaim_component=reclaim_component,
            wick_component=wick_component,
            structure_component=structure_component,
            volume_component=volume_component,
            options_component=options_component,
            instability_component=instability_component,
            raw_score=raw_score,
        )

    def _compute_confidence(self, *, features: Mapping[str, float], breakdown: TrapProbabilityBreakdown) -> float:
        signal_strength = _clip01(fabs(breakdown.raw_score) / 2.5)
        confirmation_strength = _clip01(
            0.25 * features["reclaim_strength"]
            + 0.20 * features["wick_rejection_strength"]
            + 0.20 * features["structure_reentry"]
            + 0.20 * features["trend_instability"]
            + 0.15 * features["gamma_pressure"]
        )
        return _clip01(0.45 * signal_strength + 0.55 * confirmation_strength)

    def _bounded_probability(self, probability: float) -> float:
        if probability < self._config.probability_floor:
            return self._config.probability_floor
        if probability > self._config.probability_ceiling:
            return self._config.probability_ceiling
        return probability



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _sigmoid(value: float) -> float:
    if value >= 0.0:
        denominator = 1.0 + exp(-value)
        return 1.0 / denominator
    exponent = exp(value)
    return exponent / (1.0 + exponent)



def _clip01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


__all__ = [
    "TrapObservation",
    "TrapProbabilityBreakdown",
    "TrapProbabilityModel",
    "TrapProbabilityModelComputationError",
    "TrapProbabilityModelConfig",
    "TrapProbabilityModelError",
    "TrapProbabilityModelInputError",
    "TrapProbabilityResult",
    "TrapProbabilityState",
]
