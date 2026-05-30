# FILE: platform_core/options/options_context/helpers.py
"""
Internal working types, time-to-expiry provider, and utility functions.

_ContractData is a mutable scratch object used during a single build()
pass. It is never exported.

TimeToExpiryProvider is a Protocol allowing calendar-aware TTE
implementations to be substituted without changing engine code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from math import fabs, isfinite
from typing import Protocol, runtime_checkable

from platform_core.options.iv_surface_engine import IVSurfaceSnapshot


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


# ── Time-to-expiry provider ────────────────────────────────────────────────────

@runtime_checkable
class TimeToExpiryProvider(Protocol):
    """
    Protocol for time-to-expiry calculation.

    The architecture allows future implementations based on:
    - Exchange trading calendars
    - Holiday-adjusted session counts
    - NSE/BSE-specific expiry conventions
    """
    def compute(self, expiry_date: date, valuation_timestamp: datetime) -> float:
        """Return time to expiry in years (positive float, minimum 1-day floor)."""
        ...


class CalendarDaysTTEProvider:
    """
    Calendar-day TTE: (expiry_date - today) / 365.25.

    This is the standard implementation. Replace with a trading-calendar
    provider when session-aware TTE is required.
    """

    def compute(self, expiry_date: date, valuation_timestamp: datetime) -> float:
        today = valuation_timestamp.date() if valuation_timestamp.tzinfo else valuation_timestamp.date()
        days = max((expiry_date - today).days, 1)
        return days / 365.25


# ── Internal contract working data ─────────────────────────────────────────────

@dataclass
class _ContractData:
    """
    Mutable scratch object for one option contract during a build() pass.
    Not exported. Fields are populated incrementally by GreekResolver.

    Greek availability flags:
      supports_vanna — True when vanna is a real BSM value (not 0.0 fill).
      supports_charm — False in all current cases; GreeksEngine does not yet
                       compute charm. The field is retained for future use
                       and to prevent callers from assuming 0.0 is real.
    """

    underlying_symbol: str
    expiry_date: date
    contract_symbol: str
    strike_price: float
    option_type: str           # "CALL" or "PUT"
    spot_price: float
    lot_size: int
    tte_years: float
    observed_at: datetime

    # Market data — may be None when chain is incomplete
    ltp: float | None
    oi: int | None
    change_oi: int
    volume: int
    iv: float | None
    bid: float | None
    ask: float | None

    # Greeks — populated by GreekResolver
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    vanna: float | None = None     # None = unavailable (not 0.0)
    charm: float | None = None     # None = unavailable (not 0.0)

    # Quality flags — set by GreekResolver
    supports_vanna: bool = False   # True only when vanna is real BSM output
    supports_charm: bool = False   # Always False — GreeksEngine limitation
    greek_source: str = "none"     # "chain" | "computed" | "none"


# ── Pure utility functions ─────────────────────────────────────────────────────

def _has_full_greeks(cd: _ContractData) -> bool:
    """Return True when a contract has at minimum delta, gamma, theta, and vega."""
    return (
        cd.delta is not None
        and cd.gamma is not None
        and cd.theta is not None
        and cd.vega is not None
    )


def _optional_decimal_to_float(value: object) -> float | None:
    """Convert Decimal | None to float | None. Returns None for non-finite."""
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _derive_atm_iv(iv_surface_call: IVSurfaceSnapshot | None) -> float | None:
    """Extract ATM IV from the CALL surface nearest expiry slice."""
    if iv_surface_call is None or not iv_surface_call.slices:
        return None
    return iv_surface_call.slices[0].atm_implied_volatility


def _find_atm_strike(contracts: list[_ContractData], spot: float) -> float | None:
    """Return the strike price closest to spot across all processed contracts."""
    valid = [cd for cd in contracts if cd.strike_price > 0.0]
    if not valid:
        return None
    return min(valid, key=lambda cd: fabs(cd.strike_price - spot)).strike_price


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "CalendarDaysTTEProvider",
    "TimeToExpiryProvider",
    "_ContractData",
    "_derive_atm_iv",
    "_ensure_utc",
    "_find_atm_strike",
    "_has_full_greeks",
    "_optional_decimal_to_float",
    "_utc_now",
]
