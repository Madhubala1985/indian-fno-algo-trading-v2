#!/usr/bin/env python3
"""
H2 (night 2026-07-19, pre-registered in logs/night_explorer_2026-07-19.md before
computation): does the sign of the day-over-day CHANGE in FII net index-futures
open interest (Future Index Long - Short, participant-wise NSE file) predict the
NEXT session's NIFTY futures open->close direction?

Data: data/source/participant_oi/<YYYY-MM-DD>.csv (FII row), NIFTY futures 1m
bars data/nifty_1m_2026-05-15_2026-07-16.csv. Open/close are taken
chronologically from the bar sequence (factual-QA rule: first bar's open,
last bar's close, with timestamps).

PASS rule (pre-registered): pooled hit-rate >= 58% with n >= 25 AND the mean
conditioned next-day return keeps its sign after dropping the single best day.

Usage: PYTHONPATH=. venv/bin/python scripts/study_fii_stance_tilt.py
Appends nothing; prints the study table (one-shot measurement, n is small).
"""
from __future__ import annotations

import csv
import glob
from datetime import date, datetime
from pathlib import Path

OI_DIR = Path("data/source/participant_oi")
BARS = Path("data/nifty_1m_2026-05-15_2026-07-16.csv")


def load_fii_net() -> dict[date, int]:
    out: dict[date, int] = {}
    for p in sorted(OI_DIR.glob("*.csv")):
        d = date.fromisoformat(p.stem)
        with p.open(newline="") as fh:
            for row in csv.reader(fh):
                if row and row[0].strip() == "FII":
                    out[d] = int(row[1]) - int(row[2])  # Future Index Long - Short
                    break
    return out


def load_day_oc() -> dict[date, tuple[float, float, str, str]]:
    """day -> (open, close, open_ts, close_ts) from the 1m bar sequence."""
    days: dict[date, list[tuple[str, float, float]]] = {}
    with BARS.open(newline="") as fh:
        for r in csv.DictReader(fh):
            ts = r["timestamp"]
            d = datetime.fromisoformat(ts).date()
            days.setdefault(d, []).append((ts, float(r["open"]), float(r["close"])))
    out = {}
    for d, rows in days.items():
        rows.sort(key=lambda x: x[0])
        out[d] = (rows[0][1], rows[-1][2], rows[0][0], rows[-1][0])
    return out


def main() -> int:
    fii = load_fii_net()
    oc = load_day_oc()
    oi_days = sorted(fii)
    trading_days = sorted(oc)

    signals = []  # (signal_day, next_day, sign, next_ret_pct)
    for i in range(1, len(oi_days)):
        d_prev, d = oi_days[i - 1], oi_days[i]
        delta = fii[d] - fii[d_prev]
        if delta == 0:
            continue
        nxt = next((t for t in trading_days if t > d), None)
        if nxt is None:
            continue
        o, c, ots, cts = oc[nxt]
        ret = (c - o) / o * 100.0
        signals.append((d, nxt, 1 if delta > 0 else -1, ret, delta))

    n = len(signals)
    hits = sum(1 for *_ , s, r, _d in [(a, b, s, r, dl) for a, b, s, r, dl in signals]
               if (s > 0 and r > 0) or (s < 0 and r < 0))
    cond = [s * r for _, _, s, r, _ in signals]  # signed conditioned return
    mean_cond = sum(cond) / n if n else 0.0
    best_i = max(range(n), key=lambda i: cond[i]) if n else None
    ex_best = (sum(cond) - cond[best_i]) / (n - 1) if n and n > 1 else 0.0

    ups = sum(1 for d in trading_days if oc[d][1] > oc[d][0])
    print(f"FII stance-change tilt study — n={n} signals "
          f"(OI days {oi_days[0]}..{oi_days[-1]})")
    print(f"base rate: {ups}/{len(trading_days)} sessions closed above open "
          f"({ups/len(trading_days)*100:.0f}%)")
    print(f"hit rate: {hits}/{n} = {hits/n*100:.1f}%")
    print(f"mean conditioned next-day open->close: {mean_cond:+.3f}%  "
          f"(ex-best-day {ex_best:+.3f}%, best was "
          f"{signals[best_i][0]}→{signals[best_i][1]} {cond[best_i]:+.3f}%)")
    ok = hits / n >= 0.58 and n >= 25 and (mean_cond > 0) == (ex_best > 0)
    print(f"pre-registered verdict: {'PASS' if ok else 'FAIL'}")
    print("\nper-signal detail:")
    for d, nxt, s, r, delta in signals:
        mark = "HIT " if (s > 0) == (r > 0) and r != 0 else "MISS"
        print(f"  {d} Δ={delta:>+8d} → {nxt} oc={r:>+6.2f}%  {mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
