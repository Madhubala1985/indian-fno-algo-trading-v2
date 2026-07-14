#!/usr/bin/env python3
"""
arm_readiness_check.py — the EDGE-readiness gate for arming real funds.

The 11/11 operational readiness gate checks plumbing (kill switches, reconciliation,
real-order path). It does NOT check the only thing that matters for real money:
does the strategy have a demonstrated, ROBUST positive edge? This script does.

It reads logs/trades.db and evaluates hard criteria that the model must pass on
recent dry-run data BEFORE arming. As of 2026-07-10 the model FAILS this (its
entire +₹14k is one day; ex-that it is −₹30k; LONG book −₹36k @34% WR).

Read-only. No trade effect. Run: python scripts/arm_readiness_check.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# ── Criteria (deliberately strict — this guards real money) ───────────────────
MIN_TRADING_DAYS      = 10      # need a meaningful recent sample
MIN_GREEN_FRACTION    = 0.55    # >55% of recent days net-positive
REQUIRE_EX_BEST_POS   = True    # net must stay positive after removing the best day (robustness)
MIN_LONG_WIN_RATE     = 0.45    # the LONG book must stop bleeding (was 34%)
RECENT_DAYS_WINDOW    = 12      # evaluate over the most recent N trading days


def main() -> int:
    db = Path("logs/trades.db")
    if not db.exists():
        print(f"[arm-check] {db} not found"); return 2
    c = sqlite3.connect(str(db))
    rows = c.execute(
        "select substr(entry_ts,1,10) d, direction, pnl from pt_trades "
        "where run_mode in ('live','real_dry_run') and pnl is not null order by entry_ts"
    ).fetchall()
    if not rows:
        print("[arm-check] no trades"); return 2

    per_day: dict[str, float] = {}
    for d, _dir, pnl in rows:
        per_day[d] = per_day.get(d, 0.0) + (pnl or 0.0)
    days = sorted(per_day)[-RECENT_DAYS_WINDOW:]
    window = {d: per_day[d] for d in days}
    long_pnls = [p for _d, dr, p in rows if dr == "LONG" and _d in window]
    lw = sum(1 for p in long_pnls if p > 0)

    net = sum(window.values())
    best_day = max(window, key=window.get) if window else None
    ex_best = net - window.get(best_day, 0.0)
    green = sum(1 for v in window.values() if v > 0)
    green_frac = green / len(window) if window else 0.0
    long_wr = (lw / len(long_pnls)) if long_pnls else 0.0

    checks = [
        (f"trading days >= {MIN_TRADING_DAYS}", len(window) >= MIN_TRADING_DAYS, f"{len(window)}"),
        (f"green fraction >= {MIN_GREEN_FRACTION:.0%}", green_frac >= MIN_GREEN_FRACTION, f"{green_frac:.0%} ({green}/{len(window)})"),
        ("net positive EX best day (robust)", (ex_best > 0) if REQUIRE_EX_BEST_POS else True, f"Rs {ex_best:,.0f} (best {best_day}=+{window.get(best_day,0):,.0f})"),
        (f"LONG win rate >= {MIN_LONG_WIN_RATE:.0%}", long_wr >= MIN_LONG_WIN_RATE, f"{long_wr:.0%} (n={len(long_pnls)})"),
    ]
    print(f"\n=== ARM-READINESS (edge) — last {len(window)} trading days ===")
    print(f"  net Rs {net:,.0f}   |   ex-best-day Rs {ex_best:,.0f}\n")
    all_pass = True
    for name, ok, detail in checks:
        all_pass &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name:38s} {detail}")
    print(f"\n  VERDICT: {'READY to arm (edge criteria met)' if all_pass else 'NOT READY — do not arm real funds'}\n")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
