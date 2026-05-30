# FILE: platform_core/ml/confidence_engine.py
"""
platform_core.ml.confidence_engine
═══════════════════════════════════════════════════════════════════════
Production-grade confidence scoring engine for institutional derivatives
intelligence workflows.

The engine fuses causal model outputs into an interpretable decision confidence
score suitable for downstream signal filtering, risk controls, and execution
ranking. It is deterministic, replay-safe, walk-forward-safe, and calibration
aware. No online learning or non-causal fitting occurs inside this module.

The design emphasizes:
- Agreement-aware confidence fusion.
- Probability sharpness and margin sensitivity.
- Regime-conditioned confidence shaping.
- Calibration-awareness hooks via externally supplied quality metrics.
- Serialization-safe state for Redis/PostgreSQL/TimescaleDB/replay workflows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import fabs
from math import isfinite
from typing import Mapping


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ConfidenceEngineError(RuntimeError):
    """Base exception for confidence engine failures."""


class ConfidenceEngineInputError(ConfidenceEngineError):
    """Raised when inputs are invalid or causally unsafe."""


class ConfidenceEngineComputationError(ConfidenceEngineError):
    """Raised when confidence scoring cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class CalibrationQuality:
    """Externally estimated calibration quality metrics for a model stream."""

    brier_skill: float
    expected_calibration_error: float
    stability_score: float

    def __post_init__(self) -> None:
        numeric_values = {
            "brier_skill": self.brier_skill,
            "expected_calibration_error": self.expected_calibration_error,
            "stability_score": self.stability_score,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if not 0.0 <= self.expected_calibration_error <= 1.0:
            raise ValueError("expected_calibration_error must be between 0 and 1")
        if not 0.0 <= self.stability_score <= 1.0:
            raise ValueError("stability_score must be between 0 and 1")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "brier_skill": self.brier_skill,
            "expected_calibration_error": self.expected_calibration_error,
            "stability_score": self.stability_score,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceEngineConfig:
    """Configuration for decision confidence fusion."""

    directional_margin_weight: float = 1.10
    model_agreement_weight: float = 1.00
    probability_sharpness_weight: float = 0.85
    regime_alignment_weight: float = 0.70
    calibration_weight: float = 0.75
    liquidity_quality_weight: float = 0.55
    options_quality_weight: float = 0.65
    uncertainty_penalty_weight: float = 1.10
    minimum_confidence: float = 0.01
    maximum_confidence: float = 0.99
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        positive_fields = {
            "directional_margin_weight": self.directional_margin_weight,
            "model_agreement_weight": self.model_agreement_weight,
            "probability_sharpness_weight": self.probability_sharpness_weight,
            "regime_alignment_weight": self.regime_alignment_weight,
            "calibration_weight": self.calibration_weight,
            "liquidity_quality_weight": self.liquidity_quality_weight,
            "options_quality_weight": self.options_quality_weight,
            "uncertainty_penalty_weight": self.uncertainty_penalty_weight,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if not 0.0 <= self.minimum_confidence < self.maximum_confidence <= 1.0:
            raise ValueError("minimum_confidence and maximum_confidence must satisfy 0 <= min < max <= 1")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class ConfidenceObservation:
    """Causal fused-model observation used for final confidence scoring."""

    underlying_symbol: str
    timestamp: datetime
    continuation_probability: float
    reversal_probability: float
    trap_probability: float
    regime_confidence: float
    feature_readiness: float
    liquidity_score: float
    option_data_quality_score: float
    realized_volatility_ratio: float
    spread_bps: float
    volume_ratio: float
    model_calibration: CalibrationQuality | None = None
    regime_label: str | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        bounded_values = {
            "continuation_probability": self.continuation_probability,
            "reversal_probability": self.reversal_probability,
            "trap_probability": self.trap_probability,
            "regime_confidence": self.regime_confidence,
            "feature_readiness": self.feature_readiness,
            "liquidity_score": self.liquidity_score,
            "option_data_quality_score": self.option_data_quality_score,
        }
        for field_name, field_value in bounded_values.items():
            if not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        numeric_values = {
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "spread_bps": self.spread_bps,
            "volume_ratio": self.volume_ratio,
        }
        for field_name, field_value in numeric_values.items():
            if not isfinite(field_value):
                raise ValueError(f"{field_name} must be finite")
        if self.spread_bps < 0.0:
            raise ValueError("spread_bps must be non-negative")
        if self.volume_ratio < 0.0:
            raise ValueError("volume_ratio must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "continuation_probability": self.continuation_probability,
            "reversal_probability": self.reversal_probability,
            "trap_probability": self.trap_probability,
            "regime_confidence": self.regime_confidence,
            "feature_readiness": self.feature_readiness,
            "liquidity_score": self.liquidity_score,
            "option_data_quality_score": self.option_data_quality_score,
            "realized_volatility_ratio": self.realized_volatility_ratio,
            "spread_bps": self.spread_bps,
            "volume_ratio": self.volume_ratio,
            "model_calibration": None if self.model_calibration is None else self.model_calibration.to_dict(),
            "regime_label": self.regime_label,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """Interpretable component decomposition for decision confidence."""

    directional_margin_component: float
    model_agreement_component: float
    probability_sharpness_component: float
    regime_alignment_component: float
    calibration_component: float
    liquidity_quality_component: float
    options_quality_component: float
    uncertainty_penalty_component: float
    raw_confidence_score: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "directional_margin_component": self.directional_margin_component,
            "model_agreement_component": self.model_agreement_component,
            "probability_sharpness_component": self.probability_sharpness_component,
            "regime_alignment_component": self.regime_alignment_component,
            "calibration_component": self.calibration_component,
            "liquidity_quality_component": self.liquidity_quality_component,
            "options_quality_component": self.options_quality_component,
            "uncertainty_penalty_component": self.uncertainty_penalty_component,
            "raw_confidence_score": self.raw_confidence_score,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """Final decision confidence output."""

    underlying_symbol: str
    timestamp: datetime
    confidence: float
    directional_bias: str
    actionability_score: float
    features: dict[str, float]
    breakdown: ConfidenceBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "confidence": self.confidence,
            "directional_bias": self.directional_bias,
            "actionability_score": self.actionability_score,
            "features": dict(self.features),
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceEngineState:
    """Serializable engine state for replay persistence."""

    processed_observations: int
    last_underlying_symbol: str | None
    last_timestamp: datetime | None
    last_confidence: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "processed_observations": self.processed_observations,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_timestamp": _ensure_utc(self.last_timestamp).isoformat() if self.last_timestamp else None,
            "last_confidence": self.last_confidence,
        }


class ConfidenceEngine:
    """Deterministic decision confidence engine for fused model outputs."""

    def __init__(self, *, config: ConfidenceEngineConfig | None = None) -> None:
        self._config = config or ConfidenceEngineConfig()
        self._processed_observations = 0
        self._last_underlying_symbol: str | None = None
        self._last_timestamp: datetime | None = None
        self._last_score: ConfidenceScore | None = None

    @property
    def state(self) -> ConfidenceEngineState:
        return ConfidenceEngineState(
            processed_observations=self._processed_observations,
            last_underlying_symbol=self._last_underlying_symbol,
            last_timestamp=self._last_timestamp,
            last_confidence=None if self._last_score is None else self._last_score.confidence,
        )

    def reset(self) -> None:
        self._processed_observations = 0
        self._last_underlying_symbol = None
        self._last_timestamp = None
        self._last_score = None

    def score(
        self,
        *,
        observation: ConfidenceObservation,
        valuation_timestamp: datetime | None = None,
    ) -> ConfidenceScore:
        self._validate_observation(observation=observation, valuation_timestamp=valuation_timestamp)
        normalized_timestamp = _ensure_utc(observation.timestamp)
        features = self._extract_features(observation=observation)
        breakdown = self._compute_breakdown(observation=observation, features=features)
        confidence = self._bounded_confidence(breakdown.raw_confidence_score)
        actionability_score = self._compute_actionability(features=features, confidence=confidence)
        directional_bias = self._directional_bias(observation=observation)
        score = ConfidenceScore(
            underlying_symbol=observation.underlying_symbol,
            timestamp=normalized_timestamp,
            confidence=confidence,
            directional_bias=directional_bias,
            actionability_score=actionability_score,
            features=features,
            breakdown=breakdown,
            metadata={
                "processed_observations": self._processed_observations + 1,
                "regime_label": observation.regime_label,
                "regime_confidence": observation.regime_confidence,
                "calibration_supplied": observation.model_calibration is not None,
            },
        )
        self._processed_observations += 1
        self._last_underlying_symbol = observation.underlying_symbol
        self._last_timestamp = normalized_timestamp
        self._last_score = score
        return score

    def get_last_score(self) -> ConfidenceScore:
        if self._last_score is None:
            raise ConfidenceEngineComputationError("no confidence score is available")
        return self._last_score

    def _validate_observation(self, *, observation: ConfidenceObservation, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(observation.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise ConfidenceEngineInputError("observation timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise ConfidenceEngineInputError("observation is stale relative to valuation_timestamp")
        if self._last_underlying_symbol is not None and observation.underlying_symbol != self._last_underlying_symbol:
            raise ConfidenceEngineInputError("engine instance supports only one underlying_symbol per stateful stream")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise ConfidenceEngineInputError("observation timestamp must be non-decreasing for replay determinism")

    def _extract_features(self, *, observation: ConfidenceObservation) -> dict[str, float]:
        continuation_margin = max(observation.continuation_probability - observation.reversal_probability, 0.0)
        reversal_margin = max(observation.reversal_probability - observation.continuation_probability, 0.0)
        directional_margin = fabs(observation.continuation_probability - observation.reversal_probability)
        model_agreement = _clip01(1.0 - fabs((observation.continuation_probability + observation.reversal_probability) - 1.0))
        probability_sharpness = _clip01(
            0.55 * directional_margin
            + 0.25 * fabs(observation.continuation_probability - 0.5) * 2.0
            + 0.20 * fabs(observation.reversal_probability - 0.5) * 2.0
        )
        regime_alignment = self._regime_alignment_score(observation=observation)
        calibration_strength = self._calibration_strength(observation.model_calibration)
        liquidity_quality = _clip01(
            0.50 * observation.liquidity_score
            + 0.25 * min(observation.volume_ratio, 2.0) / 2.0
            + 0.25 * max(0.0, 1.0 - min(observation.spread_bps, 25.0) / 25.0)
        )
        options_quality = _clip01(
            0.60 * observation.option_data_quality_score
            + 0.25 * observation.feature_readiness
            + 0.15 * max(0.0, 1.0 - min(fabs(observation.realized_volatility_ratio - 1.0), 1.0))
        )
        uncertainty_penalty = _clip01(
            0.40 * observation.trap_probability
            + 0.20 * max(0.0, 0.55 - directional_margin) / 0.55
            + 0.20 * max(0.0, 0.50 - observation.feature_readiness) / 0.50
            + 0.20 * max(0.0, 0.40 - observation.liquidity_score) / 0.40
        )
        features = {
            "continuation_margin": continuation_margin,
            "reversal_margin": reversal_margin,
            "directional_margin": directional_margin,
            "model_agreement": model_agreement,
            "probability_sharpness": probability_sharpness,
            "regime_alignment": regime_alignment,
            "calibration_strength": calibration_strength,
            "liquidity_quality": liquidity_quality,
            "options_quality": options_quality,
            "uncertainty_penalty": uncertainty_penalty,
        }
        invalid_names = [name for name, value in features.items() if not isfinite(value)]
        if invalid_names:
            raise ConfidenceEngineComputationError(
                "non-finite confidence features encountered: " + ", ".join(sorted(invalid_names))
            )
        return features

    def _compute_breakdown(
        self,
        *,
        observation: ConfidenceObservation,
        features: Mapping[str, float],
    ) -> ConfidenceBreakdown:
        directional_margin_component = self._config.directional_margin_weight * features["directional_margin"]
        model_agreement_component = self._config.model_agreement_weight * features["model_agreement"]
        probability_sharpness_component = self._config.probability_sharpness_weight * features["probability_sharpness"]
        regime_alignment_component = self._config.regime_alignment_weight * features["regime_alignment"]
        calibration_component = self._config.calibration_weight * features["calibration_strength"]
        liquidity_quality_component = self._config.liquidity_quality_weight * features["liquidity_quality"]
        options_quality_component = self._config.options_quality_weight * features["options_quality"]
        uncertainty_penalty_component = self._config.uncertainty_penalty_weight * features["uncertainty_penalty"]
        raw_confidence_score = (
            0.02
            + directional_margin_component
            + model_agreement_component
            + probability_sharpness_component
            + regime_alignment_component
            + calibration_component
            + liquidity_quality_component
            + options_quality_component
            - uncertainty_penalty_component
        )
        raw_confidence_score = _clip01(raw_confidence_score)
        return ConfidenceBreakdown(
            directional_margin_component=directional_margin_component,
            model_agreement_component=model_agreement_component,
            probability_sharpness_component=probability_sharpness_component,
            regime_alignment_component=regime_alignment_component,
            calibration_component=calibration_component,
            liquidity_quality_component=liquidity_quality_component,
            options_quality_component=options_quality_component,
            uncertainty_penalty_component=uncertainty_penalty_component,
            raw_confidence_score=raw_confidence_score,
        )

    def _compute_actionability(self, *, features: Mapping[str, float], confidence: float) -> float:
        actionability = (
            0.45 * confidence
            + 0.20 * features["liquidity_quality"]
            + 0.15 * features["options_quality"]
            + 0.10 * features["directional_margin"]
            + 0.10 * features["model_agreement"]
        )
        return _clip01(actionability)

    def _directional_bias(self, *, observation: ConfidenceObservation) -> str:
        if observation.continuation_probability > observation.reversal_probability:
            return "CONTINUATION"
        if observation.reversal_probability > observation.continuation_probability:
            return "REVERSAL"
        return "NEUTRAL"

    def _regime_alignment_score(self, *, observation: ConfidenceObservation) -> float:
        if observation.regime_label is None:
            return observation.regime_confidence
        regime_label = observation.regime_label.upper()
        base = observation.regime_confidence
        if "TREND" in regime_label:
            base += 0.08
        if "TRANSITION" in regime_label:
            base -= 0.12
        if "HIGH_VOL" in regime_label:
            base -= 0.06
        if "RANGE" in regime_label:
            base -= 0.04
        if base < 0.0:
            base = 0.0
        return _clip01(base)

    @staticmethod
    def _calibration_strength(calibration: CalibrationQuality | None) -> float:
        if calibration is None:
            return 0.50
        ece_quality = 1.0 - calibration.expected_calibration_error
        brier_quality = _clip01((calibration.brier_skill + 1.0) / 2.0)
        return _clip01(0.40 * ece_quality + 0.35 * brier_quality + 0.25 * calibration.stability_score)

    def _bounded_confidence(self, confidence: float) -> float:
        if confidence < self._config.minimum_confidence:
            return self._config.minimum_confidence
        if confidence > self._config.maximum_confidence:
            return self._config.maximum_confidence
        return confidence



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _clip01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


__all__ = [
    "CalibrationQuality",
    "ConfidenceBreakdown",
    "ConfidenceEngine",
    "ConfidenceEngineComputationError",
    "ConfidenceEngineConfig",
    "ConfidenceEngineError",
    "ConfidenceEngineInputError",
    "ConfidenceEngineState",
    "ConfidenceObservation",
    "ConfidenceScore",
]
