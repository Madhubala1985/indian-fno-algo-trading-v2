#!/usr/bin/env python3
"""
loser_autopsy.py — batch-4 §2. Feature matrix over losing trades (trades.db only, no replay).
Pre-registered hypotheses H1/H2, ex-best-day + per-day consistency mandatory. Findings are
MECHANISMS with sample sizes, not narratives.

Features (what the static records support):
  conviction (conf_eff), entry time-of-day, DTE, regime (join regime_shadow), bias age
  (bars since the MCB exec_bias last flipped, join htf_shadow), bars_held, and a "ever went
  green" PROXY from exit_reason: BREAKEVEN_RATCHET ⇒ peaked ≥+8% then round-tripped (went green);
  a STOP_LOSS loss ⇒ never peaked +8% (else the ratchet would have saved it) ⇒ never meaningfully
  green. (True MFE/bars-to-MAE and entry spread are NOT in the records — noted, not faked.)
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from datetime import date, timezone, datetime, timedelta
from statistics import median, mean

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCLUDE = ("VOID_WRONG_CAPITAL_RESET", "FORCE_CLOSE_USER_STOP")


def _shadow_series(path, field):
    idx = {}
    try:
        for line in open(path):
            d = json.loads(line)
            idx.setdefault(d["underlying"], []).append((d["ts"], d.get(field)))
    except FileNotFoundError:
        return {}
    for u in idx:
        idx[u].sort()
    return idx


def _at(series, u, ts):
    rec = series.get(u)
    if not rec:
        return None
    tss = [r[0] for r in rec]
    i = bisect_right(tss, ts) - 1
    return rec[i][1] if i >= 0 else None


def _bias_age(series, u, ts):
    """bars since exec_bias last changed, as of ts (join htf_shadow exec_bias)."""
    rec = series.get(u)
    if not rec:
        return None
    tss = [r[0] for r in rec]
    i = bisect_right(tss, ts) - 1
    if i < 0:
        return None
    cur = rec[i][1]
    age = 0
    j = i
    while j >= 0 and rec[j][1] == cur:
        age += 1
        j -= 1
    return age


def _went_green(exit_reason):
    if exit_reason and "BREAKEVEN_RATCHET" in exit_reason:
        return True           # peaked +8% then round-tripped
    if exit_reason and "STOP_LOSS" in exit_reason:
        return False          # never peaked +8% (ratchet would have saved it)
    return None               # STRUCTURE_EXIT etc. — unknown


def main():
    c = sqlite3.connect("logs/trades.db")
    regime = _shadow_series("logs/regime_shadow.jsonl", "vol_regime")
    htf = _shadow_series("logs/htf_shadow.jsonl", "exec_bias")
    rows = []
    for r in c.execute("""select underlying,direction,conf_eff,entry_premium,bars_held,
                                 expiry_date,entry_ts,exit_reason,pnl from pt_trades"""):
        u, d, conf, ep, bars, exp, ets, xr, pnl = r
        if xr in _EXCLUDE or pnl is None:
            continue
        try:
            tod = datetime.fromisoformat(ets).astimezone(_IST).hour + \
                  datetime.fromisoformat(ets).astimezone(_IST).minute / 60
        except Exception:
            tod = None
        try:
            dte = (date.fromisoformat(exp[:10]) - date.fromisoformat(ets[:10])).days
        except Exception:
            dte = None
        rows.append(dict(u=u, dir=d, conf=conf, bars=bars or 0, dte=dte, tod=tod,
                         regime=_at(regime, u, ets), bias_age=_bias_age(htf, u, ets),
                         green=_went_green(xr), xr=(xr or "").split(" ")[0], pnl=pnl,
                         day=ets[:10]))
    W = [r for r in rows if r["pnl"] > 0]
    L = [r for r in rows if r["pnl"] <= 0]

    def _m(ts, k):
        v = [t[k] for t in ts if t[k] is not None]
        return median(v) if v else float("nan")

    print("=" * 92)
    print(f"  §2 LOSER AUTOPSY — {len(rows)} trades ({len(W)} win / {len(L)} loss), trades.db only")
    print("=" * 92)
    print(f"  {'feature (median)':24} {'winners':>10} {'losers':>10}")
    for k, lab in (("conf", "conviction conf_eff"), ("bias_age", "bias age (bars)"),
                   ("tod", "entry hour IST"), ("dte", "DTE"), ("bars", "bars held")):
        print(f"  {lab:24} {_m(W,k):>10.2f} {_m(L,k):>10.2f}")

    print("\n── H1: high-conviction + stale-bias co-occur in losers? (cross-asset leg pending #4) ──")
    cm = median([t["conf"] for t in rows if t["conf"] is not None])
    am = median([t["bias_age"] for t in rows if t["bias_age"] is not None] or [0])
    def _trap(t):
        return (t["conf"] is not None and t["conf"] >= cm and
                t["bias_age"] is not None and t["bias_age"] >= am)
    lt = sum(_trap(t) for t in L); wt = sum(_trap(t) for t in W)
    print(f"  thresholds: conf≥{cm:.2f}, bias_age≥{am:.0f} bars")
    print(f"  trap-fingerprint rate: losers {lt}/{len(L)} ({100*lt//max(len(L),1)}%)  "
          f"winners {wt}/{len(W)} ({100*wt//max(len(W),1)}%)")
    trap_trades = [t for t in rows if _trap(t)]
    print(f"  P&L of trap-fingerprint trades: ₹{sum(t['pnl'] for t in trap_trades):+,.0f} "
          f"(n={len(trap_trades)}) — H1 {'PASS (composite veto candidate)' if lt/max(len(L),1) > wt/max(len(W),1) else 'FAIL'}"
          f"; cross-asset 3rd leg only testable after #4 accrues")

    print("\n── H2: losers die slowly / never go green → TIME-STOP spec ──")
    green_known = [t for t in L if t["green"] is not None]
    never_green = [t for t in green_known if not t["green"]]
    print(f"  loser exit-reason mix: " + ", ".join(
        f"{k}={sum(1 for t in L if t['xr']==k)}" for k in sorted({t['xr'] for t in L})))
    print(f"  losers with known green-status: {len(green_known)}; NEVER went +8% green: "
          f"{len(never_green)} ({100*len(never_green)//max(len(green_known),1)}%)")
    print(f"  median bars_held: winners {_m(W,'bars'):.0f} vs losers {_m(L,'bars'):.0f} "
          f"({'losers LINGER longer → H2 support' if _m(L,'bars')>_m(W,'bars') else 'losers NOT longer → H2 weak'})")
    ng_bars = sorted(t["bars"] for t in never_green)
    if ng_bars:
        print(f"  never-green losers bars_held: median {median(ng_bars):.0f}, "
              f"p75 {ng_bars[int(len(ng_bars)*0.75)]:.0f} → TIME-STOP candidate N≈median (STUDY ONLY, do not wire)")

    print("\n── conviction inversion — monotonic or a few trades? (bucket conf_eff) ──")
    for lo, hi in ((0.0, 0.6), (0.6, 0.8), (0.8, 0.95), (0.95, 1.01)):
        b = [t for t in rows if t["conf"] is not None and lo <= t["conf"] < hi]
        if b:
            w = sum(1 for t in b if t["pnl"] > 0)
            print(f"  conf [{lo:.2f},{hi:.2f}): n={len(b):>2}  WR {100*w//len(b):>3}%  "
                  f"P&L ₹{sum(t['pnl'] for t in b):>+8,.0f}  avg ₹{mean(t['pnl'] for t in b):>+7,.0f}")

    print("\n── per-day consistency (loser P&L by day; ex-best-day guard) ──")
    byday = {}
    for t in L:
        byday[t["day"]] = byday.get(t["day"], 0.0) + t["pnl"]
    worst = sorted(byday.items(), key=lambda kv: kv[1])[:5]
    print("  worst loser-days: " + ", ".join(f"{d}:{v:+,.0f}" for d, v in worst))
    _tot_loss = sum(byday.values())
    _wshare = (100.0 * worst[0][1] / _tot_loss) if _tot_loss else 0.0
    print(f"  loser P&L total ₹{_tot_loss:+,.0f} across {len(byday)} days "
          f"(worst day {worst[0][0]} = {_wshare:.0f}% of total loss)")
    print("=" * 92)


if __name__ == "__main__":
    raise SystemExit(main())
