# FILE: platform_core/options/options_context/engine_runner.py
"""
EngineRunner: drives all five options analytics engines from normalised
_ContractData lists.

Each public method accepts a pre-filtered contract list for one expiry and
writes the resulting snapshot into the caller-supplied output dict. Engine
failures are caught and appended to warnings; execution continues.

Dealer hedging note:
  Only contracts where cd.supports_vanna=True are included in dealer
  analytics. This ensures vanna contributions are real BSM values. Charm
  is always 0.0 (GreeksEngine limitation) and is passed as 0.0 to satisfy
  DealerGreekPoint's required float field — this is documented in
  OptionsContext.charm_unavailable.
"""
from __future__ import annotations

from datetime import date, datetime

from platform_core.options.oi_analytics import (
    OIAnalyticsEngine, OIAnalyticsConfig, OIContractPoint, OIAnalyticsSnapshot,
    OIAnalyticsError,
)
from platform_core.options.gamma_exposure import (
    GammaExposureEngine, GammaExposureConfig, GammaExposurePoint, GammaExposureSnapshot,
    GammaExposureError,
)
from platform_core.options.iv_surface_engine import (
    IVSurfaceEngine, IVSurfaceEngineConfig, IVSurfacePoint, IVSurfaceSnapshot,
    IVSurfaceEngineError,
)
from platform_core.options.dealer_hedging import (
    DealerHedgingEngine, DealerHedgingConfig, DealerGreekPoint, DealerHedgingSnapshot,
    DealerHedgingError,
)
from platform_core.options.premium_behavior import (
    PremiumBehaviorEngine, PremiumBehaviorConfig, PremiumBehaviorPoint, PremiumBehaviorSnapshot,
    PremiumBehaviorError,
)
from platform_core.options.options_context.helpers import _ContractData, _has_full_greeks
from platform_core.options.options_context.models import OptionsContextBuilderConfig


class EngineRunner:
    """
    Creates and owns instances of all five analytics engines.

    One EngineRunner is created per OptionsContextBuilder instance and
    reused across build() calls. Engine state is overwritten on each call.
    """

    def __init__(self, config: OptionsContextBuilderConfig) -> None:
        cfg = config
        self._risk_free_rate = cfg.risk_free_rate
        self._dividend_yield = cfg.dividend_yield
        self._oi_engine = OIAnalyticsEngine(
            config=OIAnalyticsConfig(concentration_top_n=5, support_resistance_top_n=5)
        )
        self._gex_engine = GammaExposureEngine(
            config=GammaExposureConfig(
                default_call_dealer_sign=cfg.gex_call_dealer_sign,
                default_put_dealer_sign=cfg.gex_put_dealer_sign,
                wall_top_n=5,
            )
        )
        self._iv_engine = IVSurfaceEngine(
            config=IVSurfaceEngineConfig(
                minimum_points_per_expiry=cfg.iv_surface_min_points_per_expiry,
                minimum_implied_volatility=cfg.min_iv_for_surface,
                maximum_implied_volatility=cfg.max_iv_for_surface,
                interpolation_clamp_to_domain=True,
            )
        )
        self._dealer_engine = DealerHedgingEngine(
            config=DealerHedgingConfig(
                default_call_dealer_sign=cfg.gex_call_dealer_sign,
                default_put_dealer_sign=cfg.gex_put_dealer_sign,
            )
        )
        self._premium_engine = PremiumBehaviorEngine(config=PremiumBehaviorConfig())

    # ── Per-expiry runners ─────────────────────────────────────────────────────

    def build_oi_snapshot(
        self,
        contracts: list[_ContractData],
        expiry: date,
        ts: datetime,
        spot: float,
        output: dict[date, OIAnalyticsSnapshot],
        warnings: list[str],
    ) -> None:
        points = [
            OIContractPoint(
                underlying_symbol=cd.underlying_symbol,
                expiry_date=cd.expiry_date,
                strike_price=cd.strike_price,
                option_type=cd.option_type,
                open_interest=cd.oi,
                change_in_open_interest=cd.change_oi,
                volume=cd.volume,
                implied_volatility=cd.iv,
                last_price=cd.ltp,
                observed_at=cd.observed_at,
            )
            for cd in contracts
            if cd.oi is not None and cd.oi >= 0
        ]
        if not points:
            warnings.append(f"OI analytics skipped for {expiry}: no valid OI data")
            return
        try:
            output[expiry] = self._oi_engine.build_snapshot(
                points=points, spot_price=spot, valuation_timestamp=ts
            )
        except (OIAnalyticsError, ValueError) as exc:
            warnings.append(f"OI analytics failed for {expiry}: {exc}")

    def build_gex_snapshot(
        self,
        contracts: list[_ContractData],
        expiry: date,
        ts: datetime,
        output: dict[date, GammaExposureSnapshot],
        warnings: list[str],
    ) -> None:
        points = [
            GammaExposurePoint(
                underlying_symbol=cd.underlying_symbol,
                expiry_date=cd.expiry_date,
                option_symbol=cd.contract_symbol,
                option_type=cd.option_type,
                strike_price=cd.strike_price,
                spot_price=cd.spot_price,
                lot_size=cd.lot_size,
                open_interest=cd.oi,
                change_in_open_interest=cd.change_oi,
                volume=cd.volume,
                time_to_expiry_years=cd.tte_years,
                gamma=cd.gamma,
                implied_volatility=cd.iv,
                observed_at=cd.observed_at,
            )
            for cd in contracts
            if cd.gamma is not None and cd.oi is not None and cd.oi > 0
        ]
        if not points:
            warnings.append(f"GEX skipped for {expiry}: no contracts with gamma and OI")
            return
        try:
            output[expiry] = self._gex_engine.build_snapshot(
                points=points, valuation_timestamp=ts
            )
        except (GammaExposureError, ValueError) as exc:
            warnings.append(f"GEX failed for {expiry}: {exc}")

    def build_dealer_snapshot(
        self,
        contracts: list[_ContractData],
        expiry: date,
        ts: datetime,
        output: dict[date, DealerHedgingSnapshot],
        warnings: list[str],
    ) -> None:
        """
        Dealer analytics only includes contracts with real BSM vanna
        (cd.supports_vanna=True). Contracts with only chain Greeks are
        excluded unless BSM vanna was supplemented from IV.

        charm=0.0 is passed to satisfy the DealerGreekPoint float requirement.
        This is a documented limitation tracked by OptionsContext.charm_unavailable.
        """
        points = [
            DealerGreekPoint(
                underlying_symbol=cd.underlying_symbol,
                expiry_date=cd.expiry_date,
                option_symbol=cd.contract_symbol,
                option_type=cd.option_type,
                strike_price=cd.strike_price,
                spot_price=cd.spot_price,
                lot_size=cd.lot_size,
                open_interest=cd.oi,
                change_in_open_interest=cd.change_oi,
                volume=cd.volume,
                time_to_expiry_years=cd.tte_years,
                implied_volatility=cd.iv,
                delta=cd.delta,
                gamma=cd.gamma,
                vanna=cd.vanna,       # real BSM vanna (supports_vanna=True guard below)
                charm=0.0,            # unavailable — GreeksEngine limitation
                observed_at=cd.observed_at,
            )
            for cd in contracts
            if (
                _has_full_greeks(cd)
                and cd.supports_vanna          # real vanna only
                and cd.vanna is not None
                and cd.iv is not None
                and cd.oi is not None and cd.oi > 0
            )
        ]
        if not points:
            warnings.append(
                f"Dealer hedging skipped for {expiry}: "
                "no contracts with full Greeks and real BSM vanna"
            )
            return
        try:
            output[expiry] = self._dealer_engine.build_snapshot(
                points=points, valuation_timestamp=ts
            )
        except (DealerHedgingError, ValueError) as exc:
            warnings.append(f"Dealer hedging failed for {expiry}: {exc}")

    def build_premium_snapshot(
        self,
        contracts: list[_ContractData],
        expiry: date,
        ts: datetime,
        output: dict[date, PremiumBehaviorSnapshot],
        warnings: list[str],
    ) -> None:
        points = [
            PremiumBehaviorPoint(
                underlying_symbol=cd.underlying_symbol,
                expiry_date=cd.expiry_date,
                option_symbol=cd.contract_symbol,
                option_type=cd.option_type,
                strike_price=cd.strike_price,
                spot_price=cd.spot_price,
                lot_size=cd.lot_size,
                last_traded_price=cd.ltp,
                open_interest=cd.oi,
                change_in_open_interest=cd.change_oi,
                volume=cd.volume,
                time_to_expiry_years=cd.tte_years,
                implied_volatility=cd.iv,
                delta=cd.delta,
                gamma=cd.gamma,
                theta=cd.theta,
                vega=cd.vega,
                observed_at=cd.observed_at,
            )
            for cd in contracts
            if (
                _has_full_greeks(cd)
                and cd.iv is not None
                and cd.ltp is not None and cd.ltp >= 0.0
                and cd.oi is not None and cd.oi >= 0
            )
        ]
        if not points:
            warnings.append(f"Premium behavior skipped for {expiry}: no complete data")
            return
        try:
            output[expiry] = self._premium_engine.build_snapshot(
                points=points, valuation_timestamp=ts
            )
        except (PremiumBehaviorError, ValueError) as exc:
            warnings.append(f"Premium behavior failed for {expiry}: {exc}")

    # ── IV surface (cross-expiry) ──────────────────────────────────────────────

    def build_iv_surface(
        self,
        all_contracts: list[_ContractData],
        underlying: str,
        option_type: str,
        ts: datetime,
        min_iv: float,
        warnings: list[str],
    ) -> IVSurfaceSnapshot | None:
        points = [
            IVSurfacePoint(
                instrument_id=cd.contract_symbol,
                underlying_symbol=cd.underlying_symbol,
                option_symbol=cd.contract_symbol,
                expiry_date=cd.expiry_date,
                option_type=cd.option_type,
                strike_price=cd.strike_price,
                spot_price=cd.spot_price,
                implied_volatility=cd.iv,
                time_to_expiry_years=cd.tte_years,
                risk_free_rate=self._risk_free_rate,
                dividend_yield=self._dividend_yield,
                observed_at=cd.observed_at,
                open_interest=cd.oi,
                volume=cd.volume,
            )
            for cd in all_contracts
            if (
                cd.option_type == option_type
                and cd.iv is not None
                and cd.iv >= min_iv
                and cd.tte_years > 0.0
            )
        ]
        if not points:
            warnings.append(
                f"IV surface ({option_type}) skipped: no valid IV points"
            )
            return None
        try:
            return self._iv_engine.build_surface(points=points, valuation_timestamp=ts)
        except (IVSurfaceEngineError, ValueError) as exc:
            warnings.append(f"IV surface ({option_type}) failed: {exc}")
            return None


__all__ = ["EngineRunner"]
