#!/usr/bin/env python3
"""
replay_day.py — run the surgical replay for one underlying/day on REAL data.

Loads 1m index bars (warm-start = prior days, session = the target day), fetches
the real historical option chain (cached), and replays the core decision path,
reporting trades + P&L at the real recorded option premiums (no Black-Scholes).

Usage:
  PYTHONPATH=. python3 scripts/replay_day.py --underlying SENSEX --day 2026-06-30
  PYTHONPATH=. python3 scripts/replay_day.py --underlying SENSEX --day 2026-06-30 --conv-floor 0.40
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from platform_core.backtesting.backtest_runner import LiveBar
from platform_core.data.live.kite_adapter import KiteAdapter
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
from platform_core.backtesting.surgical_replay import SurgicalReplay

_CFG = {
    "SENSEX":    {"feed": "SENSEX",    "exch": "BFO", "step": 100.0, "lot": 20},
    "NIFTY":     {"feed": "NIFTY50",   "exch": "NFO", "step": 50.0,  "lot": 75},
    "BANKNIFTY": {"feed": "BANKNIFTY", "exch": "NFO", "step": 100.0, "lot": 30},
}


def _load_csv(path: Path, feed: str) -> list[LiveBar]:
    bars = []
    with path.open(newline="") as fh:
        for r in _csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bars.append(LiveBar(symbol=feed, timeframe="MIN_1", start_timestamp=ts,
                                    end_timestamp=ts + timedelta(minutes=1),
                                    open_price=float(r["open"]), high_price=float(r["high"]),
                                    low_price=float(r["low"]), close_price=float(r["close"]),
                                    volume=float(r.get("volume") or 0)))
            except Exception:
                pass
    return bars


def _newest_csv(underlying: str) -> Path | None:
    pat = {"SENSEX": "sensex", "NIFTY": "nifty", "BANKNIFTY": "banknifty"}[underlying]
    files = [Path(p) for p in glob.glob(f"data/{pat}_1m_*.csv")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", required=True, choices=list(_CFG))
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--conv-floor", type=float, default=None)
    ap.add_argument("--strikes", type=int, default=8)
    args = ap.parse_args()

    cfg = _CFG[args.underlying]
    day = date.fromisoformat(args.day)
    csv_path = _newest_csv(args.underlying)
    if csv_path is None:
        print("No index CSV in data/ — run scripts/refresh_mcb_warmstart_data.py")
        return 1
    all_bars = _load_csv(csv_path, cfg["feed"])
    warm = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() < day]
    session = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() == day]
    if not session:
        print(f"No index bars for {day} in {csv_path.name}")
        return 1
    print(f"[replay] {args.underlying} {day}  warm_bars={len(warm)}  session_bars={len(session)}")

    a = KiteAdapter()
    expiry = a.select_expiry(args.underlying, day, min_dte=2, exchange=cfg["exch"])
    spot_atm = session[0].open_price
    print(f"[replay] expiry={expiry}  spot_atm≈{spot_atm:.0f}  fetching real chain (strikes ±{args.strikes})…")
    fetcher = HistoricalOptionChainFetcher(a)
    cache = fetcher.fetch_day(underlying=args.underlying, day=day, expiry=expiry,
                              spot_atm=spot_atm, strike_step=cfg["step"],
                              strikes_around_atm=args.strikes, exchange=cfg["exch"])
    chain_by_min = fetcher.load_day(cache)
    print(f"[replay] chain minutes={len(chain_by_min)}  cache={cache.name}")

    rep = SurgicalReplay(underlying_symbol=args.underlying, conviction_floor=args.conv_floor)
    rep.warm_start(warm, cfg["feed"])
    res = rep.run_day(underlying=args.underlying, feed_symbol=cfg["feed"], exchange=cfg["exch"],
                      expiry=expiry, lot_size=cfg["lot"], strike_step=cfg["step"], day=day,
                      session_bars=session, chain_by_min=chain_by_min, fetcher=fetcher)

    print("\n" + "=" * 74)
    print(f"  REPLAY RESULT — {args.underlying} {day}  (real historical premiums)")
    print("=" * 74)
    if not res.trades:
        print("  no trades (bias never directional, or all gated)")
    for t in res.trades:
        print(f"  {t.entry_ts[11:16]}→{t.exit_ts[11:16]}  {t.contract:20} {t.direction:5} "
              f"lots={t.lots} conv={t.conviction:.2f}  entry={t.entry_prem:.1f} exit={t.exit_prem:.1f} "
              f"pnl=₹{t.pnl:>+8.0f}  {t.reason}")
    print("-" * 74)
    print(f"  TRADES {len(res.trades)}   TOTAL P&L ₹{res.total_pnl:+,.0f}   (conv_floor={args.conv_floor})")
    if res.blocks:
        print("  entry-gate blocks: " + "  ".join(
            f"{k}={v}" for k, v in sorted(res.blocks.items(), key=lambda kv: -kv[1])))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
