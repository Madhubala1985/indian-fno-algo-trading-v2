#!/usr/bin/env python3
"""
futures_momentum_turn_study.py — night-explorer (2026-07-14), NEW pre-registered hypothesis.

PRE-REGISTRATION (written before any result was computed):

  Context: crossasset_shadow.py's LAST_TURN detector (constituent-flow rolling-mean sign
  flip) has only 2 days of live data and 0 logged TURN events (its old 09:15-09:35-only
  window never overlapped the 07-13 recovery). Before waiting weeks for that shadow to
  accrue, this asks a cheaper precursor question on data we ALREADY have 45 days of: does
  the SAME rolling-mean-turn logic, applied to FUTURES PRICE momentum instead of constituent
  stock flow (data/source/{NIFTY,BANKNIFTY,SENSEX}_FUT_1m_45d.csv, real traded bars), have
  any forward-looking edge at all? If a price-momentum turn is noise, a constituent-flow turn
  (a noisier, laggier proxy of the same thing) is unlikely to be better — informative either way.

  Method: per symbol, per trading day independently (a turn's forward window never crosses
  into the next day — no overnight leakage). At each bar t (t>=10 within the day), form a
  10-sample buffer of 1-bar % returns [t-9..t]. roll = mean(buf[-5:]) (recent 5-bar mean
  return), prev = mean(buf[:5]) (the preceding 5-bar mean return, i.e. roll shifted by 5
  bars) — this exactly mirrors crossasset_shadow's mom/roll construction (a FRESH push, not
  a sticky from-open level). TURN-to-LONG fires when prev <= -thr and roll >= +thr; TURN-to-
  SHORT is the mirror. thr = 0.02% (index futures 1-bar return stdev measured on this same
  file is 0.041-0.051%; a 5-bar mean's stdev under independence would be ~stdev/sqrt(5) =
  0.018-0.023%, so 0.02% is ~1 rolling-sigma — a real push, not simply picking whatever
  threshold flatters the result). Cooldown: after a turn fires, no new turn on the same
  symbol/day for 5 bars (avoids double-counting one push as many turns).

  Test: forward CLOSE-TO-CLOSE return in the turn's direction over the next 5/15/30 bars
  from the turn bar's close. Compare turn-direction win rate to 50% and mean forward move to
  the round-trip futures cost hurdle (~0.03% per the futures_sleeve cost model: brokerage+
  STT+txn+GST+stamp+slippage on ~₹various notional ≈ 2-4 bps one-way, call it 0.03% RT).

  PASS bar (pre-registered, not moved after seeing data): at the 30-bar horizon, turn-
  direction win rate > 55% AND mean forward move > 0.05% (clears the ~0.03% RT cost hurdle
  with margin) AND the result is not carried entirely by one day (ex-best-day still passes
  both bars). Anything short of that is FAIL / NOT PROMOTABLE — logged as a null result, not
  reworded as "promising."
"""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import datetime

THR = 0.02          # % — rolling-5-bar-mean turn threshold (see docstring)
COOLDOWN_BARS = 5
HORIZONS = (5, 15, 30)
RT_COST_PCT = 0.03   # % round-trip futures cost hurdle (brokerage+STT+txn+GST+stamp+slippage)

SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")


def load_days(sym):
    path = f"data/source/{sym}_FUT_1m_45d.csv"
    days = defaultdict(list)
    with open(path) as fh:
        r = csv.DictReader(fh)
        for row in r:
            ts = datetime.fromisoformat(row["timestamp"])
            days[ts.date()].append((ts, float(row["close"])))
    for d in days:
        days[d].sort(key=lambda x: x[0])
    return days


def find_turns(closes):
    """closes: list of (ts, close) for ONE day, chronological. Returns list of
    (idx, direction, roll) — idx is the bar the turn fires ON."""
    rets = [None] + [100.0 * (closes[i][1] - closes[i - 1][1]) / closes[i - 1][1]
                      for i in range(1, len(closes))]
    turns = []
    buf = []
    last_fire = -999
    for i in range(len(closes)):
        if rets[i] is None:
            continue
        buf.append(rets[i])
        if len(buf) > 10:
            buf.pop(0)
        if len(buf) < 10:
            continue
        prev = sum(buf[:5]) / 5
        roll = sum(buf[-5:]) / 5
        if i - last_fire < COOLDOWN_BARS:
            continue
        if prev <= -THR and roll >= THR:
            turns.append((i, "LONG", roll))
            last_fire = i
        elif prev >= THR and roll <= -THR:
            turns.append((i, "SHORT", roll))
            last_fire = i
    return turns


def main():
    all_results = []   # (sym, date, idx, direction, horizon, fwd_move_signed)
    per_symbol_n = defaultdict(int)
    for sym in SYMBOLS:
        days = load_days(sym)
        for date, closes in sorted(days.items()):
            if len(closes) < 40:
                continue
            turns = find_turns(closes)
            per_symbol_n[sym] += len(turns)
            for idx, direction, roll in turns:
                base_px = closes[idx][1]
                sign = 1.0 if direction == "LONG" else -1.0
                for h in HORIZONS:
                    j = min(idx + h, len(closes) - 1)
                    fwd_px = closes[j][1]
                    fwd_move = sign * 100.0 * (fwd_px - base_px) / base_px
                    all_results.append((sym, date, idx, direction, h, fwd_move))

    print("=" * 92)
    print("  FUTURES MOMENTUM-TURN STUDY (pre-registered, 45-day backfill)")
    print("=" * 92)
    total_turns = sum(per_symbol_n.values())
    print(f"  turn events: {total_turns}  ({dict(per_symbol_n)})")
    if total_turns == 0:
        print("  No turns detected — thr/cooldown too strict for this data. FAIL (no signal to test).")
        return 0

    for h in HORIZONS:
        rows = [r for r in all_results if r[4] == h]
        n = len(rows)
        wins = sum(1 for r in rows if r[5] > 0)
        mean_move = statistics.mean(r[5] for r in rows)
        wr = 100.0 * wins / n if n else 0.0
        # ex-best-day: drop the single (sym,date) with the largest total signed contribution
        by_day = defaultdict(float)
        for r in rows:
            by_day[(r[0], r[1])] += r[5]
        best_day = max(by_day, key=by_day.get) if by_day else None
        rows_ex = [r for r in rows if (r[0], r[1]) != best_day]
        n_ex = len(rows_ex)
        mean_move_ex = statistics.mean(r[5] for r in rows_ex) if rows_ex else 0.0
        wr_ex = (100.0 * sum(1 for r in rows_ex if r[5] > 0) / n_ex) if n_ex else 0.0
        verdict = "PASS" if (wr > 55.0 and mean_move > 0.05 and wr_ex > 55.0 and mean_move_ex > 0.05) else "FAIL"
        print(f"  +{h:2d} bars: n={n:4d}  win_rate={wr:5.1f}%  mean_fwd_move={mean_move:+.4f}%"
              f"  |  ex-best-day({best_day}): n={n_ex} wr={wr_ex:5.1f}% mean={mean_move_ex:+.4f}%"
              f"  -> {verdict}")

    print("-" * 92)
    for sym in SYMBOLS:
        rows = [r for r in all_results if r[0] == sym and r[4] == 30]
        n = len(rows)
        if n == 0:
            continue
        wins = sum(1 for r in rows if r[5] > 0)
        mean_move = statistics.mean(r[5] for r in rows)
        print(f"  {sym:10s} +30bar: n={n:4d}  win_rate={100.0*wins/n:5.1f}%  mean_fwd_move={mean_move:+.4f}%")
    print("=" * 92)
    print(f"  round-trip cost hurdle used for PASS bar: {RT_COST_PCT}%")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
