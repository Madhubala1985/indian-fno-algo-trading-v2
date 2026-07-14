#!/usr/bin/env python3
"""
straddle_regime_router.py — (b) can the LIVE RegimeEngine route the straddle sleeve?

The opening-range router failed because the days that matter (07-08) expanded in the
AFTERNOON after a quiet morning. But a straddle need not be predicted at 09:15 — the
RegimeEngine is a COINCIDENT spike detector (bar_range > 1.5x avg), so it flips to
HIGH_VOLATILITY/EXPANSION on the bar the move starts. This tests: enter an ATM straddle
the FIRST time regime flips to EXPANSION inside the tradeable window, hold to close.
Uses the REAL live-emitted regime from logs/regime_shadow.jsonl (ground truth, causal).

Per-lot P&L is reported (sizing-free, apples-to-apples). The directional day P&L (from
the straddle_study JSON) is overlaid for context, plus the trigger time so we can see
WHEN the sleeve would have entered relative to the move.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from straddle_study import (_CFG, _CACHE, _load_all_bars, _minute_map,
                            _nearest_minute, _atm_strike, _recorded_days)
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
from platform_core.execution.transaction_costs import apply_costs

_IST = timezone(timedelta(hours=5, minutes=30))


def _load_regime():
    """{(underlying, dayiso): [(ts_iso, vol_regime, dir_regime), ...] sorted by ts}."""
    out: dict[tuple, list] = {}
    p = Path("logs/regime_shadow.jsonl")
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        key = (d["underlying"], d["ts"][:10])
        out.setdefault(key, []).append((d["ts"], d.get("vol_regime"), d.get("dir_regime")))
    for k in out:
        out[k].sort()
    return out


def _ist_hm(ts_iso: str) -> str:
    return datetime.fromisoformat(ts_iso).astimezone(_IST).strftime("%H:%M")


def _in_window(ts_iso: str, start_h=9, start_m=20, cutoff_h=14, cutoff_m=30) -> bool:
    t = datetime.fromisoformat(ts_iso).astimezone(_IST)
    after_start = (t.hour, t.minute) >= (start_h, start_m)
    before_cut = (t.hour, t.minute) <= (cutoff_h, cutoff_m)
    return after_start and before_cut


def _straddle_pnl_per_lot(*, underlying, lot_size, entry_iso, exit_iso, mm, minutes):
    em = _nearest_minute(mm, entry_iso, minutes)
    if em is None:
        return None
    legs_e = mm[em]
    strike = _atm_strike(legs_e)
    if strike is None:
        return None
    ce_e, pe_e = legs_e[(strike, "CE")], legs_e[(strike, "PE")]
    xm = _nearest_minute(mm, exit_iso, minutes)
    if xm is None:
        return None
    legs_x = mm[xm]
    ce_x, pe_x = legs_x.get((strike, "CE")), legs_x.get((strike, "PE"))
    if ce_x is None or pe_x is None:
        return None
    qty = lot_size
    gross = qty * ((ce_x + pe_x) - (ce_e + pe_e))
    _, c1 = apply_costs(0.0, entry_premium=ce_e, exit_premium=ce_x, quantity=qty, underlying=underlying)
    _, c2 = apply_costs(0.0, entry_premium=pe_e, exit_premium=pe_x, quantity=qty, underlying=underlying)
    return {"strike": strike, "entry_unit": ce_e + pe_e, "exit_unit": ce_x + pe_x,
            "net_per_lot": gross - (c1 + c2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--study-json", default="scratchpad_straddle.json")
    ap.add_argument("--exit", choices=["close", "regime_off"], default="close",
                    help="close=hold to session close; regime_off=exit after 2 non-HIGH bars")
    args = ap.parse_args()

    regime = _load_regime()
    study = json.loads(Path(args.study_json).read_text()) if Path(args.study_json).exists() else {"sessions": []}
    dir_day = {(s["underlying"], s["day"]): (sum(t["dir_net"] for t in s["trades"]), s["range_pct"], len(s["trades"]))
               for s in study["sessions"]}
    thr = study.get("threshold_range_pct", 0.75)

    print(f"REGIME-ROUTED STRADDLE (enter on first HIGH_VOLATILITY flip 09:20-14:30, exit={args.exit})")
    print(f"regime source: logs/regime_shadow.jsonl (real live-emitted)   expansion label: full-day range >= {thr:.2f}%")
    print(f"{'day':>18} {'lbl':>4} {'range%':>6} {'trig':>6} {'entry@':>6}->{'exit':<6} "
          f"{'strad/lot':>10}  {'DIR(day)':>9}")

    rows = []
    for underlying in ("NIFTY", "SENSEX"):
        cfg = _CFG[underlying]
        fetcher = HistoricalOptionChainFetcher(adapter=None)
        for day in _recorded_days(underlying):
            key = (underlying, day.isoformat())
            reg = regime.get(key)
            if not reg:
                continue
            expiry = fetcher.cached_expiry(underlying, day)
            if expiry is None:
                continue
            cache = _CACHE / f"{underlying}_{day.isoformat()}_{expiry.isoformat()}.jsonl"
            if not cache.exists():
                continue
            mm = _minute_map(fetcher.load_day(cache))
            minutes = sorted(mm)
            if not minutes:
                continue
            # first EXPANSION/HIGH_VOL flip inside window
            trig = None
            for i, (ts, vol, dirr) in enumerate(reg):
                if vol == "HIGH_VOLATILITY" and _in_window(ts):
                    trig = (i, ts)
                    break
            dpnl, rpct, ntr = dir_day.get(key, (0.0, 0.0, 0))
            lbl = "EXP" if rpct >= thr else "comp"
            if trig is None:
                rows.append((underlying, day.isoformat(), lbl, rpct, None, None, dpnl))
                print(f"{underlying+' '+day.isoformat():>18} {lbl:>4} {rpct:6.2f} {'--':>6} "
                      f"{'--':>6}  {'--':<6} {'(no trigger)':>10}  {dpnl:>+9,.0f}")
                continue
            ti, tts = trig
            # exit minute
            if args.exit == "close":
                exit_iso = minutes[-1]
            else:
                # first bar after trigger with 2 consecutive non-HIGH regimes
                exit_iso = minutes[-1]
                streak = 0
                for (ts, vol, dirr) in reg[ti + 1:]:
                    streak = streak + 1 if vol != "HIGH_VOLATILITY" else 0
                    if streak >= 2:
                        exit_iso = ts
                        break
            res = _straddle_pnl_per_lot(underlying=underlying, lot_size=cfg["lot"],
                                        entry_iso=tts, exit_iso=exit_iso, mm=mm, minutes=minutes)
            per_lot = res["net_per_lot"] if res else None
            rows.append((underlying, day.isoformat(), lbl, rpct, tts, per_lot, dpnl))
            print(f"{underlying+' '+day.isoformat():>18} {lbl:>4} {rpct:6.2f} {_ist_hm(tts):>6} "
                  f"{_ist_hm(tts):>6}->{_ist_hm(exit_iso):<6} "
                  f"{(f'{per_lot:+,.0f}' if per_lot is not None else 'n/a'):>10}  {dpnl:>+9,.0f}")

    # aggregate per-lot by day type + the key question: did it catch 07-08?
    print("\n-- per-lot straddle P&L by day type (regime-triggered) --")
    for lbl in ("EXP", "comp"):
        sub = [r for r in rows if r[2] == lbl and r[5] is not None]
        tot = sum(r[5] for r in sub)
        wins = sum(1 for r in sub if r[5] > 0)
        print(f"  {lbl:>4}: {len(sub):>2} triggered days   sum/lot ₹{tot:>+9,.0f}   "
              f"{wins}/{len(sub)} green")
    notrig = [r for r in rows if r[4] is None]
    print(f"  no-trigger days: {len(notrig)}  "
          f"({sum(1 for r in notrig if r[2]=='EXP')} were EXP — MISSED by regime)")
    print("\n-- the 07-08 test (afternoon expansion that opening-range missed) --")
    for r in rows:
        if r[1] == "2026-07-08":
            print(f"  {r[0]} 07-08: trigger={_ist_hm(r[4]) if r[4] else 'NONE'}  "
                  f"straddle/lot ₹{r[5]:+,.0f}" if r[5] is not None else f"  {r[0]} 07-08: no trigger")


if __name__ == "__main__":
    raise SystemExit(main())
