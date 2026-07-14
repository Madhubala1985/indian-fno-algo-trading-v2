#!/usr/bin/env python3
"""
reprice_live_straddle.py — the straddle study RE-ANCHORED to LIVE trades.

The parity test (replay_parity_test.py) showed SurgicalReplay is unfaithful (1/11 days),
so the original straddle_study directional baseline is invalid. This redoes the straddle-
vs-directional comparison on the REAL live trades (logs/trades.db pt_trades): for each live
trade, reprice an ATM straddle over the SAME entry/exit window on the recorded chain, plus
a hold-to-close variant. Aggregate, split by expansion/compression day (index realised range).

Only days with a recorded chain are used (06-29..07-10 overlap). Live entry/exit ts are UTC
and align with the chain's UTC minute keys.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timezone

from straddle_study import (_CFG, _CACHE, _load_all_bars, _minute_map,
                            _reprice_straddle, _recorded_days)
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher


def _live_by_day():
    c = sqlite3.connect("logs/trades.db")
    out: dict[tuple, list] = {}
    for r in c.execute("""select date(entry_ts), underlying, contract_symbol, direction,
                                 lots, lot_size, entry_premium, pnl, entry_ts, exit_ts
                          from pt_trades order by entry_ts"""):
        out.setdefault((r[1], r[0]), []).append({
            "contract": r[2], "dir": r[3], "lots": r[4] or 1, "lot_size": r[5] or 1,
            "entry_prem": r[6] or 0.0, "pnl": r[7] or 0.0,
            "entry_ts": r[8], "exit_ts": r[9]})
    return out


def main():
    live = _live_by_day()
    fetchers = {u: HistoricalOptionChainFetcher(adapter=None) for u in _CFG}
    rows = []  # (u, day, range%, list of per-trade dicts)
    for u in ("NIFTY", "SENSEX"):
        cfg = _CFG[u]
        bars_all = _load_all_bars(u, cfg["feed"])
        for d in _recorded_days(u):
            key = (u, d.isoformat())
            trades = live.get(key)
            if not trades:
                continue
            expiry = fetchers[u].cached_expiry(u, d)
            if expiry is None:
                continue
            cache = _CACHE / f"{u}_{d.isoformat()}_{expiry.isoformat()}.jsonl"
            if not cache.exists():
                continue
            mm = _minute_map(fetchers[u].load_day(cache))
            minutes = sorted(mm)
            if not minutes:
                continue
            close_iso = minutes[-1]
            sess = [b for b in bars_all if b.start_timestamp.astimezone(timezone.utc).date() == d]
            rng = (100.0 * (max(b.high_price for b in sess) - min(b.low_price for b in sess))
                   / sess[0].open_price) if sess else 0.0
            tr = []
            for t in trades:
                if not t["entry_ts"] or not t["exit_ts"]:
                    continue
                ei = t["entry_ts"].replace(" ", "T")
                xi = t["exit_ts"].replace(" ", "T")
                s = _reprice_straddle(underlying=u, lot_size=t["lot_size"], dir_lots=t["lots"],
                                      dir_entry_prem=t["entry_prem"], entry_iso=ei, exit_iso=xi,
                                      mm=mm, sorted_minutes=minutes)
                se = _reprice_straddle(underlying=u, lot_size=t["lot_size"], dir_lots=t["lots"],
                                       dir_entry_prem=t["entry_prem"], entry_iso=ei, exit_iso=close_iso,
                                       mm=mm, sorted_minutes=minutes)
                tr.append({"dir_pnl": t["pnl"], "contract": t["contract"], "dir": t["dir"],
                           "strad": s.net if s.ok else None, "eod": se.net if se.ok else None})
            rows.append((u, d.isoformat(), rng, tr))

    all_tr = [t for _, _, _, ts in rows for t in ts]
    ranges = sorted(r[2] for r in rows)
    thr = ranges[len(ranges) // 2] if ranges else 0.0

    def agg(ts):
        dp = sum(t["dir_pnl"] for t in ts)
        s = [t for t in ts if t["strad"] is not None]
        e = [t for t in ts if t["eod"] is not None]
        return (len(ts), dp, sum(t["strad"] for t in s), sum(t["eod"] for t in e),
                sum(1 for t in ts if t["dir_pnl"] > 0))

    def line(title, ts):
        n, dp, sp, ep, w = agg(ts)
        wr = 100.0 * w / n if n else 0.0
        print(f"  {title:20} n={n:>3}  DIR(live) ₹{dp:>+9,.0f} ({wr:3.0f}%WR)   "
              f"STRAD ₹{sp:>+9,.0f}   STRAD_EOD ₹{ep:>+9,.0f}")

    print("=" * 96)
    print(f"  LIVE-ANCHORED STRADDLE STUDY — {len(rows)} day-symbols, {len(all_tr)} REAL live trades")
    print(f"  (replaces the replay-baseline study; expansion = day range >= {thr:.2f}%)")
    print("=" * 96)
    line("ALL", all_tr)
    exp = [t for u, d, r, ts in rows if r >= thr for t in ts]
    comp = [t for u, d, r, ts in rows if r < thr for t in ts]
    line("EXPANSION days", exp)
    line("COMPRESSION days", comp)
    print("-" * 96)
    for u in ("NIFTY", "SENSEX"):
        line(u, [t for uu, d, r, ts in rows if uu == u for t in ts])
    print("-" * 96)
    print("  per day-symbol:")
    for u, d, r, ts in sorted(rows, key=lambda x: (x[1], x[0])):
        n, dp, sp, ep, w = agg(ts)
        lbl = "EXP " if r >= thr else "comp"
        dirs = "/".join(sorted({t["dir"] for t in ts}))
        print(f"    {lbl} {u:6} {d}  range={r:4.2f}%  n={n:>2} [{dirs:11}]  "
              f"DIR ₹{dp:>+8,.0f}   STRAD ₹{sp:>+8,.0f}   EOD ₹{ep:>+8,.0f}")
    print("=" * 96)
    n, dp, sp, ep, w = agg(all_tr)
    print(f"  VERDICT: live directional ₹{dp:+,.0f}  vs  same-window straddle ₹{sp:+,.0f}  "
          f"vs  hold-to-close straddle ₹{ep:+,.0f}")
    print("=" * 96)


if __name__ == "__main__":
    raise SystemExit(main())
