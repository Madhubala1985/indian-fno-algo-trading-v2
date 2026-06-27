#!/usr/bin/env python3
"""
analyze_htf_shadow.py — measurement harness for the Phase-1 HTF-bias shadow.

Per docs/intelligence_wiring_plan.md. Answers the only question that matters for
the HTF wiring:

    Do trades ALIGNED with the higher-timeframe bias win more / earn more than
    trades that FIGHT it?  (If yes, HTF alignment should filter/down-weight.)

Reads logs/htf_shadow.jsonl (produced by dry-run sessions), joins each executed
trade in logs/trades.db to the nearest-prior shadow bar (no lookahead) by
underlying, and buckets win-rate / avg PnL by how many HTF timeframes agreed with
the trade's bias. Read-only.

Usage:
  PYTHONPATH=. python3 scripts/analyze_htf_shadow.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

SHADOW_PATH = Path("logs/htf_shadow.jsonl")
DB_PATH = Path("logs/trades.db")


def main() -> int:
    if not SHADOW_PATH.exists():
        print(f"\n[no data] {SHADOW_PATH} not found yet — run dry-run session(s) with the\n"
              "HTF shadow enabled (PAPER_HTF_SHADOW=1, the default) to generate it.")
        return 0
    shadow: dict[str, list] = defaultdict(list)
    for line in SHADOW_PATH.read_text().splitlines():
        try:
            d = json.loads(line)
            shadow[d["underlying"]].append((d["ts"], d.get("htf_agree"), d.get("htf_against")))
        except Exception:
            continue
    for u in shadow:
        shadow[u].sort()

    def agree_at(underlying: str, ts_iso: str):
        recs = shadow.get(underlying, [])
        best = None
        for ts, ag, _ag2 in recs:
            if ts <= ts_iso:
                best = ag
            else:
                break
        return best

    if not DB_PATH.exists():
        print(f"[no db] {DB_PATH} not found.")
        return 0
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    # bucket by alignment strength: 0-1 = weak/against, 2 = mixed, 3-4 = strong-aligned
    agg = defaultdict(lambda: [0, 0, 0.0])
    matched = unmatched = 0

    def bucket(a):
        if a is None:
            return None
        if a >= 3:
            return "aligned (3-4 TF)"
        if a == 2:
            return "mixed (2 TF)"
        return "weak/against (0-1 TF)"

    for r in c.execute("select underlying, entry_ts, pnl from pt_trades where state='CLOSED'"):
        b = bucket(agree_at(r["underlying"], r["entry_ts"]))
        if b is None:
            unmatched += 1
            continue
        matched += 1
        agg[b][0] += 1
        agg[b][1] += 1 if r["pnl"] > 0 else 0
        agg[b][2] += r["pnl"]

    print(f"\nHTF alignment vs trade outcome  (matched {matched}, {unmatched} unmatched):")
    print(f"  {'alignment':22} {'n':>3} {'wins':>5} {'WR%':>5} {'avgPnL':>9}")
    for b in ("aligned (3-4 TF)", "mixed (2 TF)", "weak/against (0-1 TF)"):
        n, w, p = agg.get(b, [0, 0, 0.0])
        if n:
            print(f"  {b:22} {n:>3} {w:>5} {100*w/n:>5.0f} {p/n:>9.0f}")
    print("\nPROMOTE HTF filtering/down-weighting only if 'aligned' clearly beats\n"
          "'weak/against' on a meaningful, out-of-sample sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
