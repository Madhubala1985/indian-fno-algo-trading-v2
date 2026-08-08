#!/usr/bin/env python3
"""kronos_multiday_vs_iv.py — Kronos vs option-implied at the MULTI-DAY horizon.

This is the horizon where the variance risk premium demonstrably exists in my data
(ATM straddles decay 5%/15%/28% over 1/2/3 days). The question is whether Kronos's
sampled-path expected move disagrees with what options CHARGE in a way that pays.

  implied_move  = ATM straddle scaled to the horizon by sqrt(time)
  kronos_move   = mean |terminal - spot| across sampled paths
  actual_move   = realised |terminal - spot|
  short-vol P&L proxy = implied_move - actual_move   (collect implied, pay realised)

Controls, because a filter that can't beat unconditional selling is worthless:
  - sell ALWAYS vs sell only when options look rich vs the model
  - Kronos vs implied as pure forecasters of the realised move
"""
from __future__ import annotations
import collections, datetime as dt, glob, json, os, sys
import numpy as np

ROOT = "/home/madhu/institutional_ai/institutional_trading_system_v2"
os.chdir(ROOT); sys.path.insert(0, ROOT)
HORIZON = float(os.environ.get("KR_DAYS", "2"))
SRC = f"data/kronos/eval_multiday_{int(HORIZON)}d.jsonl"


def hhmm(ts):
    h, m = int(ts[11:13]) + 5, int(ts[14:16]) + 30
    if m >= 60: m -= 60; h += 1
    return h * 60 + m


def chain_at_1515(sym, day):
    fs = sorted(f for f in glob.glob(f"data/option_history/{sym}_{day}_*.jsonl")
                if not os.path.basename(f).startswith("_"))
    if not fs: return None, None
    exp = os.path.basename(fs[0])[:-6].split("_")[2]
    per = collections.defaultdict(dict)
    for line in open(fs[0]):
        try: d = json.loads(line)
        except Exception: continue
        if not d.get("ltp") or d["ltp"] <= 0: continue
        per[hhmm(d["ts"])][(round(float(d["strike"])), d["type"])] = float(d["ltp"])
    if not per: return None, None
    t = max([m for m in per if m <= 1515], default=None)
    if t is None: return None, None
    snap = per[t]
    ks = [k for k in {x[0] for x in snap} if (k, "CE") in snap and (k, "PE") in snap]
    if not ks: return None, None
    k = min(ks, key=lambda s: abs(snap[(s, "CE")] - snap[(s, "PE")]))
    spot = k + snap[(k, "CE")] - snap[(k, "PE")]
    return (snap[(k, "CE")] + snap[(k, "PE")], spot), exp


def main() -> int:
    rows = [json.loads(l) for l in open(SRC) if l.strip()]
    out = []
    for r in rows:
        got, exp = chain_at_1515(r["sym"], r["day"])
        if not got: continue
        strad, spot = got
        d0 = dt.date.fromisoformat(r["day"]); d1 = dt.date.fromisoformat(exp)
        sess_to_exp = max(sum(1 for i in range((d1 - d0).days + 1)
                              if (d0 + dt.timedelta(days=i)).weekday() < 5) - 1, 0.5)
        implied_full = 10000 * strad / spot
        # ATM straddle ~= 0.8 * sigma_period * S  ->  expected |move| ~= straddle/0.8
        implied_move = (implied_full / 0.8) * (HORIZON / sess_to_exp) ** 0.5
        out.append(dict(sym=r["sym"], day=r["day"], dte=sess_to_exp,
                        kronos=r["pred_absmove_bps"], implied=implied_move,
                        actual=r["act_absmove_bps"], act_range=r["act_range_bps"],
                        kr_range=r["pred_range_bps"]))
    if len(out) < 12:
        print(f"only {len(out)} matched points"); return 1
    K = np.array([o["kronos"] for o in out]); I = np.array([o["implied"] for o in out])
    A = np.array([o["actual"] for o in out])
    def c(a, b):
        return float(np.corrcoef(a, b)[0, 1]) if a.std() and b.std() else float("nan")
    print(f"matched points {len(out)} | symbols {sorted({o['sym'] for o in out})} "
          f"| sessions {len({o['day'] for o in out})} | horizon {HORIZON}d\n")
    print("Q1 — FORECASTING THE REALISED MOVE")
    print(f"   Kronos  corr {c(K,A):+.3f}   mean {K.mean():7.1f} bps")
    print(f"   IMPLIED corr {c(I,A):+.3f}   mean {I.mean():7.1f} bps")
    print(f"   ACTUAL                       mean {A.mean():7.1f} bps")
    print(f"   -> options price {I.mean()/A.mean():.2f}x the realised move"
          f"  ({'VRP PRESENT' if I.mean()>A.mean() else 'NO PREMIUM'})\n")
    pnl = I - A                                   # collect implied, pay realised
    kcal = K * (A.mean() / K.mean()) if K.mean() else K
    S = I - kcal
    print("Q2 — IS 'OPTIONS RICH VS MODEL' INFORMATIVE?")
    print(f"   corr(spread, short-vol P&L) : {c(S,pnl):+.3f}")
    q = np.quantile(S, [0.5])
    hi = S >= q[0]
    print(f"   sell ALWAYS          : {pnl.mean():+8.1f} bps  (n={len(pnl)}, "
          f"win {100*np.mean(pnl>0):.0f}%)")
    print(f"   sell when RICH vs model: {pnl[hi].mean():+8.1f} bps  (n={hi.sum()}, "
          f"win {100*np.mean(pnl[hi]>0):.0f}%)")
    print(f"   sell when CHEAP vs model: {pnl[~hi].mean():+8.1f} bps  (n={(~hi).sum()}, "
          f"win {100*np.mean(pnl[~hi]>0):.0f}%)")
    edge = pnl[hi].mean() - pnl.mean()
    print(f"   -> conditioning edge  : {edge:+8.1f} bps")
    # translate to rupees on a 1-lot short straddle
    LOT = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}
    print("\n   NOTE: 1 bps on NIFTY(24.5k,lot65) ~ Rs16 gross; a short straddle round trip "
          "costs ~Rs130.\n   So the conditioning edge must exceed ~8 bps to pay for itself.")
    print(f"   VERDICT: {'WORTH TRADING' if edge > 8 else 'DOES NOT CLEAR COSTS'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
