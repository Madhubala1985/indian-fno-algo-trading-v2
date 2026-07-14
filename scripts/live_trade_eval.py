#!/usr/bin/env python3
"""
live_trade_eval.py — validate the live gates + re-check replay-derived A/B verdicts on the
ONLY trustworthy corpus: real live trades (logs/trades.db pt_trades). Nothing here uses the
unfaithful SurgicalReplay (see validation_reports/replay_parity_and_ab_trust.md).

Covers:
  (Task #12) LONG_ALIGNMENT_GATE + NIFTY_LONG_GATE validation —
     * LONG vs SHORT P&L split (the core thesis: shorts profit, longs bleed)
     * per-symbol × direction buckets (NIFTY-long the worst?)
     * aligned vs misaligned LONG, joined to htf_shadow.jsonl is_aligned (valid 07-10+ only)
     * estimated gate impact = P&L of the LONG trades each gate would have blocked
  (Task #13) conviction-floor A/B re-check on live conf fields (memory: conviction anti-predictive)
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from pathlib import Path


def _live_trades():
    c = sqlite3.connect("logs/trades.db")
    return [dict(underlying=r[0], direction=r[1], conf_raw=r[2], conf_eff=r[3], pnl=r[4],
                 entry_ts=r[5], lots=r[6], day=(r[5] or "")[:10])
            for r in c.execute("""select underlying,direction,conf_raw,conf_eff,pnl,entry_ts,lots
                                  from pt_trades order by entry_ts""")]


def _htf_aligned_index():
    """{underlying: (sorted_ts_list, {ts: is_aligned})} from htf_shadow.jsonl (non-null only)."""
    idx: dict[str, dict] = {}
    p = Path("logs/htf_shadow.jsonl")
    if not p.exists():
        return idx
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("is_aligned") is None:
            continue
        idx.setdefault(d["underlying"], {})[d["ts"]] = bool(d["is_aligned"])
    return {u: (sorted(m), m) for u, m in idx.items()}


def _aligned_at(idx, underlying, entry_ts):
    rec = idx.get(underlying)
    if not rec:
        return None
    ts_list, m = rec
    i = bisect_right(ts_list, entry_ts) - 1   # nearest bar at/<= entry
    return m[ts_list[i]] if i >= 0 else None


def _fmt(title, trades):
    """MANDATORY: every bucket prints ex-best-day. A bucket P&L carried by one day is noise,
    not an edge — the ex-best-day column exposes exactly that (batch-4 directive #0)."""
    n = len(trades)
    if not n:
        print(f"  {title:34} (0 trades)")
        return
    pnl = sum(t["pnl"] for t in trades)
    w = sum(1 for t in trades if t["pnl"] > 0)
    # ex-best-day: drop the single most-profitable DAY (all its trades) from the bucket
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]
    best_day = max(by_day, key=by_day.get) if by_day else None
    best_val = by_day.get(best_day, 0.0)
    ex_trades = [t for t in trades if t["day"] != best_day]
    ex_pnl = sum(t["pnl"] for t in ex_trades)
    ex = (f"ex-best ₹{ex_pnl:>+8,.0f}/{len(ex_trades):>2}t (drop {best_day} ₹{best_val:+,.0f})"
          if best_day else "")
    print(f"  {title:34} n={n:>3}  P&L ₹{pnl:>+9,.0f}  WR {100*w/n:3.0f}%  avg ₹{pnl/n:>+7,.0f}  |  {ex}")


def main():
    trades = _live_trades()
    idx = _htf_aligned_index()
    print("=" * 92)
    print(f"  LIVE-TRADE EVALUATION — {len(trades)} real trades (trades.db); replay NOT used")
    print("=" * 92)

    print("\n── Task #12a: direction split (alignment-gate thesis) ──")
    _fmt("ALL", trades)
    _fmt("LONG", [t for t in trades if t["direction"] == "LONG"])
    _fmt("SHORT", [t for t in trades if t["direction"] == "SHORT"])

    print("\n── Task #12b: per-symbol × direction (NIFTY-long gate) ──")
    for u in sorted({t["underlying"] for t in trades}):
        for d in ("LONG", "SHORT"):
            _fmt(f"{u} {d}", [t for t in trades if t["underlying"] == u and t["direction"] == d])

    print("\n── Task #12c: aligned vs misaligned LONG (htf_shadow is_aligned; valid 07-10+ only) ──")
    longs = [t for t in trades if t["direction"] == "LONG"]
    tagged = [(t, _aligned_at(idx, t["underlying"], t["entry_ts"])) for t in longs]
    have = [(t, a) for t, a in tagged if a is not None]
    print(f"  LONG trades with alignment data: {len(have)}/{len(longs)} "
          f"(rest predate the htf_shadow null-fix)")
    _fmt("LONG & aligned",     [t for t, a in have if a])
    _fmt("LONG & MISALIGNED",  [t for t, a in have if not a])

    print("\n── Task #12d: estimated gate impact (P&L the gates would REMOVE) ──")
    mis_long_pnl = sum(t["pnl"] for t, a in have if not a)
    nifty_long = [t for t in trades if t["direction"] == "LONG"
                  and t["underlying"] in ("NIFTY", "NIFTY50")]
    print(f"  LONG_ALIGNMENT_GATE would drop misaligned longs: ₹{mis_long_pnl:+,.0f} "
          f"({'saves' if mis_long_pnl < 0 else 'COSTS'} the book)")
    print(f"  NIFTY_LONG_GATE would drop ALL NIFTY longs:      ₹{sum(t['pnl'] for t in nifty_long):+,.0f} "
          f"(n={len(nifty_long)})")

    print("\n── Task #13: conviction-floor A/B re-check on LIVE conf (memory: anti-predictive) ──")
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    def _avg(ts, k):
        v = [t[k] for t in ts if t[k] is not None]
        return sum(v) / len(v) if v else float("nan")
    print(f"  avg conf_raw  winners={_avg(winners,'conf_raw'):.3f}  losers={_avg(losers,'conf_raw'):.3f}")
    print(f"  avg conf_eff  winners={_avg(winners,'conf_eff'):.3f}  losers={_avg(losers,'conf_eff'):.3f}")
    for floor in (0.40, 0.60, 0.80):
        kept = [t for t in trades if (t["conf_eff"] or 0) >= floor]
        dropped = [t for t in trades if (t["conf_eff"] or 0) < floor]
        print(f"  conv_floor {floor:.2f}: keep {len(kept)} (₹{sum(t['pnl'] for t in kept):+,.0f}) | "
              f"drop {len(dropped)} (₹{sum(t['pnl'] for t in dropped):+,.0f})")
    print("\n  NOTE: verdicts NOT checkable from static live records (need faithful replay-from-state")
    print("  once snapshots accumulate): trailing-stop, strike-rotation, pre-DTE margin, OTM skew.")
    print("=" * 92)


if __name__ == "__main__":
    raise SystemExit(main())
