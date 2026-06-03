"""
scripts/validate_option_chain_support.py
═══════════════════════════════════════════════════════════════════════
Pre-flight validation of Kite option-chain support for each target
underlying (BANKNIFTY, NIFTY, SENSEX).

For each symbol prints:
  1. Instrument metadata (from Kite master: lot sizes, expiries, steps)
  2. Chain fetch result (contracts, candidates, CALL/PUT counts)
  3. Strike intelligence — top-5 CALL and PUT candidates with full
     factor breakdowns (delta, gamma, theta, IV, OI, spread, score)
  4. Repeated-strike analysis — explains why a strike dominates

Does NOT:
  - Open a WebSocket connection
  - Place or simulate any trade
  - Write to DB, state file, or CSV
  - Modify any live pipeline state

Usage (during market hours):
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py --symbol NIFTY
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py --symbol ALL

Arguments:
    --symbol  BANKNIFTY | NIFTY | SENSEX | ALL   (default: ALL)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.data.live.instrument_registry import OPTION_CHAIN_CONFIG
from platform_core.data.live.kite_adapter import KiteAdapter
from platform_core.options.options_context import (
    OptionsContextBuilder,
    OptionsContextBuilderConfig,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
    StrikeRecommendation,
)

_W = 74

# ── Spot-price helpers ─────────────────────────────────────────────────────────

_LTP_SYMBOL: dict[str, str] = {
    "BANKNIFTY": "NSE:NIFTY BANK",
    "NIFTY":     "NSE:NIFTY 50",
    "SENSEX":    "BSE:SENSEX",
}
_FALLBACK_SPOTS: dict[str, float] = {
    "BANKNIFTY": 52_000.0,
    "NIFTY":     24_000.0,
    "SENSEX":    80_000.0,
}


def _fetch_spot(adapter: KiteAdapter, underlying: str) -> float:
    ltp_sym = _LTP_SYMBOL.get(underlying)
    if ltp_sym:
        try:
            result = adapter.kite.ltp([ltp_sym])
            price = result.get(ltp_sym, {}).get("last_price")
            if price and float(price) > 0:
                return float(price)
        except Exception:
            pass
    fb = _FALLBACK_SPOTS.get(underlying, 50_000.0)
    print(f"  [spot] LTP unavailable — using fallback ₹{fb:,.0f}")
    return fb


# ── Print helpers ──────────────────────────────────────────────────────────────

def _hdr(text: str, ch: str = "─") -> None:
    print(f"\n{ch * _W}")
    print(f"  {text}")
    print(ch * _W)


def _row(label: str, value: str, indent: int = 2) -> None:
    sp = " " * indent
    print(f"{sp}{label:<30}{value}")


def _print_candidate(rank: int, r: StrikeRecommendation, atm: float) -> None:
    dist = r.strike_price - atm
    bd   = r.breakdown
    print(
        f"    #{rank}  {r.option_symbol:<28}  "
        f"strike={r.strike_price:.0f}  dist={dist:+.0f}  "
        f"DTE={r.days_to_expiry}"
    )
    print(
        f"         prem={r.last_traded_price:.1f}  "
        f"δ={r.delta:+.3f}  γ={r.gamma:.6f}  "
        f"θ={r.theta:.2f}/day  "
        f"IV={r.implied_volatility:.1%}  OI={r.open_interest:,}"
    )
    print(
        f"         score={r.composite_score:.4f}  "
        f"liq={bd.liquidity:.3f}  θ_eff={bd.theta_efficiency:.3f}  "
        f"iv_attr={bd.iv_attractiveness:.3f}  "
        f"Δfit={bd.delta_fitness:.3f}"
    )
    print(
        f"         oi_supp={bd.oi_support:.3f}  oi_act={bd.oi_activity:.3f}  "
        f"gex={bd.gex_proximity:.3f}  maxpain={bd.max_pain_distance:.3f}  "
        f"grade={r.liquidity_grade}  oi_class={r.oi_classification}"
    )


def _repeated_strike_analysis(
    call_recs: list[StrikeRecommendation],
    put_recs:  list[StrikeRecommendation],
    atm: float,
    step: float,
) -> None:
    if not call_recs:
        return
    best_call = call_recs[0]
    best_put  = put_recs[0] if put_recs else None

    same = (
        best_put is not None
        and abs(best_call.strike_price - best_put.strike_price) < 1e-6
    )

    print("\n  Repeated-strike analysis:")

    if same:
        print(f"    CALL and PUT both select strike {best_call.strike_price:.0f}")
    else:
        print(f"    CALL winner: {best_call.strike_price:.0f}  "
              f"PUT winner: {best_put.strike_price:.0f if best_put else 'n/a'}")

    dist_atm = abs(best_call.strike_price - atm)
    atm_label = "ATM" if dist_atm < step * 0.5 else (
        f"NEAR-ATM (+{dist_atm:.0f})" if best_call.strike_price > atm
        else f"NEAR-ATM (-{dist_atm:.0f})"
    )
    print(f"    Winner position: {atm_label}")

    # Score margin
    margin = (
        call_recs[0].composite_score - call_recs[1].composite_score
        if len(call_recs) >= 2 else None
    )
    if margin is not None:
        margin_label = "SCORE_DRIVEN" if margin >= 0.05 else "CLOSE_RACE"
        print(f"    Score margin vs rank-2: {margin:.4f} → {margin_label}")

    # Adjacent strikes in top-5
    top5_strikes = {r.strike_price for r in call_recs[:5]}
    adj_up   = best_call.strike_price + step
    adj_down = best_call.strike_price - step
    if adj_up in top5_strikes or adj_down in top5_strikes:
        print(f"    Adjacent strike also ranked (±{step:.0f}) — ADJACENT_STRIKE_SCORED")
    else:
        print(f"    No adjacent strike in top-5 — winner is clearly dominant in window")

    # Dominant factor classification
    bd = best_call.breakdown
    scores = {
        "delta_fitness":      bd.delta_fitness,
        "liquidity":          bd.liquidity,
        "oi_support":         bd.oi_support,
        "oi_activity":        bd.oi_activity,
        "gex_proximity":      bd.gex_proximity,
        "iv_attractiveness":  bd.iv_attractiveness,
        "max_pain_distance":  bd.max_pain_distance,
        "theta_efficiency":   bd.theta_efficiency,
    }
    total = sum(scores.values())
    if total > 0:
        fracs = {k: v / total for k, v in scores.items()}
        top_factor, top_frac = max(fracs.items(), key=lambda kv: kv[1])
        classification = (
            "DELTA_DOMINATED"    if top_factor == "delta_fitness" and top_frac > 0.5 else
            "LIQUIDITY_DOMINATED" if top_factor == "liquidity" and top_frac > 0.45 else
            "DATA_DEAD"          if bd.oi_support < 0.05 and bd.oi_activity < 0.05 else
            "SCORE_DRIVEN"
        )
        print(f"    Dominant factor: {top_factor} ({top_frac:.1%}) → {classification}")


# ── Per-symbol validation ─────────────────────────────────────────────────────

def _validate_one(
    adapter: KiteAdapter,
    opt_ctx_builder: OptionsContextBuilder,
    strike_engine: StrikeSelectionEngine,
    config_key: str,
) -> bool:
    meta = OPTION_CHAIN_CONFIG[config_key]
    underlying = meta.option_underlying
    exchange   = meta.exchange
    lot_size   = meta.lot_size
    step       = meta.strike_step

    _hdr(f"INSTRUMENT METADATA — {config_key}")
    _row("feed_symbol:",      meta.feed_symbol)
    _row("option_underlying:", underlying)
    _row("exchange:",         exchange)
    _row("ref lot_size:",     str(lot_size))
    _row("ref strike_step:",  f"{step:.0f}")

    # Metadata from instrument master
    meta_info = adapter.resolve_option_metadata(
        underlying=underlying,
        exchange=exchange,
        reference_lot_size=lot_size,
        reference_strike_step=step,
    )
    if "error" in meta_info:
        print(f"  [FAIL] Metadata: {meta_info['error']}")
        return False

    _row("instruments found:", str(meta_info.get("num_option_instruments", "?")))
    _row("nearest 3 expiries:", str(meta_info.get("nearest_3_expiries", [])))
    _row("observed lot sizes:", str(meta_info.get("observed_lot_sizes", [])))
    _row("inferred step:",      str(meta_info.get("inferred_strike_step", "?")))
    _row("sample CE:",          str(meta_info.get("sample_ce", "?")))
    _row("sample PE:",          str(meta_info.get("sample_pe", "?")))
    for w in meta_info.get("warnings", []):
        print(f"  ⚠  {w}")

    # Spot
    spot = _fetch_spot(adapter, underlying)
    atm  = round(spot / step) * step
    _row("spot (live/fallback):", f"₹{spot:,.2f}")
    _row("ATM strike:",          f"{atm:.0f}")

    # Nearest expiry
    try:
        expiry = adapter.nearest_expiry(underlying, date.today(), exchange=exchange)
    except Exception as exc:
        print(f"  [FAIL] nearest_expiry: {exc}")
        return False
    _row("nearest expiry:", str(expiry))

    # Chain fetch
    _hdr(f"CHAIN FETCH — {config_key}")
    try:
        chain = adapter.fetch_option_chain(
            underlying=underlying,
            expiry_date=expiry,
            spot_price=spot,
            lot_size=lot_size,
            exchange=exchange,
            strike_step=step,
        )
    except Exception as exc:
        print(f"  [FAIL] fetch_option_chain: {exc}")
        return False

    chains = [chain]
    try:
        expiry_next = adapter.nearest_expiry(
            underlying, expiry + timedelta(days=1), exchange=exchange
        )
        chain_next = adapter.fetch_option_chain(
            underlying=underlying,
            expiry_date=expiry_next,
            spot_price=spot,
            lot_size=lot_size,
            exchange=exchange,
            strike_step=step,
        )
        chains.append(chain_next)
        _row("second expiry fetched:", str(expiry_next))
    except Exception:
        pass

    calls = [c for c in chain.contracts if c.option_side == "CALL"]
    puts  = [c for c in chain.contracts if c.option_side == "PUT"]
    _row("contracts (nearest):", str(len(chain.contracts)))
    _row("  CALL:", str(len(calls)))
    _row("  PUT:",  str(len(puts)))

    if not chain.contracts:
        print("  [FAIL] 0 contracts — chain empty")
        return False

    # OptionsContext + candidates
    try:
        ctx = opt_ctx_builder.build(
            snapshots=chains,
            spot_price=spot,
            lot_size=lot_size,
            valuation_timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:
        print(f"  [FAIL] OptionsContextBuilder: {exc}")
        return False

    cands = list(ctx.strike_candidates) if ctx.strike_candidates else []
    _row("candidates built:", str(len(cands)))
    _row("ATM IV:",           f"{ctx.atm_iv:.1%}" if ctx.atm_iv else "n/a")

    if not cands:
        print("  [FAIL] 0 candidates — OptionsContextBuilder produced nothing")
        return False

    # Strike selection — top 5 CALL and PUT
    ts = datetime.now(timezone.utc)

    def _top5(bias: str) -> list[StrikeRecommendation]:
        try:
            r = strike_engine.select(
                bias=bias,
                candidates=cands,
                valuation_timestamp=ts,
                oi_snapshot=ctx.oi_snapshot,
                gex_snapshot=ctx.gex_snapshot,
                atm_iv=ctx.atm_iv,
            )
            return list(r.recommendations)
        except Exception as exc:
            print(f"  [WARN] select({bias}) error: {exc}")
            return []

    call_recs = _top5("LONG")
    put_recs  = _top5("SHORT")

    _hdr(f"TOP-5 CALL CANDIDATES — {config_key}")
    if not call_recs:
        print("  No CALL candidates passed filters")
    for r in call_recs[:5]:
        _print_candidate(r.rank, r, atm)

    _hdr(f"TOP-5 PUT CANDIDATES — {config_key}")
    if not put_recs:
        print("  No PUT candidates passed filters")
    for r in put_recs[:5]:
        _print_candidate(r.rank, r, atm)

    _repeated_strike_analysis(call_recs, put_recs, atm, step)

    print(f"\n  [PASS] {config_key}  underlying={underlying}  "
          f"exchange={exchange}  contracts={len(chain.contracts)}  "
          f"candidates={len(cands)}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Kite option-chain support for live execution targets."
    )
    parser.add_argument(
        "--symbol",
        default="ALL",
        choices=["BANKNIFTY", "NIFTY", "SENSEX", "ALL"],
        help="Which underlying to test (default: ALL)",
    )
    args = parser.parse_args()

    targets = (
        list(OPTION_CHAIN_CONFIG.keys())
        if args.symbol == "ALL"
        else [args.symbol]
    )

    _hdr("OPTION CHAIN VALIDATION SUITE", "═")
    print(f"  Targets: {', '.join(targets)}")
    print(f"  Date:    {date.today()}")

    _hdr("KITE SESSION CHECK", "─")
    adapter = KiteAdapter()
    valid, err = adapter.check_session()
    if not valid:
        print(f"  [FAIL] Token invalid or expired: {err}")
        print("  Action: python scripts/generate_kite_session.py")
        sys.exit(1)
    print("  [PASS] Access token valid")

    opt_ctx_builder = OptionsContextBuilder(
        config=OptionsContextBuilderConfig(compute_missing_greeks=True)
    )
    strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))

    results: dict[str, bool] = {}
    for key in targets:
        try:
            ok = _validate_one(adapter, opt_ctx_builder, strike_engine, key)
        except Exception as exc:
            print(f"\n  [FAIL] {key} unexpected error: {exc}")
            ok = False
        results[key] = ok

    _hdr("SUMMARY", "═")
    passed = [k for k, v in results.items() if v]
    failed = [k for k, v in results.items() if not v]
    print(f"  Passed: {len(passed)}/{len(results)}")
    for k in failed:
        print(f"  FAIL: {k}")
    print()

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
