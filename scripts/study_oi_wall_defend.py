#!/usr/bin/env python
"""H7 (pre-registered 2026-07-19 late-afternoon run, night_explorer_2026-07-19.md):

Writer-OI wall defend-vs-cover predicts break-vs-reject at wall touches.

Definitions (fixed in the pre-registration BEFORE computation):
- Chain: data/option_history/{U}_{day}_{expiry}.jsonl, nearest expiry per (U, day).
- At minute t, CE wall = strike with max CE OI in that minute's snapshot; PE wall
  likewise from PE OI.
- Touch = first minute after 10:00 IST (04:30 UTC) where spot is within 0.10% of
  the CE wall from below (0 <= (wall-spot)/wall <= 0.001), resp. the PE wall from
  above. Max one event per side per day.
- Signal = delta OI at the touched wall strike over the 30 minutes before touch
  (nearest snapshot at or before touch-30m): >= +2% of touch-minute wall OI ->
  DEFEND; <= -2% -> COVER; else neutral (excluded from the graded rule).
- Outcome: REJECT if spot is back on the original side of the wall 60 minutes
  after touch (or at the last bar <= 15:20 IST if sooner); BREAK otherwise.
- Hypothesis: DEFEND -> REJECT, COVER -> BREAK.
- PASS iff combined hit rate >= 65% with n >= 15 non-neutral events, stays >= 60%
  after dropping the single day contributing the most hits, and DEFEND reject-rate
  > pooled reject-rate AND COVER break-rate > pooled break-rate.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
HIST = Path("data/option_history")
SPOT = {
    "NIFTY": "data/nifty_1m_2026-05-15_2026-07-16.csv",
    "BANKNIFTY": "data/banknifty_1m_2026-05-15_2026-07-16.csv",
    "SENSEX": "data/sensex_1m_2026-05-15_2026-07-16.csv",
}
TOUCH_PCT = 0.001
SIG_LOOKBACK = timedelta(minutes=30)
SIG_THRESH = 0.02
OUTCOME_FWD = timedelta(minutes=60)
SESSION_END_IST = dtime(15, 20)
START_IST = dtime(10, 0)


def chain_files() -> dict[tuple[str, date], Path]:
    """(underlying, day) -> nearest-expiry chain file."""
    best: dict[tuple[str, date], tuple[date, Path]] = {}
    pat = re.compile(r"^([A-Z]+)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.jsonl$")
    for p in sorted(HIST.glob("*.jsonl")):
        m = pat.match(p.name)
        if not m:
            continue
        u, day, exp = m.group(1), date.fromisoformat(m.group(2)), date.fromisoformat(m.group(3))
        key = (u, day)
        if key not in best or exp < best[key][0]:
            best[key] = (exp, p)
    return {k: v[1] for k, v in best.items()}


def load_chain(path: Path):
    """minute(datetime UTC) -> {(strike, type): oi}"""
    by_min: dict[datetime, dict] = defaultdict(dict)
    for line in path.open():
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = datetime.fromisoformat(d["ts"]).astimezone(timezone.utc)
        by_min[ts][(float(d["strike"]), d["type"])] = float(d["oi"])
    return dict(sorted(by_min.items()))


def load_spot(path: str) -> dict[date, list[tuple[datetime, float]]]:
    days: dict[date, list] = defaultdict(list)
    with open(path) as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"])
            days[ts.astimezone(IST).date()].append((ts, float(row["close"])))
    return {d: sorted(v) for d, v in days.items()}


def wall(snap: dict, opt_type: str):
    cands = [(oi, k) for (k, t), oi in snap.items() if t == opt_type]
    if not cands:
        return None, 0.0
    oi, strike = max(cands)
    return strike, oi


def main() -> int:
    events = []
    skipped = []
    files = chain_files()
    spot_cache = {u: load_spot(p) for u, p in SPOT.items()}
    for (u, day), path in sorted(files.items()):
        bars = spot_cache[u].get(day)
        if not bars:
            skipped.append(f"{u} {day} (no index bars)")
            continue
        chain = load_chain(path)
        if not chain:
            skipped.append(f"{u} {day} (empty chain)")
            continue
        minutes = list(chain)
        end_utc = datetime.combine(day, SESSION_END_IST, IST).astimezone(timezone.utc)
        done = {"CE": False, "PE": False}
        for ts, close in bars:
            tsu = ts.astimezone(timezone.utc)
            if ts.astimezone(IST).time() < START_IST or tsu > end_utc:
                continue
            # nearest chain snapshot at or before this bar
            snap_ts = None
            for m in minutes:
                if m <= tsu:
                    snap_ts = m
                else:
                    break
            if snap_ts is None or tsu - snap_ts > timedelta(minutes=3):
                continue
            snap = chain[snap_ts]
            for side in ("CE", "PE"):
                if done[side]:
                    continue
                w, oi_now = wall(snap, side)
                if w is None or oi_now <= 0:
                    continue
                dist = (w - close) / w if side == "CE" else (close - w) / w
                if not (0 <= dist <= TOUCH_PCT):
                    continue
                done[side] = True
                # signal: wall-strike OI change over the 30 min before touch
                prior_ts = None
                for m in minutes:
                    if m <= snap_ts - SIG_LOOKBACK:
                        prior_ts = m
                    else:
                        break
                if prior_ts is None:
                    sig = "no_history"
                else:
                    oi_prior = chain[prior_ts].get((w, side))
                    if oi_prior is None:
                        sig = "no_history"
                    else:
                        chg = (oi_now - oi_prior) / oi_now
                        sig = ("DEFEND" if chg >= SIG_THRESH
                               else "COVER" if chg <= -SIG_THRESH else "neutral")
                # outcome: spot side at touch+60m (or last bar <= 15:20 IST)
                target = min(tsu + OUTCOME_FWD, end_utc)
                out_close = None
                for bts, bclose in bars:
                    if bts.astimezone(timezone.utc) <= target:
                        out_close = bclose
                    else:
                        break
                reject = out_close < w if side == "CE" else out_close > w
                events.append({
                    "underlying": u, "day": day.isoformat(), "side": side,
                    "wall": w, "touch_ts": tsu.isoformat(),
                    "touch_close": close, "wall_oi": oi_now, "signal": sig,
                    "outcome_close": out_close,
                    "outcome": "REJECT" if reject else "BREAK",
                })
    out = Path("logs/study_oi_wall_defend.jsonl")
    with out.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    graded = [e for e in events if e["signal"] in ("DEFEND", "COVER")]
    hits = [e for e in graded if (e["signal"] == "DEFEND") == (e["outcome"] == "REJECT")]
    n_rej = sum(1 for e in events if e["outcome"] == "REJECT")
    print(f"touch events: {len(events)}  (skipped days: {len(skipped)})")
    for s in skipped:
        print(f"  skipped: {s}")
    print(f"pooled reject rate: {n_rej}/{len(events)}"
          + (f" = {n_rej/len(events):.1%}" if events else ""))
    for side in ("CE", "PE"):
        se = [e for e in events if e["side"] == side]
        sr = sum(1 for e in se if e["outcome"] == "REJECT")
        print(f"  {side}: {len(se)} touches, reject {sr}/{len(se)}")
    by_sig = defaultdict(list)
    for e in events:
        by_sig[e["signal"]].append(e)
    for sig, es in sorted(by_sig.items()):
        rej = sum(1 for e in es if e["outcome"] == "REJECT")
        print(f"  signal={sig}: n={len(es)} reject={rej} break={len(es)-rej}")
    if graded:
        print(f"graded rule: {len(hits)}/{len(graded)} = {len(hits)/len(graded):.1%} hit")
        by_day = defaultdict(int)
        for e in hits:
            by_day[(e['underlying'], e['day'])] += 1
        if by_day:
            best_day, best_n = max(by_day.items(), key=lambda kv: kv[1])
            exn, exd = len(hits) - best_n, len(graded) - sum(
                1 for e in graded if (e['underlying'], e['day']) == best_day)
            print(f"ex-best-day ({best_day}, -{best_n} hits): {exn}/{exd}"
                  + (f" = {exn/exd:.1%}" if exd else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
