# FILE: platform_core/ml/continuation_classifier.py
"""
platform_core.ml.continuation_classifier
═══════════════════════════════════════════════════════════════════════
Production-grade causal continuation classifier for institutional
Indian index derivatives workflows.

The classifier estimates the probability that the active directional move is
more likely to continue than reverse, based solely on finalized causal inputs.
It is deterministic, replay-safe, walk-forward-safe, and interpretable. The
module is intentionally dependency-light and suitable for real-time streaming,
replay, and downstream ensemble integration.

This classifier complements the reversal classifier. It does not assume that
continuation probability is a trivial inverse of another model; instead it
scores continuation conditions directly from structure, trend quality,
participation, volatility alignment, and options-context support.
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


class ContinuationClassifierError(RuntimeError):
    """Base exception for continuation classifier failures."""


class ContinuationClassifierInputError(ContinuationClassifierError):
    """Raised when inputs are invalid or violate causal assumptions."""


class ContinuationClassifierComputationError(ContinuationClassifierError):
    """Raised when continuation probabilities cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class ContinuationClassifierConfig:
    """Configuration for deterministic continuation inference."""

    trend_quality_weight: float = 1.15
    participation_weight: float = 0.90
    structure_hold_weight: float = 1.05
    volatility_alignment_weight: float = 0.75
    options_support_weight: float = 0.85
    premium_support_weight: float = 0.45
    regime_alignment_weight: float = 0.80
    anti_trap_weight: float = 1.10
    probability_floor: float = 0.01
    probability_ceiling: float = 0.99
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "trend_quality_weight": self.trend_quality_weight,
            "participation_weight": self.participation_weight,
            "structure_hold_weight": self.structure_hold_weight,
            "volatility_alignment_weight": self.volatility_alignment_weight,
            "options_support_weight": self.options_support_weight,
            "premium_support_weight": self.premium_support_weight,
            "regime_alignment_weight": self.regime_alignment_weight,
            "anti_trap_weight": self.anti_trap_weight,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not 0.0 <= self.probability_floor < self.probability_ceiling <= 1.0:
            raise ValueError("probability_floor and probability_ceiling must satisfy 0 <= floor < ceiling <= 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class ContinuationObservation:
    """Causal snapshot used for continuation classification."""

    underlying_symbol: str
    timestamp: datetime
    active_direction: str
    trap_probability: float
    reversal_probability: float
    trend_score: float
    trend_efficiency: float
    directional_persistence: float
    realized_volatility_ratio: float
    range_compression: float
    close_location_value: float
    volume_ratio: float
    breakout_hold_pct: float
    pullback_shallow_pct: float
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
        bounded_zero_one = {
            "trap_probability": self.trap_probability,
            "reversal_probability": self.reversal_probability,
            "trend_efficiency": self.trend_efficiency,
            "directional_persistence": self.directional_persistence,
            "breakout_hold_pct": self.breakout_hold_pct,
            "pullback_shallow_pct": self.pullback_shallow_pct,
        }
        for field_name, field_value in bounded_zero_one.items():
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
            "reversal_probability": self.reversal_probability,
            "trend_score": self.trend_score,
            "trend_efficiency": self.trend_efficiency,
            "directional_persistence": self.directional_persistence,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "range_compression": self.range_compression,
            "close_location_value": self.close_location_value,
            "volume_ratio": self.volume_ratio,
            "breakout_hold_pct": self.breakout_hold_pct,
            "pullback_shallow_pct": self.pullback_shallow_pct,
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
class ContinuationBreakdown:
    """Interpretable continuation score decomposition."""

    trend_quality_component: float
    participation_component: float
    structure_hold_component: float
    volatility_alignment_component: float
    options_support_component: float
    premium_support_component: float
    regime_alignment_component: float
    anti_trap_component: float
    raw_score: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "trend_quality_component": self.trend_quality_component,
            "participation_component": self.participation_component,
            "structure_hold_component": self.structure_hold_component,
            "volatility_alignment_component": self.volatility_alignment_component,
            "options_support_component": self.options_support_component,
            "premium_support_component": self.premium_support_component,
            "regime_alignment_component": self.regime_alignment_component,
            "anti_trap_component": self.anti_trap_component,
            "raw_score": self.raw_score,
        }


@dataclass(frozen=True, slots=True)
class ContinuationClassification:
    """Bounded continuation probability output."""

    underlying_symbol: str
    timestamp: datetime
    active_direction: str
    continuation_probability: float
    reversal_probability: float
    confidence: float
    features: dict[str, float]
    breakdown: ContinuationBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "active_direction": self.active_direction,
            "continuation_probability": self.continuation_probability,
            "reversal_probability": self.reversal_probability,
            "confidence": self.confidence,
            "features": dict(self.features),
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ContinuationClassifierState:
    """Serializable classifier state for replay persistence."""

    processed_observations: int
    last_underlying_symbol: str | None
    last_timestamp: datetime | None
    last_continuation_probability: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_observations": self.processed_observations,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_continuation_probability": self.last_continuation_probability,
        }


class ContinuationClassifier:
    """Deterministic continuation classifier using causal snapshot features."""

    def __init__(self, *, config: ContinuationClassifierConfig | None = None) -> None:
        self._config = config or ContinuationClassifierConfig()
        self._processed_observations = 0
        self._last_underlying_symbol: str | None = None
        self._last_timestamp: datetime | None = None
        self._last_classification: ContinuationClassification | None = None

    @property
    def state(self) -> ContinuationClassifierState:
        return ContinuationClassifierState(
            processed_observations=self._processed_observations,
            last_underlying_symbol=self._last_underlying_symbol,
            last_timestamp=self._last_timestamp,
            last_continuation_probability=None if self._last_classification is None else self._last_classification.continuation_probability,
        )

    def reset(self) -> None:
        self._processed_observations = 0
        self._last_underlying_symbol = None
        self._last_timestamp = None
        self._last_classification = None

    def classify(
        self,
        *,
        observation: ContinuationObservation,
        valuation_timestamp: datetime | None = None,
    ) -> ContinuationClassification:
        self._validate_observation(observation=observation, valuation_timestamp=valuation_timestamp)
        normalized_timestamp = _ensure_utc(observation.timestamp)
        features = self._extract_features(observation=observation)
        breakdown = self._compute_breakdown(observation=observation, features=features)
        continuation_probability = self._bounded_probability(_sigmoid(breakdown.raw_score))
        confidence = self._compute_confidence(features=features, breakdown=breakdown)
        classification = ContinuationClassification(
            underlying_symbol=observation.underlying_symbol,
            timestamp=normalized_timestamp,
            active_direction=observation.normalized_direction,
            continuation_probability=continuation_probability,
            reversal_probability=1.0 - continuation_probability,
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

    def get_last_classification(self) -> ContinuationClassification:
        if self._last_classification is None:
            raise ContinuationClassifierComputationError("no continuation classification is available")
        return self._last_classification

    def _validate_observation(self, *, observation: ContinuationObservation, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise ContinuationClassifierInputError("observation timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise ContinuationClassifierInputError("observation is stale relative to valuation_timestamp")
        if self._last_underlying_symbol is not None and observation.underlying_symbol != self._last_underlying_symbol:
            raise ContinuationClassifierInputError("classifier instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise ContinuationClassifierInputError("observation timestamp must be non-decreasing for replay determinism")

    def _extract_features(self, *, observation: ContinuationObservation) -> dict[str, float]:
        trend_quality = _clip01(
            0.45 * observation.trend_efficiency
            + 0.30 * observation.directional_persistence
            + 0.25 * _clip01(fabs(observation.trend_score) / 0.01)
        )
        participation = _clip01(min(observation.volume_ratio, 2.0) / 2.0)
        structure_hold = _clip01(0.60 * observation.breakout_hold_pct + 0.40 * observation.pullback_shallow_pct)
        volatility_alignment = _clip01(max(0.0, 1.4 - fabs(observation.realized_volatility_ratio - 1.0)) / 1.4)
        close_location_confirmation = _clip01(observation.close_location_value if observation.normalized_direction == "UP" else -observation.close_location_value)
        if observation.normalized_direction == "UP":
            gamma_support = _clip01(max(0.0, observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            delta_support = _clip01(max(0.0, observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
            skew_support = _clip01(max(0.0, 1.0 - observation.put_call_ratio))
        else:
            gamma_support = _clip01(max(0.0, -observation.net_gamma_exposure) / max(observation.total_premium_oi_notional, 1.0))
            delta_support = _clip01(max(0.0, -observation.net_delta_exposure) / max(observation.total_premium_oi_notional, 1.0))
            skew_support = _clip01(max(0.0, observation.put_call_ratio - 1.0))
        vanna_support = _clip01(max(0.0, observation.net_vanna_exposure) / max(observation.total_premium_oi_notional, 1.0))
        charm_drag = _clip01(fabs(observation.net_charm_exposure) / max(observation.total_premium_oi_notional, 1.0))
        premium_support = _clip01(1.0 - min(observation.total_extrinsic_value / max(observation.total_premium_oi_notional, 1.0), 1.0))
        theta_drag = _clip01(fabs(observation.net_time_decay_per_day) / max(observation.total_premium_oi_notional, 1.0))
        regime_alignment = self._regime_alignment_score(observation=observation)
        anti_trap_strength = _clip01(1.0 - max(observation.trap_probability, observation.reversal_probability))
        iv_alignment = _clip01(max(0.0, 1.0 - fabs(observation.implied_volatility_change)))
        range_expansion_readiness = _clip01(min(observation.range_compression, 0.05) / 0.05)
        features = {
            "trend_quality": trend_quality,
            "participation": participation,
            "structure_hold": structure_hold,
            "volatility_alignment": volatility_alignment,
            "close_location_confirmation": close_location_confirmation,
            "gamma_support": gamma_support,
            "delta_support": delta_support,
            "skew_support": skew_support,
            "vanna_support": vanna_support,
            "charm_drag": charm_drag,
            "premium_support": premium_support,
            "theta_drag": theta_drag,
            "regime_alignment": regime_alignment,
            "anti_trap_strength": anti_trap_strength,
            "iv_alignment": iv_alignment,
            "range_expansion_readiness": range_expansion_readiness,
        }
        invalid_names = [name for name, value in features.items() if not isfinite(value)]
        if invalid_names:
            raise ContinuationClassifierComputationError(
                "non-finite continuation features encountered: " + ", ".join(sorted(invalid_names))
            )
        return features

    def _compute_breakdown(
        self,
        *,
        observation: ContinuationObservation,
        features: Mapping[str, float],
    ) -> ContinuationBreakdown:
        trend_quality_component = self._config.trend_quality_weight * features["trend_quality"]
        participation_component = self._config.participation_weight * (
            0.65 * features["participation"] + 0.35 * features["close_location_confirmation"]
        )
        structure_hold_component = self._config.structure_hold_weight * (
            0.65 * features["structure_hold"] + 0.35 * features["range_expansion_readiness"]
        )
        volatility_alignment_component = self._config.volatility_alignment_weight * (
            0.55 * features["volatility_alignment"] + 0.45 * features["iv_alignment"]
        )
        options_support_component = self._config.options_support_weight * (
            0.30 * features["gamma_support"]
            + 0.25 * features["delta_support"]
            + 0.20 * features["skew_support"]
            + 0.15 * features["vanna_support"]
            - 0.10 * features["charm_drag"]
        )
        premium_support_component = self._config.premium_support_weight * (
            0.65 * features["premium_support"] - 0.35 * features["theta_drag"]
        )
        regime_alignment_component = self._config.regime_alignment_weight * features["regime_alignment"]
        anti_trap_component = self._config.anti_trap_weight * features["anti_trap_strength"]
        raw_score = (
            -1.10
            + trend_quality_component
            + participation_component
            + structure_hold_component
            + volatility_alignment_component
            + options_support_component
            + premium_support_component
            + regime_alignment_component
            + anti_trap_component
            - 0.25 * max(observation.reversal_probability - observation.trap_probability, 0.0)
        )
        return ContinuationBreakdown(
            trend_quality_component=trend_quality_component,
            participation_component=participation_component,
            structure_hold_component=structure_hold_component,
            volatility_alignment_component=volatility_alignment_component,
            options_support_component=options_support_component,
            premium_support_component=premium_support_component,
            regime_alignment_component=regime_alignment_component,
            anti_trap_component=anti_trap_component,
            raw_score=raw_score,
        )

    def _regime_alignment_score(self, *, observation: ContinuationObservation) -> float:
        if observation.regime_label is None:
            return _clip01(0.40 * observation.trend_efficiency + 0.30 * observation.directional_persistence)
        regime_label = observation.regime_label.upper()
        base = 0.0
        if "TREND" in regime_label:
            base += 0.55
        if "LOW_VOL" in regime_label:
            base += 0.15
        if "RANGE" in regime_label:
            base -= 0.20
        if "TRANSITION" in regime_label:
            base -= 0.18
        if "HIGH_VOL" in regime_label:
            base -= 0.10
        base += 0.20 * (observation.regime_confidence or 0.0)
        if base < 0.0:
            base = 0.0
        return _clip01(base)

    def _compute_confidence(self, *, features: Mapping[str, float], breakdown: ContinuationBreakdown) -> float:
        signal_strength = _clip01(fabs(breakdown.raw_score) / 2.5)
        confirmation_strength = _clip01(
            0.22 * features["trend_quality"]
            + 0.18 * features["structure_hold"]
            + 0.18 * features["anti_trap_strength"]
            + 0.16 * features["participation"]
            + 0.14 * features["gamma_support"]
            + 0.12 * features["regime_alignment"]
        )
        return _clip01(0.42 * signal_strength + 0.58 * confirmation_strength)

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
    "ContinuationBreakdown",
    "ContinuationClassification",
    "ContinuationClassifier",
    "ContinuationClassifierComputationError",
    "ContinuationClassifierConfig",
    "ContinuationClassifierError",
    "ContinuationClassifierInputError",
    "ContinuationClassifierState",
    "ContinuationObservation",
]
