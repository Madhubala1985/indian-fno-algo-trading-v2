# FILE: platform_core/options/options_context/builder.py
"""
OptionsContextBuilder: thin orchestrator that coordinates the pipeline.

Responsibilities:
  1. Validate inputs.
  2. Extract _ContractData from OptionChainSnapshot objects.
  3. Delegate Greek resolution to GreekResolver.
  4. Group contracts by expiry and drive EngineRunner.
  5. Build IV surfaces across all expiries.
  6. Assemble StrikeCandidates via StrikeCandidateFactory.
  7. Derive convenience fields and return OptionsContext.

No analytics logic lives here. All computation is delegated.
"""
from __future__ import annotations

from datetime import date, datetime
from math import isfinite

from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
)
from platform_core.options.oi_analytics import OIAnalyticsSnapshot
from platform_core.options.gamma_exposure import GammaExposureSnapshot
from platform_core.options.iv_surface_engine import IVSurfaceSnapshot
from platform_core.options.dealer_hedging import DealerHedgingSnapshot
from platform_core.options.premium_behavior import PremiumBehaviorSnapshot

from platform_core.options.options_context.exceptions import OptionsContextInputError
from platform_core.options.options_context.models import (
    OptionsContextBuilderConfig,
    OptionsContext,
)
from platform_core.options.options_context.helpers import (
    _ContractData,
    CalendarDaysTTEProvider,
    TimeToExpiryProvider,
    _optional_decimal_to_float,
    _derive_atm_iv,
    _find_atm_strike,
    _ensure_utc,
    _utc_now,
)
from platform_core.options.options_context.greek_resolver import GreekResolver
from platform_core.options.options_context.engine_runner import EngineRunner
from platform_core.options.options_context.strike_candidate_factory import StrikeCandidateFactory


class OptionsContextBuilder:
    """
    Orchestrates the options analytics pipeline from raw chain data to a
    unified OptionsContext ready for StrikeSelectionEngine.

    One instance can be reused across multiple build() calls. Each call
    produces a fresh OptionsContext; no state persists between calls.

    Usage:
        builder = OptionsContextBuilder()
        context = builder.build(
            snapshots=[weekly_chain, monthly_chain],
            spot_price=52400.0,
            lot_size=15,
        )
        result = strike_engine.select(
            bias="LONG",
            candidates=list(context.strike_candidates),
            oi_snapshot=context.oi_snapshot,
            gex_snapshot=context.gex_snapshot,
            atm_iv=context.atm_iv,
            valuation_timestamp=context.valuation_timestamp,
        )
    """

    def __init__(
        self,
        *,
        config: OptionsContextBuilderConfig | None = None,
        tte_provider: TimeToExpiryProvider | None = None,
    ) -> None:
        self._config = config or OptionsContextBuilderConfig()
        self._tte = tte_provider or CalendarDaysTTEProvider()
        self._resolver = GreekResolver(self._config)
        self._runner = EngineRunner(self._config)
        self._candidates = StrikeCandidateFactory()

    @property
    def config(self) -> OptionsContextBuilderConfig:
        return self._config

    def build(
        self,
        *,
        snapshots: list[OptionChainSnapshot],
        spot_price: float,
        lot_size: int,
        valuation_timestamp: datetime | None = None,
        change_in_oi: dict[str, int] | None = None,
    ) -> OptionsContext:
        """
        Build a complete OptionsContext from one or more expiry snapshots.

        Parameters
        ----------
        snapshots:
            One or more OptionChainSnapshot objects (one per expiry).
            All must share the same underlying_symbol.
        spot_price:
            Current index spot price. Required by GEX, Dealer, Premium engines.
        lot_size:
            Lot size for the underlying (from instrument registry / config).
        valuation_timestamp:
            Causal timestamp for this build pass. Defaults to now().
        change_in_oi:
            Optional {contract_symbol: change_in_oi} from prior snapshot.
            When absent, all contracts default to 0 (neutral OI momentum).
        """
        self._validate(snapshots, spot_price, lot_size)
        underlying = snapshots[0].underlying_symbol
        effective_ts = _ensure_utc(valuation_timestamp) if valuation_timestamp else _utc_now()
        change_lookup: dict[str, int] = change_in_oi or {}
        warnings: list[str] = []

        # ── Extract and normalise all contracts ────────────────────────────────
        all_contracts: list[_ContractData] = []
        for snap in snapshots:
            tte = self._tte.compute(snap.expiry_date, effective_ts)
            for contract in snap.contracts:
                cd = self._extract(
                    contract=contract,
                    expiry_date=snap.expiry_date,
                    spot_price=spot_price,
                    lot_size=lot_size,
                    tte_years=tte,
                    observed_at=effective_ts,
                    change_lookup=change_lookup,
                )
                all_contracts.append(cd)

        # ── Resolve Greeks for each contract ───────────────────────────────────
        computed_count = chain_count = bsm_vanna_count = 0
        for cd in all_contracts:
            result = self._resolver.resolve(cd, warnings)
            if result == "computed":
                computed_count += 1
            elif result == "chain":
                chain_count += 1
            if cd.supports_vanna:
                bsm_vanna_count += 1

        # ── Group by expiry, sort nearest first ────────────────────────────────
        by_expiry: dict[date, list[_ContractData]] = {}
        for cd in all_contracts:
            by_expiry.setdefault(cd.expiry_date, []).append(cd)
        sorted_expiries = sorted(by_expiry.keys())
        primary_expiry = sorted_expiries[0] if sorted_expiries else None

        # ── Build per-expiry engine snapshots ──────────────────────────────────
        oi_snaps: dict[date, OIAnalyticsSnapshot] = {}
        gex_snaps: dict[date, GammaExposureSnapshot] = {}
        dealer_snaps: dict[date, DealerHedgingSnapshot] = {}
        premium_snaps: dict[date, PremiumBehaviorSnapshot] = {}

        for expiry, contracts in by_expiry.items():
            self._runner.build_oi_snapshot(contracts, expiry, effective_ts, spot_price, oi_snaps, warnings)
            self._runner.build_gex_snapshot(contracts, expiry, effective_ts, gex_snaps, warnings)
            self._runner.build_dealer_snapshot(contracts, expiry, effective_ts, dealer_snaps, warnings)
            self._runner.build_premium_snapshot(contracts, expiry, effective_ts, premium_snaps, warnings)

        # ── Build IV surfaces (cross-expiry, one per option type) ──────────────
        iv_call = self._runner.build_iv_surface(
            all_contracts, underlying, "CALL", effective_ts,
            self._config.min_iv_for_surface, warnings
        )
        iv_put = self._runner.build_iv_surface(
            all_contracts, underlying, "PUT", effective_ts,
            self._config.min_iv_for_surface, warnings
        )

        # ── Build StrikeCandidates ─────────────────────────────────────────────
        candidates = self._candidates.build_candidates(all_contracts)

        # ── Select primary expiry results ──────────────────────────────────────
        oi_snap = oi_snaps.get(primary_expiry) if primary_expiry else None
        gex_snap = gex_snaps.get(primary_expiry) if primary_expiry else None
        dealer_snap = dealer_snaps.get(primary_expiry) if primary_expiry else None
        premium_snap = premium_snaps.get(primary_expiry) if primary_expiry else None

        return OptionsContext(
            underlying_symbol=underlying,
            spot_price=spot_price,
            lot_size=lot_size,
            valuation_timestamp=effective_ts,
            primary_expiry=primary_expiry,
            oi_snapshot=oi_snap,
            gex_snapshot=gex_snap,
            iv_surface_call=iv_call,
            iv_surface_put=iv_put,
            dealer_hedging_snapshot=dealer_snap,
            premium_behavior_snapshot=premium_snap,
            atm_iv=_derive_atm_iv(iv_call),
            atm_strike=_find_atm_strike(all_contracts, spot_price),
            pcr=oi_snap.put_call_ratio_open_interest if oi_snap else None,
            max_pain=oi_snap.max_pain_strike if oi_snap else None,
            gamma_regime=gex_snap.net_gamma_regime if gex_snap else None,
            gamma_flip_strike=gex_snap.gamma_flip_strike if gex_snap else None,
            vanna_is_bsm=(bsm_vanna_count > 0),
            charm_unavailable=True,
            strike_candidates=candidates,
            snapshots_processed=len(snapshots),
            contracts_processed=len(all_contracts),
            contracts_with_computed_greeks=computed_count,
            contracts_with_chain_greeks=chain_count,
            contracts_with_bsm_vanna=bsm_vanna_count,
            expiries_processed=tuple(e.isoformat() for e in sorted_expiries),
            build_warnings=tuple(warnings),
            metadata={
                "underlying": underlying,
                "expiry_count": len(sorted_expiries),
                "strike_candidates_count": len(candidates),
                "primary_expiry": primary_expiry.isoformat() if primary_expiry else None,
                "atm_iv_pct": round(_derive_atm_iv(iv_call) * 100, 2)
                              if _derive_atm_iv(iv_call) is not None else None,
                "vanna_is_bsm": bsm_vanna_count > 0,
                "charm_unavailable": True,
            },
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _validate(
        snapshots: list[OptionChainSnapshot],
        spot_price: float,
        lot_size: int,
    ) -> None:
        if not snapshots:
            raise OptionsContextInputError("snapshots must be non-empty")
        if spot_price <= 0.0 or not isfinite(spot_price):
            raise OptionsContextInputError("spot_price must be finite and positive")
        if lot_size <= 0:
            raise OptionsContextInputError("lot_size must be positive")
        underlying = snapshots[0].underlying_symbol
        for snap in snapshots:
            if snap.underlying_symbol != underlying:
                raise OptionsContextInputError(
                    "all snapshots must share the same underlying_symbol"
                )

    @staticmethod
    def _extract(
        *,
        contract: OptionContractSnapshot,
        expiry_date: date,
        spot_price: float,
        lot_size: int,
        tte_years: float,
        observed_at: datetime,
        change_lookup: dict[str, int],
    ) -> _ContractData:
        iv = _optional_decimal_to_float(contract.implied_volatility)
        ltp = _optional_decimal_to_float(contract.last_traded_price)
        bid = _optional_decimal_to_float(contract.bid_price)
        ask = _optional_decimal_to_float(contract.ask_price)
        chain_delta = _optional_decimal_to_float(contract.greeks.delta)
        chain_gamma = _optional_decimal_to_float(contract.greeks.gamma)
        chain_theta = _optional_decimal_to_float(contract.greeks.theta)
        chain_vega = _optional_decimal_to_float(contract.greeks.vega)

        # Establish initial greek_source from chain data presence
        initial_source = (
            "chain"
            if (chain_delta is not None and chain_gamma is not None)
            else "none"
        )

        return _ContractData(
            underlying_symbol=contract.underlying_symbol,
            expiry_date=expiry_date,
            contract_symbol=contract.contract_symbol,
            strike_price=float(contract.strike_price),
            option_type=contract.option_side,
            spot_price=spot_price,
            lot_size=lot_size,
            tte_years=tte_years,
            observed_at=observed_at,
            ltp=ltp,
            oi=contract.open_interest,
            change_oi=change_lookup.get(contract.contract_symbol, 0),
            volume=contract.traded_volume or 0,
            iv=iv,
            bid=bid,
            ask=ask,
            delta=chain_delta,
            gamma=chain_gamma,
            theta=chain_theta,
            vega=chain_vega,
            vanna=None,
            charm=None,
            supports_vanna=False,
            supports_charm=False,
            greek_source=initial_source,
        )


__all__ = ["OptionsContextBuilder"]
