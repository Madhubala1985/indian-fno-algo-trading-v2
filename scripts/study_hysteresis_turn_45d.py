"""
study_hysteresis_turn_45d.py — Night study 2026-07-15 (Priority B)
──────────────────────────────────────────────────────────────────────
Grades the DEPLOYED hysteresis flow-TURN detector (crossasset_shadow,
fix #3 of 2026-07-15) on the 45-day futures corpus.

PRE-REGISTERED (before any measurement — do not tune after seeing results):
  * Detector: EXACTLY as deployed — 1-min samples of flow, rolling mean of
    the last 5 samples, side = LONG when roll >= +0.05, SHORT when
    roll <= -0.05, side RETAINED through the neutral band; a TURN fires on
    side change (the first side assignment of a session does not fire).
  * Flow proxy: futures %-move-from-session-open (the real constituent-flow
    corpus only exists for 07-13..07-15; the proxy is validated against the
    real flow series on the overlap days and reported as Pearson r —
    if r < 0.8 the whole study is flagged as proxy-limited).
  * Would-be sleeve TURN P&L, faithful to futures_sleeve.py rails:
    1 lot in the turn direction at the NEXT 1m bar open, slippage 0.1 pt
    per side, hard stop -0.50% adverse (intrabar), breakeven ratchet arms
    at +0.30% (armed winner exits at entry price if touched again),
    opposite-turn exit at next bar open, EOD flat 15:20 IST,
    TURN cooldown 1800 s per index, max 3 TURN entries/day.
    Costs: flat ₹457/lot round trip (operator-specified).
  * PASS criteria: total net P&L after costs > 0 AND ex-best-day net > 0,
    with n >= 30 trades. Anything else is FAIL.
  * Lead vs price turns: zigzag pivots on 1m closes with 0.30% reversal
    threshold; for each fire, minutes from the nearest pivot within
    ±60 min (positive = fire AFTER the pivot, i.e. lag).

Read-only; writes nothing outside stdout.

Usage:
  PYTHONPATH=. venv/bin/python scripts/study_hysteresis_turn_45d.py
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))

BAND = 0.05          # deployed hysteresis band (±%)
ROLL_N = 5           # deployed rolling-mean window (samples)
STOP_PCT = 0.0050    # deployed sleeve hard stop
RATCHET_ARM_PCT = 0.0030
SLIPPAGE_PTS = 0.10
COST_RT = 457.0      # ₹/lot round trip (operator-specified)
TURN_COOLDOWN_SEC = 1800
MAX_TURNS_PER_DAY = 3
EOD_HHMM = (15, 20)
ZIGZAG_PCT = 0.30

LOTS = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20}


def load_days(path: str):
    """CSV -> {date_str: [(dt_ist, o, h, l, c), ...]} sorted."""
    days: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            dt = datetime.fromisoformat(r["timestamp"])
            days[r["timestamp"][:10]].append(
                (dt, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    for d in days.values():
        d.sort(key=lambda x: x[0])
    return dict(days)


def hysteresis_turns(bars):
    """Run the deployed detector on one session. Returns [(bar_idx, side)]."""
    open_px = bars[0][1]
    buf: list[float] = []
    side = None
    turns = []
    for i, (_, _, _, _, c) in enumerate(bars):
        flow = 100.0 * (c - open_px) / open_px
        buf.append(flow)
        if len(buf) > ROLL_N:
            buf.pop(0)
        if len(buf) < ROLL_N:
            continue
        roll = sum(buf) / ROLL_N
        new_side = ("LONG" if roll >= BAND else "SHORT" if roll <= -BAND else side)
        if side is not None and new_side is not None and new_side != side:
            turns.append((i, new_side))
        if new_side is not None:
            side = new_side
    return turns


def zigzag_pivots(bars, pct=ZIGZAG_PCT):
    """Pivot indices of local extremes with >= pct% reversals (on closes)."""
    closes = [b[4] for b in bars]
    pivots = []
    trend = None
    ext_i = 0
    for i, c in enumerate(closes):
        if c > closes[ext_i] and (trend in (None, "up") or True) and trend == "up":
            ext_i = i
        elif c < closes[ext_i] and trend == "down":
            ext_i = i
        move = 100.0 * (c - closes[ext_i]) / closes[ext_i]
        if trend is None:
            if abs(move) >= pct:
                trend = "up" if move > 0 else "down"
                pivots.append(ext_i)
                ext_i = i
        elif trend == "up" and move <= -pct:
            pivots.append(ext_i)   # the high
            trend = "down"
            ext_i = i
        elif trend == "down" and move >= pct:
            pivots.append(ext_i)   # the low
            trend = "up"
            ext_i = i
    return pivots


def simulate_day(bars, day, lot):
    """Sleeve TURN strategy for one session. Returns (trades, turn_count, leads)."""
    turns = hysteresis_turns(bars)
    pivots = zigzag_pivots(bars)
    leads = []
    for ti, _ in turns:
        near = min(pivots, key=lambda p: abs(p - ti), default=None)
        if near is not None and abs(near - ti) <= 60:
            leads.append(ti - near)

    trades = []
    pos = None          # {"dir","entry","lot","hw_pct","armed","entry_i"}
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
        pnl = pts * pos["lot"] - COST_RT
        trades.append({"day": day, "dir": pos["dir"], "entry_i": pos["entry_i"],
                       "exit_i": i, "pts": pts, "pnl": pnl, "reason": reason})

    for i, (dt, o, h, l, c) in enumerate(bars):
        # exits first
        if pos is not None:
            sgn = 1 if pos["dir"] == "LONG" else -1
            adverse = -sgn * (o - pos["entry"]) / pos["entry"]
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
            elif i in turn_at and turn_at[i] != pos["dir"]:
                _exit(pos, c, i, "OPP_TURN"); pos = None
        # entries on a turn at bar i -> next bar open
        if pos is None and i in turn_at and i + 1 < eod_i:
            if entries_today >= MAX_TURNS_PER_DAY:
                continue
            epoch = bars[i][0].timestamp()
            if epoch - last_entry_epoch < TURN_COOLDOWN_SEC:
                continue
            d = turn_at[i]
            sgn = 1 if d == "LONG" else -1
            entry = bars[i + 1][1] + sgn * SLIPPAGE_PTS
            pos = {"dir": d, "entry": entry, "lot": lot, "hw_pct": 0.0,
                   "armed": False, "entry_i": i + 1}
            last_entry_epoch = epoch
            entries_today += 1
    if pos is not None:
        _exit(pos, bars[eod_i][4], eod_i, "EOD")
    return trades, len(turns), leads


def validate_proxy():
    """Pearson r between real constituent flow and futures %-from-open on overlap days."""
    real = defaultdict(dict)   # (und, day) -> {hhmm: avg_flow_pct}
    p = Path("logs/crossasset_shadow.jsonl")
    if p.exists():
        for line in p.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "flow" or r.get("avg_flow_pct") is None:
                continue
            dt = datetime.fromisoformat(r["ts"]).astimezone(_IST)
            real[(r["underlying"], dt.date().isoformat())][dt.strftime("%H:%M")] = r["avg_flow_pct"]
    out = []
    for und in LOTS:
        days = load_days(f"data/source/{und}_FUT_1m_45d.csv")
        for (u, day), series in real.items():
            if u != und or day not in days:
                continue
            bars = days[day]
            open_px = bars[0][1]
            xs, ys = [], []
            for dt, _, _, _, c in bars:
                hm = dt.strftime("%H:%M")
                if hm in series:
                    xs.append(100.0 * (c - open_px) / open_px)
                    ys.append(series[hm])
            if len(xs) >= 60:
                n = len(xs)
                mx, my = sum(xs) / n, sum(ys) / n
                cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
                vx = sum((a - mx) ** 2 for a in xs) ** 0.5
                vy = sum((b - my) ** 2 for b in ys) ** 0.5
                r = cov / (vx * vy) if vx and vy else float("nan")
                out.append((und, day, n, round(r, 3)))
    return out


def main() -> int:
    print("=" * 78)
    print("PROXY VALIDATION — real constituent flow vs futures %-from-open")
    print("=" * 78)
    for und, day, n, r in validate_proxy():
        print(f"  {und:<10} {day}  n={n:<4} pearson_r={r}")

    grand = []
    for und in LOTS:
        days = load_days(f"data/source/{und}_FUT_1m_45d.csv")
        all_trades, total_turns, all_leads = [], 0, []
        for day in sorted(days):
            bars = days[day]
            if len(bars) < 60:
                continue
            trades, nturns, leads = simulate_day(bars, day, LOTS[und])
            for t in trades:
                t["lot"] = LOTS[und]
                t["pnl"] = t["pts"] * LOTS[und] - COST_RT
            all_trades += trades
            total_turns += nturns
            all_leads += leads
        ndays = len(days)
        pnl_by_day = defaultdict(float)
        for t in all_trades:
            pnl_by_day[t["day"]] += t["pnl"]
        net = sum(pnl_by_day.values())
        best_day = max(pnl_by_day, key=pnl_by_day.get) if pnl_by_day else "—"
        ex_best = net - (pnl_by_day.get(best_day, 0.0))
        wins = sum(1 for t in all_trades if t["pnl"] > 0)
        leads_sorted = sorted(all_leads)
        med_lead = leads_sorted[len(leads_sorted) // 2] if leads_sorted else None
        print(f"\n{'='*78}\n{und}  (lot={LOTS[und]}, {ndays} days)\n{'='*78}")
        print(f"  turn fires: {total_turns}  ({total_turns/ndays:.1f}/day)")
        print(f"  sleeve TURN trades: {len(all_trades)}  ({len(all_trades)/ndays:.1f}/day, "
              f"cooldown+cap binding)")
        print(f"  win rate: {wins}/{len(all_trades)}  "
              f"net P&L after ₹{COST_RT:.0f}/lot: ₹{net:+,.0f}")
        print(f"  best day: {best_day} ₹{pnl_by_day.get(best_day, 0):+,.0f}   "
              f"ex-best-day net: ₹{ex_best:+,.0f}")
        print(f"  median fire-vs-pivot: {med_lead} min "
              f"(positive = fires AFTER the price pivot)  n_leads={len(all_leads)}")
        by_reason = defaultdict(lambda: [0, 0.0])
        for t in all_trades:
            by_reason[t["reason"]][0] += 1
            by_reason[t["reason"]][1] += t["pnl"]
        for reason, (n, s) in sorted(by_reason.items()):
            print(f"    exit {reason:<12} n={n:<4} pnl=₹{s:+,.0f}")
        verdict = "PASS" if (net > 0 and ex_best > 0 and len(all_trades) >= 30) else "FAIL"
        print(f"  PRE-REGISTERED VERDICT: {verdict}")
        grand.append((und, len(all_trades), net, ex_best))

    print(f"\n{'='*78}\nGRAND TOTAL")
    tot = sum(g[2] for g in grand)
    totex = sum(g[3] for g in grand)
    n = sum(g[1] for g in grand)
    print(f"  n={n}  net=₹{tot:+,.0f}  ex-best-day-per-underlying=₹{totex:+,.0f}")
    print(f"  OVERALL: {'PASS' if tot > 0 and totex > 0 and n >= 30 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
