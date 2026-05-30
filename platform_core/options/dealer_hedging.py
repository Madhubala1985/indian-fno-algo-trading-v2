# FILE: platform_core/options/dealer_hedging.py
"""
platform_core.options.dealer_hedging
═══════════════════════════════════════════════════════════════════════
Production-grade dealer hedging approximation engine for Indian index options.

This module converts causally observed option-chain Greek snapshots into
replay-safe approximations of dealer delta, gamma, vanna, and charm hedging
pressure. It is designed for institutional analytics pipelines where exact
dealer inventory is unavailable and must be estimated from public options data
under explicit, configurable assumptions.
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


class DealerHedgingError(RuntimeError):
    """Base exception for dealer hedging approximation failures."""


class DealerHedgingInputError(DealerHedgingError):
    """Raised when public chain or Greeks inputs are invalid."""


class DealerHedgingComputationError(DealerHedgingError):
    """Raised when hedging approximation cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class DealerHedgingConfig:
    """Configuration controlling dealer sign assumptions and exposure scaling."""

    default_call_dealer_sign: int = -1
    default_put_dealer_sign: int = 1
    use_change_in_oi_weighting: bool = True
    change_in_oi_weight_cap: float = 3.0
    minimum_time_to_expiry_years: float = 1.0e-8
    minimum_abs_gamma_for_flip_detection: float = 1.0e-9
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
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class DealerGreekPoint:
    """Single option contract snapshot with Greek sensitivities and OI."""

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
    implied_volatility: float
    delta: float
    gamma: float
    vanna: float
    charm: float
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
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "time_to_expiry_years": self.time_to_expiry_years,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "vanna": self.vanna,
            "charm": self.charm,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True, slots=True)
class DealerExposureLevel:
    """Strike-level estimated dealer exposures and hedge-flow sensitivities."""

    strike_price: float
    option_type: str
    estimated_dealer_sign: int
    open_interest: int
    change_in_open_interest: int
    notional_contracts: float
    weighted_contracts: float
    delta_exposure: float
    gamma_exposure: float
    vanna_exposure: float
    charm_exposure: float
    hedge_flow_per_1pct_spot_move: float
    hedge_flow_per_1vol_point_move: float
    hedge_flow_per_1day_decay: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "estimated_dealer_sign": self.estimated_dealer_sign,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "notional_contracts": self.notional_contracts,
            "weighted_contracts": self.weighted_contracts,
            "delta_exposure": self.delta_exposure,
            "gamma_exposure": self.gamma_exposure,
            "vanna_exposure": self.vanna_exposure,
            "charm_exposure": self.charm_exposure,
            "hedge_flow_per_1pct_spot_move": self.hedge_flow_per_1pct_spot_move,
            "hedge_flow_per_1vol_point_move": self.hedge_flow_per_1vol_point_move,
            "hedge_flow_per_1day_decay": self.hedge_flow_per_1day_decay,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class DealerHedgingSnapshot:
    """Aggregate dealer-hedging approximation for one underlying-expiry snapshot."""

    underlying_symbol: str
    expiry_date: date
    valuation_timestamp: datetime | None
    spot_price: float
    level_exposures: tuple[DealerExposureLevel, ...]
    net_delta_exposure: float
    net_gamma_exposure: float
    net_vanna_exposure: float
    net_charm_exposure: float
    hedge_flow_per_1pct_spot_move: float
    hedge_flow_per_1vol_point_move: float
    hedge_flow_per_1day_decay: float
    call_delta_exposure: float
    put_delta_exposure: float
    call_gamma_exposure: float
    put_gamma_exposure: float
    gamma_flip_strike: float | None
    dominant_gamma_strike: float | None
    dominant_vanna_strike: float | None
    dominant_charm_strike: float | None
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "spot_price": self.spot_price,
            "level_exposures": [level.to_dict() for level in self.level_exposures],
            "net_delta_exposure": self.net_delta_exposure,
            "net_gamma_exposure": self.net_gamma_exposure,
            "net_vanna_exposure": self.net_vanna_exposure,
            "net_charm_exposure": self.net_charm_exposure,
            "hedge_flow_per_1pct_spot_move": self.hedge_flow_per_1pct_spot_move,
            "hedge_flow_per_1vol_point_move": self.hedge_flow_per_1vol_point_move,
            "hedge_flow_per_1day_decay": self.hedge_flow_per_1day_decay,
            "call_delta_exposure": self.call_delta_exposure,
            "put_delta_exposure": self.put_delta_exposure,
            "call_gamma_exposure": self.call_gamma_exposure,
            "put_gamma_exposure": self.put_gamma_exposure,
            "gamma_flip_strike": self.gamma_flip_strike,
            "dominant_gamma_strike": self.dominant_gamma_strike,
            "dominant_vanna_strike": self.dominant_vanna_strike,
            "dominant_charm_strike": self.dominant_charm_strike,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class DealerHedgingState:
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


class DealerHedgingEngine:
    """Replay-safe dealer hedging approximation engine."""

    def __init__(self, *, config: DealerHedgingConfig | None = None) -> None:
        self._config = config or DealerHedgingConfig()
        self._snapshots: dict[tuple[str, date], DealerHedgingSnapshot] = {}
        self._snapshot_count = 0
        self._last_underlying_symbol: str | None = None
        self._last_expiry_date: date | None = None
        self._last_valuation_timestamp: datetime | None = None

    @property
    def state(self) -> DealerHedgingState:
        return DealerHedgingState(
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
        points: list[DealerGreekPoint],
        valuation_timestamp: datetime | None = None,
    ) -> DealerHedgingSnapshot:
        if not points:
            raise DealerHedgingInputError("points must not be empty")
        filtered_points = self._filter_points(points=points, valuation_timestamp=valuation_timestamp)
        if not filtered_points:
            raise DealerHedgingComputationError("no valid dealer Greek points available after filtering")
        underlying_symbol = filtered_points[0].underlying_symbol
        expiry_date = filtered_points[0].expiry_date
        spot_price = filtered_points[0].spot_price
        for point in filtered_points:
            if point.underlying_symbol != underlying_symbol:
                raise DealerHedgingInputError("all points must share the same underlying_symbol")
            if point.expiry_date != expiry_date:
                raise DealerHedgingInputError("all points must share the same expiry_date")
        level_exposures = [self._build_level_exposure(point=point, reference_spot_price=spot_price) for point in filtered_points]
        level_exposures.sort(key=lambda level: (level.strike_price, level.option_type))
        net_delta_exposure = sum(level.delta_exposure for level in level_exposures)
        net_gamma_exposure = sum(level.gamma_exposure for level in level_exposures)
        net_vanna_exposure = sum(level.vanna_exposure for level in level_exposures)
        net_charm_exposure = sum(level.charm_exposure for level in level_exposures)
        hedge_flow_per_1pct_spot_move = sum(level.hedge_flow_per_1pct_spot_move for level in level_exposures)
        hedge_flow_per_1vol_point_move = sum(level.hedge_flow_per_1vol_point_move for level in level_exposures)
        hedge_flow_per_1day_decay = sum(level.hedge_flow_per_1day_decay for level in level_exposures)
        call_levels = [level for level in level_exposures if level.option_type == "CALL"]
        put_levels = [level for level in level_exposures if level.option_type == "PUT"]
        valuation_ts = _ensure_utc(valuation_timestamp) if valuation_timestamp else self._infer_snapshot_timestamp(points=filtered_points)
        snapshot = DealerHedgingSnapshot(
            underlying_symbol=underlying_symbol,
            expiry_date=expiry_date,
            valuation_timestamp=valuation_ts,
            spot_price=spot_price,
            level_exposures=tuple(level_exposures),
            net_delta_exposure=net_delta_exposure,
            net_gamma_exposure=net_gamma_exposure,
            net_vanna_exposure=net_vanna_exposure,
            net_charm_exposure=net_charm_exposure,
            hedge_flow_per_1pct_spot_move=hedge_flow_per_1pct_spot_move,
            hedge_flow_per_1vol_point_move=hedge_flow_per_1vol_point_move,
            hedge_flow_per_1day_decay=hedge_flow_per_1day_decay,
            call_delta_exposure=sum(level.delta_exposure for level in call_levels),
            put_delta_exposure=sum(level.delta_exposure for level in put_levels),
            call_gamma_exposure=sum(level.gamma_exposure for level in call_levels),
            put_gamma_exposure=sum(level.gamma_exposure for level in put_levels),
            gamma_flip_strike=self._estimate_gamma_flip_strike(levels=level_exposures),
            dominant_gamma_strike=self._dominant_strike(levels=level_exposures, metric="gamma"),
            dominant_vanna_strike=self._dominant_strike(levels=level_exposures, metric="vanna"),
            dominant_charm_strike=self._dominant_strike(levels=level_exposures, metric="charm"),
            metadata={
                "raw_point_count": len(points),
                "filtered_point_count": len(filtered_points),
                "default_call_dealer_sign": self._config.default_call_dealer_sign,
                "default_put_dealer_sign": self._config.default_put_dealer_sign,
                "change_in_oi_weighting": self._config.use_change_in_oi_weighting,
            },
        )
        self._snapshots[(underlying_symbol, expiry_date)] = snapshot
        self._snapshot_count += 1
        self._last_underlying_symbol = underlying_symbol
        self._last_expiry_date = expiry_date
        self._last_valuation_timestamp = valuation_ts
        return snapshot

    def get_snapshot(self, *, underlying_symbol: str, expiry_date: date) -> DealerHedgingSnapshot:
        snapshot = self._snapshots.get((underlying_symbol, expiry_date))
        if snapshot is None:
            raise DealerHedgingComputationError("requested dealer hedging snapshot is not available")
        return snapshot

    def _filter_points(
        self,
        *,
        points: list[DealerGreekPoint],
        valuation_timestamp: datetime | None,
    ) -> list[DealerGreekPoint]:
        filtered: list[DealerGreekPoint] = []
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
                point.implied_volatility,
                point.delta,
                point.gamma,
                point.vanna,
                point.charm,
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

    def _build_level_exposure(self, *, point: DealerGreekPoint, reference_spot_price: float) -> DealerExposureLevel:
        dealer_sign = self._estimate_dealer_sign(option_type=point.normalized_option_type)
        notional_contracts = float(point.open_interest * point.lot_size)
        weighted_contracts = notional_contracts * self._change_in_oi_weight(point.change_in_open_interest, point.open_interest)
        delta_exposure = dealer_sign * point.delta * weighted_contracts * point.spot_price
        gamma_exposure = dealer_sign * point.gamma * weighted_contracts * point.spot_price * point.spot_price
        vanna_exposure = dealer_sign * point.vanna * weighted_contracts * point.spot_price
        charm_exposure = dealer_sign * point.charm * weighted_contracts * point.spot_price
        hedge_flow_per_1pct_spot_move = -gamma_exposure * 0.01
        hedge_flow_per_1vol_point_move = -vanna_exposure * 0.01
        hedge_flow_per_1day_decay = -charm_exposure / 365.0
        return DealerExposureLevel(
            strike_price=point.strike_price,
            option_type=point.normalized_option_type,
            estimated_dealer_sign=dealer_sign,
            open_interest=point.open_interest,
            change_in_open_interest=point.change_in_open_interest,
            notional_contracts=notional_contracts,
            weighted_contracts=weighted_contracts,
            delta_exposure=delta_exposure,
            gamma_exposure=gamma_exposure,
            vanna_exposure=vanna_exposure,
            charm_exposure=charm_exposure,
            hedge_flow_per_1pct_spot_move=hedge_flow_per_1pct_spot_move,
            hedge_flow_per_1vol_point_move=hedge_flow_per_1vol_point_move,
            hedge_flow_per_1day_decay=hedge_flow_per_1day_decay,
            distance_from_spot_pct=((point.strike_price / reference_spot_price) - 1.0) * 100.0,
        )

    def _estimate_dealer_sign(self, *, option_type: str) -> int:
        if option_type == "CALL":
            return self._config.default_call_dealer_sign
        if option_type == "PUT":
            return self._config.default_put_dealer_sign
        raise DealerHedgingInputError("option_type must be CALL or PUT")

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

    def _estimate_gamma_flip_strike(self, *, levels: list[DealerExposureLevel]) -> float | None:
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

    def _dominant_strike(self, *, levels: list[DealerExposureLevel], metric: str) -> float | None:
        if not levels:
            return None
        if metric == "gamma":
            chosen = max(levels, key=lambda level: fabs(level.gamma_exposure))
            return chosen.strike_price
        if metric == "vanna":
            chosen = max(levels, key=lambda level: fabs(level.vanna_exposure))
            return chosen.strike_price
        if metric == "charm":
            chosen = max(levels, key=lambda level: fabs(level.charm_exposure))
            return chosen.strike_price
        raise DealerHedgingInputError("metric must be gamma, vanna, or charm")

    def _infer_snapshot_timestamp(self, *, points: list[DealerGreekPoint]) -> datetime | None:
        timestamps = [_ensure_utc(point.observed_at) for point in points if point.observed_at is not None]
        if not timestamps:
            return None
        return max(timestamps)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DealerExposureLevel",
    "DealerGreekPoint",
    "DealerHedgingComputationError",
    "DealerHedgingConfig",
    "DealerHedgingEngine",
    "DealerHedgingError",
    "DealerHedgingInputError",
    "DealerHedgingSnapshot",
    "DealerHedgingState",
]
