# FILE: platform_core/options/premium_behavior.py
"""
platform_core.options.premium_behavior
═══════════════════════════════════════════════════════════════════════
Production-grade premium behavior analytics engine for Indian index options.

This module computes deterministic, replay-safe premium-behavior diagnostics
from causally observed option-chain snapshots. It focuses on option premium
formation under time decay, implied-volatility shifts, intrinsic/extrinsic
composition, moneyness buckets, and open-interest-aware premium concentration.
The engine is intended for downstream microstructure, regime, execution, and
model-feature pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timezone
from math import fabs
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class PremiumBehaviorError(RuntimeError):
    """Base exception for premium behavior analytics failures."""


class PremiumBehaviorInputError(PremiumBehaviorError):
    """Raised when premium behavior inputs are invalid."""


class PremiumBehaviorComputationError(PremiumBehaviorError):
    """Raised when premium behavior analytics cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class PremiumBehaviorConfig:
    """Configuration for public-data premium behavior analytics."""

    minimum_time_to_expiry_years: float = 1.0e-8
    stale_snapshot_max_age_seconds: float | None = None
    itm_moneyness_threshold_pct: float = 1.0
    otm_moneyness_threshold_pct: float = 1.0
    minimum_premium: float = 0.0
    minimum_abs_theta_for_decay_signal: float = 1.0e-9
    price_pressure_top_n: int = 5

    def __post_init__(self) -> None:
        if self.minimum_time_to_expiry_years <= 0.0:
            raise ValueError("minimum_time_to_expiry_years must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")
        if self.itm_moneyness_threshold_pct < 0.0:
            raise ValueError("itm_moneyness_threshold_pct must be non-negative")
        if self.otm_moneyness_threshold_pct < 0.0:
            raise ValueError("otm_moneyness_threshold_pct must be non-negative")
        if self.minimum_premium < 0.0:
            raise ValueError("minimum_premium must be non-negative")
        if self.minimum_abs_theta_for_decay_signal <= 0.0:
            raise ValueError("minimum_abs_theta_for_decay_signal must be positive")
        if self.price_pressure_top_n <= 0:
            raise ValueError("price_pressure_top_n must be positive")


@dataclass(frozen=True, slots=True)
class PremiumBehaviorPoint:
    """Single option contract snapshot used for premium analytics."""

    underlying_symbol: str
    expiry_date: date
    option_symbol: str
    option_type: str
    strike_price: float
    spot_price: float
    lot_size: int
    last_traded_price: float
    open_interest: int
    change_in_open_interest: int
    volume: int
    time_to_expiry_years: float
    implied_volatility: float
    delta: float
    gamma: float
    theta: float
    vega: float
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        option_type_normalized = self.option_type.upper()
        if option_type_normalized not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if not self.option_symbol.strip():
            raise ValueError("option_symbol must be non-empty")
        if self.strike_price <= 0.0:
            raise ValueError("strike_price must be positive")
        if self.spot_price <= 0.0:
            raise ValueError("spot_price must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.last_traded_price < 0.0:
            raise ValueError("last_traded_price must be non-negative")
        if self.open_interest < 0:
            raise ValueError("open_interest must be non-negative")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.time_to_expiry_years < 0.0:
            raise ValueError("time_to_expiry_years must be non-negative")
        if self.implied_volatility < 0.0:
            raise ValueError("implied_volatility must be non-negative")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "option_symbol": self.option_symbol,
            "option_type": self.normalized_option_type,
            "strike_price": self.strike_price,
            "spot_price": self.spot_price,
            "lot_size": self.lot_size,
            "last_traded_price": self.last_traded_price,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "time_to_expiry_years": self.time_to_expiry_years,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True, slots=True)
class PremiumBehaviorLevel:
    """Strike-level premium diagnostics."""

    strike_price: float
    option_type: str
    open_interest: int
    change_in_open_interest: int
    volume: int
    last_traded_price: float
    intrinsic_value: float
    extrinsic_value: float
    intrinsic_share: float
    extrinsic_share: float
    moneyness_pct: float
    moneyness_bucket: str
    premium_notional: float
    premium_oi_notional: float
    premium_volume_notional: float
    iv_sensitivity_per_1vol_point: float
    time_decay_per_day: float
    elasticity_to_spot_pct: float
    convexity_to_spot_pct: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "last_traded_price": self.last_traded_price,
            "intrinsic_value": self.intrinsic_value,
            "extrinsic_value": self.extrinsic_value,
            "intrinsic_share": self.intrinsic_share,
            "extrinsic_share": self.extrinsic_share,
            "moneyness_pct": self.moneyness_pct,
            "moneyness_bucket": self.moneyness_bucket,
            "premium_notional": self.premium_notional,
            "premium_oi_notional": self.premium_oi_notional,
            "premium_volume_notional": self.premium_volume_notional,
            "iv_sensitivity_per_1vol_point": self.iv_sensitivity_per_1vol_point,
            "time_decay_per_day": self.time_decay_per_day,
            "elasticity_to_spot_pct": self.elasticity_to_spot_pct,
            "convexity_to_spot_pct": self.convexity_to_spot_pct,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class PremiumPressureLevel:
    """Premium concentration or decay-pressure ranking level."""

    strike_price: float
    option_type: str
    metric_value: float
    normalized_weight: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "metric_value": self.metric_value,
            "normalized_weight": self.normalized_weight,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class PremiumBehaviorSnapshot:
    """Aggregate premium behavior analytics for one underlying-expiry snapshot."""

    underlying_symbol: str
    expiry_date: date
    valuation_timestamp: datetime | None
    spot_price: float
    level_metrics: tuple[PremiumBehaviorLevel, ...]
    total_premium_notional: float
    total_premium_oi_notional: float
    total_premium_volume_notional: float
    total_intrinsic_value: float
    total_extrinsic_value: float
    call_extrinsic_share: float
    put_extrinsic_share: float
    atm_extrinsic_share: float
    itm_extrinsic_share: float
    otm_extrinsic_share: float
    net_time_decay_per_day: float
    net_iv_sensitivity_per_1vol_point: float
    premium_pressure_levels: tuple[PremiumPressureLevel, ...]
    decay_pressure_levels: tuple[PremiumPressureLevel, ...]
    dominant_premium_strike: float | None
    dominant_decay_strike: float | None
    premium_regime: str
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "spot_price": self.spot_price,
            "level_metrics": [level.to_dict() for level in self.level_metrics],
            "total_premium_notional": self.total_premium_notional,
            "total_premium_oi_notional": self.total_premium_oi_notional,
            "total_premium_volume_notional": self.total_premium_volume_notional,
            "total_intrinsic_value": self.total_intrinsic_value,
            "total_extrinsic_value": self.total_extrinsic_value,
            "call_extrinsic_share": self.call_extrinsic_share,
            "put_extrinsic_share": self.put_extrinsic_share,
            "atm_extrinsic_share": self.atm_extrinsic_share,
            "itm_extrinsic_share": self.itm_extrinsic_share,
            "otm_extrinsic_share": self.otm_extrinsic_share,
            "net_time_decay_per_day": self.net_time_decay_per_day,
            "net_iv_sensitivity_per_1vol_point": self.net_iv_sensitivity_per_1vol_point,
            "premium_pressure_levels": [level.to_dict() for level in self.premium_pressure_levels],
            "decay_pressure_levels": [level.to_dict() for level in self.decay_pressure_levels],
            "dominant_premium_strike": self.dominant_premium_strike,
            "dominant_decay_strike": self.dominant_decay_strike,
            "premium_regime": self.premium_regime,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class PremiumBehaviorState:
    """Serializable engine state."""

    snapshot_count: int
    last_underlying_symbol: str | None
    last_expiry_date: date | None
    last_valuation_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "snapshot_count": self.snapshot_count,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_expiry_date": self.last_expiry_date.isoformat() if self.last_expiry_date else None,
            "last_valuation_timestamp": self.last_valuation_timestamp.isoformat() if self.last_valuation_timestamp else None,
        }


class PremiumBehaviorEngine:
    """Replay-safe premium behavior analytics engine."""

    def __init__(self, *, config: PremiumBehaviorConfig | None = None) -> None:
        self._config = config or PremiumBehaviorConfig()
        self._snapshots: dict[tuple[str, date], PremiumBehaviorSnapshot] = {}
        self._snapshot_count = 0
        self._last_underlying_symbol: str | None = None
        self._last_expiry_date: date | None = None
        self._last_valuation_timestamp: datetime | None = None

    @property
    def state(self) -> PremiumBehaviorState:
        return PremiumBehaviorState(
            snapshot_count=self._snapshot_count,
            last_underlying_symbol=self._last_underlying_symbol,
            last_expiry_date=self._last_expiry_date,
            last_valuation_timestamp=self._last_valuation_timestamp,
        )

    def reset(self) -> None:
        self._snapshots.clear()
        self._snapshot_count = 0
        self._last_underlying_symbol = None
        self._last_expiry_date = None
        self._last_valuation_timestamp = None

    def build_snapshot(
        self,
        *,
        points: list[PremiumBehaviorPoint],
        valuation_timestamp: datetime | None = None,
    ) -> PremiumBehaviorSnapshot:
        if not points:
            raise PremiumBehaviorInputError("points must not be empty")
        filtered_points = self._filter_points(points=points, valuation_timestamp=valuation_timestamp)
        if not filtered_points:
            raise PremiumBehaviorComputationError("no valid premium behavior points available after filtering")
        underlying_symbol = filtered_points[0].underlying_symbol
        expiry_date = filtered_points[0].expiry_date
        spot_price = filtered_points[0].spot_price
        for point in filtered_points:
            if point.underlying_symbol != underlying_symbol:
                raise PremiumBehaviorInputError("all points must share the same underlying_symbol")
            if point.expiry_date != expiry_date:
                raise PremiumBehaviorInputError("all points must share the same expiry_date")
        level_metrics = [self._build_level(point=point, reference_spot_price=spot_price) for point in filtered_points]
        level_metrics.sort(key=lambda level: (level.strike_price, level.option_type))
        call_levels = [level for level in level_metrics if level.option_type == "CALL"]
        put_levels = [level for level in level_metrics if level.option_type == "PUT"]
        atm_levels = [level for level in level_metrics if level.moneyness_bucket == "ATM"]
        itm_levels = [level for level in level_metrics if level.moneyness_bucket == "ITM"]
        otm_levels = [level for level in level_metrics if level.moneyness_bucket == "OTM"]
        valuation_ts = _ensure_utc(valuation_timestamp) if valuation_timestamp else self._infer_snapshot_timestamp(points=filtered_points)
        premium_pressure_levels = tuple(self._rank_pressure_levels(levels=level_metrics, metric="premium_oi_notional"))
        decay_pressure_levels = tuple(self._rank_pressure_levels(levels=level_metrics, metric="time_decay_per_day"))
        total_premium_notional = sum(level.premium_notional for level in level_metrics)
        total_premium_oi_notional = sum(level.premium_oi_notional for level in level_metrics)
        total_premium_volume_notional = sum(level.premium_volume_notional for level in level_metrics)
        total_intrinsic_value = sum(level.intrinsic_value for level in level_metrics)
        total_extrinsic_value = sum(level.extrinsic_value for level in level_metrics)
        snapshot = PremiumBehaviorSnapshot(
            underlying_symbol=underlying_symbol,
            expiry_date=expiry_date,
            valuation_timestamp=valuation_ts,
            spot_price=spot_price,
            level_metrics=tuple(level_metrics),
            total_premium_notional=total_premium_notional,
            total_premium_oi_notional=total_premium_oi_notional,
            total_premium_volume_notional=total_premium_volume_notional,
            total_intrinsic_value=total_intrinsic_value,
            total_extrinsic_value=total_extrinsic_value,
            call_extrinsic_share=self._safe_ratio(sum(level.extrinsic_value for level in call_levels), sum(level.last_traded_price for level in call_levels)),
            put_extrinsic_share=self._safe_ratio(sum(level.extrinsic_value for level in put_levels), sum(level.last_traded_price for level in put_levels)),
            atm_extrinsic_share=self._safe_ratio(sum(level.extrinsic_value for level in atm_levels), sum(level.last_traded_price for level in atm_levels)),
            itm_extrinsic_share=self._safe_ratio(sum(level.extrinsic_value for level in itm_levels), sum(level.last_traded_price for level in itm_levels)),
            otm_extrinsic_share=self._safe_ratio(sum(level.extrinsic_value for level in otm_levels), sum(level.last_traded_price for level in otm_levels)),
            net_time_decay_per_day=sum(level.time_decay_per_day for level in level_metrics),
            net_iv_sensitivity_per_1vol_point=sum(level.iv_sensitivity_per_1vol_point for level in level_metrics),
            premium_pressure_levels=premium_pressure_levels,
            decay_pressure_levels=decay_pressure_levels,
            dominant_premium_strike=None if not premium_pressure_levels else premium_pressure_levels[0].strike_price,
            dominant_decay_strike=None if not decay_pressure_levels else decay_pressure_levels[0].strike_price,
            premium_regime=self._premium_regime(total_intrinsic_value=total_intrinsic_value, total_extrinsic_value=total_extrinsic_value),
            metadata={
                "raw_point_count": len(points),
                "filtered_point_count": len(filtered_points),
                "minimum_premium": self._config.minimum_premium,
                "itm_moneyness_threshold_pct": self._config.itm_moneyness_threshold_pct,
                "otm_moneyness_threshold_pct": self._config.otm_moneyness_threshold_pct,
                "price_pressure_top_n": self._config.price_pressure_top_n,
            },
        )
        self._snapshots[(underlying_symbol, expiry_date)] = snapshot
        self._snapshot_count += 1
        self._last_underlying_symbol = underlying_symbol
        self._last_expiry_date = expiry_date
        self._last_valuation_timestamp = valuation_ts
        return snapshot

    def get_snapshot(self, *, underlying_symbol: str, expiry_date: date) -> PremiumBehaviorSnapshot:
        snapshot = self._snapshots.get((underlying_symbol, expiry_date))
        if snapshot is None:
            raise PremiumBehaviorComputationError("requested premium behavior snapshot is not available")
        return snapshot

    def _filter_points(
        self,
        *,
        points: list[PremiumBehaviorPoint],
        valuation_timestamp: datetime | None,
    ) -> list[PremiumBehaviorPoint]:
        filtered: list[PremiumBehaviorPoint] = []
        normalized_valuation_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else None
        for point in points:
            values = [
                point.strike_price,
                point.spot_price,
                float(point.lot_size),
                point.last_traded_price,
                float(point.open_interest),
                float(point.change_in_open_interest),
                float(point.volume),
                point.time_to_expiry_years,
                point.implied_volatility,
                point.delta,
                point.gamma,
                point.theta,
                point.vega,
            ]
            if not all(isfinite(value) for value in values):
                continue
            if point.time_to_expiry_years < self._config.minimum_time_to_expiry_years:
                continue
            if point.last_traded_price < self._config.minimum_premium:
                continue
            if point.observed_at is not None and normalized_valuation_timestamp is not None and self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - _ensure_utc(point.observed_at)).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    continue
            filtered.append(point)
        return filtered

    def _build_level(self, *, point: PremiumBehaviorPoint, reference_spot_price: float) -> PremiumBehaviorLevel:
        intrinsic_value = self._intrinsic_value(point=point)
        extrinsic_value = max(point.last_traded_price - intrinsic_value, 0.0)
        premium_notional = point.last_traded_price * point.lot_size
        premium_oi_notional = premium_notional * point.open_interest
        premium_volume_notional = premium_notional * point.volume
        iv_sensitivity_per_1vol_point = point.vega * point.lot_size * point.open_interest * 0.01
        time_decay_per_day = point.theta * point.lot_size * point.open_interest / 365.0
        elasticity_to_spot_pct = point.delta * point.spot_price / max(point.last_traded_price, 1.0e-12)
        convexity_to_spot_pct = point.gamma * point.spot_price * point.spot_price / max(point.last_traded_price, 1.0e-12)
        return PremiumBehaviorLevel(
            strike_price=point.strike_price,
            option_type=point.normalized_option_type,
            open_interest=point.open_interest,
            change_in_open_interest=point.change_in_open_interest,
            volume=point.volume,
            last_traded_price=point.last_traded_price,
            intrinsic_value=intrinsic_value,
            extrinsic_value=extrinsic_value,
            intrinsic_share=self._safe_ratio(intrinsic_value, point.last_traded_price),
            extrinsic_share=self._safe_ratio(extrinsic_value, point.last_traded_price),
            moneyness_pct=self._moneyness_pct(point=point),
            moneyness_bucket=self._moneyness_bucket(point=point),
            premium_notional=premium_notional,
            premium_oi_notional=premium_oi_notional,
            premium_volume_notional=premium_volume_notional,
            iv_sensitivity_per_1vol_point=iv_sensitivity_per_1vol_point,
            time_decay_per_day=time_decay_per_day,
            elasticity_to_spot_pct=elasticity_to_spot_pct,
            convexity_to_spot_pct=convexity_to_spot_pct,
            distance_from_spot_pct=((point.strike_price / reference_spot_price) - 1.0) * 100.0,
        )

    def _rank_pressure_levels(self, *, levels: list[PremiumBehaviorLevel], metric: str) -> list[PremiumPressureLevel]:
        if metric == "premium_oi_notional":
            ranked = sorted(levels, key=lambda level: level.premium_oi_notional, reverse=True)
            total = sum(level.premium_oi_notional for level in ranked[: self._config.price_pressure_top_n])
            return [
                PremiumPressureLevel(
                    strike_price=level.strike_price,
                    option_type=level.option_type,
                    metric_value=level.premium_oi_notional,
                    normalized_weight=0.0 if total <= 0.0 else level.premium_oi_notional / total,
                    distance_from_spot_pct=level.distance_from_spot_pct,
                )
                for level in ranked[: self._config.price_pressure_top_n]
            ]
        if metric == "time_decay_per_day":
            ranked = sorted(levels, key=lambda level: fabs(level.time_decay_per_day), reverse=True)
            total = sum(fabs(level.time_decay_per_day) for level in ranked[: self._config.price_pressure_top_n])
            return [
                PremiumPressureLevel(
                    strike_price=level.strike_price,
                    option_type=level.option_type,
                    metric_value=level.time_decay_per_day,
                    normalized_weight=0.0 if total <= 0.0 else fabs(level.time_decay_per_day) / total,
                    distance_from_spot_pct=level.distance_from_spot_pct,
                )
                for level in ranked[: self._config.price_pressure_top_n]
            ]
        raise PremiumBehaviorInputError("metric must be premium_oi_notional or time_decay_per_day")

    def _intrinsic_value(self, *, point: PremiumBehaviorPoint) -> float:
        if point.normalized_option_type == "CALL":
            return max(point.spot_price - point.strike_price, 0.0)
        if point.normalized_option_type == "PUT":
            return max(point.strike_price - point.spot_price, 0.0)
        raise PremiumBehaviorInputError("option_type must be CALL or PUT")

    def _moneyness_pct(self, *, point: PremiumBehaviorPoint) -> float:
        if point.normalized_option_type == "CALL":
            return ((point.spot_price / point.strike_price) - 1.0) * 100.0
        if point.normalized_option_type == "PUT":
            return ((point.strike_price / point.spot_price) - 1.0) * 100.0
        raise PremiumBehaviorInputError("option_type must be CALL or PUT")

    def _moneyness_bucket(self, *, point: PremiumBehaviorPoint) -> str:
        moneyness_pct = self._moneyness_pct(point=point)
        if moneyness_pct > self._config.itm_moneyness_threshold_pct:
            return "ITM"
        if moneyness_pct < -self._config.otm_moneyness_threshold_pct:
            return "OTM"
        return "ATM"

    def _premium_regime(self, *, total_intrinsic_value: float, total_extrinsic_value: float) -> str:
        if total_extrinsic_value > total_intrinsic_value:
            return "EXTRINSIC_DOMINANT"
        if total_extrinsic_value < total_intrinsic_value:
            return "INTRINSIC_DOMINANT"
        return "BALANCED_PREMIUM"

    def _infer_snapshot_timestamp(self, *, points: list[PremiumBehaviorPoint]) -> datetime | None:
        timestamps = [_ensure_utc(point.observed_at) for point in points if point.observed_at is not None]
        if not timestamps:
            return None
        return max(timestamps)

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0.0:
            return 0.0
        return numerator / denominator



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PremiumBehaviorComputationError",
    "PremiumBehaviorConfig",
    "PremiumBehaviorEngine",
    "PremiumBehaviorError",
    "PremiumBehaviorInputError",
    "PremiumBehaviorLevel",
    "PremiumBehaviorPoint",
    "PremiumBehaviorSnapshot",
    "PremiumBehaviorState",
    "PremiumPressureLevel",
]
