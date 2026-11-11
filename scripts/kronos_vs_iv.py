#!/usr/bin/env python3
"""kronos_vs_iv.py — the decisive test: Kronos range forecast vs OPTION-IMPLIED vol.

A good range forecast only becomes money if it disagrees usefully with what options are
CHARGING for that range. So:

  implied_30min = (ATM straddle / spot) * sqrt(30 / minutes_to_expiry)     [sqrt-time scaling]
  kronos_30min  = sampled-path mean range                                   [from kronos_eval]
  actual_30min  = realised high-low                                         [ground truth]

Three questions:
  Q1  Which forecasts the actual range better — Kronos or the option market?
  Q2  Is the SPREAD (implied - kronos) informative, i.e. does "options are charging more
      than the model expects" predict that selling vol wins?
  Q3  Does trading that spread beat selling vol unconditionally? (the control that matters)
"""
from __future__ import annotations
import json, glob, os, collections, statistics, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

STEP = {"NIFTY": 50, "SENSEX": 100, "BANKNIFTY": 100}
LOT = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}


def hhmm_to_min(ts):
    h, m = int(ts[11:13]) + 5, int(ts[14:16]) + 30
    if m >= 60: m -= 60; h += 1
    return h * 60 + m


def load_chain(sym, day):
    """(minutes_ist -> {(strike,type): ltp}), plus expiry date string."""
    fs = sorted(f for f in glob.glob(f"data/option_history/{sym}_{day}_*.jsonl")
                if not os.path.basename(f).startswith("_"))
    if not fs: return None, None
    path = fs[0]
    exp = os.path.basename(path)[:-6].split("_")[2]
    per = collections.defaultdict(dict)
    for line in open(path):
        try: d = json.loads(line)
        except Exception: continue
        if not d.get("ltp") or d["ltp"] <= 0: continue
        per[hhmm_to_min(d["ts"])][(round(float(d["strike"])), d["type"])] = float(d["ltp"])
    return per, exp


def atm_straddle(snap):
    ks = [k for k in {x[0] for x in snap} if (k, "CE") in snap and (k, "PE") in snap]
    if not ks: return None, None
    k = min(ks, key=lambda s: abs(snap[(s, "CE")] - snap[(s, "PE")]))
    spot = k + snap[(k, "CE")] - snap[(k, "PE")]
    return snap[(k, "CE")] + snap[(k, "PE")], spot


def main() -> int:
    import datetime as dt
    rows = [json.loads(l) for l in open("data/kronos/eval_results.jsonl") if l.strip()]
    out = []
    cache = {}
    for r in rows:
        key = (r["sym"], r["day"])
        if key not in cache:
            cache[key] = load_chain(*key)
        per, exp = cache[key]
        if not per: continue
        tmin = int(r["tmin"])
        near = [m for m in per if abs(m - tmin) <= 6]
        if not near: continue
        snap = per[min(near, key=lambda m: abs(m - tmin))]
        strad, spot = atm_straddle(snap)
        if not strad or not spot: continue
        # minutes of TRADING time to expiry (375/session), incl. rest of today
        d0 = dt.date.fromisoformat(r["day"]); d1 = dt.date.fromisoformat(exp)
        sess = sum(1 for i in range((d1 - d0).days + 1)
                   if (d0 + dt.timedelta(days=i)).weekday() < 5)
        mins_left = max((sess - 1) * 375 + (930 - tmin), 30)
        implied_full = 10000 * strad / spot                      # bps to expiry
        implied_30 = implied_full * (30.0 / mins_left) ** 0.5    # sqrt-time scaled
        out.append(dict(sym=r["sym"], day=r["day"], tmin=tmin,
                        kronos=r["pred_range_bps"], implied=implied_30,
                        actual=r["act_range_bps"], act_ret=abs(r["act_ret_bps"])))
    if len(out) < 15:
        print(f"only {len(out)} matched points — need more"); return 1
    K = np.array([o["kronos"] for o in out]); I = np.array([o["implied"] for o in out])
    A = np.array([o["actual"] for o in out])
    def c(a, b): return float(np.corrcoef(a, b)[0, 1])
    print(f"matched points: {len(out)}   symbols {sorted({o['sym'] for o in out})}   "
          f"sessions {len({o['day'] for o in out})}\n")
    print("Q1 — WHO FORECASTS THE ACTUAL 30-MIN RANGE BETTER?")
    print(f"   Kronos  corr {c(K, A):+.3f}   mean {K.mean():7.1f} bps")
    print(f"   IMPLIED corr {c(I, A):+.3f}   mean {I.mean():7.1f} bps")
    print(f"   ACTUAL                        mean {A.mean():7.1f} bps")
    print(f"   -> options price {I.mean()/A.mean():.2f}x the realised range "
          f"(>1 = variance risk premium, the thing we harvest)")
    # calibrate Kronos level (its bias is constant, so scale it)
    kcal = K * (A.mean() / K.mean())
    print(f"   Kronos calibrated corr {c(kcal, A):+.3f}\n")

    print("Q2 — IS THE SPREAD INFORMATIVE? (implied - kronos_calibrated)")
    S = I - kcal
    # short-vol P&L proxy: you collect implied, you pay realised
    pnl = I - A
    print(f"   corr(spread, short-vol P&L proxy) : {c(S, pnl):+.3f}")
    q = np.quantile(S, [0.33, 0.67])
    for lab, m in (("LOW  spread (options cheap vs model)", S <= q[0]),
                   ("MID  spread", (S > q[0]) & (S < q[1])),
                   ("HIGH spread (options rich vs model)", S >= q[1])):
        print(f"   {lab:38} n={m.sum():>3}  mean short-vol P&L {pnl[m].mean():+7.1f} bps")
    print()
    print("Q3 — CONTROL: does the spread beat selling vol UNCONDITIONALLY?")
    print(f"   sell always            : {pnl.mean():+7.1f} bps/trade   (n={len(pnl)})")
    hi = S >= q[1]
    print(f"   sell only HIGH spread  : {pnl[hi].mean():+7.1f} bps/trade   (n={hi.sum()})")
    edge = pnl[hi].mean() - pnl.mean()
    print(f"   -> conditioning edge   : {edge:+7.1f} bps  "
          f"{'ADDS VALUE' if edge > 0 else 'NO VALUE — the filter does not help'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
