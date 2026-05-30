# FILE: platform_core/options/gamma_exposure.py
"""
platform_core.options.gamma_exposure
═══════════════════════════════════════════════════════════════════════
Production-grade gamma exposure analytics engine for Indian index options.

This module computes deterministic, replay-safe gamma exposure analytics from
causally observed option-chain Greek snapshots. It provides strike-level gamma
profiles, expiry-aware aggregate gamma, call/put wall diagnostics, gamma-flip
estimation, and hedge-flow sensitivity metrics for downstream regime,
microstructure, and execution-safety pipelines.
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


class GammaExposureError(RuntimeError):
    """Base exception for gamma exposure analytics failures."""


class GammaExposureInputError(GammaExposureError):
    """Raised when gamma exposure inputs are invalid."""


class GammaExposureComputationError(GammaExposureError):
    """Raised when gamma exposure analytics cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class GammaExposureConfig:
    """Configuration for public-data gamma exposure estimation."""

    default_call_dealer_sign: int = -1
    default_put_dealer_sign: int = 1
    use_change_in_oi_weighting: bool = True
    change_in_oi_weight_cap: float = 3.0
    minimum_time_to_expiry_years: float = 1.0e-8
    minimum_abs_gamma_for_flip_detection: float = 1.0e-9
    wall_top_n: int = 5
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.default_call_dealer_sign not in {-1, 1}:
            raise ValueError("default_call_dealer_sign must be -1 or 1")
        if self.default_put_dealer_sign not in {-1, 1}:
            raise ValueError("default_put_dealer_sign must be -1 or 1")
        if self.change_in_oi_weight_cap <= 0.0:
            raise ValueError("change_in_oi_weight_cap must be positive")
        if self.minimum_time_to_expiry_years <= 0.0:
            raise ValueError("minimum_time_to_expiry_years must be positive")
        if self.minimum_abs_gamma_for_flip_detection <= 0.0:
            raise ValueError("minimum_abs_gamma_for_flip_detection must be positive")
        if self.wall_top_n <= 0:
            raise ValueError("wall_top_n must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class GammaExposurePoint:
    """Single option contract snapshot with gamma and public chain data."""

    underlying_symbol: str
    expiry_date: date
    option_symbol: str
    option_type: str
    strike_price: float
    spot_price: float
    lot_size: int
    open_interest: int
    change_in_open_interest: int
    volume: int
    time_to_expiry_years: float
    gamma: float
    implied_volatility: float | None = None
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
        if self.open_interest < 0:
            raise ValueError("open_interest must be non-negative")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.time_to_expiry_years < 0.0:
            raise ValueError("time_to_expiry_years must be non-negative")
        if self.implied_volatility is not None and self.implied_volatility < 0.0:
            raise ValueError("implied_volatility must be non-negative when provided")

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
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "time_to_expiry_years": self.time_to_expiry_years,
            "gamma": self.gamma,
            "implied_volatility": self.implied_volatility,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True, slots=True)
class GammaExposureLevel:
    """Strike-level gamma exposure estimate."""

    strike_price: float
    option_type: str
    estimated_dealer_sign: int
    open_interest: int
    change_in_open_interest: int
    volume: int
    time_to_expiry_years: float
    raw_contracts: float
    weighted_contracts: float
    gamma: float
    gamma_exposure: float
    gamma_exposure_per_1pct_move: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "estimated_dealer_sign": self.estimated_dealer_sign,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "time_to_expiry_years": self.time_to_expiry_years,
            "raw_contracts": self.raw_contracts,
            "weighted_contracts": self.weighted_contracts,
            "gamma": self.gamma,
            "gamma_exposure": self.gamma_exposure,
            "gamma_exposure_per_1pct_move": self.gamma_exposure_per_1pct_move,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class GammaWallLevel:
    """Gamma wall level extracted from strike profile."""

    strike_price: float
    option_type: str
    gamma_exposure: float
    gamma_exposure_per_1pct_move: float
    normalized_weight: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "gamma_exposure": self.gamma_exposure,
            "gamma_exposure_per_1pct_move": self.gamma_exposure_per_1pct_move,
            "normalized_weight": self.normalized_weight,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class GammaExposureSnapshot:
    """Aggregate gamma exposure analytics for one underlying-expiry snapshot."""

    underlying_symbol: str
    expiry_date: date
    valuation_timestamp: datetime | None
    spot_price: float
    level_exposures: tuple[GammaExposureLevel, ...]
    total_gamma_exposure: float
    total_gamma_exposure_per_1pct_move: float
    call_gamma_exposure: float
    put_gamma_exposure: float
    call_gamma_exposure_per_1pct_move: float
    put_gamma_exposure_per_1pct_move: float
    gamma_flip_strike: float | None
    call_wall_strike: float | None
    put_wall_strike: float | None
    dominant_gamma_strike: float | None
    net_gamma_regime: str
    call_wall_levels: tuple[GammaWallLevel, ...]
    put_wall_levels: tuple[GammaWallLevel, ...]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "spot_price": self.spot_price,
            "level_exposures": [level.to_dict() for level in self.level_exposures],
            "total_gamma_exposure": self.total_gamma_exposure,
            "total_gamma_exposure_per_1pct_move": self.total_gamma_exposure_per_1pct_move,
            "call_gamma_exposure": self.call_gamma_exposure,
            "put_gamma_exposure": self.put_gamma_exposure,
            "call_gamma_exposure_per_1pct_move": self.call_gamma_exposure_per_1pct_move,
            "put_gamma_exposure_per_1pct_move": self.put_gamma_exposure_per_1pct_move,
            "gamma_flip_strike": self.gamma_flip_strike,
            "call_wall_strike": self.call_wall_strike,
            "put_wall_strike": self.put_wall_strike,
            "dominant_gamma_strike": self.dominant_gamma_strike,
            "net_gamma_regime": self.net_gamma_regime,
            "call_wall_levels": [level.to_dict() for level in self.call_wall_levels],
            "put_wall_levels": [level.to_dict() for level in self.put_wall_levels],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class GammaExposureState:
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


class GammaExposureEngine:
    """Replay-safe gamma exposure analytics engine."""

    def __init__(self, *, config: GammaExposureConfig | None = None) -> None:
        self._config = config or GammaExposureConfig()
        self._snapshots: dict[tuple[str, date], GammaExposureSnapshot] = {}
        self._snapshot_count = 0
        self._last_underlying_symbol: str | None = None
        self._last_expiry_date: date | None = None
        self._last_valuation_timestamp: datetime | None = None

    @property
    def state(self) -> GammaExposureState:
        return GammaExposureState(
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
        points: list[GammaExposurePoint],
        valuation_timestamp: datetime | None = None,
    ) -> GammaExposureSnapshot:
        if not points:
            raise GammaExposureInputError("points must not be empty")
        filtered_points = self._filter_points(points=points, valuation_timestamp=valuation_timestamp)
        if not filtered_points:
            raise GammaExposureComputationError("no valid gamma exposure points available after filtering")
        underlying_symbol = filtered_points[0].underlying_symbol
        expiry_date = filtered_points[0].expiry_date
        spot_price = filtered_points[0].spot_price
        for point in filtered_points:
            if point.underlying_symbol != underlying_symbol:
                raise GammaExposureInputError("all points must share the same underlying_symbol")
            if point.expiry_date != expiry_date:
                raise GammaExposureInputError("all points must share the same expiry_date")
        level_exposures = [self._build_level_exposure(point=point, reference_spot_price=spot_price) for point in filtered_points]
        level_exposures.sort(key=lambda level: (level.strike_price, level.option_type))
        call_levels = [level for level in level_exposures if level.option_type == "CALL"]
        put_levels = [level for level in level_exposures if level.option_type == "PUT"]
        total_gamma_exposure = sum(level.gamma_exposure for level in level_exposures)
        total_gamma_exposure_per_1pct_move = sum(level.gamma_exposure_per_1pct_move for level in level_exposures)
        call_gamma_exposure = sum(level.gamma_exposure for level in call_levels)
        put_gamma_exposure = sum(level.gamma_exposure for level in put_levels)
        call_gamma_exposure_per_1pct_move = sum(level.gamma_exposure_per_1pct_move for level in call_levels)
        put_gamma_exposure_per_1pct_move = sum(level.gamma_exposure_per_1pct_move for level in put_levels)
        call_wall_levels = self._extract_wall_levels(levels=call_levels, option_type="CALL")
        put_wall_levels = self._extract_wall_levels(levels=put_levels, option_type="PUT")
        snapshot_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else self._infer_snapshot_timestamp(points=filtered_points)
        snapshot = GammaExposureSnapshot(
            underlying_symbol=underlying_symbol,
            expiry_date=expiry_date,
            valuation_timestamp=snapshot_timestamp,
            spot_price=spot_price,
            level_exposures=tuple(level_exposures),
            total_gamma_exposure=total_gamma_exposure,
            total_gamma_exposure_per_1pct_move=total_gamma_exposure_per_1pct_move,
            call_gamma_exposure=call_gamma_exposure,
            put_gamma_exposure=put_gamma_exposure,
            call_gamma_exposure_per_1pct_move=call_gamma_exposure_per_1pct_move,
            put_gamma_exposure_per_1pct_move=put_gamma_exposure_per_1pct_move,
            gamma_flip_strike=self._estimate_gamma_flip_strike(levels=level_exposures),
            call_wall_strike=None if not call_wall_levels else call_wall_levels[0].strike_price,
            put_wall_strike=None if not put_wall_levels else put_wall_levels[0].strike_price,
            dominant_gamma_strike=self._dominant_gamma_strike(levels=level_exposures),
            net_gamma_regime=self._gamma_regime(total_gamma_exposure=total_gamma_exposure),
            call_wall_levels=tuple(call_wall_levels),
            put_wall_levels=tuple(put_wall_levels),
            metadata={
                "raw_point_count": len(points),
                "filtered_point_count": len(filtered_points),
                "default_call_dealer_sign": self._config.default_call_dealer_sign,
                "default_put_dealer_sign": self._config.default_put_dealer_sign,
                "change_in_oi_weighting": self._config.use_change_in_oi_weighting,
                "wall_top_n": self._config.wall_top_n,
            },
        )
        self._snapshots[(underlying_symbol, expiry_date)] = snapshot
        self._snapshot_count += 1
        self._last_underlying_symbol = underlying_symbol
        self._last_expiry_date = expiry_date
        self._last_valuation_timestamp = snapshot_timestamp
        return snapshot

    def get_snapshot(self, *, underlying_symbol: str, expiry_date: date) -> GammaExposureSnapshot:
        snapshot = self._snapshots.get((underlying_symbol, expiry_date))
        if snapshot is None:
            raise GammaExposureComputationError("requested gamma exposure snapshot is not available")
        return snapshot

    def _filter_points(
        self,
        *,
        points: list[GammaExposurePoint],
        valuation_timestamp: datetime | None,
    ) -> list[GammaExposurePoint]:
        filtered: list[GammaExposurePoint] = []
        normalized_valuation_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else None
        for point in points:
            values = [
                point.strike_price,
                point.spot_price,
                float(point.lot_size),
                float(point.open_interest),
                float(point.change_in_open_interest),
                float(point.volume),
                point.time_to_expiry_years,
                point.gamma,
            ]
            if not all(isfinite(value) for value in values):
                continue
            if point.time_to_expiry_years < self._config.minimum_time_to_expiry_years:
                continue
            if point.observed_at is not None and normalized_valuation_timestamp is not None and self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - _ensure_utc(point.observed_at)).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    continue
            filtered.append(point)
        return filtered

    def _build_level_exposure(self, *, point: GammaExposurePoint, reference_spot_price: float) -> GammaExposureLevel:
        dealer_sign = self._estimate_dealer_sign(option_type=point.normalized_option_type)
        raw_contracts = float(point.open_interest * point.lot_size)
        weighted_contracts = raw_contracts * self._change_in_oi_weight(point.change_in_open_interest, point.open_interest)
        gamma_exposure = dealer_sign * point.gamma * weighted_contracts * point.spot_price * point.spot_price
        gamma_exposure_per_1pct_move = gamma_exposure * 0.01
        return GammaExposureLevel(
            strike_price=point.strike_price,
            option_type=point.normalized_option_type,
            estimated_dealer_sign=dealer_sign,
            open_interest=point.open_interest,
            change_in_open_interest=point.change_in_open_interest,
            volume=point.volume,
            time_to_expiry_years=point.time_to_expiry_years,
            raw_contracts=raw_contracts,
            weighted_contracts=weighted_contracts,
            gamma=point.gamma,
            gamma_exposure=gamma_exposure,
            gamma_exposure_per_1pct_move=gamma_exposure_per_1pct_move,
            distance_from_spot_pct=((point.strike_price / reference_spot_price) - 1.0) * 100.0,
        )

    def _estimate_dealer_sign(self, *, option_type: str) -> int:
        if option_type == "CALL":
            return self._config.default_call_dealer_sign
        if option_type == "PUT":
            return self._config.default_put_dealer_sign
        raise GammaExposureInputError("option_type must be CALL or PUT")

    def _change_in_oi_weight(self, change_in_open_interest: int, open_interest: int) -> float:
        if not self._config.use_change_in_oi_weighting or open_interest <= 0:
            return 1.0
        normalized = change_in_open_interest / float(open_interest)
        weight = 1.0 + normalized
        if weight < 0.0:
            weight = 0.0
        if weight > self._config.change_in_oi_weight_cap:
            weight = self._config.change_in_oi_weight_cap
        return weight

    def _extract_wall_levels(self, *, levels: list[GammaExposureLevel], option_type: str) -> list[GammaWallLevel]:
        if option_type not in {"CALL", "PUT"}:
            raise GammaExposureInputError("option_type must be CALL or PUT")
        ranked = sorted(levels, key=lambda level: fabs(level.gamma_exposure), reverse=True)
        selected = ranked[: self._config.wall_top_n]
        total_abs_gamma = sum(fabs(level.gamma_exposure) for level in selected)
        wall_levels: list[GammaWallLevel] = []
        for level in selected:
            normalized_weight = 0.0 if total_abs_gamma <= 0.0 else fabs(level.gamma_exposure) / total_abs_gamma
            wall_levels.append(
                GammaWallLevel(
                    strike_price=level.strike_price,
                    option_type=level.option_type,
                    gamma_exposure=level.gamma_exposure,
                    gamma_exposure_per_1pct_move=level.gamma_exposure_per_1pct_move,
                    normalized_weight=normalized_weight,
                    distance_from_spot_pct=level.distance_from_spot_pct,
                )
            )
        return wall_levels

    def _estimate_gamma_flip_strike(self, *, levels: list[GammaExposureLevel]) -> float | None:
        if not levels:
            return None
        ordered = sorted(levels, key=lambda level: level.strike_price)
        cumulative_gamma = 0.0
        previous_cumulative_gamma: float | None = None
        previous_strike: float | None = None
        for level in ordered:
            cumulative_gamma += level.gamma_exposure
            if previous_cumulative_gamma is not None:
                if fabs(previous_cumulative_gamma) >= self._config.minimum_abs_gamma_for_flip_detection and cumulative_gamma == 0.0:
                    return level.strike_price
                if previous_cumulative_gamma * cumulative_gamma < 0.0 and previous_strike is not None:
                    denominator = fabs(previous_cumulative_gamma) + fabs(cumulative_gamma)
                    if denominator <= 0.0:
                        return level.strike_price
                    weight = fabs(previous_cumulative_gamma) / denominator
                    return previous_strike + weight * (level.strike_price - previous_strike)
            previous_cumulative_gamma = cumulative_gamma
            previous_strike = level.strike_price
        return None

    def _dominant_gamma_strike(self, *, levels: list[GammaExposureLevel]) -> float | None:
        if not levels:
            return None
        dominant = max(levels, key=lambda level: fabs(level.gamma_exposure))
        return dominant.strike_price

    def _gamma_regime(self, *, total_gamma_exposure: float) -> str:
        if total_gamma_exposure > 0.0:
            return "LONG_GAMMA"
        if total_gamma_exposure < 0.0:
            return "SHORT_GAMMA"
        return "NEUTRAL_GAMMA"

    def _infer_snapshot_timestamp(self, *, points: list[GammaExposurePoint]) -> datetime | None:
        timestamps = [_ensure_utc(point.observed_at) for point in points if point.observed_at is not None]
        if not timestamps:
            return None
        return max(timestamps)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "GammaExposureComputationError",
    "GammaExposureConfig",
    "GammaExposureEngine",
    "GammaExposureError",
    "GammaExposureInputError",
    "GammaExposureLevel",
    "GammaExposurePoint",
    "GammaExposureSnapshot",
    "GammaExposureState",
    "GammaWallLevel",
]
