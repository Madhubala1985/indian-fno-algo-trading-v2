#!/usr/bin/env python3
"""kronos_multiday.py — Kronos at the horizon where the VRP actually exists.

The 30-min test failed for a reason that was MY design error, not the model's: I pointed it
at the horizon where I had already proven there is nothing (intraday theta ~0, direction a
coin, options at 0.93x realised = no premium). The edge I actually have is MULTI-DAY — ATM
straddles decay 5%/15%/28% over 1/2/3 days.

So: 15-minute bars (25/session), forecast HORIZON_DAYS trading days ahead from the 15:15
bar of each session — the same mark my VRP study used. Sampled paths give a model-implied
expected move and range over that horizon, to be raced against the option-implied move.

Strictly causal: only bars strictly before the decision bar are fed to the model.
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np
import pandas as pd

KRONOS = "/home/madhu/institutional_ai/external/Kronos"
ROOT = "/home/madhu/institutional_ai/institutional_trading_system_v2"
sys.path.insert(0, KRONOS)
os.chdir(ROOT)

BARS_PER_DAY = 25
HORIZON_DAYS = float(os.environ.get("KR_DAYS", "2"))
PRED_LEN = int(BARS_PER_DAY * HORIZON_DAYS)
LOOKBACK = int(os.environ.get("KR_LOOKBACK", "400"))
SAMPLES = int(os.environ.get("KR_SAMPLES", "8"))
SESSIONS = int(os.environ.get("KR_SESSIONS", "30"))
SYMS = os.environ.get("KR_SYMS", "NIFTY,SENSEX,BANKNIFTY").split(",")
OUT = f"{ROOT}/data/kronos/eval_multiday_{int(HORIZON_DAYS)}d.jsonl"


def main() -> int:
    import torch
    torch.set_num_threads(int(os.environ.get("KR_THREADS", "12")))
    from model import Kronos, KronosTokenizer, KronosPredictor
    tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    mdl = Kronos.from_pretrained(os.environ.get("KR_MODEL", "NeoQuasar/Kronos-small"))
    predictor = KronosPredictor(mdl, tok, device="cpu", max_context=512)
    print(f"15min bars | horizon {HORIZON_DAYS}d = {PRED_LEN} bars | lookback {LOOKBACK} "
          f"| samples {SAMPLES}", flush=True)

    open(OUT, "w").close()
    n = 0; t0 = time.time()
    for sym in SYMS:
        df = pd.read_csv(f"data/kronos/{sym}_15min.csv")
        df["timestamps"] = pd.to_datetime(df["timestamps"])
        df["day"] = df["timestamps"].dt.date.astype(str)
        df["hm"] = df["timestamps"].dt.strftime("%H:%M")
        # decision bars = the 15:15 bar of each session (matches the VRP study's mark)
        dec = df.index[df["hm"] == "15:15"].tolist()
        dec = [i for i in dec if i - LOOKBACK >= 0 and i + PRED_LEN < len(df)][-SESSIONS:]
        for i in dec:
            x = df.loc[i - LOOKBACK:i - 1, ["open", "high", "low", "close"]].reset_index(drop=True)
            xt = df.loc[i - LOOKBACK:i - 1, "timestamps"].reset_index(drop=True)
            yt = df.loc[i:i + PRED_LEN - 1, "timestamps"].reset_index(drop=True)
            p0 = float(df.loc[i - 1, "close"])
            try:
                paths = []
                for _ in range(SAMPLES):
                    o = predictor.predict(df=x, x_timestamp=xt, y_timestamp=yt,
                                          pred_len=PRED_LEN, T=1.0, top_p=0.9,
                                          sample_count=1, verbose=False)
                    paths.append(o["close"].to_numpy(dtype=float))
            except Exception as e:
                print(f"  {sym} {df.loc[i,'day']} FAIL {type(e).__name__} {str(e)[:60]}", flush=True)
                continue
            P = np.vstack(paths)
            term = P[:, -1]
            # what a short-vol seller cares about: how FAR does it travel, and how WIDE
            pred_absmove = float(np.mean(np.abs(term - p0)) / p0) * 10000       # bps
            pred_range = float(np.mean(P.max(axis=1) - P.min(axis=1)) / p0) * 10000
            pred_disp = float(np.std(term) / p0) * 10000
            pred_ret = float(np.mean(term) / p0 - 1.0) * 10000

            fut_c = df.loc[i:i + PRED_LEN - 1, "close"].to_numpy(float)
            act_absmove = float(abs(fut_c[-1] - p0) / p0) * 10000
            act_range = float((df.loc[i:i + PRED_LEN - 1, "high"].max()
                               - df.loc[i:i + PRED_LEN - 1, "low"].min()) / p0) * 10000
            act_ret = float(fut_c[-1] / p0 - 1.0) * 10000

            rec = dict(sym=sym, day=df.loc[i, "day"], horizon_days=HORIZON_DAYS, p0=p0,
                       pred_absmove_bps=pred_absmove, pred_range_bps=pred_range,
                       pred_disp_bps=pred_disp, pred_ret_bps=pred_ret,
                       act_absmove_bps=act_absmove, act_range_bps=act_range,
                       act_ret_bps=act_ret)
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            n += 1
            if n % 5 == 0:
                el = time.time() - t0
                print(f"  {n} points  {el/60:.1f}min  ({el/n:.0f}s/pt)", flush=True)
    print(f"DONE {n} points in {(time.time()-t0)/60:.1f} min -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
