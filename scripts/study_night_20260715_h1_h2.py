"""
study_night_20260715_h1_h2.py — Night hypotheses H1/H2 of 2026-07-15/16.

Pre-registered in logs/night_notes_2026-07-15_preregistration.md BEFORE running.

H1 — TURN-entry held to EOD (opposite-turn exit removed; stop/ratchet/EOD kept).
     DECLARED post-hoc-motivated on the same corpus (Study B decomposition);
     result cannot be promoted without out-of-sample confirmation.
H2 — Midday flush-and-reclaim mean reversion (12:30–13:30 IST new session low
     ≥0.25% below open, reclaim of the 12:30 close within 30 min, long to EOD
     with stop at the flush low).

Read-only. Usage: PYTHONPATH=. venv/bin/python scripts/study_night_20260715_h1_h2.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from study_hysteresis_turn_45d import (  # noqa: E402
    COST_RT, EOD_HHMM, LOTS, MAX_TURNS_PER_DAY, RATCHET_ARM_PCT, SLIPPAGE_PTS,
    STOP_PCT, TURN_COOLDOWN_SEC, hysteresis_turns, load_days,
)


def simulate_day_h1(bars, day, lot):
    """Study-B sleeve sim with the OPP_TURN exit removed."""
    turns = hysteresis_turns(bars)
    trades = []
    pos = None
    last_entry_epoch = -1e18
    entries_today = 0
    turn_at = {i: s for i, s in turns}
    eod_i = None
    for i, (dt, *_r) in enumerate(bars):
        if (dt.hour, dt.minute) >= EOD_HHMM:
            eod_i = i
            break
    eod_i = eod_i or len(bars) - 1

    def _exit(pos, px, i, reason):
        sgn = 1 if pos["dir"] == "LONG" else -1
        px = px - sgn * SLIPPAGE_PTS
        pts = sgn * (px - pos["entry"])
        trades.append({"day": day, "dir": pos["dir"], "pts": pts,
                       "pnl": pts * lot - COST_RT, "reason": reason})

    for i, (dt, o, h, l, c) in enumerate(bars):
        if pos is not None:
            sgn = 1 if pos["dir"] == "LONG" else -1
            stop_px = pos["entry"] * (1 - sgn * STOP_PCT)
            hit_stop = (l <= stop_px) if sgn == 1 else (h >= stop_px)
            fav_pct = sgn * ((h if sgn == 1 else l) - pos["entry"]) / pos["entry"]
            pos["hw_pct"] = max(pos["hw_pct"], fav_pct)
            if pos["hw_pct"] >= RATCHET_ARM_PCT:
                pos["armed"] = True
            if i >= eod_i:
                _exit(pos, o, i, "EOD"); pos = None
            elif hit_stop and not pos["armed"]:
                _exit(pos, stop_px, i, "STOP"); pos = None
            elif pos["armed"] and ((l <= pos["entry"]) if sgn == 1 else (h >= pos["entry"])):
                _exit(pos, pos["entry"], i, "RATCHET_BE"); pos = None
        if pos is None and i in turn_at and i + 1 < eod_i:
            if entries_today >= MAX_TURNS_PER_DAY:
                continue
            epoch = bars[i][0].timestamp()
            if epoch - last_entry_epoch < TURN_COOLDOWN_SEC:
                continue
            d = turn_at[i]
            sgn = 1 if d == "LONG" else -1
            pos = {"dir": d, "entry": bars[i + 1][1] + sgn * SLIPPAGE_PTS,
                   "hw_pct": 0.0, "armed": False}
            last_entry_epoch = epoch
            entries_today += 1
    if pos is not None:
        _exit(pos, bars[eod_i][4], eod_i, "EOD")
    return trades


def simulate_day_h2(bars, day, lot):
    """Midday flush-and-reclaim: one long max per day."""
    open_px = bars[0][1]
    i1230 = None
    for i, (dt, *_r) in enumerate(bars):
        if (dt.hour, dt.minute) >= (12, 30):
            i1230 = i
            break
    if i1230 is None:
        return []
    ref_close = bars[i1230][4]
    eod_i = None
    for i, (dt, *_r) in enumerate(bars):
        if (dt.hour, dt.minute) >= EOD_HHMM:
            eod_i = i
            break
    eod_i = eod_i or len(bars) - 1

    # find qualifying flush: NEW session low in 12:30-13:30, >=0.25% below open
    pre_low = min(b[3] for b in bars[:i1230])
    flush_i = None
    flush_low = None
    for i in range(i1230, len(bars)):
        dt, o, h, l, c = bars[i]
        if (dt.hour, dt.minute) > (13, 30):
            break
        if l < pre_low and l <= open_px * (1 - 0.0025):
            if flush_low is None or l < flush_low:
                flush_i, flush_low = i, l
    if flush_i is None:
        return []
    # reclaim: within 30 min of the flush low, a close back above the 12:30 close
    entry_i = None
    for i in range(flush_i, min(flush_i + 31, len(bars) - 1)):
        if bars[i][4] > ref_close:
            entry_i = i + 1
            break
    if entry_i is None or entry_i >= eod_i:
        return []
    entry = bars[entry_i][1] + SLIPPAGE_PTS
    for i in range(entry_i, eod_i + 1):
        dt, o, h, l, c = bars[i]
        if i >= eod_i:
            px = o - SLIPPAGE_PTS
            pts = px - entry
            return [{"day": day, "pts": pts, "pnl": pts * lot - COST_RT, "reason": "EOD"}]
        if l <= flush_low:
            px = flush_low - SLIPPAGE_PTS
            pts = px - entry
            return [{"day": day, "pts": pts, "pnl": pts * lot - COST_RT, "reason": "STOP"}]
    return []


def report(tag, all_trades, criteria):
    pnl_by_day = defaultdict(float)
    for t in all_trades:
        pnl_by_day[t["day"]] += t["pnl"]
    net = sum(pnl_by_day.values())
    best = max(pnl_by_day, key=pnl_by_day.get) if pnl_by_day else "—"
    ex_best = net - pnl_by_day.get(best, 0.0)
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    n = len(all_trades)
    wr = wins / n if n else 0.0
    print(f"  n={n}  win {wins}/{n} ({100*wr:.0f}%)  net ₹{net:+,.0f}  "
          f"best {best} ₹{pnl_by_day.get(best, 0):+,.0f}  ex-best ₹{ex_best:+,.0f}")
    by_reason = defaultdict(lambda: [0, 0.0])
    for t in all_trades:
        by_reason[t["reason"]][0] += 1
        by_reason[t["reason"]][1] += t["pnl"]
    for r, (k, s) in sorted(by_reason.items()):
        print(f"    exit {r:<10} n={k:<4} pnl=₹{s:+,.0f}")
    ok = criteria(n, net, ex_best, wr)
    print(f"  PRE-REGISTERED VERDICT [{tag}]: {'PASS' if ok else 'FAIL'}")
    return n, net, ex_best


def main() -> int:
    for tag, sim, crit in (
        ("H1 hold-to-EOD", simulate_day_h1,
         lambda n, net, exb, wr: n >= 30 and net > 0 and exb > 0),
        ("H2 flush-reclaim", simulate_day_h2,
         lambda n, net, exb, wr: n >= 10 and net > 0 and exb > 0 and wr >= 0.5),
    ):
        print(f"\n{'='*78}\n{tag}\n{'='*78}")
        grand = []
        tot_n = tot_net = 0
        for und in LOTS:
            days = load_days(f"data/source/{und}_FUT_1m_45d.csv")
            all_trades = []
            for day in sorted(days):
                bars = days[day]
                if len(bars) < 60:
                    continue
                all_trades += sim(bars, day, LOTS[und])
            print(f"\n{und} (lot={LOTS[und]}, {len(days)} days)")
            n, net, exb = report(tag, all_trades, crit)
            grand.append((n, net, exb))
        gn = sum(g[0] for g in grand)
        gnet = sum(g[1] for g in grand)
        gexb = sum(g[2] for g in grand)
        print(f"\n  GRAND [{tag}]: n={gn} net ₹{gnet:+,.0f} ex-best-per-und ₹{gexb:+,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
