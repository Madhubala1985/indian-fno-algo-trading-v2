#!/usr/bin/env python3
"""
analyze_conviction_shadow.py — measurement harness for the Phase-2 conviction shadow.

Answers: does the transparent conviction score actually SEPARATE winners from
losers? (If higher-conviction trades win more / earn more, it should drive
conviction sizing — replacing the saturated MCB conf.)

Reads logs/conviction_shadow.jsonl (per-decision conviction), joins each executed
trade in logs/trades.db to the nearest-prior decision (no lookahead) by
underlying, and buckets win-rate / avg PnL by conviction tertile. Also reports
the conviction DISTRIBUTION (it must vary — if it's flat it's no better than MCB).
Read-only.

Usage:
  PYTHONPATH=. python3 scripts/analyze_conviction_shadow.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

SHADOW = Path("logs/conviction_shadow.jsonl")
DB = Path("logs/trades.db")


def main() -> int:
    if not SHADOW.exists():
        print(f"\n[no data] {SHADOW} not found yet — run dry-run session(s) with the\n"
              "conviction shadow enabled (PAPER_CONV_SHADOW=1, the default).")
        return 0
    rows = []
    by_sym: dict[str, list] = defaultdict(list)
    for line in SHADOW.read_text().splitlines():
        try:
            d = json.loads(line)
            rows.append(d)
            by_sym[d["underlying"]].append((d["ts"], d["conviction"]))
        except Exception:
            continue
    for s in by_sym:
        by_sym[s].sort()

    convs = [d["conviction"] for d in rows]
    print(f"\nConviction DISTRIBUTION over {len(convs)} decisions:")
    if convs:
        print(f"  min {min(convs):.3f}  median {statistics.median(convs):.3f}  "
              f"max {max(convs):.3f}  stdev {statistics.pstdev(convs):.3f}")
        if statistics.pstdev(convs) < 0.02:
            print("  ⚠ nearly FLAT — not differentiating (no better than MCB).")
        else:
            print("  ✓ varies — it grades setups (unlike the saturated MCB/ConfidenceEngine).")

    if not DB.exists():
        print(f"\n[no db] {DB} not found.")
        return 0

    def conv_at(sym: str, ts_iso: str):
        best = None
        for ts, cv in by_sym.get(sym, []):
            if ts <= ts_iso:
                best = cv
            else:
                break
        return best

    c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
    matched = []
    for r in c.execute("select underlying, entry_ts, pnl from pt_trades where state='CLOSED'"):
        cv = conv_at(r["underlying"], r["entry_ts"])
        if cv is not None:
            matched.append((cv, r["pnl"]))

    print(f"\nConviction vs trade outcome  (matched {matched and len(matched) or 0} trades):")
    if len(matched) < 3:
        print("  (need more matched trades — gather dry-run data, then re-run.)")
        return 0
    matched.sort()
    n = len(matched)
    thirds = [matched[: n // 3], matched[n // 3: 2 * n // 3], matched[2 * n // 3:]]
    labels = ["LOW conviction", "MID conviction", "HIGH conviction"]
    print(f"  {'bucket':16} {'n':>3} {'wins':>5} {'WR%':>5} {'avgPnL':>9}  conv-range")
    for lab, b in zip(labels, thirds):
        if not b:
            continue
        wins = sum(1 for cv, p in b if p > 0)
        avg = sum(p for cv, p in b) / len(b)
        print(f"  {lab:16} {len(b):>3} {wins:>5} {100*wins/len(b):>5.0f} {avg:>9.0f}"
              f"  [{b[0][0]:.2f}..{b[-1][0]:.2f}]")
    print("\nPROMOTE conviction sizing only if HIGH clearly beats LOW on win-rate /\n"
          "avgPnL across a meaningful, out-of-sample sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
