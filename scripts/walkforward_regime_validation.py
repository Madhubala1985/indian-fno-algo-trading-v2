#!/usr/bin/env python3
"""
walkforward_regime_validation.py — OOS validation of a regime-selectivity filter.

Per docs/intelligence_wiring_plan.md: before promoting the (already shadow-wired)
RegimeEngine from shadow to a live gate, prove it out-of-sample. This harness:

  1. Joins the per-bar regime shadow (logs/regime_shadow.jsonl) to every recorded
     live trade (logs/trades.db) by (underlying, nearest bar <= entry).
  2. Walks a chronological split forward one day at a time: for each split it
     TUNES which regime buckets are "favorable" (net-positive P&L) on the EARLY
     (in-sample) trades only, then applies that FIXED rule to the LATER (holdout)
     trades it never tuned on, and reports holdout P&L WITH vs WITHOUT the filter.

The headline is the pooled holdout result: summing every held-out trade's outcome
under the always-pre-committed filter vs trading everything. A positive, stable
delta is the evidence to promote; a fragile/negative one says "not yet — more
data". 30 trades is a small sample — read the per-split detail, not just the total.

Usage:
  PYTHONPATH=. python3 scripts/walkforward_regime_validation.py
  PYTHONPATH=. python3 scripts/walkforward_regime_validation.py --dim vol_regime --min-train 6
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REGIME_LOG = Path("logs/regime_shadow.jsonl")
TRADES_DB = Path("logs/trades.db")


def _load_regime() -> dict[str, list[dict]]:
    recs: dict[str, list[dict]] = {}
    for line in REGIME_LOG.open():
        try:
            r = json.loads(line)
        except Exception:
            continue
        recs.setdefault(r["underlying"], []).append(r)
    for u in recs:
        recs[u].sort(key=lambda r: r["ts"])
    return recs


def _nearest(recs: dict, und: str, ts: datetime, max_sec: int = 240) -> dict | None:
    best = None
    for r in recs.get(und, []):
        rt = datetime.fromisoformat(r["ts"])
        if rt <= ts and (ts - rt).total_seconds() <= max_sec:
            if best is None or rt > datetime.fromisoformat(best["ts"]):
                best = r
    return best


def _regime_label(rec: dict | None, dim: str) -> str | None:
    if rec is None:
        return None
    if dim == "combined":
        v, d = rec.get("vol_regime"), rec.get("dir_regime")
        return f"{v}/{d}" if v and d else None
    return rec.get(dim)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", default="vol_regime",
                    choices=["vol_regime", "dir_regime", "combined"],
                    help="regime dimension used as the filter key")
    ap.add_argument("--min-train", type=int, default=4,
                    help="minimum trades in the tuning window before a split scores")
    ap.add_argument("--since", default="2026-06-24", help="earliest trade date")
    args = ap.parse_args()

    regime = _load_regime()
    c = sqlite3.connect(str(TRADES_DB))
    c.row_factory = sqlite3.Row
    trades = c.execute(
        "SELECT underlying,direction,entry_ts,pnl FROM pt_trades "
        "WHERE run_mode='live' AND entry_ts>=? ORDER BY entry_ts", (args.since,),
    ).fetchall()

    rows = []
    for t in trades:
        ts = datetime.fromisoformat(t["entry_ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        label = _regime_label(_nearest(regime, t["underlying"], ts), args.dim)
        if label is None:
            continue  # no regime coverage for this trade — excluded
        rows.append(dict(ts=ts, day=t["entry_ts"][:10], und=t["underlying"],
                         pnl=float(t["pnl"]), regime=label))
    rows.sort(key=lambda r: r["ts"])

    print(f"regime dim = {args.dim}   matched trades = {len(rows)}")
    # pooled per-bucket context (in-sample overview, NOT the validation)
    buckets: dict[str, list[float]] = {}
    for r in rows:
        buckets.setdefault(r["regime"], []).append(r["pnl"])
    print("\n[context] pooled per-regime (in-sample):")
    for k, v in sorted(buckets.items(), key=lambda kv: -sum(kv[1])):
        w = sum(1 for x in v if x > 0)
        print(f"    {k:28} n={len(v):>2}  WR={w}/{len(v)}  P&L=Rs{sum(v):+,.0f}")

    # walk-forward: split at each day boundary
    days = sorted({r["day"] for r in rows})
    print("\n[walk-forward] tune favorable regimes on EARLY trades, apply to HELD-OUT day(s):")
    print(f"    {'holdout>=':12}{'train_n':>8}{'hold_n':>7}{'favorable buckets (tuned)':35}"
          f"{'hold ALL':>11}{'hold FILT':>11}{'delta':>9}")
    tot_all = tot_filt = tot_hold = 0
    for i in range(1, len(days)):
        split_day = days[i]
        train = [r for r in rows if r["day"] < split_day]
        hold = [r for r in rows if r["day"] >= split_day]
        if len(train) < args.min_train or not hold:
            continue
        # TUNE (train only): favorable = regime buckets with net-positive P&L in train
        tb: dict[str, float] = {}
        for r in train:
            tb[r["regime"]] = tb.get(r["regime"], 0.0) + r["pnl"]
        favorable = {k for k, v in tb.items() if v > 0}
        # APPLY fixed rule to holdout (never tuned on these)
        hold_all = sum(r["pnl"] for r in hold)
        hold_filt = sum(r["pnl"] for r in hold if r["regime"] in favorable)
        fav_str = ",".join(sorted(s.split("/")[0][:8] for s in favorable)) or "(none)"
        print(f"    {split_day:12}{len(train):>8}{len(hold):>7}{fav_str:35}"
              f"{hold_all:>+11,.0f}{hold_filt:>+11,.0f}{hold_filt-hold_all:>+9,.0f}")
        # accumulate the NEXT held-out day only (true forward step, no overlap)
        next_day_hold = [r for r in hold if r["day"] == split_day]
        tot_all += sum(r["pnl"] for r in next_day_hold)
        tot_filt += sum(r["pnl"] for r in next_day_hold if r["regime"] in favorable)
        tot_hold += len(next_day_hold)

    print(f"\n[OOS TOTAL] each day scored once, filter pre-committed from prior days only:")
    print(f"    held-out trades={tot_hold}   trade-ALL Rs{tot_all:+,.0f}   "
          f"regime-FILTER Rs{tot_filt:+,.0f}   delta Rs{tot_filt-tot_all:+,.0f}")
    print("    (positive+stable delta => promote regime shadow->live gate; else keep accumulating)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
