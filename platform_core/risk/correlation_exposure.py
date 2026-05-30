# FILE: platform_core/risk/correlation_exposure.py
"""
platform_core.risk.correlation_exposure
═══════════════════════════════════════════════════════════════════════
Production-grade correlation exposure engine specialized for Indian index
futures and options workflows.

The engine evaluates finalized position groups using causal pairwise and
cluster-level correlation inputs to quantify diversification quality,
concentration burden, and marginal exposure admission risk. It preserves
replay safety, produces JSON-serializable outputs, and is designed for use by
portfolio heat, volatility sizing, and pre-trade risk-gating components.

Core controls:
- Correlation-weighted gross and net exposure measurement.
- Cluster concentration detection.
- Incremental proposed-trade admission analysis.
- Diversification ratio estimation from correlation structure.
- Deterministic exposure scaling and block decisions.

This module does not price portfolios or submit orders. It emits structured
risk decisions for downstream orchestration and execution layers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import isfinite
from math import sqrt


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class CorrelationExposureError(RuntimeError):
    """Base exception for correlation exposure failures."""


class CorrelationExposureInputError(CorrelationExposureError):
    """Raised when correlation exposure inputs are invalid or causally unsafe."""


class CorrelationExposureComputationError(CorrelationExposureError):
    """Raised when correlation exposure computation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class CorrelationExposureConfig:
    """Configuration for deterministic correlation-exposure control."""

    warning_effective_correlation_fraction: float = 0.55
    block_effective_correlation_fraction: float = 0.80
    warning_cluster_share_fraction: float = 0.45
    block_cluster_share_fraction: float = 0.65
    warning_incremental_correlation_fraction: float = 0.70
    block_incremental_correlation_fraction: float = 0.90
    warning_diversification_ratio: float = 1.15
    block_diversification_ratio: float = 1.05
    max_scaling_reduction_fraction: float = 0.80
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        bounded_fields = {
            "warning_effective_correlation_fraction": self.warning_effective_correlation_fraction,
            "block_effective_correlation_fraction": self.block_effective_correlation_fraction,
            "warning_cluster_share_fraction": self.warning_cluster_share_fraction,
            "block_cluster_share_fraction": self.block_cluster_share_fraction,
            "warning_incremental_correlation_fraction": self.warning_incremental_correlation_fraction,
            "block_incremental_correlation_fraction": self.block_incremental_correlation_fraction,
            "max_scaling_reduction_fraction": self.max_scaling_reduction_fraction,
        }
        for field_name, field_value in bounded_fields.items():
            if not 0.0 < field_value <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")
        positive_fields = {
            "warning_diversification_ratio": self.warning_diversification_ratio,
            "block_diversification_ratio": self.block_diversification_ratio,
        }
        for field_name, field_value in positive_fields.items():
            if field_value <= 0.0:
                raise ValueError(f"{field_name} must be positive")
        if self.warning_effective_correlation_fraction > self.block_effective_correlation_fraction:
            raise ValueError("warning_effective_correlation_fraction must be less than or equal to block_effective_correlation_fraction")
        if self.warning_cluster_share_fraction > self.block_cluster_share_fraction:
            raise ValueError("warning_cluster_share_fraction must be less than or equal to block_cluster_share_fraction")
        if self.warning_incremental_correlation_fraction > self.block_incremental_correlation_fraction:
            raise ValueError("warning_incremental_correlation_fraction must be less than or equal to block_incremental_correlation_fraction")
        if self.warning_diversification_ratio < self.block_diversification_ratio:
            raise ValueError("warning_diversification_ratio must be greater than or equal to block_diversification_ratio")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class PositionCorrelationNode:
    """Serializable node representing a portfolio exposure bucket."""

    node_id: str
    symbol: str
    cluster_id: str
    signed_exposure: float
    realized_volatility_fraction: float

    def __post_init__(self) -> None:
        string_fields = {
            "node_id": self.node_id,
            "symbol": self.symbol,
            "cluster_id": self.cluster_id,
        }
        for field_name, field_value in string_fields.items():
            if not field_value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not isfinite(self.signed_exposure):
            raise ValueError("signed_exposure must be finite")
        if self.realized_volatility_fraction <= 0.0 or not isfinite(self.realized_volatility_fraction):
            raise ValueError("realized_volatility_fraction must be positive and finite")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "node_id": self.node_id,
            "symbol": self.symbol,
            "cluster_id": self.cluster_id,
            "signed_exposure": self.signed_exposure,
            "realized_volatility_fraction": self.realized_volatility_fraction,
        }


@dataclass(frozen=True, slots=True)
class CorrelationPair:
    """Serializable pairwise correlation observation between two nodes."""

    left_node_id: str
    right_node_id: str
    correlation_fraction: float

    def __post_init__(self) -> None:
        if not self.left_node_id.strip() or not self.right_node_id.strip():
            raise ValueError("pair node identifiers must be non-empty")
        if self.left_node_id == self.right_node_id:
            raise ValueError("pair node identifiers must be distinct")
        if not -1.0 <= self.correlation_fraction <= 1.0:
            raise ValueError("correlation_fraction must be between -1 and 1")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "left_node_id": self.left_node_id,
            "right_node_id": self.right_node_id,
            "correlation_fraction": self.correlation_fraction,
        }


@dataclass(frozen=True, slots=True)
class ProposedExposure:
    """Serializable proposed incremental exposure for admission control."""

    symbol: str
    cluster_id: str
    signed_exposure: float
    realized_volatility_fraction: float
    correlation_to_portfolio_fraction: float

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.cluster_id.strip():
            raise ValueError("symbol and cluster_id must be non-empty")
        if not isfinite(self.signed_exposure):
            raise ValueError("signed_exposure must be finite")
        if self.realized_volatility_fraction <= 0.0 or not isfinite(self.realized_volatility_fraction):
            raise ValueError("realized_volatility_fraction must be positive and finite")
        if not -1.0 <= self.correlation_to_portfolio_fraction <= 1.0:
            raise ValueError("correlation_to_portfolio_fraction must be between -1 and 1")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "symbol": self.symbol,
            "cluster_id": self.cluster_id,
            "signed_exposure": self.signed_exposure,
            "realized_volatility_fraction": self.realized_volatility_fraction,
            "correlation_to_portfolio_fraction": self.correlation_to_portfolio_fraction,
        }


@dataclass(frozen=True, slots=True)
class CorrelationExposureSnapshot:
    """Serializable finalized portfolio correlation snapshot."""

    timestamp: datetime
    nodes: tuple[PositionCorrelationNode, ...]
    pairs: tuple[CorrelationPair, ...]
    proposed_exposure: ProposedExposure | None = None

    def __post_init__(self) -> None:
        if len(self.nodes) == 0:
            raise ValueError("nodes must be non-empty")
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("node identifiers must be unique")
        for pair in self.pairs:
            if pair.left_node_id not in node_ids or pair.right_node_id not in node_ids:
                raise ValueError("pair references unknown node identifiers")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "nodes": [node.to_dict() for node in self.nodes],
            "pairs": [pair.to_dict() for pair in self.pairs],
            "proposed_exposure": self.proposed_exposure.to_dict() if self.proposed_exposure else None,
        }


@dataclass(frozen=True, slots=True)
class CorrelationExposureBreakdown:
    """Interpretable decomposition of portfolio correlation risk."""

    gross_exposure: float
    net_exposure: float
    effective_correlation_fraction: float
    largest_cluster_share_fraction: float
    diversification_ratio: float
    proposed_incremental_correlation_fraction: float | None
    proposed_cluster_share_fraction: float | None
    warning_triggered: bool
    block_triggered: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "effective_correlation_fraction": self.effective_correlation_fraction,
            "largest_cluster_share_fraction": self.largest_cluster_share_fraction,
            "diversification_ratio": self.diversification_ratio,
            "proposed_incremental_correlation_fraction": self.proposed_incremental_correlation_fraction,
            "proposed_cluster_share_fraction": self.proposed_cluster_share_fraction,
            "warning_triggered": self.warning_triggered,
            "block_triggered": self.block_triggered,
        }


@dataclass(frozen=True, slots=True)
class CorrelationExposureDecision:
    """Serializable correlation-exposure decision for downstream risk gating."""

    timestamp: datetime
    level: str
    scaling_multiplier: float
    allow_incremental_exposure: bool
    recommended_reduction_fraction: float
    dominant_cluster_id: str
    reason: str
    breakdown: CorrelationExposureBreakdown
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timestamp": _ensure_utc(self.timestamp).isoformat(),
            "level": self.level,
            "scaling_multiplier": self.scaling_multiplier,
            "allow_incremental_exposure": self.allow_incremental_exposure,
            "recommended_reduction_fraction": self.recommended_reduction_fraction,
            "dominant_cluster_id": self.dominant_cluster_id,
            "reason": self.reason,
            "breakdown": self.breakdown.to_dict(),
            "metadata": self.metadata,
        }


class CorrelationExposureEngine:
    """Deterministic correlation-exposure engine."""

    def __init__(self, *, config: CorrelationExposureConfig | None = None) -> None:
        self._config = config or CorrelationExposureConfig()
        self._last_timestamp: datetime | None = None
        self._processed_snapshots = 0
        self._last_decision: CorrelationExposureDecision | None = None

    def reset(self) -> None:
        self._last_timestamp = None
        self._processed_snapshots = 0
        self._last_decision = None

    def evaluate(
        self,
        *,
        snapshot: CorrelationExposureSnapshot,
        valuation_timestamp: datetime | None = None,
    ) -> CorrelationExposureDecision:
        timestamp = _ensure_utc(snapshot.timestamp)
        self._validate_snapshot(snapshot=snapshot, valuation_timestamp=valuation_timestamp)

        node_map = {node.node_id: node for node in snapshot.nodes}
        gross_exposure = sum(abs(node.signed_exposure) for node in snapshot.nodes)
        net_exposure = sum(node.signed_exposure for node in snapshot.nodes)
        if gross_exposure <= 0.0:
            raise CorrelationExposureComputationError("gross exposure must be positive")

        covariance_proxy = 0.0
        standalone_risk_sum = 0.0
        for node in snapshot.nodes:
            weight = abs(node.signed_exposure)
            node_variance = (weight * node.realized_volatility_fraction) ** 2
            covariance_proxy += node_variance
            standalone_risk_sum += weight * node.realized_volatility_fraction
        for pair in snapshot.pairs:
            left_node = node_map[pair.left_node_id]
            right_node = node_map[pair.right_node_id]
            left_risk = abs(left_node.signed_exposure) * left_node.realized_volatility_fraction
            right_risk = abs(right_node.signed_exposure) * right_node.realized_volatility_fraction
            covariance_proxy += 2.0 * pair.correlation_fraction * left_risk * right_risk
        covariance_proxy = max(covariance_proxy, 0.0)
        portfolio_risk_proxy = sqrt(covariance_proxy)
        diversification_ratio = (
            standalone_risk_sum / portfolio_risk_proxy if portfolio_risk_proxy > 0.0 else 1.0
        )
        effective_correlation_fraction = _clamp(
            1.0 - ((diversification_ratio - 1.0) / max(diversification_ratio, 1.0)),
            lower=0.0,
            upper=1.0,
        )

        cluster_exposure_map: dict[str, float] = {}
        for node in snapshot.nodes:
            cluster_exposure_map[node.cluster_id] = cluster_exposure_map.get(node.cluster_id, 0.0) + abs(node.signed_exposure)
        dominant_cluster_id, largest_cluster_exposure = max(cluster_exposure_map.items(), key=lambda item: item[1])
        largest_cluster_share_fraction = largest_cluster_exposure / gross_exposure

        proposed_incremental_correlation_fraction: float | None = None
        proposed_cluster_share_fraction: float | None = None
        if snapshot.proposed_exposure is not None:
            proposed_incremental_correlation_fraction = abs(snapshot.proposed_exposure.correlation_to_portfolio_fraction)
            projected_cluster_exposure = cluster_exposure_map.get(snapshot.proposed_exposure.cluster_id, 0.0) + abs(snapshot.proposed_exposure.signed_exposure)
            projected_gross_exposure = gross_exposure + abs(snapshot.proposed_exposure.signed_exposure)
            proposed_cluster_share_fraction = projected_cluster_exposure / projected_gross_exposure

        warning_triggered = (
            effective_correlation_fraction >= self._config.warning_effective_correlation_fraction
            or largest_cluster_share_fraction >= self._config.warning_cluster_share_fraction
            or diversification_ratio <= self._config.warning_diversification_ratio
            or (
                proposed_incremental_correlation_fraction is not None
                and proposed_incremental_correlation_fraction >= self._config.warning_incremental_correlation_fraction
            )
        )
        block_triggered = (
            effective_correlation_fraction >= self._config.block_effective_correlation_fraction
            or largest_cluster_share_fraction >= self._config.block_cluster_share_fraction
            or diversification_ratio <= self._config.block_diversification_ratio
            or (
                proposed_incremental_correlation_fraction is not None
                and proposed_incremental_correlation_fraction >= self._config.block_incremental_correlation_fraction
            )
            or (
                proposed_cluster_share_fraction is not None
                and proposed_cluster_share_fraction >= self._config.block_cluster_share_fraction
            )
        )

        breakdown = CorrelationExposureBreakdown(
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            effective_correlation_fraction=effective_correlation_fraction,
            largest_cluster_share_fraction=largest_cluster_share_fraction,
            diversification_ratio=diversification_ratio,
            proposed_incremental_correlation_fraction=proposed_incremental_correlation_fraction,
            proposed_cluster_share_fraction=proposed_cluster_share_fraction,
            warning_triggered=warning_triggered,
            block_triggered=block_triggered,
        )
        decision = self._decide(
            timestamp=timestamp,
            dominant_cluster_id=dominant_cluster_id,
            breakdown=breakdown,
            proposed_exposure=snapshot.proposed_exposure,
        )
        self._last_timestamp = timestamp
        self._processed_snapshots += 1
        self._last_decision = decision
        return decision

    def get_last_decision(self) -> CorrelationExposureDecision:
        if self._last_decision is None:
            raise CorrelationExposureComputationError("no correlation exposure decision is available")
        return self._last_decision

    def _validate_snapshot(self, *, snapshot: CorrelationExposureSnapshot, valuation_timestamp: datetime | None) -> None:
        normalized_timestamp = _ensure_utc(snapshot.timestamp)
        if valuation_timestamp is not None:
            normalized_valuation_timestamp = _ensure_utc(valuation_timestamp)
            if normalized_timestamp > normalized_valuation_timestamp:
                raise CorrelationExposureInputError("snapshot timestamp cannot be later than valuation_timestamp")
            if self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - normalized_timestamp).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    raise CorrelationExposureInputError("snapshot is stale relative to valuation_timestamp")
        if self._last_timestamp is not None and normalized_timestamp < self._last_timestamp:
            raise CorrelationExposureInputError("snapshot timestamp must be non-decreasing for replay determinism")

    def _decide(
        self,
        *,
        timestamp: datetime,
        dominant_cluster_id: str,
        breakdown: CorrelationExposureBreakdown,
        proposed_exposure: ProposedExposure | None,
    ) -> CorrelationExposureDecision:
        if breakdown.block_triggered:
            return self._build_decision(
                timestamp=timestamp,
                level="BLOCKED",
                scaling_multiplier=1.0 - self._config.max_scaling_reduction_fraction,
                allow_incremental_exposure=False,
                recommended_reduction_fraction=self._config.max_scaling_reduction_fraction,
                dominant_cluster_id=dominant_cluster_id,
                reason="CORRELATION_BLOCK_TRIGGER",
                breakdown=breakdown,
                proposed_exposure=proposed_exposure,
            )

        if breakdown.warning_triggered:
            scaling_multiplier = 1.0 - min(
                self._config.max_scaling_reduction_fraction,
                max(
                    0.20,
                    (breakdown.effective_correlation_fraction - self._config.warning_effective_correlation_fraction) * 1.5,
                    (breakdown.largest_cluster_share_fraction - self._config.warning_cluster_share_fraction) * 1.5,
                ),
            )
            scaling_multiplier = _clamp(scaling_multiplier, lower=0.20, upper=1.0)
            return self._build_decision(
                timestamp=timestamp,
                level="REDUCED",
                scaling_multiplier=scaling_multiplier,
                allow_incremental_exposure=proposed_exposure is None,
                recommended_reduction_fraction=1.0 - scaling_multiplier,
                dominant_cluster_id=dominant_cluster_id,
                reason="CORRELATION_WARNING_TRIGGER",
                breakdown=breakdown,
                proposed_exposure=proposed_exposure,
            )

        return self._build_decision(
            timestamp=timestamp,
            level="NORMAL",
            scaling_multiplier=1.0,
            allow_incremental_exposure=True,
            recommended_reduction_fraction=0.0,
            dominant_cluster_id=dominant_cluster_id,
            reason="CORRELATION_ACCEPTABLE",
            breakdown=breakdown,
            proposed_exposure=proposed_exposure,
        )

    def _build_decision(
        self,
        *,
        timestamp: datetime,
        level: str,
        scaling_multiplier: float,
        allow_incremental_exposure: bool,
        recommended_reduction_fraction: float,
        dominant_cluster_id: str,
        reason: str,
        breakdown: CorrelationExposureBreakdown,
        proposed_exposure: ProposedExposure | None,
    ) -> CorrelationExposureDecision:
        return CorrelationExposureDecision(
            timestamp=timestamp,
            level=level,
            scaling_multiplier=scaling_multiplier,
            allow_incremental_exposure=allow_incremental_exposure,
            recommended_reduction_fraction=recommended_reduction_fraction,
            dominant_cluster_id=dominant_cluster_id,
            reason=reason,
            breakdown=breakdown,
            metadata={
                "processed_snapshots": self._processed_snapshots + 1,
                "proposed_symbol": proposed_exposure.symbol if proposed_exposure is not None else None,
                "proposed_cluster_id": proposed_exposure.cluster_id if proposed_exposure is not None else None,
            },
        )



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



def _clamp(value: float, *, lower: float, upper: float) -> float:
    if lower > upper:
        raise CorrelationExposureComputationError("invalid clamp bounds")
    return max(lower, min(value, upper))


__all__ = [
    "CorrelationExposureComputationError",
    "CorrelationExposureConfig",
    "CorrelationExposureDecision",
    "CorrelationExposureBreakdown",
    "CorrelationExposureEngine",
    "CorrelationExposureError",
    "CorrelationExposureInputError",
    "CorrelationExposureSnapshot",
    "CorrelationPair",
    "PositionCorrelationNode",
    "ProposedExposure",
]
