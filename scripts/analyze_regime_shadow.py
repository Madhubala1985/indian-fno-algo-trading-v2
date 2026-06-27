#!/usr/bin/env python3
"""
analyze_regime_shadow.py — measurement harness for the Phase-1 regime shadow.

Per docs/intelligence_wiring_plan.md: an engine is wired in SHADOW first, then
measured OUT-OF-SAMPLE before any promotion. This harness answers the only
question that matters for the regime wiring:

    Do trades taken in HIGH-volatility regimes actually win more / earn more than
    those in LOW/NORMAL regimes?  (If yes, regime-based selectivity/sizing helps.)

Two modes:
  (default)  live   — read logs/regime_shadow.jsonl (produced by dry-run sessions)
                      and join to executed trades in logs/trades.db by underlying
                      + nearest-prior bar timestamp; report win-rate / avg PnL per
                      regime. This is the real test once dry-run data exists.
  --csv F           — retrospective: classify regime over a historical 1m CSV and
                      report the regime DISTRIBUTION (proves the signal is alive
                      and varied; no outcome join). Use for immediate sanity.

Read-only. No policy mutation, no trades.

Usage:
  PYTHONPATH=. python3 scripts/analyze_regime_shadow.py
  PYTHONPATH=. python3 scripts/analyze_regime_shadow.py --csv data/sensex_1m_2026-05-15_2026-06-18.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from platform_core.adaptive.regime_engine import RegimeEngine

SHADOW_PATH = Path("logs/regime_shadow.jsonl")
DB_PATH = Path("logs/trades.db")


def _classify_series(rows, window=20):
    """Yield (ts, vol_regime, conf_mult) per bar, causal (avg over prior bars)."""
    eng = RegimeEngine()
    buf: deque = deque(maxlen=window)
    for r in rows:
        rng = max(float(r["high"]) - float(r["low"]), 0.0)
        avg = (sum(buf) / len(buf)) if buf else rng
        buf.append(rng)
        if avg <= 0.0:
            continue
        st = eng.classify(bar_range=rng, average_range=avg)
        yield r["timestamp"], st.volatility_regime, st.confidence_multiplier


def run_csv(path: str, window: int) -> None:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    dist: dict[str, int] = defaultdict(int)
    n = 0
    for _ts, vol, _m in _classify_series(rows, window):
        dist[vol] += 1
        n += 1
    print(f"\nRegime distribution over {path}  ({n} bars, window={window}):")
    for k in ("HIGH_VOLATILITY", "NORMAL", "LOW_VOLATILITY"):
        c = dist.get(k, 0)
        print(f"  {k:16} {c:>6}  ({100*c/max(n,1):4.1f}%)")
    print("\n(Signal is meaningful if it is NOT stuck on one label. The "
          "best-day thesis = SENSEX edge concentrates in HIGH_VOLATILITY.)")


def run_live(window: int) -> None:
    if not SHADOW_PATH.exists():
        print(f"\n[no data] {SHADOW_PATH} not found yet — run dry-run session(s) with the\n"
              "regime shadow enabled to generate it, then re-run this harness.\n"
              "For an immediate signal check use: --csv <historical 1m csv>")
        return
    # Index shadow records by underlying -> sorted [(ts, vol_regime, conf_mult)]
    shadow: dict[str, list] = defaultdict(list)
    for line in SHADOW_PATH.read_text().splitlines():
        try:
            d = json.loads(line)
            shadow[d["underlying"]].append((d["ts"], d["vol_regime"], d.get("conf_mult")))
        except Exception:
            continue
    for u in shadow:
        shadow[u].sort()

    def regime_at(underlying: str, ts_iso: str):
        """Nearest-prior shadow bar for this trade's entry (no lookahead)."""
        recs = shadow.get(underlying, [])
        best = None
        for ts, vol, _m in recs:
            if ts <= ts_iso:
                best = vol
            else:
                break
        return best

    if not DB_PATH.exists():
        print(f"[no db] {DB_PATH} not found.")
        return
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    agg = defaultdict(lambda: [0, 0, 0.0])  # regime -> [n, wins, pnl]
    matched = unmatched = 0
    for r in c.execute("select underlying, entry_ts, pnl from pt_trades where state='CLOSED'"):
        reg = regime_at(r["underlying"], r["entry_ts"])
        if reg is None:
            unmatched += 1
            continue
        matched += 1
        agg[reg][0] += 1
        agg[reg][1] += 1 if r["pnl"] > 0 else 0
        agg[reg][2] += r["pnl"]
    print(f"\nRegime vs trade outcome  (matched {matched} trades, {unmatched} unmatched):")
    print(f"  {'regime':16} {'n':>3} {'wins':>5} {'WR%':>5} {'avgPnL':>9}")
    for reg in ("HIGH_VOLATILITY", "NORMAL", "LOW_VOLATILITY"):
        n, w, p = agg.get(reg, [0, 0, 0.0])
        if n:
            print(f"  {reg:16} {n:>3} {w:>5} {100*w/n:>5.0f} {p/n:>9.0f}")
    print("\nPROMOTE regime sizing only if HIGH_VOLATILITY win-rate / avgPnL clearly\n"
          "beats LOW_VOLATILITY on a meaningful, out-of-sample sample.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Regime shadow measurement harness (read-only)")
    ap.add_argument("--csv", default="", help="classify a historical 1m CSV (distribution mode)")
    ap.add_argument("--window", type=int, default=20, help="rolling range window (default 20)")
    args = ap.parse_args()
    if args.csv:
        run_csv(args.csv, args.window)
    else:
        run_live(args.window)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
