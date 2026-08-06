#!/usr/bin/env python3
"""
H3 (night 2026-07-19, pre-registered in logs/night_explorer_2026-07-19.md before
computation): time-of-day decay of day-range-breakout momentum entries on the
45d futures 1m corpus.

Proxy (fixed pre-computation): per day per symbol, after 10:00 IST the first
close above the running day-high (of all strictly prior bars) triggers LONG;
the first close below the running day-low triggers SHORT (max one per side per
day; both may fire). Entry at trigger close, exit at the 15:20 IST close,
cost 0.006% per side. Buckets: EARLY < 11:30 IST trigger, LATE >= 13:00 IST
(gray zone excluded). PASS iff EARLY mean net% > 0 AND LATE mean net% <= 0,
n >= 15 per bucket, and EARLY stays > 0 ex-best-day.

Usage: PYTHONPATH=. venv/bin/python scripts/study_tod_breakout_decay.py
"""
from __future__ import annotations

import csv
from datetime import datetime, time
from pathlib import Path

FILES = {
    "NIFTY": "data/nifty_1m_2026-05-15_2026-07-16.csv",
    "BANKNIFTY": "data/banknifty_1m_2026-05-15_2026-07-16.csv",
    "SENSEX": "data/sensex_1m_2026-05-15_2026-07-16.csv",
}
COST_PCT_PER_SIDE = 0.006
T_START = time(10, 0)
T_EARLY = time(11, 30)
T_LATE = time(13, 0)
T_EXIT = time(15, 20)


def day_bars(path: str):
    days: dict = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            ts = datetime.fromisoformat(r["timestamp"])
            days.setdefault(ts.date(), []).append(
                (ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    for d in days:
        days[d].sort(key=lambda x: x[0])
    return days


def main() -> int:
    trades = []  # (symbol, day, side, trigger_ts, net_pct)
    for sym, path in FILES.items():
        for d, bars in day_bars(path).items():
            exit_bar = None
            for b in bars:
                if b[0].time() <= T_EXIT:
                    exit_bar = b
            if exit_bar is None or exit_bar[0].time() < time(15, 0):
                continue  # truncated session
            hi = lo = None
            done_long = done_short = False
            for ts, o, h, l, c in bars:
                t = ts.time()
                if t >= T_EXIT:
                    break
                if hi is not None and t >= T_START:
                    if not done_long and c > hi:
                        done_long = True
                        net = (exit_bar[4] - c) / c * 100.0 - 2 * COST_PCT_PER_SIDE
                        trades.append((sym, d, "LONG", ts, net))
                    if not done_short and c < lo:
                        done_short = True
                        net = (c - exit_bar[4]) / c * 100.0 - 2 * COST_PCT_PER_SIDE
                        trades.append((sym, d, "SHORT", ts, net))
                hi = h if hi is None else max(hi, h)
                lo = l if lo is None else min(lo, l)

    def bucket(tr):
        t = tr[3].time()
        if t < T_EARLY:
            return "EARLY"
        if t >= T_LATE:
            return "LATE"
        return "MID"

    for name in ("EARLY", "MID", "LATE"):
        sub = [t for t in trades if bucket(t) == name]
        if not sub:
            print(f"{name}: n=0")
            continue
        rets = [t[4] for t in sub]
        mean = sum(rets) / len(rets)
        best = max(rets)
        exb = (sum(rets) - best) / (len(rets) - 1) if len(rets) > 1 else 0.0
        wins = sum(1 for r in rets if r > 0)
        print(f"{name:5} n={len(sub):3} mean={mean:+.3f}% ex-best={exb:+.3f}% "
              f"win={wins}/{len(sub)}  " +
              " ".join(f"{s}:{sum(1 for t in sub if t[0]==s)}" for s in FILES))
        for s in FILES:
            ss = [t[4] for t in sub if t[0] == s]
            if ss:
                print(f"      {s:9} n={len(ss):3} mean={sum(ss)/len(ss):+.3f}%")

    early = [t[4] for t in trades if bucket(t) == "EARLY"]
    late = [t[4] for t in trades if bucket(t) == "LATE"]
    ok = (len(early) >= 15 and len(late) >= 15
          and sum(early) / len(early) > 0
          and sum(late) / len(late) <= 0
          and (sum(early) - max(early)) / (len(early) - 1) > 0)
    print(f"\npre-registered verdict: {'PASS' if ok else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
