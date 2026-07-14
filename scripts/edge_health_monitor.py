#!/usr/bin/env python3
"""
edge_health_monitor.py — batch-4 §5. Shadow-only meta-layer. Detects OUR OWN edge decaying
(not market prediction): rolling per-bucket expectancy over LONG windows, logging the
position-size multiplier it WOULD apply. NOT wired to sizing — it accrues a shadow track record
like every other stream (logs/edge_health_shadow.jsonl).

Bucket = direction × underlying (× regime if joinable). Window = last N trades (default 20).
PRE-REGISTERED FAILURE MODE: short windows chase noise — a bucket with < N trades is reported
INSUFFICIENT and its multiplier is pinned at 1.0 (no throttle on noise). Long windows only.

Multiplier ladder (expectancy = mean net P&L/trade over the window):
   exp > 0        → 1.00 (healthy)      -500 < exp ≤ 0 → 0.75 (soft)
  -2000 < exp ≤ -500 → 0.50 (throttle)  exp ≤ -2000    → 0.25 (deep throttle)
Recovery is automatic: as the window rolls into positive expectancy the multiplier restores.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from statistics import mean

WINDOW = 20


def _mult(exp: float) -> float:
    if exp > 0:
        return 1.00
    if exp > -500:
        return 0.75
    if exp > -2000:
        return 0.50
    return 0.25


def _ex_best_day(trades):
    by_day = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]
    if not by_day:
        return 0.0
    best = max(by_day, key=by_day.get)
    ex = [t for t in trades if t["day"] != best]
    return mean(t["pnl"] for t in ex) if ex else 0.0


def main():
    c = sqlite3.connect("logs/trades.db")
    buckets = {}
    for r in c.execute("""select underlying, direction, pnl, entry_ts from pt_trades
                          where pnl is not null order by entry_ts"""):
        u, d, pnl, ets = r
        buckets.setdefault((u, d), []).append({"pnl": pnl, "day": (ets or "")[:10], "ts": ets})

    now = datetime.now(timezone.utc).isoformat()
    out = open("logs/edge_health_shadow.jsonl", "a")
    print("=" * 96)
    print(f"  §5 EDGE-HEALTH MONITOR (shadow-only, NOT wired) — window={WINDOW} trades")
    print("=" * 96)
    print(f"  {'bucket':16} {'n':>3} {'window':>7} {'exp/trade':>10} {'ex-best':>9} {'MULT':>6}  status")
    print("  " + "-" * 90)
    for (u, d), ts in sorted(buckets.items()):
        n = len(ts)
        win = ts[-WINDOW:]
        exp = mean(t["pnl"] for t in win)
        exb = _ex_best_day(win)
        if n < WINDOW:
            mult, status = 1.00, f"INSUFFICIENT (<{WINDOW}) — no throttle on noise"
        else:
            # throttle only if BOTH raw and ex-best-day expectancy are weak (one-day guard)
            mult = _mult(max(exp, exb) if exp > 0 or exb > 0 else min(exp, exb))
            status = "healthy" if mult == 1.0 else f"THROTTLE ×{mult}"
        print(f"  {u+' '+d:16} {n:>3} {len(win):>7} {exp:>+10,.0f} {exb:>+9,.0f} {mult:>6.2f}  {status}")
        out.write(json.dumps({"ts": now, "bucket": f"{u}_{d}", "n": n, "window": len(win),
                              "expectancy": round(exp, 1), "expectancy_ex_best_day": round(exb, 1),
                              "would_apply_mult": mult}) + "\n")
    out.close()
    print("=" * 96)
    print("  Logged to logs/edge_health_shadow.jsonl. A bucket must sustain a throttle across")
    print("  many rolls (not one window) before the multiplier is ever considered for wiring.")
    print("=" * 96)


if __name__ == "__main__":
    raise SystemExit(main())
