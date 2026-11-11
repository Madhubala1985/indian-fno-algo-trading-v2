#!/usr/bin/env python3
"""kronos_analyze.py — score Kronos on the only two questions that matter.

TEST A  DIRECTION: hit rate + net P&L after real Indian F&O costs.
TEST B  VOLATILITY: does the sampled-path dispersion forecast realised vol —
        AND does it beat the naive baseline (trailing realised vol)?

The baseline control is the point. My CAS work nearly produced a false positive until a
dumb always-buy-puts control was run against it; the same discipline applies here. A vol
model that cannot beat "what vol just was" adds nothing.
"""
from __future__ import annotations
import os
import json, statistics, sys
import numpy as np
import pandas as pd

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data/kronos/eval_results.jsonl")
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data/kronos")


def corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def main() -> int:
    rows = [json.loads(l) for l in open(SRC) if l.strip()]
    if len(rows) < 20:
        print(f"only {len(rows)} points — not enough yet"); return 1
    df = pd.DataFrame(rows)
    print(f"evaluation points: {len(df)}  symbols: {sorted(df['sym'].unique())}  "
          f"sessions: {df['day'].nunique()}\n")

    # ---- attach the NAIVE baseline: realised vol over the PRIOR 30 minutes ----
    # Keyed on (sym, day, tmin) rather than relying on row order. Aligning a groupby
    # against a sort_values is exactly how a baseline silently attaches to the wrong
    # rows and manufactures a false verdict.
    naive = {}
    for sym in df["sym"].unique():
        bars = pd.read_csv(f"{DATA}/{sym}_1min.csv")
        bars["timestamps"] = pd.to_datetime(bars["timestamps"])
        bars["day"] = bars["timestamps"].dt.date.astype(str)
        bars["mins"] = bars["timestamps"].dt.hour * 60 + bars["timestamps"].dt.minute
        idx = {(d, m): i for i, (d, m) in enumerate(zip(bars["day"], bars["mins"]))}
        for (day, tmin) in {(r["day"], int(r["tmin"]))
                            for _, r in df[df["sym"] == sym].iterrows()}:
            i = idx.get((day, tmin))
            if i is None or i < 31:
                continue
            prev = bars.loc[i - 30:i - 1]
            p0 = float(bars.loc[i - 1, "close"])
            naive[(sym, day, tmin)] = (
                float(np.mean(np.abs(np.diff(prev["close"].to_numpy(float)))) / p0) * 10000,
                float((prev["high"].max() - prev["low"].min()) / p0) * 10000)
    df["naive_rv_bps"] = [naive.get((r["sym"], r["day"], int(r["tmin"])), (np.nan, np.nan))[0]
                          for _, r in df.iterrows()]
    df["naive_range_bps"] = [naive.get((r["sym"], r["day"], int(r["tmin"])), (np.nan, np.nan))[1]
                             for _, r in df.iterrows()]
    before = len(df)
    df = df.dropna(subset=["naive_rv_bps", "naive_range_bps"]).reset_index(drop=True)
    print(f"naive baseline attached to {len(df)}/{before} points\n")

    # ============================ TEST A — DIRECTION ============================
    print("=" * 74); print("TEST A — DIRECTION (30-min horizon)"); print("=" * 74)
    d = df[df["pred_ret_bps"].abs() > 0]
    hit = float(np.mean(np.sign(d["pred_ret_bps"]) == np.sign(d["act_ret_bps"]))) * 100
    print(f"  corr(pred_ret, act_ret) : {corr(d['pred_ret_bps'], d['act_ret_bps']):+.3f}")
    print(f"  sign hit rate           : {hit:.1f}%   (n={len(d)})")
    print(f"  mean |actual move|      : {d['act_ret_bps'].abs().mean():.1f} bps")
    # net P&L: trade ATM option, delta~0.5, 1 lot; costs from the platform model
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from platform_core.execution.transaction_costs import (
        structure_round_trip_costs, exchange_for_underlying)
    LOT = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}
    tot = 0.0; wins = 0
    for _, r in d.iterrows():
        lot = LOT[r["sym"]]
        move_pts = r["p0"] * r["act_ret_bps"] / 10000
        gain = (move_pts if r["pred_ret_bps"] > 0 else -move_pts) * 0.5
        prem = 0.0035 * r["p0"]
        cost = structure_round_trip_costs([(prem, prem + gain, "BUY")], quantity=lot,
                                          exchange=exchange_for_underlying(r["sym"])).total
        net = gain * lot - cost; tot += net; wins += net > 0
    print(f"  NET after real costs    : Rs{tot:+,.0f} over {len(d)} trades "
          f"(Rs{tot/len(d):+,.0f}/trade, {100*wins/len(d):.0f}% profitable)")
    print(f"  VERDICT: {'TRADEABLE' if tot > 0 and hit > 53 else 'NOT TRADEABLE'}\n")

    # ============================ TEST B — VOLATILITY ===========================
    print("=" * 74); print("TEST B — VOLATILITY (the one that matters)"); print("=" * 74)
    for pred_col, act_col, naive_col, label in (
            ("pred_rv_bps", "act_rv_bps", "naive_rv_bps", "per-minute realised vol"),
            ("pred_range_bps", "act_range_bps", "naive_range_bps", "30-min range")):
        ck = corr(df[pred_col], df[act_col])
        cn = corr(df[naive_col], df[act_col])
        ratio = df[act_col].mean() / df[pred_col].mean() if df[pred_col].mean() else float("nan")
        print(f"\n  --- {label} ---")
        print(f"    Kronos corr with actual : {ck:+.3f}")
        print(f"    NAIVE  corr with actual : {cn:+.3f}   (trailing 30min)")
        print(f"    calibration (act/pred)  : {ratio:.2f}x  "
              f"(a constant bias is fine — it calibrates away)")
        # does Kronos add anything ON TOP of naive?
        X = np.column_stack([df[naive_col].to_numpy(float),
                             df[pred_col].to_numpy(float), np.ones(len(df))])
        y = df[act_col].to_numpy(float)
        try:
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            resid_naive = y - np.column_stack([df[naive_col], np.ones(len(df))]) @ \
                np.linalg.lstsq(np.column_stack([df[naive_col], np.ones(len(df))]), y, rcond=None)[0]
            resid_both = y - X @ beta
            r2_gain = 1 - (resid_both.var() / resid_naive.var()) if resid_naive.var() else 0
            print(f"    incremental R2 over naive: {100*r2_gain:+.1f}%  "
                  f"(beta_kronos={beta[1]:+.3f})")
            print(f"    VERDICT: {'ADDS SIGNAL' if r2_gain > 0.02 and ck > cn else 'NO EDGE OVER NAIVE'}")
        except Exception as e:
            print(f"    regression failed: {e}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
