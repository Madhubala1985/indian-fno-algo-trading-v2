"""
scripts/strike_selection_demo.py
═══════════════════════════════════════════════════════════════════════
Validation script for the StrikeSelectionEngine.

Synthesises a realistic BANKNIFTY option chain from a reference spot
price using the existing GreeksEngine and IVEngine. Then runs
StrikeSelectionEngine for LONG and SHORT biases, including synthetic
OI and GEX context.

No live data required. Runs fully offline.

Usage:
    python scripts/strike_selection_demo.py
"""
from __future__ import annotations

import sys
import os
from datetime import date, datetime, timezone
from math import exp, sqrt, log, erf

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.options.greeks_engine import (
    GreeksEngine,
    GreeksEngineConfig,
    OptionGreeksInput,
)
from platform_core.options.oi_analytics import (
    OIAnalyticsEngine,
    OIAnalyticsConfig,
    OIContractPoint,
)
from platform_core.options.gamma_exposure import (
    GammaExposureEngine,
    GammaExposureConfig,
    GammaExposurePoint,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
    StrikeCandidate,
)


# ── Market parameters ─────────────────────────────────────────────────────────

UNDERLYING = "BANKNIFTY"
SPOT = 52_400.0         # representative BANKNIFTY level
LOT_SIZE = 15
STRIKE_STEP = 100
RISK_FREE_RATE = 0.068  # 6.8% RBI repo rate
DIVIDEND_YIELD = 0.0

# Two expiries: weekly (7 DTE) and biweekly (14 DTE)
TODAY = date(2026, 5, 29)
WEEKLY_EXPIRY = date(2026, 6, 5)    # 7 days
BIWEEKLY_EXPIRY = date(2026, 6, 12) # 14 days

# IV model: negative skew typical for Indian index options
ATM_IV = 0.182            # 18.2% ATM IV
SKEW_SLOPE = 0.0015       # IV increases ~0.15% per 1% further OTM for puts


# ── Helpers ────────────────────────────────────────────────────────────────────

def _tte(expiry: date) -> float:
    """Time to expiry in years."""
    return max((expiry - TODAY).days, 1) / 365.25


def _skewed_iv(strike: float, spot: float, option_type: str) -> float:
    """
    Synthetic IV with a negative skew (puts pricier than equidistant calls).
    Approximates typical BANKNIFTY smile structure.
    """
    moneyness = (strike - spot) / spot  # negative = OTM put / ITM call
    if option_type == "PUT":
        # Puts: IV increases as strike falls below spot
        skew_factor = max(-moneyness, 0.0)
    else:
        # Calls: slight upward skew for deep OTM calls, gentle otherwise
        skew_factor = max(moneyness * 0.3, 0.0)
    iv = ATM_IV + SKEW_SLOPE * skew_factor * 100.0
    return max(iv, 0.05)  # floor at 5%


def _synthetic_oi(strike: float, spot: float, option_type: str) -> int:
    """
    Synthetic OI distribution peaked near ATM with realistic taper.
    Puts have slightly higher OI on the downside (institutional hedging).
    """
    distance_pct = abs((strike - spot) / spot)
    base = 150_000 * exp(-0.5 * (distance_pct / 0.025) ** 2)
    if option_type == "PUT" and strike < spot:
        base *= 1.35  # put skew in OI
    return max(int(base), 0)


def _synthetic_change_in_oi(strike: float, spot: float, option_type: str) -> int:
    """Synthetic change-in-OI: fresh buildup near ATM and at key round numbers."""
    oi = _synthetic_oi(strike, spot, option_type)
    distance_pct = abs((strike - spot) / spot)
    if distance_pct < 0.01:
        return int(oi * 0.12)   # strong buildup very close to ATM
    if distance_pct < 0.03:
        return int(oi * 0.06)
    return int(oi * 0.02)


def _synthetic_ltp(strike: float, delta: float, spot: float) -> float:
    """Crude premium estimate from delta × intrinsic-like value (demo only)."""
    intrinsic = max(spot - strike, 0.0) if delta > 0 else max(strike - spot, 0.0)
    extrinsic = max(abs(delta), 0.05) * spot * 0.015
    return round(intrinsic + extrinsic, 2)


def _synthetic_spread(ltp: float) -> tuple[float, float]:
    """Synthetic bid/ask around LTP with ~1-2% spread."""
    half = max(ltp * 0.008, 0.50)
    return round(ltp - half, 2), round(ltp + half, 2)


# ── Chain builder ─────────────────────────────────────────────────────────────

def build_chain(expiry: date) -> list[StrikeCandidate]:
    """
    Build a synthetic BANKNIFTY option chain for the given expiry using
    the real GreeksEngine for authentic BSM Greeks.
    """
    greeks_engine = GreeksEngine(config=GreeksEngineConfig(theta_scale_per_day=True))
    tte = _tte(expiry)
    candidates: list[StrikeCandidate] = []

    # Strike range: spot ± 3000 in STRIKE_STEP increments
    lo = int((SPOT - 3000) // STRIKE_STEP) * STRIKE_STEP
    hi = int((SPOT + 3000) // STRIKE_STEP) * STRIKE_STEP + STRIKE_STEP

    for strike in range(lo, hi + 1, STRIKE_STEP):
        for option_type in ("CALL", "PUT"):
            iv = _skewed_iv(strike, SPOT, option_type)

            result = greeks_engine.compute(
                option_input=OptionGreeksInput(
                    instrument_id=f"{UNDERLYING}_{strike}_{option_type[0]}",
                    underlying_symbol=UNDERLYING,
                    option_symbol=f"{UNDERLYING}{expiry.strftime('%d%b%y').upper()}{strike}{option_type[0]}",
                    option_type=option_type,
                    spot_price=SPOT,
                    strike_price=float(strike),
                    time_to_expiry_years=tte,
                    implied_volatility=iv,
                    risk_free_rate=RISK_FREE_RATE,
                    dividend_yield=DIVIDEND_YIELD,
                    lot_size=LOT_SIZE,
                )
            )

            oi = _synthetic_oi(strike, SPOT, option_type)
            chg_oi = _synthetic_change_in_oi(strike, SPOT, option_type)
            volume = int(oi * 0.08)  # ~8% daily turnover
            ltp = _synthetic_ltp(float(strike), result.delta, SPOT)
            bid, ask = _synthetic_spread(ltp)

            candidates.append(StrikeCandidate(
                underlying_symbol=UNDERLYING,
                expiry_date=expiry,
                option_symbol=result.option_symbol,
                strike_price=float(strike),
                option_type=option_type,
                spot_price=SPOT,
                last_traded_price=ltp,
                open_interest=oi,
                change_in_open_interest=chg_oi,
                volume=volume,
                implied_volatility=iv,
                delta=result.delta,
                gamma=result.gamma,
                theta=result.theta,
                vega=result.vega,
                time_to_expiry_years=tte,
                lot_size=LOT_SIZE,
                bid_price=bid,
                ask_price=ask,
            ))

    return candidates


# ── OI and GEX context builders ────────────────────────────────────────────────

def build_oi_snapshot(candidates: list[StrikeCandidate], expiry: date):
    """Build an OI snapshot from the synthetic chain (weekly expiry)."""
    oi_engine = OIAnalyticsEngine(config=OIAnalyticsConfig(
        concentration_top_n=5,
        support_resistance_top_n=5,
    ))
    now = datetime.now(timezone.utc)
    points = [
        OIContractPoint(
            underlying_symbol=c.underlying_symbol,
            expiry_date=c.expiry_date,
            strike_price=c.strike_price,
            option_type=c.option_type,
            open_interest=c.open_interest,
            change_in_open_interest=c.change_in_open_interest,
            volume=c.volume,
            implied_volatility=c.implied_volatility,
            last_price=c.last_traded_price,
            observed_at=now,
        )
        for c in candidates
        if c.expiry_date == expiry
    ]
    return oi_engine.build_snapshot(
        points=points,
        spot_price=SPOT,
        valuation_timestamp=now,
    )


def build_gex_snapshot(candidates: list[StrikeCandidate], expiry: date):
    """Build a GEX snapshot from the synthetic chain (weekly expiry)."""
    gex_engine = GammaExposureEngine(config=GammaExposureConfig())
    now = datetime.now(timezone.utc)
    points = [
        GammaExposurePoint(
            underlying_symbol=c.underlying_symbol,
            expiry_date=c.expiry_date,
            option_symbol=c.option_symbol,
            option_type=c.option_type,
            strike_price=c.strike_price,
            spot_price=c.spot_price,
            lot_size=c.lot_size,
            open_interest=c.open_interest,
            change_in_open_interest=c.change_in_open_interest,
            volume=c.volume,
            time_to_expiry_years=c.time_to_expiry_years,
            gamma=c.gamma,
            implied_volatility=c.implied_volatility,
            observed_at=now,
        )
        for c in candidates
        if c.expiry_date == expiry
    ]
    return gex_engine.build_snapshot(points=points)


# ── Main demo ─────────────────────────────────────────────────────────────────

def run_demo() -> None:
    print("=" * 72)
    print("  BANKNIFTY Strike Selection Demo")
    print(f"  Spot: {SPOT:,.0f}  |  ATM IV: {ATM_IV*100:.1f}%  |  Date: {TODAY}")
    print("=" * 72)

    # Build chains for both expiries
    print("\nBuilding synthetic option chain (using BSM GreeksEngine)...")
    weekly_chain = build_chain(WEEKLY_EXPIRY)
    biweekly_chain = build_chain(BIWEEKLY_EXPIRY)
    all_candidates = weekly_chain + biweekly_chain
    print(f"  Weekly ({WEEKLY_EXPIRY}):   {len(weekly_chain):3d} contracts")
    print(f"  Biweekly ({BIWEEKLY_EXPIRY}): {len(biweekly_chain):3d} contracts")
    print(f"  Total candidates:       {len(all_candidates):3d}")

    # Build OI and GEX context from the weekly chain
    print("\nBuilding OI Analytics snapshot...")
    oi_snapshot = build_oi_snapshot(weekly_chain, WEEKLY_EXPIRY)
    print(f"  Max pain strike:  {oi_snapshot.max_pain_strike:,.0f}")
    print(f"  PCR (OI):         {oi_snapshot.put_call_ratio_open_interest:.3f}")
    print(f"  Total call OI:    {oi_snapshot.total_call_open_interest:,}")
    print(f"  Total put OI:     {oi_snapshot.total_put_open_interest:,}")
    if oi_snapshot.call_resistance_levels:
        top_call = oi_snapshot.call_resistance_levels[0]
        print(f"  Top call wall:    {top_call.strike_price:,.0f} (OI={top_call.open_interest:,})")
    if oi_snapshot.put_support_levels:
        top_put = oi_snapshot.put_support_levels[0]
        print(f"  Top put support:  {top_put.strike_price:,.0f} (OI={top_put.open_interest:,})")

    print("\nBuilding GEX snapshot...")
    gex_snapshot = build_gex_snapshot(weekly_chain, WEEKLY_EXPIRY)
    print(f"  Gamma flip:       {gex_snapshot.gamma_flip_strike}")
    print(f"  Call wall:        {gex_snapshot.call_wall_strike}")
    print(f"  Put wall:         {gex_snapshot.put_wall_strike}")
    print(f"  Net GEX regime:   {gex_snapshot.net_gamma_regime}")

    # Configure the selection engine
    engine = StrikeSelectionEngine(config=StrikeSelectionConfig(
        top_n=5,
        target_delta_min=0.28,
        target_delta_max=0.52,
        min_open_interest=2_000,
    ))

    # ── LONG bias (bullish — buy calls) ───────────────────────────────────────
    print("\n" + "=" * 72)
    print("  BIAS: LONG (bullish — buying BANKNIFTY CALLS)")
    print("=" * 72)

    long_result = engine.select(
        bias="LONG",
        candidates=all_candidates,
        valuation_timestamp=datetime.now(timezone.utc),
        oi_snapshot=oi_snapshot,
        gex_snapshot=gex_snapshot,
        atm_iv=ATM_IV,
    )

    print(f"\n  Candidates evaluated:     {long_result.candidates_evaluated}")
    print(f"  Passed filters:           {long_result.candidates_passed_filters}")
    print(f"  ATM IV reference used:    {long_result.metadata.get('atm_iv_used', '?') }")
    print()

    if not long_result.recommendations:
        print("  No recommendations — all candidates filtered out.")
    else:
        for rec in long_result.recommendations:
            print(f"  {rec.summary()}")
            print(f"       Scores  delta={rec.breakdown.delta_fitness:.2f}  "
                  f"OI={rec.breakdown.oi_support:.2f}  "
                  f"act={rec.breakdown.oi_activity:.2f}  "
                  f"gex={rec.breakdown.gex_proximity:.2f}  "
                  f"iv={rec.breakdown.iv_attractiveness:.2f}  "
                  f"liq={rec.breakdown.liquidity:.2f}  "
                  f"mp={rec.breakdown.max_pain_distance:.2f}  "
                  f"dte×={rec.breakdown.dte_multiplier:.2f}")
            print(f"       Labels  OI={rec.oi_classification}  "
                  f"GEX={rec.gex_label}  "
                  f"IV={rec.iv_label}  "
                  f"Liq={rec.liquidity_grade}  "
                  f"Expiry={rec.expiry_label}")
            print()

    # ── SHORT bias (bearish — buy puts) ───────────────────────────────────────
    print("=" * 72)
    print("  BIAS: SHORT (bearish — buying BANKNIFTY PUTS)")
    print("=" * 72)

    short_result = engine.select(
        bias="SHORT",
        candidates=all_candidates,
        valuation_timestamp=datetime.now(timezone.utc),
        oi_snapshot=oi_snapshot,
        gex_snapshot=gex_snapshot,
        atm_iv=ATM_IV,
    )

    print(f"\n  Candidates evaluated:     {short_result.candidates_evaluated}")
    print(f"  Passed filters:           {short_result.candidates_passed_filters}")
    print()

    if not short_result.recommendations:
        print("  No recommendations — all candidates filtered out.")
    else:
        for rec in short_result.recommendations:
            print(f"  {rec.summary()}")
            print(f"       Scores  delta={rec.breakdown.delta_fitness:.2f}  "
                  f"OI={rec.breakdown.oi_support:.2f}  "
                  f"act={rec.breakdown.oi_activity:.2f}  "
                  f"gex={rec.breakdown.gex_proximity:.2f}  "
                  f"iv={rec.breakdown.iv_attractiveness:.2f}  "
                  f"liq={rec.breakdown.liquidity:.2f}  "
                  f"mp={rec.breakdown.max_pain_distance:.2f}  "
                  f"dte×={rec.breakdown.dte_multiplier:.2f}")
            print(f"       Labels  OI={rec.oi_classification}  "
                  f"GEX={rec.gex_label}  "
                  f"IV={rec.iv_label}  "
                  f"Liq={rec.liquidity_grade}  "
                  f"Expiry={rec.expiry_label}")
            print()

    # ── Best recommendation detail ─────────────────────────────────────────────
    best = long_result.best()
    if best is not None:
        print("=" * 72)
        print("  BEST LONG RECOMMENDATION — Full Reasoning")
        print("=" * 72)
        print(f"\n  {best.summary()}")
        print(f"\n  Greeks:  delta={best.delta:+.3f}  gamma={best.gamma:.5f}  "
              f"theta={best.theta:.2f}/day  vega={best.vega:.2f}/1%IV")
        print(f"  Premium: LTP={best.last_traded_price:.1f}  "
              f"lot_size={best.lot_size}  "
              f"notional={best.last_traded_price * best.lot_size:,.0f}")
        print("\n  Reasoning:")
        for line in best.reasoning:
            print(f"    · {line}")

    print("\n" + "=" * 72)
    print("  Demo complete — StrikeSelectionEngine operational")
    print("=" * 72)


if __name__ == "__main__":
    run_demo()
