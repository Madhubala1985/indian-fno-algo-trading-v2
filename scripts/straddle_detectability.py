#!/usr/bin/env python3
"""
straddle_detectability.py — is the expansion/compression day knowable EARLY (causally)?

The straddle study found the prize is regime-conditional: an EOD-held ATM straddle makes
big money on expansion days and bleeds on compression days — but that split used the FULL
day's realised range (lookahead). This checks whether the OPENING RANGE (first 15/30/45 min,
strictly causal) separates the same days, i.e. whether a real-time router could catch the
expansion sleeve before the move.
"""
from __future__ import annotations

import glob
import csv as _csv
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
_PATS = {"NIFTY": "nifty", "SENSEX": "sensex"}
_OPEN_UTC = (3, 45)  # 09:15 IST


def _bars(underlying: str):
    by_ts = {}
    for path in glob.glob(f"data/{_PATS[underlying]}_1m_*.csv"):
        with open(path, newline="") as fh:
            for r in _csv.DictReader(fh):
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    by_ts[ts] = (float(r["open"]), float(r["high"]),
                                 float(r["low"]), float(r["close"]))
                except Exception:
                    pass
    return by_ts


def _day_bars(by_ts, day):
    return {ts: v for ts, v in by_ts.items()
            if ts.astimezone(timezone.utc).date() == day}


def _range_pct(day_bars, minutes: int | None):
    if not day_bars:
        return None
    keys = sorted(day_bars)
    op = day_bars[keys[0]][0]
    if minutes is not None:
        cutoff = keys[0] + timedelta(minutes=minutes)
        keys = [k for k in keys if k < cutoff]
    if not keys or not op:
        return None
    hi = max(day_bars[k][1] for k in keys)
    lo = min(day_bars[k][2] for k in keys)
    return 100.0 * (hi - lo) / op


def main():
    import json
    from pathlib import Path
    data = json.loads(Path("scratchpad_straddle.json").read_text())
    thr = data["threshold_range_pct"]
    print(f"expansion label: full-day range >= {thr:.2f}%   "
          f"(open-range columns are CAUSAL — known intraday)")
    print(f"{'day':>18} {'lbl':>4} {'full%':>6} {'o15%':>6} {'o30%':>6} {'o45%':>6}  "
          f"{'DIR':>9} {'STRAD':>8} {'EOD':>9}")
    rows = []
    caches = {u: _bars(u) for u in _PATS}
    for s in data["sessions"]:
        if not s["trades"]:
            continue
        u, day_s = s["underlying"], s["day"]
        from datetime import date
        day = date.fromisoformat(day_s)
        db = _day_bars(caches[u], day)
        full = _range_pct(db, None)
        o15 = _range_pct(db, 15)
        o30 = _range_pct(db, 30)
        o45 = _range_pct(db, 45)
        dir_pnl = sum(t["dir_net"] for t in s["trades"])
        strad = sum(t["strad_net"] for t in s["trades"] if t["strad_ok"])
        eod = sum(t["strad_eod_net"] for t in s["trades"] if t["strad_eod_ok"])
        lbl = "EXP" if full >= thr else "comp"
        rows.append((u, day_s, lbl, full, o15, o30, o45, dir_pnl, strad, eod))
        print(f"{u+' '+day_s:>18} {lbl:>4} {full:6.2f} {o15 or 0:6.2f} {o30 or 0:6.2f} "
              f"{o45 or 0:6.2f}  {dir_pnl:>+9,.0f} {strad:>+8,.0f} {eod:>+9,.0f}")

    # separation: does o30 rank the EXP days above comp days?
    print("\n-- separation by opening-range (o30%) --")
    for cut in (0.30, 0.35, 0.40, 0.45, 0.50):
        # route: o30 >= cut -> straddle-EOD sleeve; else -> directional sleeve
        routed = 0.0
        n_strad = 0
        for (u, d, lbl, full, o15, o30, o45, dp, st, eod) in rows:
            if o30 is not None and o30 >= cut:
                routed += eod   # straddle sleeve
                n_strad += 1
            else:
                routed += dp    # directional sleeve
        print(f"  o30 >= {cut:.2f}%  -> {n_strad:>2} days straddled  "
              f"ROUTED BOOK ₹{routed:>+10,.0f}")
    dir_only = sum(r[7] for r in rows)
    print(f"  (directional-only baseline ₹{dir_only:+,.0f})")


if __name__ == "__main__":
    main()
