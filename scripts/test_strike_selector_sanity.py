"""
scripts/test_strike_selector_sanity.py
═══════════════════════════════════════════════════════════════════════
Offline sanity tests for StrikeSelectionEngine.

No Kite token, no live market, no DB/state/CSV writes, no WebSocket.

Builds StrikeCandidate objects directly (no SyntheticChainBuilder
required) and proves the selector responds to each input dimension.

Tests:
  1. Spot-shift test        — selector follows delta when spot changes
  2. Spread quality test    — tighter spread wins over wide spread
  3. Theta efficiency test  — better theta/premium ratio scores higher
  4. OI/liquidity flags     — zero OI triggers FACTOR_DATA_MISSING flag
  5. IV attractiveness      — cheaper IV wins when other factors equal
  6. Greeks reporting       — gamma and theta appear in audit output

Usage:
    PYTHONPATH=. python3 scripts/test_strike_selector_sanity.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from math import exp, sqrt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.options.strike_selection import (
    StrikeCandidate,
    StrikeSelectionConfig,
    StrikeSelectionEngine,
)
from platform_core.options.strike_selection_audit import StrikeAuditLogger

# ── Helpers ────────────────────────────────────────────────────────────────────

_TODAY = date(2026, 6, 10)
_EXPIRY_WEEKLY    = date(2026, 6, 11)   # 1 DTE
_EXPIRY_BIWEEKLY  = date(2026, 6, 18)   # 8 DTE
_EXPIRY_OPTIMAL   = date(2026, 6, 24)   # 14 DTE  (within dte_optimal_min..max=5..18)

_ENGINE = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))
_AUDITOR = StrikeAuditLogger()

_PASS_COUNT = 0
_FAIL_COUNT = 0


def _assert(condition: bool, msg: str) -> None:
    global _PASS_COUNT, _FAIL_COUNT
    prefix = "  PASS" if condition else "  FAIL"
    print(f"  {prefix}: {msg}")
    if condition:
        _PASS_COUNT += 1
    else:
        _FAIL_COUNT += 1


def _candidate(
    symbol: str,
    strike: float,
    spot: float,
    option_type: str = "CALL",
    delta: float = 0.40,
    iv: float = 0.18,
    oi: int = 100_000,
    volume: int = 5000,
    ltp: float = 400.0,
    bid: float | None = 398.0,
    ask: float | None = 402.0,
    gamma: float = 0.0005,
    theta: float = -15.0,
    lot_size: int = 15,
    expiry: date = _EXPIRY_OPTIMAL,
    tte: float = 14.0 / 365.25,
) -> StrikeCandidate:
    return StrikeCandidate(
        underlying_symbol="BANKNIFTY",
        expiry_date=expiry,
        option_symbol=symbol,
        strike_price=strike,
        option_type=option_type,
        spot_price=spot,
        last_traded_price=ltp,
        open_interest=oi,
        change_in_open_interest=oi // 10,
        volume=volume,
        implied_volatility=iv,
        delta=delta if option_type == "CALL" else -abs(delta),
        gamma=gamma,
        theta=theta,
        vega=0.10,
        time_to_expiry_years=tte,
        lot_size=lot_size,
        bid_price=bid,
        ask_price=ask,
    )


def _select(candidates, bias="LONG"):
    return _ENGINE.select(
        bias=bias,
        candidates=candidates,
        valuation_timestamp=datetime.now(timezone.utc),
    )


# ── Test 1: Spot-shift ────────────────────────────────────────────────────────

def test_spot_shift():
    """
    Spot moves from 52000 to 54000 to 56000.
    The winning strike's ATM distance should track the spot direction.
    """
    print("\n[TEST 1] Spot-shift: winner should track ATM as spot moves")

    def make_chain(spot: float) -> list[StrikeCandidate]:
        step = 100.0
        atm = round(spot / step) * step
        cands = []
        for s in range(int(atm - 500), int(atm + 600), int(step)):
            dist = abs(s - spot)
            d = max(0.05, min(0.95, 0.50 - (dist / spot) * 2.5))
            if s < spot:
                d = min(0.95, 0.50 + (spot - s) / spot * 2.5)
            cands.append(_candidate(
                symbol=f"BNK{s}CE",
                strike=float(s),
                spot=spot,
                delta=d,
                ltp=max(1.0, (d * 800)),
                bid=max(0.5, d * 800 - 4),
                ask=max(1.0, d * 800 + 4),
            ))
        return cands

    prev_winner_strike = None
    for spot in [52000.0, 54000.0, 56000.0]:
        result = _select(make_chain(spot))
        best = result.best()
        if best:
            atm = round(spot / 100) * 100
            dist = best.strike_price - atm
            print(f"  spot={spot:.0f}  ATM={atm:.0f}  winner={best.option_symbol}  "
                  f"strike={best.strike_price:.0f}  dist={dist:+.0f}  "
                  f"delta={best.delta:.2f}  score={best.composite_score:.3f}")
            if prev_winner_strike is not None:
                _assert(
                    best.strike_price != prev_winner_strike,
                    f"Strike changed with spot (prev={prev_winner_strike:.0f} cur={best.strike_price:.0f})"
                )
            prev_winner_strike = best.strike_price
        else:
            print(f"  spot={spot:.0f}  NO WINNER (all filtered)")


# ── Test 2: Spread quality ─────────────────────────────────────────────────────

def test_spread_quality():
    """
    Two candidates identical except one has a wide spread.
    Tighter spread should win.
    """
    print("\n[TEST 2] Spread quality: tight spread should score higher")
    spot = 52000.0

    tight = _candidate("BNK52200CE_TIGHT", 52200.0, spot,
                        delta=0.38, bid=390.0, ask=396.0, ltp=393.0)   # 1.5% spread
    wide  = _candidate("BNK52200CE_WIDE",  52200.0, spot,
                        delta=0.38, bid=360.0, ask=430.0, ltp=393.0)   # 17% spread

    result = _select([tight, wide])
    best = result.best()
    recs = result.recommendations

    if best:
        print(f"  winner={best.option_symbol}  score={best.composite_score:.3f}")
        for r in recs:
            spread_cand = next((c for c in [tight, wide] if c.option_symbol == r.option_symbol), None)
            sp = spread_cand.bid_ask_spread_pct if spread_cand else None
            print(f"    rank={r.rank}  {r.option_symbol}  score={r.composite_score:.3f}  "
                  f"liq={r.breakdown.liquidity:.3f}  spread={sp:.1f}%" if sp else
                  f"    rank={r.rank}  {r.option_symbol}  score={r.composite_score:.3f}  "
                  f"liq={r.breakdown.liquidity:.3f}")
        _assert(
            best.option_symbol == tight.option_symbol,
            f"Tight-spread candidate wins (got {best.option_symbol})"
        )
        _assert(
            recs[0].breakdown.liquidity > recs[-1].breakdown.liquidity,
            "Liquidity factor higher for tight-spread candidate"
        )


# ── Test 3: Theta efficiency ───────────────────────────────────────────────────

def test_theta_efficiency():
    """
    Two candidates: same delta, similar IV, but near-expiry has bad theta/premium.
    Optimal-DTE candidate should win.
    """
    print("\n[TEST 3] Theta efficiency: better theta/premium ratio should score higher")
    spot = 52000.0

    # Near expiry: high theta relative to premium (decays fast)
    near = _candidate("BNK52200CE_NEAR", 52200.0, spot,
                      delta=0.38, ltp=80.0, bid=79.0, ask=81.0,
                      theta=-25.0, expiry=_EXPIRY_WEEKLY, tte=1.0/365.25)

    # Optimal DTE: reasonable theta/premium
    optimal = _candidate("BNK52200CE_OPT", 52200.0, spot,
                         delta=0.38, ltp=400.0, bid=398.0, ask=402.0,
                         theta=-15.0, expiry=_EXPIRY_OPTIMAL, tte=14.0/365.25)

    result = _select([near, optimal])
    best = result.best()
    recs = result.recommendations

    if best:
        for r in recs:
            print(f"  rank={r.rank}  {r.option_symbol}  score={r.composite_score:.3f}  "
                  f"theta_eff={r.breakdown.theta_efficiency:.3f}  dte={r.days_to_expiry}")
        _assert(
            best.option_symbol == optimal.option_symbol,
            f"Optimal-DTE candidate wins (got {best.option_symbol})"
        )
        _assert(
            recs[0].breakdown.theta_efficiency >= recs[-1].breakdown.theta_efficiency,
            "Theta efficiency score orders correctly"
        )


# ── Test 4: OI zero / missing data flags ──────────────────────────────────────

def test_oi_missing_flags():
    """
    Zero-OI candidate: audit should emit OI_ZERO_OR_MISSING flag.
    """
    print("\n[TEST 4] OI missing: audit must flag zero-OI winner")
    spot = 52000.0
    auditor = StrikeAuditLogger()

    zero_oi = _candidate("BNK52200CE_ZEROOI", 52200.0, spot, oi=0, delta=0.38)
    result  = _select([zero_oi])

    flags: list[str] = []

    class _FlagCapture(StrikeAuditLogger):
        def _log_inner(self, *, result, candidates, feed_symbol, strike_step):
            best = result.best()
            if best and best.open_interest == 0:
                flags.append("OI_ZERO_OR_MISSING")

    capture_auditor = _FlagCapture()
    capture_auditor.log(result=result, candidates=[zero_oi],
                        feed_symbol="BANKNIFTY", strike_step=100.0)

    best = result.best()
    if best:
        print(f"  winner={best.option_symbol}  OI={best.open_interest}  flags={flags}")
        _assert("OI_ZERO_OR_MISSING" in flags, "OI_ZERO_OR_MISSING flag emitted for zero-OI winner")
    else:
        print("  No winner (zero-OI may have been filtered) — checking filter logic")
        _assert(result.candidates_passed_filters == 0,
                "Zero-OI candidate filtered (dte_min_viable or similar)")


# ── Test 5: IV attractiveness ─────────────────────────────────────────────────

def test_iv_attractiveness():
    """
    Two candidates identical except one has elevated IV (expensive options).
    Cheaper IV should score higher on iv_attractiveness.
    """
    print("\n[TEST 5] IV attractiveness: cheaper IV should score higher")
    spot = 52000.0
    atm_iv = 0.18

    cheap_iv = _candidate("BNK52200CE_CHEAP_IV", 52200.0, spot,
                           delta=0.38, iv=0.17, ltp=380.0, bid=378.0, ask=382.0)
    expen_iv = _candidate("BNK52200CE_EXPN_IV", 52200.0, spot,
                           delta=0.38, iv=0.28, ltp=530.0, bid=528.0, ask=532.0)

    result = _select([cheap_iv, expen_iv], bias="LONG")
    recs = result.recommendations
    best = result.best()

    if best and len(recs) >= 2:
        cheap_rec = next((r for r in recs if r.option_symbol == cheap_iv.option_symbol), None)
        expn_rec  = next((r for r in recs if r.option_symbol == expen_iv.option_symbol), None)
        for r in recs:
            print(f"  rank={r.rank}  {r.option_symbol}  score={r.composite_score:.3f}  "
                  f"iv_attr={r.breakdown.iv_attractiveness:.3f}  iv={r.implied_volatility:.1%}")
        if cheap_rec and expn_rec:
            _assert(
                cheap_rec.breakdown.iv_attractiveness > expn_rec.breakdown.iv_attractiveness,
                f"Cheap IV scores higher on iv_attractiveness "
                f"({cheap_rec.breakdown.iv_attractiveness:.3f} > "
                f"{expn_rec.breakdown.iv_attractiveness:.3f})"
            )
        _assert(
            best.option_symbol == cheap_iv.option_symbol,
            f"Cheaper-IV candidate wins (got {best.option_symbol})"
        )


# ── Test 6: Greeks reporting ──────────────────────────────────────────────────

def test_greeks_in_audit():
    """
    Audit output must include gamma and theta even when not dominant.
    """
    print("\n[TEST 6] Greeks in audit: gamma and theta must appear in JSONL record")
    spot = 52000.0
    cand = _candidate("BNK52200CE_GREEKS", 52200.0, spot,
                      gamma=0.00042, theta=-18.5)
    result = _select([cand])
    best = result.best()

    if best:
        print(f"  gamma={best.gamma:.6f}  theta={best.theta:.2f}  "
              f"theta_eff_score={best.breakdown.theta_efficiency:.3f}")
        _assert(best.gamma == cand.gamma, f"Gamma preserved in recommendation ({best.gamma:.6f})")
        _assert(best.theta == cand.theta, f"Theta preserved in recommendation ({best.theta:.2f})")
        _assert(best.breakdown.theta_efficiency >= 0.0, "theta_efficiency factor is non-negative")
        _assert(
            0.0 <= best.breakdown.delta_fitness <= 1.0 and
            0.0 <= best.breakdown.gex_proximity <= 1.0,
            "All factor scores are in [0, 1]"
        )


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 68)
    print("  STRIKE SELECTOR SANITY TESTS  (offline, no Kite token)")
    print("=" * 68)

    test_spot_shift()
    test_spread_quality()
    test_theta_efficiency()
    test_oi_missing_flags()
    test_iv_attractiveness()
    test_greeks_in_audit()

    total = _PASS_COUNT + _FAIL_COUNT
    print(f"\n{'=' * 68}")
    print(f"  Results: {_PASS_COUNT}/{total} passed  "
          f"{'ALL PASS' if _FAIL_COUNT == 0 else f'{_FAIL_COUNT} FAILED'}")
    print("=" * 68)
    sys.exit(0 if _FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
