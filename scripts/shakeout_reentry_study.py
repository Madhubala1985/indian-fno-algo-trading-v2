"""
Shakeout re-entry study — pre-registered rule simulated on REAL recorded premiums.

BACKGROUND (institutional mining 2026-07-14): 10/18 (55%) of losing exits saw
the SAME contract's real premium recover ABOVE OUR ENTRY within 60 minutes of
our exit, and 0/18 saw partial bounces — bimodal: either fully-right-but-
shaken-out, or genuinely wrong. That was an observation; this is the trading
rule simulated against the recorded option chains (data/option_history/*,
one line per option per minute, real Kite LTP).

PRE-REGISTERED RULE (fixed before the first run — do not tune after):
  Trigger : within 30 min of a LOSING exit, the exited contract's real LTP
            prints back at/above our ORIGINAL entry premium.
  Action  : re-enter same contract, same lots, at that trigger LTP.
  Exit    : bar-close stop at −20% from re-entry premium, else 15:20 IST LTP.
  Costs   : 1.5% of re-entry premium notional round-trip (measured hurdle).
PASS BAR : net P&L positive after costs AND positive ex-best-day, n ≥ 8
           triggers. Below n=8 → INSUFFICIENT, no verdict.

Run: PYTHONPATH=. python3 scripts/shakeout_reentry_study.py
"""
from __future__ import annotations

import glob
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
COST_FRAC = 0.015
STOP_FRAC = 0.20
TRIGGER_WINDOW_MIN = 30
EOD_IST = "15:20"


def to_ist(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(IST)


def load_contract_path(symbol: str, day: str) -> list[tuple[datetime, float]]:
    """(ts_ist, ltp) series for one contract on one day, from recorded chains."""
    out = []
    for path in glob.glob(f"data/option_history/*_{day}_*.jsonl"):
        for ln in open(path):
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("tradingsymbol") != symbol:
                continue
            ts = r.get("ts") or ""
            ltp = r.get("ltp")
            if not ts or not isinstance(ltp, (int, float)) or ltp <= 0:
                continue
            t = to_ist(ts)
            if t.strftime("%Y-%m-%d") == day:
                out.append((t, float(ltp)))
    out.sort()
    return out


def main() -> int:
    con = sqlite3.connect("logs/trades.db")
    trades = con.execute(
        "SELECT contract_symbol, underlying, direction, lots, lot_size, "
        "entry_premium, exit_premium, exit_ts, pnl FROM pt_trades "
        "WHERE pnl < 0 AND state != 'VOID' AND exit_ts IS NOT NULL "
        "ORDER BY exit_ts").fetchall()

    results, no_data = [], 0
    for (sym, und, direction, lots, lot_size, entry_p, exit_p, exit_ts, pnl) in trades:
        t_exit = to_ist(exit_ts)
        day = t_exit.strftime("%Y-%m-%d")
        path = load_contract_path(sym, day)
        after = [(t, p) for t, p in path if t > t_exit]
        if not after:
            no_data += 1
            continue
        window_end = t_exit + timedelta(minutes=TRIGGER_WINDOW_MIN)
        trig = next(((t, p) for t, p in after
                     if t <= window_end and p >= entry_p), None)
        if trig is None:
            results.append(dict(sym=sym, day=day, trig=False))
            continue
        t0, p0 = trig
        stop_level = p0 * (1 - STOP_FRAC)
        exit_price, exit_kind = None, None
        for t, p in after:
            if t <= t0:
                continue
            if p <= stop_level:
                exit_price, exit_kind = p, "STOP"
                break
            if t.strftime("%H:%M") >= EOD_IST:
                exit_price, exit_kind = p, "EOD"
                break
        if exit_price is None:
            exit_price, exit_kind = after[-1][1], "LAST"
        qty = lots * lot_size
        gross = (exit_price - p0) * qty
        cost = COST_FRAC * p0 * qty
        results.append(dict(sym=sym, day=day, trig=True, t0=t0.strftime("%H:%M"),
                            p0=p0, exit=exit_price, kind=exit_kind,
                            net=gross - cost, orig_pnl=pnl))

    trigs = [r for r in results if r.get("trig")]
    print("=" * 84)
    print("  SHAKEOUT RE-ENTRY STUDY (pre-registered rule; real recorded premiums)")
    print("=" * 84)
    print(f"losing exits considered: {len(trades)}  |  with post-exit chain data: "
          f"{len(results)}  |  no-data skipped: {no_data}")
    print(f"triggered re-entries: {len(trigs)} / {len(results)}")
    for r in trigs:
        print(f"  {r['day']} {r['sym']:24} reenter@{r['p0']:8.2f} {r['t0']}  "
              f"exit@{r['exit']:8.2f} ({r['kind']})  net={r['net']:+9.0f}  "
              f"(orig loss {r['orig_pnl']:+.0f})")
    if len(trigs) < 8:
        print(f"\nVERDICT: INSUFFICIENT (n={len(trigs)} < 8) — keep accruing; "
              f"no promotion either way.")
        return 0
    net = sum(r["net"] for r in trigs)
    by_day: dict[str, float] = {}
    for r in trigs:
        by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["net"]
    best_day = max(by_day, key=by_day.get)
    ex_best = net - by_day[best_day]
    wins = sum(1 for r in trigs if r["net"] > 0)
    print(f"\nnet={net:+,.0f}  wins={wins}/{len(trigs)}  "
          f"ex-best-day({best_day})={ex_best:+,.0f}")
    verdict = "PASS" if (net > 0 and ex_best > 0) else "FAIL"
    print(f"VERDICT: {verdict} (bar: net>0 AND ex-best-day>0, n≥8)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
