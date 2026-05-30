"""
scripts/options_context_demo.py
═══════════════════════════════════════════════════════════════════════
End-to-end validation of the full options intelligence pipeline:

    OptionChainSnapshot(s)
        → OptionsContextBuilder
        → OptionsContext
        → StrikeSelectionEngine
        → StrikeRecommendation

Synthesises a realistic BANKNIFTY option chain using
OptionContractSnapshot objects (from the data layer), runs it through
OptionsContextBuilder which automatically invokes all five analytics
engines, then passes the resulting context to StrikeSelectionEngine.

No live data or external dependencies required.

Usage:
    python scripts/options_context_demo.py
"""
from __future__ import annotations

import sys
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from math import exp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
    OptionGreeksSnapshot,
)
from platform_core.options.options_context import (
    OptionsContextBuilder,
    OptionsContextBuilderConfig,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
)


# ── Synthetic market parameters ────────────────────────────────────────────────

UNDERLYING = "BANKNIFTY"
EXCHANGE = "NSE"
SPOT = 52_400.0
LOT_SIZE = 15
STRIKE_STEP = 100
ATM_IV = 0.182
RISK_FREE = 0.068
TODAY = date(2026, 5, 29)

# Two expiries — weekly (7 DTE) and biweekly (14 DTE)
WEEKLY_EXPIRY = date(2026, 6, 5)
BIWEEKLY_EXPIRY = date(2026, 6, 12)

SEQUENCE = 0  # global sequence counter


def _next_seq() -> int:
    global SEQUENCE
    SEQUENCE += 1
    return SEQUENCE


def _skewed_iv(strike: float, option_type: str) -> Decimal:
    moneyness = (strike - SPOT) / SPOT
    if option_type == "PUT":
        skew_factor = max(-moneyness, 0.0)
    else:
        skew_factor = max(moneyness * 0.3, 0.0)
    iv = ATM_IV + 0.0015 * skew_factor * 100.0
    return Decimal(str(max(round(iv, 4), 0.05)))


def _synthetic_oi(strike: float, option_type: str) -> int:
    d = abs((strike - SPOT) / SPOT)
    base = 150_000 * exp(-0.5 * (d / 0.025) ** 2)
    if option_type == "PUT" and strike < SPOT:
        base *= 1.35
    return max(int(base), 500)


def _synthetic_ltp(strike: float, option_type: str, tte_days: int) -> Decimal:
    intrinsic = max(SPOT - strike, 0.0) if option_type == "CALL" else max(strike - SPOT, 0.0)
    # rough extrinsic estimate
    d = abs((strike - SPOT) / SPOT)
    extrinsic = max(0.5 - d * 10, 0.05) * SPOT * 0.008 * (tte_days / 7.0) ** 0.5
    return Decimal(str(round(intrinsic + extrinsic, 2)))


def _build_chain_snapshot(expiry: date, tte_days: int) -> OptionChainSnapshot:
    """Build a synthetic OptionChainSnapshot for one expiry."""
    now = datetime.now(tz=timezone.utc)
    contracts: list[OptionContractSnapshot] = []

    lo = int((SPOT - 2500) // STRIKE_STEP) * STRIKE_STEP
    hi = int((SPOT + 2500) // STRIKE_STEP) * STRIKE_STEP + STRIKE_STEP

    for strike in range(lo, hi + 1, STRIKE_STEP):
        for opt_type in ("CALL", "PUT"):
            iv = _skewed_iv(float(strike), opt_type)
            oi = _synthetic_oi(float(strike), opt_type)
            ltp = _synthetic_ltp(float(strike), opt_type, tte_days)
            half_spread = max(float(ltp) * 0.008, 0.50)
            bid = Decimal(str(round(float(ltp) - half_spread, 2)))
            ask = Decimal(str(round(float(ltp) + half_spread, 2)))

            sym = f"{UNDERLYING}{expiry.strftime('%d%b%y').upper()}{strike}{opt_type[0]}E"
            contracts.append(
                OptionContractSnapshot(
                    contract_symbol=sym,
                    underlying_symbol=UNDERLYING,
                    exchange=EXCHANGE,
                    expiry_date=expiry,
                    strike_price=Decimal(str(strike)),
                    option_side=opt_type,  # type: ignore[arg-type]
                    event_timestamp=now,
                    received_timestamp=now,
                    last_traded_price=ltp,
                    bid_price=bid,
                    ask_price=ask,
                    traded_volume=max(int(oi * 0.08), 100),
                    open_interest=oi,
                    implied_volatility=iv,
                    greeks=OptionGreeksSnapshot(),  # no chain Greeks — builder will compute
                    is_tradable=True,
                    source="synthetic_demo",
                )
            )

    return OptionChainSnapshot(
        chain_id=f"{UNDERLYING}-{expiry.isoformat()}",
        underlying_symbol=UNDERLYING,
        expiry_date=expiry,
        exchange=EXCHANGE,
        event_timestamp=now,
        received_timestamp=now,
        contracts=tuple(contracts),
        sequence_number=_next_seq(),
        update_kind="SNAPSHOT",
        is_replay=False,
        source="synthetic_demo",
    )


def run_demo() -> None:
    print("=" * 72)
    print("  Full Options Intelligence Pipeline Demo")
    print(f"  Underlying: {UNDERLYING}  |  Spot: {SPOT:,.0f}  |  Date: {TODAY}")
    print("=" * 72)

    # ── Step 1: Build synthetic OptionChainSnapshot objects ────────────────────
    print("\n[1/4] Building synthetic OptionChainSnapshot objects...")
    weekly_snap = _build_chain_snapshot(WEEKLY_EXPIRY, tte_days=7)
    biweekly_snap = _build_chain_snapshot(BIWEEKLY_EXPIRY, tte_days=14)
    print(f"  Weekly chain  ({WEEKLY_EXPIRY}):   {len(weekly_snap.contracts)} contracts")
    print(f"  Biweekly chain ({BIWEEKLY_EXPIRY}): {len(biweekly_snap.contracts)} contracts")

    # ── Step 2: Run OptionsContextBuilder ─────────────────────────────────────
    print("\n[2/4] Running OptionsContextBuilder (all 5 engines)...")
    builder = OptionsContextBuilder(
        config=OptionsContextBuilderConfig(
            risk_free_rate=RISK_FREE,
            compute_missing_greeks=True,
        )
    )
    context = builder.build(
        snapshots=[weekly_snap, biweekly_snap],
        spot_price=SPOT,
        lot_size=LOT_SIZE,
    )

    # Print context summary
    print(f"\n  ─── OptionsContext built ───")
    print(f"  Snapshots processed:        {context.snapshots_processed}")
    print(f"  Contracts processed:        {context.contracts_processed}")
    print(f"  Greeks computed via BSM:    {context.contracts_with_computed_greeks}")
    print(f"  Greeks from chain:          {context.contracts_with_chain_greeks}")
    print(f"  StrikeCandidates ready:     {len(context.strike_candidates)}")
    print(f"  Expiries processed:         {', '.join(context.expiries_processed)}")
    print(f"  Build warnings:             {len(context.build_warnings)}")
    for w in context.build_warnings[:3]:
        print(f"    ⚠  {w}")

    print(f"\n  ─── Engine Outputs ───")
    print(f"  OI Analytics:               {'✓' if context.oi_snapshot else '✗'}")
    print(f"  Gamma Exposure:             {'✓' if context.gex_snapshot else '✗'}")
    print(f"  IV Surface (CALL):          {'✓' if context.iv_surface_call else '✗'}")
    print(f"  IV Surface (PUT):           {'✓' if context.iv_surface_put else '✗'}")
    print(f"  Dealer Hedging:             {'✓' if context.dealer_hedging_snapshot else '✗'}")
    print(f"  Premium Behavior:           {'✓' if context.premium_behavior_snapshot else '✗'}")

    print(f"\n  ─── Derived Analytics ───")
    print(f"  ATM IV:                     {context.atm_iv * 100:.2f}%" if context.atm_iv else "  ATM IV:                     N/A")
    print(f"  ATM Strike:                 {context.atm_strike:,.0f}" if context.atm_strike else "  ATM Strike:                 N/A")
    print(f"  Put-Call Ratio (OI):        {context.pcr:.3f}" if context.pcr else "  PCR:                        N/A")
    print(f"  Max Pain:                   {context.max_pain:,.0f}" if context.max_pain else "  Max Pain:                   N/A")
    print(f"  Gamma Regime:               {context.gamma_regime or 'N/A'}")
    print(f"  Gamma Flip Strike:          {context.gamma_flip_strike:,.0f}" if context.gamma_flip_strike else "  Gamma Flip:                 N/A")

    if context.oi_snapshot:
        oi = context.oi_snapshot
        print(f"\n  ─── OI Analytics Detail (primary expiry) ───")
        print(f"  Total call OI:              {oi.total_call_open_interest:,}")
        print(f"  Total put OI:               {oi.total_put_open_interest:,}")
        if oi.call_resistance_levels:
            top_r = oi.call_resistance_levels[0]
            print(f"  Top call resistance:        {top_r.strike_price:,.0f}  OI={top_r.open_interest:,}")
        if oi.put_support_levels:
            top_s = oi.put_support_levels[0]
            print(f"  Top put support:            {top_s.strike_price:,.0f}  OI={top_s.open_interest:,}")

    if context.gex_snapshot:
        gex = context.gex_snapshot
        print(f"\n  ─── Gamma Exposure Detail ───")
        print(f"  Call wall:                  {gex.call_wall_strike:,.0f}" if gex.call_wall_strike else "  Call wall:  N/A")
        print(f"  Put wall:                   {gex.put_wall_strike:,.0f}" if gex.put_wall_strike else "  Put wall:   N/A")
        print(f"  Gamma flip:                 {gex.gamma_flip_strike:,.0f}" if gex.gamma_flip_strike else "  Gamma flip: N/A")
        print(f"  Net GEX regime:             {gex.net_gamma_regime}")

    if context.dealer_hedging_snapshot:
        dh = context.dealer_hedging_snapshot
        print(f"\n  ─── Dealer Hedging Pressure ───")
        print(f"  Net delta exposure:         {dh.net_delta_exposure:,.0f}")
        print(f"  Net gamma exposure:         {dh.net_gamma_exposure:,.4f}")
        print(f"  Hedge flow per 1% move:     {dh.hedge_flow_per_1pct_spot_move:,.0f}")
        print(f"  Dominant gamma strike:      {dh.dominant_gamma_strike:,.0f}" if dh.dominant_gamma_strike else "  Dominant gamma: N/A")

    if context.premium_behavior_snapshot:
        pb = context.premium_behavior_snapshot
        print(f"\n  ─── Premium Behavior ───")
        print(f"  Total premium OI notional:  {pb.total_premium_oi_notional:,.0f}")
        print(f"  Call extrinsic share:       {pb.call_extrinsic_share * 100:.1f}%")
        print(f"  Put extrinsic share:        {pb.put_extrinsic_share * 100:.1f}%")
        print(f"  Net theta decay/day:        {pb.net_time_decay_per_day:,.2f}")
        print(f"  Premium regime:             {pb.premium_regime}")

    # ── Step 3: Run StrikeSelectionEngine ─────────────────────────────────────
    print("\n[3/4] Running StrikeSelectionEngine (LONG bias)...")
    strike_engine = StrikeSelectionEngine(
        config=StrikeSelectionConfig(
            top_n=3,
            target_delta_min=0.28,
            target_delta_max=0.52,
            min_open_interest=2_000,
        )
    )
    long_result = strike_engine.select(
        bias="LONG",
        candidates=list(context.strike_candidates),
        valuation_timestamp=context.valuation_timestamp,
        oi_snapshot=context.oi_snapshot,
        gex_snapshot=context.gex_snapshot,
        atm_iv=context.atm_iv,
    )

    print(f"  Candidates passed filters:  {long_result.candidates_passed_filters} / {long_result.candidates_evaluated}")

    # ── Step 4: Print the final recommendations ────────────────────────────────
    print("\n[4/4] TRADE RECOMMENDATIONS")
    print("=" * 72)
    print(f"  Bias: LONG  |  {UNDERLYING} @ {SPOT:,.0f}  |  ATM IV: {context.atm_iv * 100:.2f}%" if context.atm_iv else f"  Bias: LONG  |  {UNDERLYING} @ {SPOT:,.0f}")
    print()

    if not long_result.recommendations:
        print("  No recommendations — all candidates filtered out.")
    else:
        for rec in long_result.recommendations:
            print(f"  {rec.summary()}")
            print(
                f"       Components:  "
                f"delta={rec.breakdown.delta_fitness:.2f}  "
                f"OI={rec.breakdown.oi_support:.2f}  "
                f"activity={rec.breakdown.oi_activity:.2f}  "
                f"gex={rec.breakdown.gex_proximity:.2f}  "
                f"iv={rec.breakdown.iv_attractiveness:.2f}  "
                f"liq={rec.breakdown.liquidity:.2f}  "
                f"mp={rec.breakdown.max_pain_distance:.2f}  "
                f"dte×={rec.breakdown.dte_multiplier:.2f}"
            )
            print(
                f"       Context:     "
                f"OI={rec.oi_classification}  "
                f"GEX={rec.gex_label}  "
                f"IV={rec.iv_label}  "
                f"Liq={rec.liquidity_grade}  "
                f"Expiry={rec.expiry_label}"
            )
            print()

    # Full reasoning for best pick
    best = long_result.best()
    if best is not None:
        print("─" * 72)
        print(f"  BEST RECOMMENDATION — Full Reasoning")
        print("─" * 72)
        print(f"  {best.summary()}")
        print(f"\n  Contract details:")
        print(f"    Strike:    {best.strike_price:,.0f}")
        print(f"    Type:      {best.option_type}")
        print(f"    Expiry:    {best.expiry_date}  ({best.days_to_expiry} DTE)")
        print(f"    Delta:     {best.delta:+.3f}")
        print(f"    Gamma:     {best.gamma:.5f}")
        print(f"    Theta:     {best.theta:.2f} / day")
        print(f"    Vega:      {best.vega:.2f} / 1% IV")
        print(f"    IV:        {best.implied_volatility * 100:.2f}%")
        print(f"    LTP:       {best.last_traded_price:.2f}")
        print(f"    OI:        {best.open_interest:,}")
        print(f"    Lot size:  {best.lot_size}")
        print(f"    Notional:  {best.last_traded_price * best.lot_size:,.0f}")
        print(f"\n  Scoring reasoning (one line per dimension):")
        for line in best.reasoning:
            print(f"    · {line}")

    print("\n" + "=" * 72)
    print("  Pipeline complete — OptionChainSnapshot → StrikeRecommendation")
    print("=" * 72)


if __name__ == "__main__":
    run_demo()
