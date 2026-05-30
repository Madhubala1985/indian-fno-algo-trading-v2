# FILE: platform_core/options/oi_analytics.py
"""
platform_core.options.oi_analytics
═══════════════════════════════════════════════════════════════════════
Production-grade open-interest analytics engine for Indian index options.

This module computes deterministic, replay-safe option-chain open-interest
analytics from causally observed snapshots. It provides strike-wise OI summaries,
change-in-OI analytics, put-call metrics, concentration statistics, max-pain,
support/resistance clustering, and expiry-aware positioning diagnostics without
embedding execution or trading-decision logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timezone
from math import isfinite


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class OIAnalyticsError(RuntimeError):
    """Base exception for OI analytics failures."""


class OIAnalyticsInputError(OIAnalyticsError):
    """Raised when option-chain OI inputs are invalid."""


class OIAnalyticsComputationError(OIAnalyticsError):
    """Raised when OI analytics cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class OIAnalyticsConfig:
    """Configuration controlling snapshot validation and derived analytics."""

    concentration_top_n: int = 5
    support_resistance_top_n: int = 5
    minimum_strike_positive_gap: float = 1.0e-9
    require_non_negative_change_oi: bool = False
    stale_snapshot_max_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.concentration_top_n <= 0:
            raise ValueError("concentration_top_n must be positive")
        if self.support_resistance_top_n <= 0:
            raise ValueError("support_resistance_top_n must be positive")
        if self.minimum_strike_positive_gap <= 0.0:
            raise ValueError("minimum_strike_positive_gap must be positive")
        if self.stale_snapshot_max_age_seconds is not None and self.stale_snapshot_max_age_seconds <= 0.0:
            raise ValueError("stale_snapshot_max_age_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class OIContractPoint:
    """Single observed option-chain contract row."""

    underlying_symbol: str
    expiry_date: date
    strike_price: float
    option_type: str
    open_interest: int
    change_in_open_interest: int
    volume: int = 0
    implied_volatility: float | None = None
    last_price: float | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        option_type_normalized = self.option_type.upper()
        if option_type_normalized not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if self.strike_price <= 0.0:
            raise ValueError("strike_price must be positive")
        if self.open_interest < 0:
            raise ValueError("open_interest must be non-negative")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.implied_volatility is not None and self.implied_volatility < 0.0:
            raise ValueError("implied_volatility must be non-negative when provided")
        if self.last_price is not None and self.last_price < 0.0:
            raise ValueError("last_price must be non-negative when provided")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "strike_price": self.strike_price,
            "option_type": self.normalized_option_type,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "implied_volatility": self.implied_volatility,
            "last_price": self.last_price,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True, slots=True)
class StrikeOIProfile:
    """Strike-level OI aggregation for calls and puts."""

    strike_price: float
    call_open_interest: int
    put_open_interest: int
    call_change_in_open_interest: int
    put_change_in_open_interest: int
    call_volume: int
    put_volume: int
    net_open_interest: int
    total_open_interest: int
    net_change_in_open_interest: int
    pcr_at_strike: float | None
    call_last_price: float | None
    put_last_price: float | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "call_open_interest": self.call_open_interest,
            "put_open_interest": self.put_open_interest,
            "call_change_in_open_interest": self.call_change_in_open_interest,
            "put_change_in_open_interest": self.put_change_in_open_interest,
            "call_volume": self.call_volume,
            "put_volume": self.put_volume,
            "net_open_interest": self.net_open_interest,
            "total_open_interest": self.total_open_interest,
            "net_change_in_open_interest": self.net_change_in_open_interest,
            "pcr_at_strike": self.pcr_at_strike,
            "call_last_price": self.call_last_price,
            "put_last_price": self.put_last_price,
        }


@dataclass(frozen=True, slots=True)
class OIClusterLevel:
    """Cluster or level extracted from OI concentrations."""

    strike_price: float
    open_interest: int
    change_in_open_interest: int
    volume: int
    option_type: str
    normalized_weight: float
    distance_from_spot_pct: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "strike_price": self.strike_price,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "option_type": self.option_type,
            "normalized_weight": self.normalized_weight,
            "distance_from_spot_pct": self.distance_from_spot_pct,
        }


@dataclass(frozen=True, slots=True)
class OIAnalyticsSnapshot:
    """Comprehensive OI analytics for one underlying-expiry snapshot."""

    underlying_symbol: str
    expiry_date: date
    valuation_timestamp: datetime | None
    spot_price: float
    strike_profiles: tuple[StrikeOIProfile, ...]
    total_call_open_interest: int
    total_put_open_interest: int
    total_call_change_in_open_interest: int
    total_put_change_in_open_interest: int
    total_call_volume: int
    total_put_volume: int
    put_call_ratio_open_interest: float | None
    put_call_ratio_change_in_open_interest: float | None
    put_call_ratio_volume: float | None
    max_pain_strike: float | None
    call_resistance_levels: tuple[OIClusterLevel, ...]
    put_support_levels: tuple[OIClusterLevel, ...]
    highest_total_oi_strike: float | None
    highest_total_oi_value: int
    weighted_call_strike: float | None
    weighted_put_strike: float | None
    weighted_total_strike: float | None
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "spot_price": self.spot_price,
            "strike_profiles": [profile.to_dict() for profile in self.strike_profiles],
            "total_call_open_interest": self.total_call_open_interest,
            "total_put_open_interest": self.total_put_open_interest,
            "total_call_change_in_open_interest": self.total_call_change_in_open_interest,
            "total_put_change_in_open_interest": self.total_put_change_in_open_interest,
            "total_call_volume": self.total_call_volume,
            "total_put_volume": self.total_put_volume,
            "put_call_ratio_open_interest": self.put_call_ratio_open_interest,
            "put_call_ratio_change_in_open_interest": self.put_call_ratio_change_in_open_interest,
            "put_call_ratio_volume": self.put_call_ratio_volume,
            "max_pain_strike": self.max_pain_strike,
            "call_resistance_levels": [level.to_dict() for level in self.call_resistance_levels],
            "put_support_levels": [level.to_dict() for level in self.put_support_levels],
            "highest_total_oi_strike": self.highest_total_oi_strike,
            "highest_total_oi_value": self.highest_total_oi_value,
            "weighted_call_strike": self.weighted_call_strike,
            "weighted_put_strike": self.weighted_put_strike,
            "weighted_total_strike": self.weighted_total_strike,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class OIAnalyticsState:
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


class OIAnalyticsEngine:
    """Replay-safe open-interest analytics engine for Indian index options."""

    def __init__(self, *, config: OIAnalyticsConfig | None = None) -> None:
        self._config = config or OIAnalyticsConfig()
        self._snapshots: dict[tuple[str, date], OIAnalyticsSnapshot] = {}
        self._snapshot_count = 0
        self._last_underlying_symbol: str | None = None
        self._last_expiry_date: date | None = None
        self._last_valuation_timestamp: datetime | None = None

    @property
    def state(self) -> OIAnalyticsState:
        return OIAnalyticsState(
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
        points: list[OIContractPoint],
        spot_price: float,
        valuation_timestamp: datetime | None = None,
    ) -> OIAnalyticsSnapshot:
        if not points:
            raise OIAnalyticsInputError("points must not be empty")
        if not isfinite(spot_price) or spot_price <= 0.0:
            raise OIAnalyticsInputError("spot_price must be finite and positive")
        filtered_points = self._filter_points(points=points, valuation_timestamp=valuation_timestamp)
        if not filtered_points:
            raise OIAnalyticsComputationError("no valid OI points available after filtering")
        underlying_symbol = filtered_points[0].underlying_symbol
        expiry_date = filtered_points[0].expiry_date
        for point in filtered_points:
            if point.underlying_symbol != underlying_symbol:
                raise OIAnalyticsInputError("all points must share the same underlying_symbol")
            if point.expiry_date != expiry_date:
                raise OIAnalyticsInputError("all points must share the same expiry_date")
        strike_profiles = self._build_strike_profiles(points=filtered_points)
        if not strike_profiles:
            raise OIAnalyticsComputationError("no strike profiles were generated")
        total_call_open_interest = sum(profile.call_open_interest for profile in strike_profiles)
        total_put_open_interest = sum(profile.put_open_interest for profile in strike_profiles)
        total_call_change_in_open_interest = sum(profile.call_change_in_open_interest for profile in strike_profiles)
        total_put_change_in_open_interest = sum(profile.put_change_in_open_interest for profile in strike_profiles)
        total_call_volume = sum(profile.call_volume for profile in strike_profiles)
        total_put_volume = sum(profile.put_volume for profile in strike_profiles)
        put_call_ratio_open_interest = self._safe_ratio(total_put_open_interest, total_call_open_interest)
        put_call_ratio_change_in_open_interest = self._safe_ratio(total_put_change_in_open_interest, total_call_change_in_open_interest)
        put_call_ratio_volume = self._safe_ratio(total_put_volume, total_call_volume)
        max_pain_strike = self._compute_max_pain_strike(profiles=strike_profiles)
        highest_total_profile = max(strike_profiles, key=lambda profile: profile.total_open_interest)
        call_resistance_levels = self._extract_levels(
            profiles=strike_profiles,
            spot_price=spot_price,
            option_type="CALL",
            top_n=self._config.support_resistance_top_n,
        )
        put_support_levels = self._extract_levels(
            profiles=strike_profiles,
            spot_price=spot_price,
            option_type="PUT",
            top_n=self._config.support_resistance_top_n,
        )
        snapshot_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else self._infer_snapshot_timestamp(points=filtered_points)
        snapshot = OIAnalyticsSnapshot(
            underlying_symbol=underlying_symbol,
            expiry_date=expiry_date,
            valuation_timestamp=snapshot_timestamp,
            spot_price=spot_price,
            strike_profiles=tuple(strike_profiles),
            total_call_open_interest=total_call_open_interest,
            total_put_open_interest=total_put_open_interest,
            total_call_change_in_open_interest=total_call_change_in_open_interest,
            total_put_change_in_open_interest=total_put_change_in_open_interest,
            total_call_volume=total_call_volume,
            total_put_volume=total_put_volume,
            put_call_ratio_open_interest=put_call_ratio_open_interest,
            put_call_ratio_change_in_open_interest=put_call_ratio_change_in_open_interest,
            put_call_ratio_volume=put_call_ratio_volume,
            max_pain_strike=max_pain_strike,
            call_resistance_levels=tuple(call_resistance_levels),
            put_support_levels=tuple(put_support_levels),
            highest_total_oi_strike=highest_total_profile.strike_price,
            highest_total_oi_value=highest_total_profile.total_open_interest,
            weighted_call_strike=self._weighted_average_strike(strike_profiles, option_type="CALL"),
            weighted_put_strike=self._weighted_average_strike(strike_profiles, option_type="PUT"),
            weighted_total_strike=self._weighted_average_total_strike(strike_profiles),
            metadata={
                "raw_point_count": len(points),
                "filtered_point_count": len(filtered_points),
                "strike_count": len(strike_profiles),
                "concentration_top_n": self._config.concentration_top_n,
                "support_resistance_top_n": self._config.support_resistance_top_n,
                "max_pain_available": max_pain_strike is not None,
            },
        )
        key = (underlying_symbol, expiry_date)
        self._snapshots[key] = snapshot
        self._snapshot_count += 1
        self._last_underlying_symbol = underlying_symbol
        self._last_expiry_date = expiry_date
        self._last_valuation_timestamp = snapshot.valuation_timestamp
        return snapshot

    def get_snapshot(self, *, underlying_symbol: str, expiry_date: date) -> OIAnalyticsSnapshot:
        snapshot = self._snapshots.get((underlying_symbol, expiry_date))
        if snapshot is None:
            raise OIAnalyticsComputationError("requested OI analytics snapshot is not available")
        return snapshot

    def _filter_points(
        self,
        *,
        points: list[OIContractPoint],
        valuation_timestamp: datetime | None,
    ) -> list[OIContractPoint]:
        filtered: list[OIContractPoint] = []
        normalized_valuation_timestamp = _ensure_utc(valuation_timestamp) if valuation_timestamp else None
        for point in points:
            numeric_values = [float(point.open_interest), float(point.change_in_open_interest), float(point.volume), point.strike_price]
            if not all(isfinite(value) for value in numeric_values):
                continue
            if self._config.require_non_negative_change_oi and point.change_in_open_interest < 0:
                continue
            if point.observed_at is not None and normalized_valuation_timestamp is not None and self._config.stale_snapshot_max_age_seconds is not None:
                age_seconds = (normalized_valuation_timestamp - _ensure_utc(point.observed_at)).total_seconds()
                if age_seconds > self._config.stale_snapshot_max_age_seconds:
                    continue
            filtered.append(point)
        return filtered

    def _build_strike_profiles(self, *, points: list[OIContractPoint]) -> list[StrikeOIProfile]:
        grouped: dict[float, dict[str, OIContractPoint]] = {}
        for point in points:
            by_type = grouped.setdefault(point.strike_price, {})
            existing = by_type.get(point.normalized_option_type)
            if existing is None:
                by_type[point.normalized_option_type] = point
                continue
            existing_timestamp = -1.0 if existing.observed_at is None else _ensure_utc(existing.observed_at).timestamp()
            incoming_timestamp = -1.0 if point.observed_at is None else _ensure_utc(point.observed_at).timestamp()
            if incoming_timestamp >= existing_timestamp:
                by_type[point.normalized_option_type] = point
        strike_profiles: list[StrikeOIProfile] = []
        for strike_price in sorted(grouped.keys()):
            by_type = grouped[strike_price]
            call_point = by_type.get("CALL")
            put_point = by_type.get("PUT")
            call_open_interest = 0 if call_point is None else call_point.open_interest
            put_open_interest = 0 if put_point is None else put_point.open_interest
            call_change_in_open_interest = 0 if call_point is None else call_point.change_in_open_interest
            put_change_in_open_interest = 0 if put_point is None else put_point.change_in_open_interest
            call_volume = 0 if call_point is None else call_point.volume
            put_volume = 0 if put_point is None else put_point.volume
            total_open_interest = call_open_interest + put_open_interest
            strike_profiles.append(
                StrikeOIProfile(
                    strike_price=strike_price,
                    call_open_interest=call_open_interest,
                    put_open_interest=put_open_interest,
                    call_change_in_open_interest=call_change_in_open_interest,
                    put_change_in_open_interest=put_change_in_open_interest,
                    call_volume=call_volume,
                    put_volume=put_volume,
                    net_open_interest=put_open_interest - call_open_interest,
                    total_open_interest=total_open_interest,
                    net_change_in_open_interest=put_change_in_open_interest - call_change_in_open_interest,
                    pcr_at_strike=self._safe_ratio(put_open_interest, call_open_interest),
                    call_last_price=None if call_point is None else call_point.last_price,
                    put_last_price=None if put_point is None else put_point.last_price,
                )
            )
        return strike_profiles

    def _extract_levels(
        self,
        *,
        profiles: list[StrikeOIProfile],
        spot_price: float,
        option_type: str,
        top_n: int,
    ) -> list[OIClusterLevel]:
        if option_type not in {"CALL", "PUT"}:
            raise OIAnalyticsInputError("option_type must be CALL or PUT")
        ranked_profiles = sorted(
            profiles,
            key=lambda profile: (
                profile.call_open_interest if option_type == "CALL" else profile.put_open_interest,
                profile.call_change_in_open_interest if option_type == "CALL" else profile.put_change_in_open_interest,
                profile.call_volume if option_type == "CALL" else profile.put_volume,
            ),
            reverse=True,
        )
        selected = ranked_profiles[:top_n]
        total_selected_oi = sum(profile.call_open_interest if option_type == "CALL" else profile.put_open_interest for profile in selected)
        levels: list[OIClusterLevel] = []
        for profile in selected:
            open_interest = profile.call_open_interest if option_type == "CALL" else profile.put_open_interest
            change_in_open_interest = profile.call_change_in_open_interest if option_type == "CALL" else profile.put_change_in_open_interest
            volume = profile.call_volume if option_type == "CALL" else profile.put_volume
            normalized_weight = 0.0 if total_selected_oi <= 0 else open_interest / total_selected_oi
            levels.append(
                OIClusterLevel(
                    strike_price=profile.strike_price,
                    open_interest=open_interest,
                    change_in_open_interest=change_in_open_interest,
                    volume=volume,
                    option_type=option_type,
                    normalized_weight=normalized_weight,
                    distance_from_spot_pct=((profile.strike_price / spot_price) - 1.0) * 100.0,
                )
            )
        return levels

    def _compute_max_pain_strike(self, *, profiles: list[StrikeOIProfile]) -> float | None:
        if not profiles:
            return None
        pain_by_settlement: list[tuple[float, float]] = []
        for settlement_profile in profiles:
            settlement = settlement_profile.strike_price
            total_pain = 0.0
            for profile in profiles:
                call_pain = max(0.0, settlement - profile.strike_price) * float(profile.call_open_interest)
                put_pain = max(0.0, profile.strike_price - settlement) * float(profile.put_open_interest)
                total_pain += call_pain + put_pain
            pain_by_settlement.append((settlement, total_pain))
        return min(pain_by_settlement, key=lambda item: (item[1], abs(item[0])))[0]

    def _weighted_average_strike(self, profiles: list[StrikeOIProfile], *, option_type: str) -> float | None:
        if option_type == "CALL":
            total_oi = sum(profile.call_open_interest for profile in profiles)
            if total_oi <= 0:
                return None
            return sum(profile.strike_price * profile.call_open_interest for profile in profiles) / float(total_oi)
        if option_type == "PUT":
            total_oi = sum(profile.put_open_interest for profile in profiles)
            if total_oi <= 0:
                return None
            return sum(profile.strike_price * profile.put_open_interest for profile in profiles) / float(total_oi)
        raise OIAnalyticsInputError("option_type must be CALL or PUT")

    def _weighted_average_total_strike(self, profiles: list[StrikeOIProfile]) -> float | None:
        total_oi = sum(profile.total_open_interest for profile in profiles)
        if total_oi <= 0:
            return None
        return sum(profile.strike_price * profile.total_open_interest for profile in profiles) / float(total_oi)

    def _safe_ratio(self, numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return numerator / float(denominator)

    def _infer_snapshot_timestamp(self, *, points: list[OIContractPoint]) -> datetime | None:
        timestamps = [_ensure_utc(point.observed_at) for point in points if point.observed_at is not None]
        if not timestamps:
            return None
        return max(timestamps)



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "OIAnalyticsComputationError",
    "OIAnalyticsConfig",
    "OIAnalyticsEngine",
    "OIAnalyticsError",
    "OIAnalyticsInputError",
    "OIAnalyticsSnapshot",
    "OIAnalyticsState",
    "OIClusterLevel",
    "OIContractPoint",
    "StrikeOIProfile",
]
