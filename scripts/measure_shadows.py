#!/usr/bin/env python3
"""
measure_shadows.py — one-shot post-close measurement of all shadow engines.

Runs the three read-only shadow harnesses (regime, HTF, conviction) and a short
trade summary in a single command, so each session can be evaluated at a glance.
Read-only; no policy mutation. Run after market close once a dry-run session has
produced shadow data.

Usage:
  PYTHONPATH=. python3 scripts/measure_shadows.py
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def _hdr(t: str) -> None:
    print("\n" + "=" * 70 + f"\n  {t}\n" + "=" * 70, flush=True)


def _today_trades() -> None:
    db = Path("logs/trades.db")
    if not db.exists():
        print("  (no trades.db)")
        return
    today = datetime.now(IST).date().isoformat()
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
    agg = defaultdict(lambda: [0, 0, 0.0])
    for r in c.execute("select underlying, pnl from pt_trades "
                       "where state='CLOSED' and substr(entry_ts,1,10)=?", (today,)):
        a = agg[r["underlying"]]; a[0] += 1; a[1] += 1 if r["pnl"] > 0 else 0; a[2] += r["pnl"]
    if not agg:
        print(f"  no closed trades for {today}")
        return
    tot = 0.0
    for sym, (n, w, p) in sorted(agg.items()):
        print(f"  {sym:9} {n} trades  {w}W/{n-w}L  ₹{p:+,.0f}")
        tot += p
    print(f"  {'TOTAL':9} ₹{tot:+,.0f}")


def main() -> int:
    _hdr(f"SHADOW MEASUREMENT — {datetime.now(IST):%Y-%m-%d %H:%M IST}")
    _hdr("TODAY'S TRADES")
    _today_trades()
    for title, script in [
        ("REGIME shadow", "scripts/analyze_regime_shadow.py"),
        ("HTF-ALIGNMENT shadow", "scripts/analyze_htf_shadow.py"),
        ("CONVICTION shadow", "scripts/analyze_conviction_shadow.py"),
    ]:
        _hdr(title)
        sys.stdout.flush()
        try:
            subprocess.run([sys.executable, script], check=False)
        except Exception as exc:
            print(f"  [error running {script}] {exc}")
    print("\n" + "=" * 70)
    print("  Reminder: promote a shadow into live sizing/gating ONLY when it")
    print("  separates winners OUT-OF-SAMPLE across a meaningful trade count.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
