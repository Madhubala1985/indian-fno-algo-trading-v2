#!/usr/bin/env python3
"""kronos_eval.py — evaluate Kronos on NIFTY/SENSEX/BANKNIFTY the only two ways that matter.

NOT evaluated on MSE. A model that improves next-candle error by 10% can still make zero
rupees, because any signal must clear ~Rs250/round-trip in Indian F&O fees.

Two tests, both from ONE forward pass per evaluation point (sample_count paths):
  TEST A  DIRECTION — mean of sampled terminal prices vs the actual move.
          Needs materially better than 50% to survive costs.
  TEST B  VOLATILITY — dispersion across sampled paths as a forecast of realised vol.
          This is the one I expect to work, because my own evidence says direction is
          near-chance while the vol/VRP structure is the only thing clearing costs.

Strictly causal: the model sees only bars strictly before the decision index.
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np
import pandas as pd

KRONOS = "/home/madhu/institutional_ai/external/Kronos"
DATA = "/home/madhu/institutional_ai/institutional_trading_system_v2/data/kronos"
OUT = "/home/madhu/institutional_ai/institutional_trading_system_v2/data/kronos/eval_results.jsonl"
sys.path.insert(0, KRONOS)

LOOKBACK = int(os.environ.get("KR_LOOKBACK", "256"))
PRED_LEN = int(os.environ.get("KR_PRED", "30"))       # 30 min — a tradeable horizon
SAMPLES  = int(os.environ.get("KR_SAMPLES", "10"))
SESSIONS = int(os.environ.get("KR_SESSIONS", "30"))   # most recent N sessions
PER_DAY  = [int(x) for x in os.environ.get("KR_TIMES", "600,660,720,780").split(",")]  # IST minutes
MODEL    = os.environ.get("KR_MODEL", "NeoQuasar/Kronos-small")


def main() -> int:
    import torch
    torch.set_num_threads(int(os.environ.get("KR_THREADS", "12")))
    from model import Kronos, KronosTokenizer, KronosPredictor
    tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    mdl = Kronos.from_pretrained(MODEL)
    predictor = KronosPredictor(mdl, tok, device="cpu", max_context=512)
    print(f"loaded {MODEL} | lookback {LOOKBACK} pred {PRED_LEN} samples {SAMPLES}", flush=True)

    open(OUT, "w").close()
    n_done = 0
    t_start = time.time()
    for sym in ("NIFTY", "SENSEX", "BANKNIFTY"):
        df = pd.read_csv(f"{DATA}/{sym}_1min.csv")
        df["timestamps"] = pd.to_datetime(df["timestamps"])
        df["day"] = df["timestamps"].dt.date
        df["mins"] = df["timestamps"].dt.hour * 60 + df["timestamps"].dt.minute
        days = sorted(df["day"].unique())[-SESSIONS:]
        for day in days:
            for tmin in PER_DAY:
                sel = df.index[(df["day"] == day) & (df["mins"] == tmin)]
                if len(sel) == 0:
                    continue
                i = int(sel[0])
                if i - LOOKBACK < 0 or i + PRED_LEN >= len(df):
                    continue
                # forecast window must stay inside the same session
                if df.loc[i + PRED_LEN - 1, "day"] != day:
                    continue
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
                    print(f"  {sym} {day} {tmin} FAIL {type(e).__name__} {str(e)[:60]}", flush=True)
                    continue
                P = np.vstack(paths)                       # (SAMPLES, PRED_LEN)
                term = P[:, -1]
                pred_ret = float(np.mean(term) / p0 - 1.0) * 10000          # bps
                # dispersion across sampled paths = model's own uncertainty
                pred_disp = float(np.std(term) / p0) * 10000                # bps
                # model-implied realised vol: mean abs 1-step move across paths
                steps = np.abs(np.diff(P, axis=1)) / p0
                pred_rv = float(np.mean(steps)) * 10000                     # bps per minute
                # model-implied RANGE over the window
                pred_range = float(np.mean(P.max(axis=1) - P.min(axis=1)) / p0) * 10000

                fut = df.loc[i:i + PRED_LEN - 1, "close"].to_numpy(dtype=float)
                act_ret = float(fut[-1] / p0 - 1.0) * 10000
                act_rv = float(np.mean(np.abs(np.diff(fut)) / p0)) * 10000
                act_range = float((df.loc[i:i + PRED_LEN - 1, "high"].max()
                                   - df.loc[i:i + PRED_LEN - 1, "low"].min()) / p0) * 10000

                rec = dict(sym=sym, day=str(day), tmin=tmin, p0=p0,
                           pred_ret_bps=pred_ret, pred_disp_bps=pred_disp,
                           pred_rv_bps=pred_rv, pred_range_bps=pred_range,
                           act_ret_bps=act_ret, act_rv_bps=act_rv, act_range_bps=act_range)
                with open(OUT, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                n_done += 1
                if n_done % 10 == 0:
                    el = time.time() - t_start
                    print(f"  {n_done} points  {el/60:.1f}min elapsed  "
                          f"({el/n_done:.1f}s/point)", flush=True)
    print(f"DONE {n_done} points in {(time.time()-t_start)/60:.1f} min -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
