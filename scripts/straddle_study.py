#!/usr/bin/env python3
"""
straddle_study.py — test the "identity, not direction" reframe on REAL recorded chains.

Hypothesis under test: the book is a long-VOLATILITY book wearing a direction-picker.
If so, replacing each directional entry with a direction-neutral ATM STRADDLE (buy CE+PE)
over the SAME entry trigger should (a) not lose the monster expansion days and (b) shed the
compression-day directional bleed — i.e. straddles should carry the book at least as well
while deleting the MCB / alignment / bias-staleness machinery from the trade path.

Method (zero new infrastructure, purely a repricing join):
  1. For every recorded (underlying, day) session, run the FAITHFUL SurgicalReplay to get the
     exact directional trades the live model would take (entry_ts, exit_ts, direction, net P&L).
  2. Reprice each trade three ways on the SAME recorded per-minute chain:
       DIR     — what the model actually did (directional, net of costs)  [baseline]
       STRAD   — buy ATM CE+PE at entry_ts, exit at the SAME exit_ts       [direction deleted]
       STRAD_EOD — buy ATM CE+PE at entry_ts, hold to session close        [hold-through variant]
     Budget-matched: the straddle deploys the same rupee premium the directional trade did.
  3. Aggregate overall and split by a day-level EXPANSION label (index realised range %),
     because the reframe's whole claim is a conditional one: straddles win the book by paying
     on expansion days and bleeding less on compression days.

Honest caveats (printed in the report):
  * STRAD exits at the directional structure exit — a moment chosen to protect a DIRECTIONAL
    position, not a straddle. It is the assumption-free baseline, neither systematically kind
    nor cruel to the straddle. STRAD_EOD is the cheap upper-ish-bound counterfactual.
  * ATM strike is chosen data-only as argmin|CE-PE| at the entry minute (put-call parity),
    so no spot feed is needed and the choice is faithful to what was tradeable.
  * 22 sessions is a first-pass corpus. This is a hypothesis screen, not a promotion gate.

Usage:
  PYTHONPATH=. python3 scripts/straddle_study.py
  PYTHONPATH=. python3 scripts/straddle_study.py --risk-frac 0.08 --json out.json
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from platform_core.backtesting.backtest_runner import LiveBar
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
from platform_core.backtesting.surgical_replay import SurgicalReplay
from platform_core.execution.transaction_costs import apply_costs

# Mirror replay_day.py's per-underlying config.
_CFG = {
    "SENSEX": {"feed": "SENSEX",  "exch": "BFO", "step": 100.0, "lot": 20},
    "NIFTY":  {"feed": "NIFTY50", "exch": "NFO", "step": 50.0,  "lot": 75},
}
_CACHE = Path("data/option_history")


# ── index bars: merge ALL csvs for an underlying, dedupe by timestamp ─────────
def _load_all_bars(underlying: str, feed: str) -> list[LiveBar]:
    pat = {"SENSEX": "sensex", "NIFTY": "nifty"}[underlying]
    by_ts: dict[datetime, LiveBar] = {}
    for path in glob.glob(f"data/{pat}_1m_*.csv"):
        with open(path, newline="") as fh:
            for r in _csv.DictReader(fh):
                try:
                    ts = datetime.fromisoformat(r["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    by_ts[ts] = LiveBar(
                        symbol=feed, timeframe="MIN_1", start_timestamp=ts,
                        end_timestamp=ts + timedelta(minutes=1),
                        open_price=float(r["open"]), high_price=float(r["high"]),
                        low_price=float(r["low"]), close_price=float(r["close"]),
                        volume=float(r.get("volume") or 0),
                    )
                except Exception:
                    pass
    return [by_ts[k] for k in sorted(by_ts)]


def _recorded_days(underlying: str) -> list[date]:
    days = set()
    for p in _CACHE.glob(f"{underlying}_*.jsonl"):
        try:
            days.add(date.fromisoformat(p.stem.split("_")[1]))
        except (ValueError, IndexError):
            pass
    return sorted(days)


# ── ATM straddle repricing on a single day's chain ───────────────────────────
def _minute_map(chain_by_min: dict) -> dict:
    """{minute_iso: {(strike,type): ltp>0}} for fast leg lookup."""
    out: dict[str, dict] = {}
    for minute, contracts in chain_by_min.items():
        d = {}
        for c in contracts:
            ltp = c.get("ltp")
            if ltp and ltp > 0:
                d[(float(c["strike"]), c["type"])] = float(ltp)
        out[minute] = d
    return out


def _nearest_minute(mm: dict, target_iso: str, sorted_minutes: list[str],
                    max_back: int = 5) -> str | None:
    """Exact minute if present, else the nearest EARLIER minute within max_back."""
    if target_iso in mm and mm[target_iso]:
        return target_iso
    # sorted_minutes is ascending ISO; find last <= target
    import bisect
    i = bisect.bisect_right(sorted_minutes, target_iso) - 1
    steps = 0
    while i >= 0 and steps < max_back:
        m = sorted_minutes[i]
        if mm.get(m):
            return m
        i -= 1
        steps += 1
    return None


def _atm_strike(legs: dict) -> float | None:
    """argmin|CE-PE| across strikes present with BOTH legs (put-call parity ATM)."""
    strikes = {s for (s, t) in legs}
    best, best_gap = None, float("inf")
    for s in strikes:
        ce, pe = legs.get((s, "CE")), legs.get((s, "PE"))
        if ce is None or pe is None:
            continue
        gap = abs(ce - pe)
        if gap < best_gap:
            best, best_gap = s, gap
    return best


@dataclass
class Reprice:
    ok: bool = False
    strike: float = 0.0
    entry_unit: float = 0.0     # CE+PE premium at entry
    exit_unit: float = 0.0      # CE+PE premium at exit
    lots: int = 0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0


def _reprice_straddle(*, underlying: str, lot_size: int, dir_lots: int,
                      dir_entry_prem: float, entry_iso: str, exit_iso: str,
                      mm: dict, sorted_minutes: list[str]) -> Reprice:
    em = _nearest_minute(mm, entry_iso, sorted_minutes)
    if em is None:
        return Reprice()
    legs_e = mm[em]
    strike = _atm_strike(legs_e)
    if strike is None:
        return Reprice()
    ce_e, pe_e = legs_e[(strike, "CE")], legs_e[(strike, "PE")]
    entry_unit = ce_e + pe_e
    if entry_unit <= 0:
        return Reprice()
    # exit legs at the SAME strike
    xm = _nearest_minute(mm, exit_iso, sorted_minutes)
    if xm is None:
        return Reprice()
    legs_x = mm[xm]
    ce_x, pe_x = legs_x.get((strike, "CE")), legs_x.get((strike, "PE"))
    if ce_x is None or pe_x is None:
        return Reprice()
    exit_unit = ce_x + pe_x
    # budget-match the directional premium outlay; integer lots (realism)
    dir_budget = dir_lots * lot_size * dir_entry_prem
    lots = max(1, round(dir_budget / (lot_size * entry_unit)))
    qty = lots * lot_size
    gross = qty * (exit_unit - entry_unit)
    # costs: charge each leg separately (2-leg structure)
    _, c_ce = apply_costs(0.0, entry_premium=ce_e, exit_premium=ce_x,
                          quantity=qty, underlying=underlying)
    _, c_pe = apply_costs(0.0, entry_premium=pe_e, exit_premium=pe_x,
                          quantity=qty, underlying=underlying)
    costs = c_ce + c_pe
    return Reprice(ok=True, strike=strike, entry_unit=entry_unit, exit_unit=exit_unit,
                   lots=lots, gross=gross, costs=costs, net=gross - costs)


@dataclass
class DayRow:
    underlying: str
    day: str
    range_pct: float
    trades: list = field(default_factory=list)  # list of dict per trade


def _run_session(underlying: str, day: date, *, risk_frac: float, max_lots: int,
                 conv_floor: float | None) -> DayRow | None:
    cfg = _CFG[underlying]
    all_bars = _load_all_bars(underlying, cfg["feed"])
    warm = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() < day]
    session = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() == day]
    if not session:
        return None
    # offline: derive expiry from the cache filename, never touch Kite
    fetcher = HistoricalOptionChainFetcher(adapter=None)
    expiry = fetcher.cached_expiry(underlying, day)
    if expiry is None:
        return None
    cache = _CACHE / f"{underlying}_{day.isoformat()}_{expiry.isoformat()}.jsonl"
    if not cache.exists():
        return None
    chain_by_min = fetcher.load_day(cache)
    mm = _minute_map(chain_by_min)
    sorted_minutes = sorted(mm)

    rep = SurgicalReplay(underlying_symbol=underlying, feed_symbol=cfg["feed"],
                         conviction_floor=conv_floor, capital=400000.0,
                         risk_frac=risk_frac, max_lots=max_lots)
    rep.warm_start(warm, cfg["feed"])
    res = rep.run_day(underlying=underlying, feed_symbol=cfg["feed"], exchange=cfg["exch"],
                      expiry=expiry, lot_size=cfg["lot"], strike_step=cfg["step"], day=day,
                      session_bars=session, chain_by_min=chain_by_min, fetcher=fetcher)

    day_hi = max(b.high_price for b in session)
    day_lo = min(b.low_price for b in session)
    day_open = session[0].open_price
    range_pct = 100.0 * (day_hi - day_lo) / day_open if day_open else 0.0

    close_iso = session[-1].start_timestamp.astimezone(timezone.utc).isoformat()
    row = DayRow(underlying=underlying, day=day.isoformat(), range_pct=range_pct)
    for t in res.trades:
        strad = _reprice_straddle(
            underlying=underlying, lot_size=t.lot_size, dir_lots=t.lots,
            dir_entry_prem=t.entry_prem, entry_iso=t.entry_ts, exit_iso=t.exit_ts,
            mm=mm, sorted_minutes=sorted_minutes)
        strad_eod = _reprice_straddle(
            underlying=underlying, lot_size=t.lot_size, dir_lots=t.lots,
            dir_entry_prem=t.entry_prem, entry_iso=t.entry_ts, exit_iso=close_iso,
            mm=mm, sorted_minutes=sorted_minutes)
        row.trades.append({
            "entry": t.entry_ts[11:16], "exit": t.exit_ts[11:16],
            "dir": t.direction, "or_ext": t.or_ext, "conv": t.conviction,
            "dir_net": t.pnl,
            "strad_ok": strad.ok, "strad_net": strad.net if strad.ok else None,
            "strad_eod_ok": strad_eod.ok,
            "strad_eod_net": strad_eod.net if strad_eod.ok else None,
        })
    return row


# ── reporting ─────────────────────────────────────────────────────────────────
def _agg(trades: list[dict]) -> dict:
    dir_pnl = sum(t["dir_net"] for t in trades)
    s = [t for t in trades if t["strad_ok"]]
    se = [t for t in trades if t["strad_eod_ok"]]
    strad_pnl = sum(t["strad_net"] for t in s)
    eod_pnl = sum(t["strad_eod_net"] for t in se)
    dir_w = sum(1 for t in trades if t["dir_net"] > 0)
    strad_w = sum(1 for t in s if t["strad_net"] > 0)
    return {
        "n": len(trades), "n_strad": len(s),
        "dir_pnl": dir_pnl, "strad_pnl": strad_pnl, "eod_pnl": eod_pnl,
        "dir_wr": 100.0 * dir_w / len(trades) if trades else 0.0,
        "strad_wr": 100.0 * strad_w / len(s) if s else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk-frac", type=float, default=0.08,
                    help="per-trade risk fraction (live armed profile = 0.08)")
    ap.add_argument("--max-lots", type=int, default=4)
    ap.add_argument("--conv-floor", type=float, default=None)
    ap.add_argument("--expansion-pct", type=float, default=None,
                    help="range%% threshold for 'expansion' day; default = median split")
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    rows: list[DayRow] = []
    for underlying in ("NIFTY", "SENSEX"):
        for day in _recorded_days(underlying):
            try:
                row = _run_session(underlying, day, risk_frac=args.risk_frac,
                                   max_lots=args.max_lots, conv_floor=args.conv_floor)
            except Exception as e:  # a single bad day must not sink the study
                print(f"  ! {underlying} {day}: {type(e).__name__}: {e}")
                continue
            if row is None:
                continue
            rows.append(row)
            n = len(row.trades)
            tag = "no trades" if n == 0 else f"{n} trades"
            print(f"  {underlying} {row.day}  range={row.range_pct:4.2f}%  {tag}")

    all_trades = [t for r in rows for t in r.trades]
    if not all_trades:
        print("\nNo trades produced across the corpus — nothing to compare.")
        return 0

    # expansion split by day-level realised range
    ranges = sorted(r.range_pct for r in rows if r.trades)
    thr = args.expansion_pct if args.expansion_pct is not None else (
        ranges[len(ranges) // 2] if ranges else 0.0)
    exp_days = {(r.underlying, r.day) for r in rows if r.range_pct >= thr}
    exp_trades = [t for r in rows for t in r.trades if (r.underlying, r.day) in exp_days]
    comp_trades = [t for r in rows for t in r.trades if (r.underlying, r.day) not in exp_days]

    def _bar(title: str, trades: list[dict]) -> None:
        a = _agg(trades)
        if a["n"] == 0:
            print(f"  {title:22} (no trades)")
            return
        cov = f"{a['n_strad']}/{a['n']}"
        print(f"  {title:22} trades={a['n']:>3}  cov={cov:>6}   "
              f"DIR ₹{a['dir_pnl']:>+9,.0f} ({a['dir_wr']:4.0f}%WR)   "
              f"STRAD ₹{a['strad_pnl']:>+9,.0f} ({a['strad_wr']:4.0f}%WR)   "
              f"STRAD_EOD ₹{a['eod_pnl']:>+9,.0f}")

    print("\n" + "=" * 100)
    print(f"  STRADDLE REFRAME STUDY  —  {len(rows)} sessions, {len(all_trades)} directional entries")
    print(f"  risk_frac={args.risk_frac:.0%}  conv_floor={args.conv_floor}  "
          f"expansion threshold = range ≥ {thr:.2f}%")
    print("=" * 100)
    _bar("ALL", all_trades)
    _bar("EXPANSION days", exp_trades)
    _bar("COMPRESSION days", comp_trades)

    # NIFTY vs SENSEX split (memory: NIFTY is the net loser directionally)
    print("-" * 100)
    for u in ("NIFTY", "SENSEX"):
        _bar(f"{u}", [t for r in rows if r.underlying == u for t in r.trades])

    # per-session ledger
    print("-" * 100)
    print("  per-session:  DIR vs STRAD vs STRAD_EOD (net ₹)")
    for r in sorted(rows, key=lambda x: (x.day, x.underlying)):
        if not r.trades:
            continue
        a = _agg(r.trades)
        exp = "EXP " if (r.underlying, r.day) in exp_days else "comp"
        print(f"    {exp} {r.underlying:6} {r.day}  range={r.range_pct:4.2f}%  n={a['n']:>2}  "
              f"DIR ₹{a['dir_pnl']:>+8,.0f}   STRAD ₹{a['strad_pnl']:>+8,.0f}   "
              f"EOD ₹{a['eod_pnl']:>+8,.0f}")

    print("=" * 100)
    ov = _agg(all_trades)
    verdict = ("STRADDLE carries the book" if ov["strad_pnl"] > ov["dir_pnl"]
               else "DIRECTIONAL still ahead")
    print(f"  VERDICT (same-window): {verdict}  "
          f"(DIR ₹{ov['dir_pnl']:+,.0f}  vs  STRAD ₹{ov['strad_pnl']:+,.0f}  "
          f"vs  STRAD_EOD ₹{ov['eod_pnl']:+,.0f})")
    print("  CAVEAT: STRAD exits at the DIRECTIONAL structure-exit minute; STRAD_EOD holds to close.")
    print("=" * 100)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "threshold_range_pct": thr,
            "sessions": [{"underlying": r.underlying, "day": r.day,
                          "range_pct": r.range_pct, "trades": r.trades} for r in rows],
            "aggregate": {"all": _agg(all_trades), "expansion": _agg(exp_trades),
                          "compression": _agg(comp_trades)},
        }, indent=2))
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
