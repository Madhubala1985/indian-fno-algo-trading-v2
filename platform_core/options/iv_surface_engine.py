# FILE: platform_core/options/iv_surface_engine.py
"""
platform_core.options.iv_surface_engine
═══════════════════════════════════════════════════════════════════════
Production-grade implied volatility surface engine for Indian index options.

This module constructs deterministic, replay-safe implied volatility surfaces
from discrete option IV observations across strikes and expiries. It provides
incremental snapshot ingestion, robust filtering, moneyness-based organization,
linear interpolation on strike and expiry dimensions, and JSON-serializable
state suitable for Redis/PostgreSQL/TimescaleDB persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timezone
from math import exp
from math import isfinite
from math import log
from statistics import median


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class IVSurfaceEngineError(RuntimeError):
    """Base exception for IV surface engine failures."""


class IVSurfaceInputError(IVSurfaceEngineError):
    """Raised when IV surface inputs are invalid."""


class IVSurfaceComputationError(IVSurfaceEngineError):
    """Raised when an IV surface cannot be constructed or queried safely."""


@dataclass(frozen=True, slots=True)
class IVSurfaceEngineConfig:
    """Numerical and filtering configuration for IV surface construction."""

    minimum_time_to_expiry_years: float = 1.0e-8
    minimum_implied_volatility: float = 1.0e-6
    maximum_implied_volatility: float = 8.0
    minimum_points_per_expiry: int = 3
    maximum_log_moneyness_abs: float = 0.35
    minimum_strike_gap: float = 1.0e-9
    stale_snapshot_max_age_seconds: float | None = None
    interpolation_clamp_to_domain: bool = True

    def __post_init__(self) -> None:
        if self.minimum_time_to_expiry_years <= 0.0:
            raise ValueError("minimum_time_to_expiry_years must be positive")
        if self.minimum_implied_volatility <= 0.0:
            raise ValueError("minimum_implied_volatility must be positive")
        if self.maximum_implied_volatility <= self.minimum_implied_volatility:
            raise ValueError("maximum_implied_volatility must exceed minimum_implied_volatility")
        if self.minimum_points_per_expiry < 2:
            raise ValueError("minimum_points_per_expiry must be at least 2")
        if self.maximum_log_moneyness_abs <= 0.0:
            raise ValueError("maximum_log_moneyness_abs must be positive")
        if self.minimum_strike_gap <= 0.0:
            raise ValueError("minimum_strike_gap must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class IVSurfacePoint:
    """Single implied volatility observation for one option contract."""

    instrument_id: str
    underlying_symbol: str
    option_symbol: str
    expiry_date: date
    option_type: str
    strike_price: float
    spot_price: float
    implied_volatility: float
    time_to_expiry_years: float
    risk_free_rate: float
    dividend_yield: float = 0.0
    observed_at: datetime | None = None
    open_interest: int | None = None
    volume: int | None = None

    def __post_init__(self) -> None:
        option_type_normalized = self.option_type.upper()
        if option_type_normalized not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if not self.option_symbol.strip():
            raise ValueError("option_symbol must be non-empty")
        if self.strike_price <= 0.0:
            raise ValueError("strike_price must be positive")
        if self.spot_price <= 0.0:
            raise ValueError("spot_price must be positive")
        if self.implied_volatility < 0.0:
            raise ValueError("implied_volatility must be non-negative")
        if self.time_to_expiry_years < 0.0:
            raise ValueError("time_to_expiry_years must be non-negative")
        if self.open_interest is not None and self.open_interest < 0:
            raise ValueError("open_interest must be non-negative when provided")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume must be non-negative when provided")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "underlying_symbol": self.underlying_symbol,
            "option_symbol": self.option_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "option_type": self.normalized_option_type,
            "strike_price": self.strike_price,
            "spot_price": self.spot_price,
            "implied_volatility": self.implied_volatility,
            "time_to_expiry_years": self.time_to_expiry_years,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "open_interest": self.open_interest,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class SurfaceNode:
    """Normalized surface node used for interpolation."""

    instrument_id: str
    option_symbol: str
    expiry_date: date
    option_type: str
    strike_price: float
    spot_price: float
    forward_price: float
    log_moneyness: float
    implied_volatility: float
    time_to_expiry_years: float
    observed_at: datetime | None
    open_interest: int | None
    volume: int | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "option_symbol": self.option_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "option_type": self.option_type,
            "strike_price": self.strike_price,
            "spot_price": self.spot_price,
            "forward_price": self.forward_price,
            "log_moneyness": self.log_moneyness,
            "implied_volatility": self.implied_volatility,
            "time_to_expiry_years": self.time_to_expiry_years,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "open_interest": self.open_interest,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class ExpirySlice:
    """Single expiry slice of the IV surface."""

    underlying_symbol: str
    option_type: str
    expiry_date: date
    time_to_expiry_years: float
    forward_price: float
    reference_spot_price: float
    nodes: tuple[SurfaceNode, ...]
    atm_implied_volatility: float
    skew_slope: float
    convexity: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "option_type": self.option_type,
            "expiry_date": self.expiry_date.isoformat(),
            "time_to_expiry_years": self.time_to_expiry_years,
            "forward_price": self.forward_price,
            "reference_spot_price": self.reference_spot_price,
            "nodes": [node.to_dict() for node in self.nodes],
            "atm_implied_volatility": self.atm_implied_volatility,
            "skew_slope": self.skew_slope,
            "convexity": self.convexity,
        }


@dataclass(frozen=True, slots=True)
class IVSurfaceSnapshot:
    """Complete surface snapshot for a single underlying and option type."""

    underlying_symbol: str
    option_type: str
    valuation_timestamp: datetime | None
    slices: tuple[ExpirySlice, ...]
    atm_term_structure: tuple[tuple[float, float], ...]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "option_type": self.option_type,
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "slices": [slice_.to_dict() for slice_ in self.slices],
            "atm_term_structure": [[tenor, iv] for tenor, iv in self.atm_term_structure],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class IVSurfaceQuery:
    """Query for interpolated surface volatility."""

    underlying_symbol: str
    option_type: str
    strike_price: float
    time_to_expiry_years: float
    reference_spot_price: float
    risk_free_rate: float
    dividend_yield: float = 0.0

    def __post_init__(self) -> None:
        option_type_normalized = self.option_type.upper()
        if option_type_normalized not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if self.strike_price <= 0.0:
            raise ValueError("strike_price must be positive")
        if self.time_to_expiry_years < 0.0:
            raise ValueError("time_to_expiry_years must be non-negative")
        if self.reference_spot_price <= 0.0:
            raise ValueError("reference_spot_price must be positive")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()


@dataclass(frozen=True, slots=True)
class IVSurfaceQueryResult:
    """Interpolated IV surface lookup result."""

    underlying_symbol: str
    option_type: str
    strike_price: float
    time_to_expiry_years: float
    reference_spot_price: float
    interpolated_implied_volatility: float
    forward_price: float
    log_moneyness: float
    lower_tenor: float
    upper_tenor: float
    used_tenor_interpolation: bool
    valuation_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "option_type": self.option_type,
            "strike_price": self.strike_price,
            "time_to_expiry_years": self.time_to_expiry_years,
            "reference_spot_price": self.reference_spot_price,
            "interpolated_implied_volatility": self.interpolated_implied_volatility,
            "forward_price": self.forward_price,
            "log_moneyness": self.log_moneyness,
            "lower_tenor": self.lower_tenor,
            "upper_tenor": self.upper_tenor,
            "used_tenor_interpolation": self.used_tenor_interpolation,
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
        }


@dataclass(frozen=True, slots=True)
class IVSurfaceEngineState:
    """Serializable engine state for replay-safe persistence."""

    snapshot_count: int
    last_underlying_symbol: str | None
    last_option_type: str | None
    last_valuation_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "snapshot_count": self.snapshot_count,
            "last_underlying_symbol": self.last_underlying_symbol,
            "last_option_type": self.last_option_type,
            "last_valuation_timestamp": self.last_valuation_timestamp.isoformat() if self.last_valuation_timestamp else None,
        }


class IVSurfaceEngine:
    """Replay-safe implied volatility surface construction and querying engine."""

    def __init__(self, *, config: IVSurfaceEngineConfig | None = None) -> None:
        self._config = config or IVSurfaceEngineConfig()
        self._snapshots: dict[tuple[str, str], IVSurfaceSnapshot] = {}
        self._snapshot_count = 0
        self._last_underlying_symbol: str | None = None
        self._last_option_type: str | None = None
        self._last_valuation_timestamp: datetime | None = None

    @property
    def state(self) -> IVSurfaceEngineState:
        return IVSurfaceEngineState(
            snapshot_count=self._snapshot_count,
            last_underlying_symbol=self._last_underlying_symbol,
            last_option_type=self._last_option_type,
            last_valuation_timestamp=self._last_valuation_timestamp,
        )

    def reset(self) -> None:
        self._snapshots.clear()
        self._snapshot_count = 0
        self._last_underlying_symbol = None
        self._last_option_type = None
        self._last_valuation_timestamp = None

    def build_surface(
        self,
        *,
        points: list[IVSurfacePoint],
        valuation_timestamp: datetime | None = None,
    ) -> IVSurfaceSnapshot:
        if not points:
            raise IVSurfaceInputError("points must not be empty")
        filtered_points = self._filter_points(points=points, valuation_timestamp=valuation_timestamp)
        if not filtered_points:
            raise IVSurfaceComputationError("no valid IV points available after filtering")
        underlying_symbol = filtered_points[0].underlying_symbol
        option_type = filtered_points[0].normalized_option_type
        for point in filtered_points:
            if point.underlying_symbol != underlying_symbol:
                raise IVSurfaceInputError("all points must share the same underlying_symbol")
            if point.normalized_option_type != option_type:
                raise IVSurfaceInputError("all points must share the same option_type")
        grouped: dict[date, list[SurfaceNode]] = {}
        for point in filtered_points:
            forward_price = point.spot_price * exp((point.risk_free_rate - point.dividend_yield) * point.time_to_expiry_years)
            log_moneyness = log(point.strike_price / forward_price)
            node = SurfaceNode(
                instrument_id=point.instrument_id,
                option_symbol=point.option_symbol,
                expiry_date=point.expiry_date,
                option_type=point.normalized_option_type,
                strike_price=point.strike_price,
                spot_price=point.spot_price,
                forward_price=forward_price,
                log_moneyness=log_moneyness,
                implied_volatility=point.implied_volatility,
                time_to_expiry_years=max(point.time_to_expiry_years, self._config.minimum_time_to_expiry_years),
                observed_at=_ensure_utc(point.observed_at) if point.observed_at else None,
                open_interest=point.open_interest,
                volume=point.volume,
            )
            grouped.setdefault(point.expiry_date, []).append(node)
        slices: list[ExpirySlice] = []
        for expiry_date, nodes in grouped.items():
            deduplicated_nodes = self._deduplicate_and_sort_nodes(nodes=nodes)
            if len(deduplicated_nodes) < self._config.minimum_points_per_expiry:
                continue
            atm_iv = self._interpolate_within_slice(nodes=deduplicated_nodes, target_log_moneyness=0.0)
            skew_slope = self._estimate_slope(nodes=deduplicated_nodes, target_log_moneyness=0.0)
            convexity = self._estimate_convexity(nodes=deduplicated_nodes, target_log_moneyness=0.0)
            reference_node = min(deduplicated_nodes, key=lambda item: abs(item.log_moneyness))
            slices.append(
                ExpirySlice(
                    underlying_symbol=underlying_symbol,
                    option_type=option_type,
                    expiry_date=expiry_date,
                    time_to_expiry_years=reference_node.time_to_expiry_years,
                    forward_price=reference_node.forward_price,
                    reference_spot_price=reference_node.spot_price,
                    nodes=tuple(deduplicated_nodes),
                    atm_implied_volatility=atm_iv,
                    skew_slope=skew_slope,
                    convexity=convexity,
                )
            )
        if not slices:
            raise IVSurfaceComputationError("no expiry slices met minimum point requirements")
        slices.sort(key=lambda slice_: slice_.time_to_expiry_years)
        normalized_valuation_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else self._infer_snapshot_timestamp(filtered_points)
        snapshot = IVSurfaceSnapshot(
            underlying_symbol=underlying_symbol,
            option_type=option_type,
            valuation_timestamp=normalized_valuation_timestamp,
            slices=tuple(slices),
            atm_term_structure=tuple((slice_.time_to_expiry_years, slice_.atm_implied_volatility) for slice_ in slices),
            metadata={
                "slice_count": len(slices),
                "raw_point_count": len(points),
                "filtered_point_count": len(filtered_points),
                "surface_domain": "log_moneyness_x_time_to_expiry",
                "interpolation": "linear_strike_and_tenor",
            },
        )
        key = (snapshot.underlying_symbol, snapshot.option_type)
        self._snapshots[key] = snapshot
        self._snapshot_count += 1
        self._last_underlying_symbol = snapshot.underlying_symbol
        self._last_option_type = snapshot.option_type
        self._last_valuation_timestamp = snapshot.valuation_timestamp
        return snapshot

    def get_surface_snapshot(self, *, underlying_symbol: str, option_type: str) -> IVSurfaceSnapshot:
        key = (underlying_symbol, option_type.upper())
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            raise IVSurfaceComputationError("requested IV surface snapshot is not available")
        return snapshot

    def interpolate(self, *, query: IVSurfaceQuery) -> IVSurfaceQueryResult:
        snapshot = self.get_surface_snapshot(
            underlying_symbol=query.underlying_symbol,
            option_type=query.normalized_option_type,
        )
        tenor = max(query.time_to_expiry_years, self._config.minimum_time_to_expiry_years)
        forward_price = query.reference_spot_price * exp((query.risk_free_rate - query.dividend_yield) * tenor)
        target_log_moneyness = log(query.strike_price / forward_price)
        slices = snapshot.slices
        if len(slices) == 1:
            interpolated_iv = self._interpolate_within_slice(nodes=list(slices[0].nodes), target_log_moneyness=target_log_moneyness)
            return IVSurfaceQueryResult(
                underlying_symbol=query.underlying_symbol,
                option_type=query.normalized_option_type,
                strike_price=query.strike_price,
                time_to_expiry_years=tenor,
                reference_spot_price=query.reference_spot_price,
                interpolated_implied_volatility=interpolated_iv,
                forward_price=forward_price,
                log_moneyness=target_log_moneyness,
                lower_tenor=slices[0].time_to_expiry_years,
                upper_tenor=slices[0].time_to_expiry_years,
                used_tenor_interpolation=False,
                valuation_timestamp=snapshot.valuation_timestamp,
            )
        lower_slice, upper_slice = self._locate_tenor_bracket(slices=slices, target_tenor=tenor)
        lower_iv = self._interpolate_within_slice(nodes=list(lower_slice.nodes), target_log_moneyness=target_log_moneyness)
        if lower_slice.time_to_expiry_years == upper_slice.time_to_expiry_years:
            interpolated_iv = lower_iv
            used_tenor_interpolation = False
        else:
            upper_iv = self._interpolate_within_slice(nodes=list(upper_slice.nodes), target_log_moneyness=target_log_moneyness)
            tenor_weight = (tenor - lower_slice.time_to_expiry_years) / (upper_slice.time_to_expiry_years - lower_slice.time_to_expiry_years)
            interpolated_iv = lower_iv + tenor_weight * (upper_iv - lower_iv)
            used_tenor_interpolation = True
        return IVSurfaceQueryResult(
            underlying_symbol=query.underlying_symbol,
            option_type=query.normalized_option_type,
            strike_price=query.strike_price,
            time_to_expiry_years=tenor,
            reference_spot_price=query.reference_spot_price,
            interpolated_implied_volatility=interpolated_iv,
            forward_price=forward_price,
            log_moneyness=target_log_moneyness,
            lower_tenor=lower_slice.time_to_expiry_years,
            upper_tenor=upper_slice.time_to_expiry_years,
            used_tenor_interpolation=used_tenor_interpolation,
            valuation_timestamp=snapshot.valuation_timestamp,
        )

    def _filter_points(
        self,
        *,
        points: list[IVSurfacePoint],
        valuation_timestamp: datetime | None,
    ) -> list[IVSurfacePoint]:
        filtered: list[IVSurfacePoint] = []
        normalized_valuation_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else None
        for point in points:
            numeric_values = [
                point.strike_price,
                point.spot_price,
                point.implied_volatility,
                point.time_to_expiry_years,
                point.risk_free_rate,
                point.dividend_yield,
            ]
            if not all(isfinite(value) for value in numeric_values):
                continue
            if point.implied_volatility < self._config.minimum_implied_volatility:
                continue
            if point.implied_volatility > self._config.maximum_implied_volatility:
                continue
            if point.time_to_expiry_years < self._config.minimum_time_to_expiry_years:
                continue
            forward_price = point.spot_price * exp((point.risk_free_rate - point.dividend_yield) * point.time_to_expiry_years)
            if forward_price <= 0.0:
                continue
            log_moneyness = log(point.strike_price / forward_price)
            if abs(log_moneyness) > self._config.maximum_log_moneyness_abs:
                continue
            if (
                normalized_valuation_timestamp is not None
                and point.observed_at is not None
                and self._config.stale_snapshot_max_age_seconds is not None
            ):
                age_seconds = (normalized_valuation_timestamp - _ensure_utc(point.observed_at)).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    continue
            filtered.append(point)
        return filtered

    def _deduplicate_and_sort_nodes(self, *, nodes: list[SurfaceNode]) -> list[SurfaceNode]:
        grouped_by_strike: dict[float, list[SurfaceNode]] = {}
        for node in nodes:
            grouped_by_strike.setdefault(node.strike_price, []).append(node)
        deduplicated: list[SurfaceNode] = []
        for strike, strike_nodes in grouped_by_strike.items():
            if len(strike_nodes) == 1:
                deduplicated.append(strike_nodes[0])
                continue
            chosen = max(
                strike_nodes,
                key=lambda item: (
                    0 if item.observed_at is None else item.observed_at.timestamp(),
                    item.open_interest if item.open_interest is not None else -1,
                    item.volume if item.volume is not None else -1,
                ),
            )
            deduplicated.append(chosen)
        deduplicated.sort(key=lambda item: item.log_moneyness)
        return deduplicated

    def _interpolate_within_slice(self, *, nodes: list[SurfaceNode], target_log_moneyness: float) -> float:
        if len(nodes) < 2:
            raise IVSurfaceComputationError("at least two nodes are required for slice interpolation")
        domain_min = nodes[0].log_moneyness
        domain_max = nodes[-1].log_moneyness
        if target_log_moneyness <= domain_min:
            if self._config.interpolation_clamp_to_domain:
                return nodes[0].implied_volatility
            raise IVSurfaceComputationError("target log moneyness lies below slice domain")
        if target_log_moneyness >= domain_max:
            if self._config.interpolation_clamp_to_domain:
                return nodes[-1].implied_volatility
            raise IVSurfaceComputationError("target log moneyness lies above slice domain")
        left = nodes[0]
        for right in nodes[1:]:
            if target_log_moneyness <= right.log_moneyness:
                x0 = left.log_moneyness
                x1 = right.log_moneyness
                if abs(x1 - x0) <= self._config.minimum_strike_gap:
                    return 0.5 * (left.implied_volatility + right.implied_volatility)
                weight = (target_log_moneyness - x0) / (x1 - x0)
                return left.implied_volatility + weight * (right.implied_volatility - left.implied_volatility)
            left = right
        return nodes[-1].implied_volatility

    def _estimate_slope(self, *, nodes: list[SurfaceNode], target_log_moneyness: float) -> float:
        lower_node, upper_node = self._neighbor_pair(nodes=nodes, target_log_moneyness=target_log_moneyness)
        denominator = upper_node.log_moneyness - lower_node.log_moneyness
        if abs(denominator) <= self._config.minimum_strike_gap:
            return 0.0
        return (upper_node.implied_volatility - lower_node.implied_volatility) / denominator

    def _estimate_convexity(self, *, nodes: list[SurfaceNode], target_log_moneyness: float) -> float:
        if len(nodes) < 3:
            return 0.0
        nearest_index = min(range(len(nodes)), key=lambda idx: abs(nodes[idx].log_moneyness - target_log_moneyness))
        if nearest_index == 0:
            triplet = nodes[0:3]
        elif nearest_index == len(nodes) - 1:
            triplet = nodes[-3:]
        else:
            triplet = nodes[nearest_index - 1 : nearest_index + 2]
        x0, x1, x2 = triplet[0].log_moneyness, triplet[1].log_moneyness, triplet[2].log_moneyness
        y0, y1, y2 = triplet[0].implied_volatility, triplet[1].implied_volatility, triplet[2].implied_volatility
        denom_left = x1 - x0
        denom_right = x2 - x1
        denom_total = x2 - x0
        if abs(denom_left) <= self._config.minimum_strike_gap or abs(denom_right) <= self._config.minimum_strike_gap or abs(denom_total) <= self._config.minimum_strike_gap:
            return 0.0
        slope_left = (y1 - y0) / denom_left
        slope_right = (y2 - y1) / denom_right
        return 2.0 * (slope_right - slope_left) / denom_total

    def _neighbor_pair(self, *, nodes: list[SurfaceNode], target_log_moneyness: float) -> tuple[SurfaceNode, SurfaceNode]:
        if target_log_moneyness <= nodes[0].log_moneyness:
            return nodes[0], nodes[1]
        if target_log_moneyness >= nodes[-1].log_moneyness:
            return nodes[-2], nodes[-1]
        left = nodes[0]
        for right in nodes[1:]:
            if target_log_moneyness <= right.log_moneyness:
                return left, right
            left = right
        return nodes[-2], nodes[-1]

    def _locate_tenor_bracket(self, *, slices: tuple[ExpirySlice, ...], target_tenor: float) -> tuple[ExpirySlice, ExpirySlice]:
        if target_tenor <= slices[0].time_to_expiry_years:
            if self._config.interpolation_clamp_to_domain:
                return slices[0], slices[0]
            raise IVSurfaceComputationError("target tenor lies below surface domain")
        if target_tenor >= slices[-1].time_to_expiry_years:
            if self._config.interpolation_clamp_to_domain:
                return slices[-1], slices[-1]
            raise IVSurfaceComputationError("target tenor lies above surface domain")
        lower = slices[0]
        for upper in slices[1:]:
            if target_tenor <= upper.time_to_expiry_years:
                return lower, upper
            lower = upper
        return slices[-1], slices[-1]

    def _infer_snapshot_timestamp(self, points: list[IVSurfacePoint]) -> datetime | None:
        timestamps = [_ensure_utc(point.observed_at) for point in points if point.observed_at is not None]
        if not timestamps:
            return None
        epoch_values = [timestamp.timestamp() for timestamp in timestamps]
        return datetime.fromtimestamp(median(epoch_values), tz=timezone.utc)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ExpirySlice",
    "IVSurfaceComputationError",
    "IVSurfaceEngine",
    "IVSurfaceEngineConfig",
    "IVSurfaceEngineError",
    "IVSurfaceEngineState",
    "IVSurfaceInputError",
    "IVSurfacePoint",
    "IVSurfaceQuery",
    "IVSurfaceQueryResult",
    "IVSurfaceSnapshot",
    "SurfaceNode",
]
