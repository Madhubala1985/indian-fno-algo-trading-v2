#!/usr/bin/env python3
"""
cost_hurdle.py — batch-4 §1c. Per-trade and per-underlying HURDLE = round-trip cost + theta
over the median hold, as % of entry premium. Decides whether NIFTY's preference-down is
MECHANICAL (structurally worse hurdle) or scar tissue (just a bad run).

round-trip cost%  : exact, from the live transaction_costs model.
theta%            : ESTIMATE — ATM rule-of-thumb daily theta ≈ 0.5/DTE of premium, prorated by
                    the trade's hold fraction of the 375-min session. Approximate in absolute
                    terms, but applied identically to both symbols so the COMPARISON is fair.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from statistics import median

from platform_core.execution.transaction_costs import apply_costs

_SESSION_MIN = 375


def main():
    c = sqlite3.connect("logs/trades.db")
    rows = c.execute("""select underlying, lots, lot_size, entry_premium, exit_premium,
                               expiry_date, entry_ts, bars_held, pnl
                        from pt_trades where entry_premium>0""").fetchall()
    per = {}   # underlying -> list of dicts
    for u, lots, ls, ep, xp, expiry, ets, bars, pnl in rows:
        qty = (lots or 0) * (ls or 0)
        if qty <= 0 or not ep:
            continue
        _, cost = apply_costs(0.0, entry_premium=ep, exit_premium=(xp or ep),
                              quantity=qty, underlying=u)
        cost_pct = 100.0 * cost / (ep * qty)
        # DTE
        try:
            dte = max((date.fromisoformat(expiry[:10]) - date.fromisoformat(ets[:10])).days, 0) + 0.5
        except Exception:
            dte = 1.0
        hold_frac = (bars or 0) / _SESSION_MIN
        theta_day_frac = 0.5 / dte           # ATM rule of thumb (fraction of premium/day)
        theta_pct = 100.0 * theta_day_frac * hold_frac
        per.setdefault(u, []).append(dict(cost_pct=cost_pct, theta_pct=theta_pct,
                                          hurdle=cost_pct + theta_pct, bars=(bars or 0),
                                          ep=ep, dte=dte, pnl=pnl or 0.0))

    print("=" * 92)
    print("  §1c COST HURDLE — round-trip cost + theta over median hold, as % of entry premium")
    print("=" * 92)
    print(f"  {'undl':7} {'n':>3} {'med_prem':>9} {'med_DTE':>8} {'med_hold':>9} "
          f"{'cost%':>7} {'theta%':>7} {'HURDLE%':>8}")
    print("  " + "-" * 84)
    summary = {}
    for u in sorted(per):
        ts = per[u]
        med_cost = median(t["cost_pct"] for t in ts)
        med_theta = median(t["theta_pct"] for t in ts)
        med_hurdle = median(t["hurdle"] for t in ts)
        summary[u] = med_hurdle
        print(f"  {u:7} {len(ts):>3} {median(t['ep'] for t in ts):>9.1f} "
              f"{median(t['dte'] for t in ts):>8.1f} {median(t['bars'] for t in ts):>7.0f}m "
              f"{med_cost:>6.2f}% {med_theta:>6.2f}% {med_hurdle:>7.2f}%")
    print("=" * 92)
    if "NIFTY" in summary and "SENSEX" in summary:
        nh, sh = summary["NIFTY"], summary["SENSEX"]
        worse = nh > sh
        print(f"  NIFTY hurdle {nh:.2f}%  vs  SENSEX {sh:.2f}%  →  NIFTY is "
              f"{'STRUCTURALLY WORSE (mechanical)' if worse else 'NOT worse (preference = scar tissue, revisit)'}"
              f" by {abs(nh-sh):.2f}pp")
        print("  Driver: NIFTY's lower premium makes fixed costs + the ₹0.05 tick a bigger % of premium;")
        print("  a trade must clear this hurdle before any edge shows. Mechanism, sample sizes above.")
    print("=" * 92)


if __name__ == "__main__":
    raise SystemExit(main())
