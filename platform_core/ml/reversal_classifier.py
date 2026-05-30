# FILE: platform_core/ml/reversal_classifier.py
"""
platform_core.ml.reversal_classifier
═══════════════════════════════════════════════════════════════════════
Production-grade causal reversal classifier for institutional derivatives
intelligence workflows.

The classifier estimates the probability that current conditions favor a
reversal rather than trend continuation. It is deterministic, replay-safe,
walk-forward-safe, and interpretable. The implementation consumes finalized,
causally observed feature snapshots and converts them into bounded reversal
probabilities with component-level diagnostics.

This module is intentionally lightweight and does not depend on external ML
libraries. It provides a robust classification layer that can be calibrated or
stacked later by downstream model orchestration without changing the interface.
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


class ReversalClassifierError(RuntimeError):
    """Base exception for reversal classifier failures."""


class ReversalClassifierInputError(ReversalClassifierError):
    """Raised when inputs are invalid or causally unsafe."""


class ReversalClassifierComputationError(ReversalClassifierError):
    """Raised when reversal probabilities cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class ReversalClassifierConfig:
    """Configuration for deterministic reversal inference."""

    trap_probability_weight: float = 1.25
    regime_transition_weight: float = 0.70
    exhaustion_weight: float = 0.95
    volatility_reversion_weight: float = 0.70
    options_reversal_weight: float = 0.85
    premium_stress_weight: float = 0.55
    structure_failure_weight: float = 1.00
    probability_floor: float = 0.01
    probability_ceiling: float = 0.99
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "trap_probability_weight": self.trap_probability_weight,
            "regime_transition_weight": self.regime_transition_weight,
            "exhaustion_weight": self.exhaustion_weight,
            "volatility_reversion_weight": self.volatility_reversion_weight,
            "options_reversal_weight": self.options_reversal_weight,
            "premium_stress_weight": self.premium_stress_weight,
            "structure_failure_weight": self.structure_failure_weight,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not 0.0 <= self.probability_floor < self.probability_ceiling <= 1.0:
            raise ValueError("probability_floor and probability_ceiling must satisfy 0 <= floor < ceiling <= 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class ReversalObservation:
    """Causal snapshot used for reversal classification."""

    underlying_symbol: str
    timestamp: datetime
    active_direction: str
    trap_probability: float
    trend_score: float
    trend_efficiency: float
    directional_persistence: float
    realized_volatility_ratio: float
    range_compression: float
    close_location_value: float
    volume_ratio: float
    net_gamma_exposure: float
    net_delta_exposure: float
    net_vanna_exposure: float
    net_charm_exposure: float
    total_premium_oi_notional: float
    total_extrinsic_value: float
    net_time_decay_per_day: float
    put_call_ratio: float
    implied_volatility_change: float
    regime_label: str | None = None
    regime_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        direction = self.active_direction.upper()
        if direction not in {"UP", "DOWN"}:
            raise ValueError("active_direction must be UP or DOWN")
        bounded_probability_values = {
            "trap_probability": self.trap_probability,
            "trend_efficiency": self.trend_efficiency,
            "directional_persistence": self.directional_persistence,
        }
        for field_name, field_value in bounded_probability_values.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        if not -1.0 <= self.close_location_value <= 1.0:
            raise ValueError("close_location_value must be between -1 and 1")
        numeric_values = {
            "trend_score": self.trend_score,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "range_compression": self.range_compression,
            "volume_ratio": self.volume_ratio,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "total_extrinsic_value": self.total_extrinsic_value,
            "net_time_decay_per_day": self.net_time_decay_per_day,
            "put_call_ratio": self.put_call_ratio,
            "implied_volatility_change": self.implied_volatility_change,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.volume_ratio < 0.0:
            raise ValueError("volume_ratio must be non-negative")
        if self.total_premium_oi_notional < 0.0 or self.total_extrinsic_value < 0.0:
            raise ValueError("premium notionals must be non-negative")
        if self.regime_confidence is not None and not 0.0 <= self.regime_confidence <= 1.0:
            raise ValueError("regime_confidence must be between 0 and 1 when provided")

    @property
    def normalized_direction(self) -> str:
        return self.active_direction.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "active_direction": self.normalized_direction,
            "trap_probability": self.trap_probability,
            "trend_score": self.trend_score,
            "trend_efficiency": self.trend_efficiency,
            "directional_persistence": self.directional_persistence,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "range_compression": self.range_compression,
            "close_location_value": self.close_location_value,
            "volume_ratio": self.volume_ratio,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_delta_exposure": self.net_delta_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "total_extrinsic_value": self.total_extrinsic_value,
            "net_time_decay_per_day": self.net_time_decay_per_day,
            "put_call_ratio": self.put_call_ratio,
            "implied_volatility_change": self.implied_volatility_change,
            "regime_label": self.regime_label,
            "regime_confidence": self.regime_confidence,
        }


@dataclass(frozen=True, slots=True)
class ReversalBreakdown:
    """Interpretable component contributions for reversal inference."""

    trap_component: float
    regime_component: float
    exhaustion_component: float
    volatility_component: float
    options_component: float
    premium_component: float
    structure_component: float
    raw_score: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "trap_component": self.trap_component,
            "regime_component": self.regime_component,
            "exhaustion_component": self.exhaustion_component,
            "volatility_component": self.volatility_component,
            "options_component": self.options_component,
            "premium_component": self.premium_component,
            "structure_component": self.structure_component,
            "raw_score": self.raw_score,
        }


@dataclass(frozen=True, slots=True)
class ReversalClassification:
    """Bounded reversal probability output."""

    underlying_symbol: str
    timestamp: datetime
    active_direction: str
    reversal_probability: float
    continuation_probability: float
    confidence: float
    features: dict[str, float]
    breakdown: ReversalBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "active_direction": self.active_direction,
            "reversal_probability": self.reversal_probability,
            "continuation_probability": self.continuation_probability,
            "confidence": self.confidence,
            "features": dict(self.features),
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ReversalClassifierState:
    """Serializable classifier state for replay persistence."""

    processed_observations: int
    last_underlying_symbol: str | None
    last_timestamp: datetime | None
    last_reversal_probability: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_observations": self.processed_observations,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_reversal_probability": self.last_reversal_probability,
        }


class ReversalClassifier:
    """Deterministic reversal classifier using causal snapshot features."""

    def __init__(self, *, config: ReversalClassifierConfig | None = None) -> None:
        self._config = config or ReversalClassifierConfig()
        self._processed_observations = 0
        self._last_underlying_symbol: str | None = None
        self._last_timestamp: datetime | None = None
        self._last_classification: ReversalClassification | None = None

    @property
    def state(self) -> ReversalClassifierState:
        return ReversalClassifierState(
            processed_observations=self._processed_observations,
            last_underlying_symbol=self._last_underlying_symbol,
            last_timestamp=self._last_timestamp,
            last_reversal_probability=None if self._last_classification is None else self._last_classification.reversal_probability,
        )

    def reset(self) -> None:
        self._processed_observations = 0
        self._last_underlying_symbol = None
        self._last_timestamp = None
        self._last_classification = None

    def classify(
        self,
        *,
        observation: ReversalObservation,
        valuation_timestamp: datetime | None = None,
    ) -> ReversalClassification:
        self._validate_observation(observation=observation, valuation_timestamp=valuation_timestamp)
        normalized_timestamp = _ensure_utc(observation.timestamp)
        features = self._extract_features(observation=observation)
        breakdown = self._compute_breakdown(observation=observation, features=features)
        reversal_probability = self._bounded_probability(_sigmoid(breakdown.raw_score))
        continuation_probability = 1.0 - reversal_probability
        confidence = self._compute_confidence(features=features, breakdown=breakdown)
        classification = ReversalClassification(
            underlying_symbol=observation.underlying_symbol,
            timestamp=normalized_timestamp,
            active_direction=observation.normalized_direction,
            reversal_probability=reversal_probability,
            continuation_probability=continuation_probability,
            confidence=confidence,
            features=features,
            breakdown=breakdown,
            metadata={
                "processed_observations": self._processed_observations + 1,
                "regime_label": observation.regime_label,
                "regime_confidence": observation.regime_confidence,
            },
        )
        self._processed_observations += 1
        self._last_underlying_symbol = observation.underlying_symbol
        self._last_timestamp = normalized_timestamp
        self._last_classification = classification
        return classification

    def get_last_classification(self) -> ReversalClassification:
        if self._last_classification is None:
            raise ReversalClassifierComputationError("no reversal classification is available")
        return self._last_classification

    def _validate_observation(self, *, observation: ReversalObservation, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise ReversalClassifierInputError("observation timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise ReversalClassifierInputError("observation is stale relative to valuation_timestamp")
        if self._last_underlying_symbol is not None and observation.underlying_symbol != self._last_underlying_symbol:
            raise ReversalClassifierInputError("classifier instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise ReversalClassifierInputError("observation timestamp must be non-decreasing for replay determinism")

    def _extract_features(self, *, observation: ReversalObservation) -> dict[str, float]:
        trend_exhaustion = _clip01((1.0 - observation.trend_efficiency) * 0.55 + (1.0 - observation.directional_persistence) * 0.45)
        volatility_reversion = _clip01(max(0.0, observation.realized_volatility_ratio - 1.0))
        volume_exhaustion = _clip01(max(0.0, 1.5 - observation.volume_ratio) / 1.5)
        structure_failure = _clip01(max(0.0, 0.04 - observation.range_compression) / 0.04)
        close_location_reversal = _clip01(-observation.close_location_value if observation.normalized_direction == "UP" else observation.close_location_value)
        if observation.normalized_direction == "UP":
            gamma_adverse = _clip01(max(0.0, -observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            delta_unwind = _clip01(max(0.0, -observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
            skew_pressure = _clip01(max(0.0, observation.put_call_ratio - 1.0))
        else:
            gamma_adverse = _clip01(max(0.0, observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            delta_unwind = _clip01(max(0.0, observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
            skew_pressure = _clip01(max(0.0, 1.0 - observation.put_call_ratio))
        vanna_pressure = _clip01(fabs(observation.net_vanna_exposure) / max(observation.total_premium_oi_notional, 1.0))
        charm_pressure = _clip01(fabs(observation.net_charm_exposure) / max(observation.total_premium_oi_notional, 1.0))
        premium_stress = _clip01(observation.total_extrinsic_value / max(observation.total_premium_oi_notional, 1.0))
        theta_pressure = _clip01(fabs(observation.net_time_decay_per_day) / max(observation.total_premium_oi_notional, 1.0))
        regime_transition = self._regime_transition_score(observation=observation)
        iv_reversal = _clip01(fabs(observation.implied_volatility_change))
        features = {
            "trap_probability": observation.trap_probability,
            "trend_exhaustion": trend_exhaustion,
            "volatility_reversion": volatility_reversion,
            "volume_exhaustion": volume_exhaustion,
            "structure_failure": structure_failure,
            "close_location_reversal": close_location_reversal,
            "gamma_adverse": gamma_adverse,
            "delta_unwind": delta_unwind,
            "skew_pressure": skew_pressure,
            "vanna_pressure": vanna_pressure,
            "charm_pressure": charm_pressure,
            "premium_stress": premium_stress,
            "theta_pressure": theta_pressure,
            "regime_transition": regime_transition,
            "iv_reversal": iv_reversal,
        }
        invalid_names = [name for name, value in features.items() if not isfinite(value)]
        if invalid_names:
            raise ReversalClassifierComputationError(
                "non-finite reversal features encountered: " + ", ".join(sorted(invalid_names))
            )
        return features

    def _compute_breakdown(
        self,
        *,
        observation: ReversalObservation,
        features: Mapping[str, float],
    ) -> ReversalBreakdown:
        trap_component = self._config.trap_probability_weight * features["trap_probability"]
        regime_component = self._config.regime_transition_weight * features["regime_transition"]
        exhaustion_component = self._config.exhaustion_weight * (
            0.50 * features["trend_exhaustion"] + 0.25 * features["volume_exhaustion"] + 0.25 * features["close_location_reversal"]
        )
        volatility_component = self._config.volatility_reversion_weight * (
            0.60 * features["volatility_reversion"] + 0.40 * features["iv_reversal"]
        )
        options_component = self._config.options_reversal_weight * (
            0.35 * features["gamma_adverse"]
            + 0.25 * features["delta_unwind"]
            + 0.20 * features["skew_pressure"]
            + 0.10 * features["vanna_pressure"]
            + 0.10 * features["charm_pressure"]
        )
        premium_component = self._config.premium_stress_weight * (
            0.65 * features["premium_stress"] + 0.35 * features["theta_pressure"]
        )
        structure_component = self._config.structure_failure_weight * (
            0.60 * features["structure_failure"] + 0.40 * features["close_location_reversal"]
        )
        raw_score = (
            -1.05
            + trap_component
            + regime_component
            + exhaustion_component
            + volatility_component
            + options_component
            + premium_component
            + structure_component
            - 0.20 * max(0.0, fabs(observation.trend_score) - 0.0015)
        )
        return ReversalBreakdown(
            trap_component=trap_component,
            regime_component=regime_component,
            exhaustion_component=exhaustion_component,
            volatility_component=volatility_component,
            options_component=options_component,
            premium_component=premium_component,
            structure_component=structure_component,
            raw_score=raw_score,
        )

    def _regime_transition_score(self, *, observation: ReversalObservation) -> float:
        if observation.regime_label is None:
            return 0.35 * _clip01(1.0 - observation.trend_efficiency)
        regime_label = observation.regime_label.upper()
        base = 0.0
        if "TRANSITION" in regime_label:
            base += 0.65
        if "RANGE" in regime_label:
            base += 0.25
        if "HIGH_VOL" in regime_label:
            base += 0.20
        if "TREND" in regime_label and (observation.regime_confidence or 0.0) >= 0.60:
            base -= 0.20
        if base < 0.0:
            base = 0.0
        return _clip01(base)

    def _compute_confidence(self, *, features: Mapping[str, float], breakdown: ReversalBreakdown) -> float:
        signal_strength = _clip01(fabs(breakdown.raw_score) / 2.5)
        confirmation_strength = _clip01(
            0.25 * features["trap_probability"]
            + 0.20 * features["structure_failure"]
            + 0.20 * features["trend_exhaustion"]
            + 0.20 * features["gamma_adverse"]
            + 0.15 * features["regime_transition"]
        )
        return _clip01(0.40 * signal_strength + 0.60 * confirmation_strength)

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
    "ReversalBreakdown",
    "ReversalClassification",
    "ReversalClassifier",
    "ReversalClassifierComputationError",
    "ReversalClassifierConfig",
    "ReversalClassifierError",
    "ReversalClassifierInputError",
    "ReversalClassifierState",
    "ReversalObservation",
]
