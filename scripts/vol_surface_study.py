#!/usr/bin/env python3
"""
vol_surface_study.py — batch-4 §3. IV-engine data was computed but never consulted. STUDY ONLY.

3a DTE buckets: P&L / WR / est. theta paid by DTE at entry. Do losses concentrate where theta
   is highest (low DTE)?
3b IV-richness buckets: entry ATM IV vs trailing 5-day realized vol (both annualised). Hypothesis:
   buying premium when IV is RICH (iv/realized high) underperforms; 07-08 paid because IV was cheap.

ex-best-day is printed for every bucket (standing rule — a bucket carried by one day is noise).
Candidate entry filter only; do not wire.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timezone
from math import log, sqrt
from statistics import pstdev

from straddle_study import _CFG, _load_all_bars

_SESSION_MIN = 375


def _daily_closes(u):
    cfg = _CFG.get(u)
    if not cfg:
        return {}
    last = {}
    for b in _load_all_bars(u, cfg["feed"]):
        d = b.start_timestamp.astimezone(timezone.utc).date().isoformat()
        last[d] = b.close_price      # last bar of the day wins (sorted ascending)
    return last


def _realized_vol(closes, upto_day, n=5):
    days = sorted(d for d in closes if d < upto_day)[-(n + 1):]
    if len(days) < 3:
        return None
    rets = [log(closes[days[i]] / closes[days[i - 1]]) for i in range(1, len(days))
            if closes[days[i - 1]]]
    return pstdev(rets) * sqrt(252) if len(rets) >= 2 else None


def _ex_best(trades):
    by_day = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]
    if not by_day:
        return 0.0, 0
    best = max(by_day, key=by_day.get)
    ex = [t for t in trades if t["day"] != best]
    return sum(t["pnl"] for t in ex), len(ex)


def _row(label, trades):
    n = len(trades)
    if not n:
        print(f"  {label:16} (0)")
        return
    pnl = sum(t["pnl"] for t in trades)
    w = sum(1 for t in trades if t["pnl"] > 0)
    exp, exn = _ex_best(trades)
    print(f"  {label:16} n={n:>2}  P&L ₹{pnl:>+8,.0f}  WR {100*w//n:>3}%  "
          f"theta {sum(t['theta'] for t in trades)/n:>4.2f}%  |  ex-best ₹{exp:>+8,.0f}/{exn}t")


def main():
    c = sqlite3.connect("logs/trades.db")
    closes = {u: _daily_closes(u) for u in _CFG}
    trades = []
    for r in c.execute("""select underlying,entry_iv,expiry_date,entry_ts,bars_held,pnl
                          from pt_trades where entry_premium>0"""):
        u, iv, exp, ets, bars, pnl = r
        if pnl is None:
            continue
        try:
            dte = (date.fromisoformat(exp[:10]) - date.fromisoformat(ets[:10])).days
        except Exception:
            dte = None
        rv = _realized_vol(closes.get(u, {}), ets[:10]) if u in closes else None
        rich = (iv / rv) if (iv and rv) else None
        dte_eff = max(dte or 0, 0) + 0.5
        theta = 100.0 * (0.5 / dte_eff) * ((bars or 0) / _SESSION_MIN)
        trades.append(dict(u=u, iv=iv, rv=rv, rich=rich, dte=dte, theta=theta,
                           pnl=pnl, day=ets[:10]))

    print("=" * 84)
    print(f"  §3 VOL-SURFACE STUDY — {len(trades)} trades (study only, not wired)")
    print("=" * 84)
    print("── 3a: bucket by DTE at entry ──")
    for lab, lo, hi in (("DTE 0–1", 0, 2), ("DTE 2–3", 2, 4), ("DTE 4–6", 4, 7), ("DTE 7+", 7, 999)):
        _row(lab, [t for t in trades if t["dte"] is not None and lo <= t["dte"] < hi])

    print("\n── 3b: bucket by IV-richness (entry ATM IV / trailing 5d realized vol) ──")
    have = [t for t in trades if t["rich"] is not None]
    print(f"  ({len(have)}/{len(trades)} trades have realized-vol context)")
    for lab, lo, hi in (("cheap <0.9", 0, 0.9), ("fair 0.9–1.1", 0.9, 1.1),
                        ("rich 1.1–1.4", 1.1, 1.4), ("v.rich >1.4", 1.4, 99)):
        _row(lab, [t for t in have if lo <= t["rich"] < hi])
    cheap = [t for t in have if t["rich"] < 1.1]
    rich = [t for t in have if t["rich"] >= 1.1]
    if cheap and rich:
        cp, _ = _ex_best(cheap); rp, _ = _ex_best(rich)
        print(f"\n  cheap-IV (ex-best) ₹{cp:+,.0f}/{len(cheap)}t  vs  rich-IV (ex-best) ₹{rp:+,.0f}/{len(rich)}t"
              f"  → hypothesis {'SUPPORTED' if cp > rp else 'NOT supported'} (candidate entry filter, study only)")
    print("=" * 84)


if __name__ == "__main__":
    raise SystemExit(main())
