#!/usr/bin/env python3
"""
conservative_fill_recompute.py — batch-4 §1b. Re-mark the 66-trade book conservatively:
entries at ASK, exits at BID, +1 tick slippage per side. Report the delta vs LTP (mid) marking.

Marking: ask = ltp·(1+spread_side)+tick ; bid = ltp·(1-spread_side)−tick.
Long-option book (always a buy): pnl = (exit − entry)·qty. Conservative = worse both sides.

HONESTY: the live cost model ALREADY charges spread (SPREAD_FRAC_PER_SIDE=0.25%/side), so the
stored NET P&L embeds the spread half of this. The genuinely ADDITIONAL haircut vs today's book
is the TICK SLIPPAGE (2 ticks/round-trip). Both are reported separately + per underlying.
"""
from __future__ import annotations

import sqlite3

SPREAD_SIDE = 0.0025   # matches transaction_costs.SPREAD_FRAC_PER_SIDE
TICK = 0.05            # NSE/BSE option tick


def main():
    c = sqlite3.connect("logs/trades.db")
    rows = c.execute("""select underlying, lots, lot_size, entry_premium, exit_premium, pnl
                        from pt_trades where exit_premium is not null and exit_premium>0""").fetchall()
    agg = {}   # underlying -> dict of sums
    tot = dict(ltp=0.0, cons=0.0, tick=0.0, spread=0.0, net=0.0, n=0)
    for u, lots, ls, ep, xp, pnl in rows:
        qty = (lots or 0) * (ls or 0)
        if qty <= 0 or not ep or not xp:
            continue
        ltp_gross = (xp - ep) * qty
        ask = ep * (1 + SPREAD_SIDE) + TICK
        bid = xp * (1 - SPREAD_SIDE) - TICK
        cons_gross = (bid - ask) * qty
        tick_only = -(2 * TICK) * qty                        # additional vs today
        spread_only = -(SPREAD_SIDE * (ep + xp)) * qty       # already in live cost model
        a = agg.setdefault(u, dict(ltp=0.0, cons=0.0, tick=0.0, spread=0.0, net=0.0, n=0))
        for d in (a, tot):
            d["ltp"] += ltp_gross; d["cons"] += cons_gross; d["tick"] += tick_only
            d["spread"] += spread_only; d["net"] += (pnl or 0.0); d["n"] += 1

    print("=" * 96)
    print("  §1b CONSERVATIVE FILL RE-MARK — ASK entry / BID exit / +1 tick per side")
    print(f"  spread/side={SPREAD_SIDE:.2%}  tick=₹{TICK}   ({tot['n']} trades with exit prices)")
    print("=" * 96)
    hdr = f"  {'bucket':10} {'LTP-gross':>12} {'CONS-gross':>12} {'delta':>10} {'  of which tick':>16} {'spread(in-cost)':>16}"
    print(hdr); print("  " + "-" * 92)
    for u in sorted(agg):
        a = agg[u]
        delta = a["cons"] - a["ltp"]
        print(f"  {u:10} {a['ltp']:>+12,.0f} {a['cons']:>+12,.0f} {delta:>+10,.0f} {a['tick']:>+16,.0f} {a['spread']:>+16,.0f}")
    d = tot["cons"] - tot["ltp"]
    print("  " + "-" * 92)
    print(f"  {'ALL':10} {tot['ltp']:>+12,.0f} {tot['cons']:>+12,.0f} {d:>+10,.0f} {tot['tick']:>+16,.0f} {tot['spread']:>+16,.0f}")
    print("=" * 96)
    print(f"  Stored NET book (already includes spread cost): ₹{tot['net']:+,.0f}")
    print(f"  → ADDITIONAL haircut not yet in the book (tick slippage): ₹{tot['tick']:+,.0f}")
    print(f"  → Conservative NET book (stored NET + tick slippage):     ₹{tot['net'] + tot['tick']:+,.0f}")
    print(f"    (the spread half of the delta, ₹{tot['spread']:+,.0f}, is ALREADY in the stored NET via the cost model)")
    print("=" * 96)


if __name__ == "__main__":
    raise SystemExit(main())
