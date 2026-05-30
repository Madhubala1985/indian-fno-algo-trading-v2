# FILE: platform_core/options/options_context/models.py
"""
OptionsContextBuilderConfig and OptionsContext dataclasses.

OptionsContext is the unified output of the options analytics pipeline.
It carries the outputs of all five analytics engines derived from one or
more OptionChainSnapshot objects, convenience derived fields, and a
pre-built tuple of StrikeCandidate objects for direct consumption by
StrikeSelectionEngine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from platform_core.options.oi_analytics import OIAnalyticsSnapshot
    from platform_core.options.gamma_exposure import GammaExposureSnapshot
    from platform_core.options.iv_surface_engine import IVSurfaceSnapshot
    from platform_core.options.dealer_hedging import DealerHedgingSnapshot
    from platform_core.options.premium_behavior import PremiumBehaviorSnapshot
    from platform_core.options.strike_selection import StrikeCandidate


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class OptionsContextBuilderConfig:
    """
    Static parameters for the OptionsContextBuilder.

    Runtime inputs (spot_price, lot_size, snapshots) are passed to build(),
    not held here. Only market constants and engine tuning belong here.
    """

    risk_free_rate: float = 0.068        # RBI repo rate proxy (annualised)
    dividend_yield: float = 0.0

    # Greek computation controls
    compute_missing_greeks: bool = True  # use GreeksEngine when chain lacks Greeks
    min_iv_for_greeks: float = 0.01      # skip Greek computation if IV < 1%

    # IV surface filters
    min_iv_for_surface: float = 0.01
    max_iv_for_surface: float = 5.00
    iv_surface_min_points_per_expiry: int = 2

    # GEX and Dealer — dealer sign assumptions
    gex_call_dealer_sign: int = -1       # dealers short calls → long gamma → short delta
    gex_put_dealer_sign: int = 1         # dealers long puts  → short gamma → long delta

    def __post_init__(self) -> None:
        if not isfinite(self.risk_free_rate):
            raise ValueError("risk_free_rate must be finite")
        if not isfinite(self.dividend_yield):
            raise ValueError("dividend_yield must be finite")
        if self.min_iv_for_greeks <= 0.0:
            raise ValueError("min_iv_for_greeks must be positive")
        if self.min_iv_for_surface <= 0.0:
            raise ValueError("min_iv_for_surface must be positive")
        if self.max_iv_for_surface <= self.min_iv_for_surface:
            raise ValueError("max_iv_for_surface must exceed min_iv_for_surface")
        if self.iv_surface_min_points_per_expiry < 2:
            raise ValueError("iv_surface_min_points_per_expiry must be at least 2")
        if self.gex_call_dealer_sign not in {-1, 1}:
            raise ValueError("gex_call_dealer_sign must be -1 or 1")
        if self.gex_put_dealer_sign not in {-1, 1}:
            raise ValueError("gex_put_dealer_sign must be -1 or 1")


@dataclass(frozen=True, slots=True)
class OptionsContext:
    """
    Unified options analytics context for one underlying at one moment.

    Primary expiry is the nearest-dated expiry processed. Its engine
    outputs are surfaced at the top level for direct consumption.

    strike_candidates combines all expiries — pass directly to
    StrikeSelectionEngine.select().

    Greek quality flags:
      vanna_is_bsm  — True when vanna values in DealerHedging are real BSM
                      outputs (contracts without BSM-computed vanna are
                      excluded from dealer analytics).
      charm_unavailable — Always True in the current implementation because
                      GreeksEngine.compute() does not yet calculate charm.
                      Charm contributions in DealerHedging are zero.
    """

    underlying_symbol: str
    spot_price: float
    lot_size: int
    valuation_timestamp: datetime
    primary_expiry: date | None

    # Primary expiry engine outputs
    oi_snapshot: "OIAnalyticsSnapshot | None"
    gex_snapshot: "GammaExposureSnapshot | None"
    iv_surface_call: "IVSurfaceSnapshot | None"
    iv_surface_put: "IVSurfaceSnapshot | None"
    dealer_hedging_snapshot: "DealerHedgingSnapshot | None"
    premium_behavior_snapshot: "PremiumBehaviorSnapshot | None"

    # Convenience derived fields
    atm_iv: float | None
    atm_strike: float | None
    pcr: float | None
    max_pain: float | None
    gamma_regime: str | None
    gamma_flip_strike: float | None

    # Greek quality flags
    vanna_is_bsm: bool          # True when dealer analytics use real BSM vanna
    charm_unavailable: bool     # Always True — GreeksEngine limitation

    # StrikeCandidates for StrikeSelectionEngine (all expiries)
    strike_candidates: "tuple[StrikeCandidate, ...]"

    # Diagnostics
    snapshots_processed: int
    contracts_processed: int
    contracts_with_computed_greeks: int
    contracts_with_chain_greeks: int
    contracts_with_bsm_vanna: int
    expiries_processed: tuple[str, ...]
    build_warnings: tuple[str, ...]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        from platform_core.options.options_context.helpers import _ensure_utc
        return {
            "underlying_symbol": self.underlying_symbol,
            "spot_price": self.spot_price,
            "lot_size": self.lot_size,
            "valuation_timestamp": _ensure_utc(self.valuation_timestamp).isoformat(),
            "primary_expiry": self.primary_expiry.isoformat() if self.primary_expiry else None,
            "oi_snapshot_available": self.oi_snapshot is not None,
            "gex_snapshot_available": self.gex_snapshot is not None,
            "iv_surface_call_available": self.iv_surface_call is not None,
            "iv_surface_put_available": self.iv_surface_put is not None,
            "dealer_hedging_available": self.dealer_hedging_snapshot is not None,
            "premium_behavior_available": self.premium_behavior_snapshot is not None,
            "atm_iv": self.atm_iv,
            "atm_strike": self.atm_strike,
            "pcr": self.pcr,
            "max_pain": self.max_pain,
            "gamma_regime": self.gamma_regime,
            "gamma_flip_strike": self.gamma_flip_strike,
            "vanna_is_bsm": self.vanna_is_bsm,
            "charm_unavailable": self.charm_unavailable,
            "strike_candidates_count": len(self.strike_candidates),
            "snapshots_processed": self.snapshots_processed,
            "contracts_processed": self.contracts_processed,
            "contracts_with_computed_greeks": self.contracts_with_computed_greeks,
            "contracts_with_chain_greeks": self.contracts_with_chain_greeks,
            "contracts_with_bsm_vanna": self.contracts_with_bsm_vanna,
            "expiries_processed": list(self.expiries_processed),
            "build_warnings": list(self.build_warnings),
            "metadata": self.metadata,
        }


__all__ = ["OptionsContext", "OptionsContextBuilderConfig"]
