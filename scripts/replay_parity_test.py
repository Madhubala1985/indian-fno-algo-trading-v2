#!/usr/bin/env python3
"""
replay_parity_test.py — §2.1 acceptance test: is SurgicalReplay FAITHFUL to live?

The straddle study used SurgicalReplay as the "directional baseline". Its faithfulness
was ASSERTED, never measured. This compares, per overlapping day, the REAL live trades
(logs/trades.db pt_trades) against what SurgicalReplay produces on the same recorded
chains — trade count, direction, contract, and net P&L — and prints a parity verdict.

CAVEAT (stated, not hidden): live trades were taken by whatever code was live THAT day;
SurgicalReplay runs TODAY's gate stack on the same data. So divergence conflates (a) replay
infidelity with (b) deliberate gate evolution since. Sign flips / missing monster days are
too large to be cost-model drift and flag a real problem either way.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timezone
from pathlib import Path

from straddle_study import (_CFG, _CACHE, _load_all_bars, _recorded_days)
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
from platform_core.backtesting.surgical_replay import SurgicalReplay


def _live_trades():
    c = sqlite3.connect("logs/trades.db")
    out: dict[tuple, list] = {}
    for r in c.execute("""select date(entry_ts), underlying, contract_symbol, direction,
                                 lots, pnl, entry_ts, exit_ts, exit_reason
                          from pt_trades order by entry_ts"""):
        out.setdefault((r[1], r[0]), []).append({
            "contract": r[2], "dir": r[3], "lots": r[4], "pnl": r[5],
            "entry": (r[6] or "")[11:16], "exit": (r[7] or "")[11:16], "reason": r[8]})
    return out


def _replay_day(underlying, day):
    cfg = _CFG[underlying]
    bars = _load_all_bars(underlying, cfg["feed"])
    warm = [b for b in bars if b.start_timestamp.astimezone(timezone.utc).date() < day]
    session = [b for b in bars if b.start_timestamp.astimezone(timezone.utc).date() == day]
    if not session:
        return None
    fetcher = HistoricalOptionChainFetcher(adapter=None)
    expiry = fetcher.cached_expiry(underlying, day)
    if expiry is None:
        return None
    cache = _CACHE / f"{underlying}_{day.isoformat()}_{expiry.isoformat()}.jsonl"
    if not cache.exists():
        return None
    chain = fetcher.load_day(cache)
    rep = SurgicalReplay(underlying_symbol=underlying, feed_symbol=cfg["feed"],
                         capital=400000.0, risk_frac=0.08, max_lots=4)
    rep.warm_start(warm, cfg["feed"])
    res = rep.run_day(underlying=underlying, feed_symbol=cfg["feed"], exchange=cfg["exch"],
                      expiry=expiry, lot_size=cfg["lot"], strike_step=cfg["step"], day=day,
                      session_bars=session, chain_by_min=chain, fetcher=fetcher)
    return [{"contract": t.contract, "dir": t.direction, "lots": t.lots, "pnl": t.pnl,
             "entry": t.entry_ts[11:16], "exit": t.exit_ts[11:16], "reason": t.reason}
            for t in res.trades]


def main():
    live = _live_trades()
    days = set()
    for u in ("NIFTY", "SENSEX"):
        for d in _recorded_days(u):
            if (u, d.isoformat()) in live:
                days.add((u, d))
    days = sorted(days, key=lambda x: (x[1], x[0]))

    print("§2.1 REPLAY-PARITY ACCEPTANCE TEST — live (trades.db) vs SurgicalReplay@today\n")
    print(f"{'day':>17}  {'LIVE n/pnl':>16}   {'REPLAY n/pnl':>16}   {'dir match':>10}  verdict")
    agg_live = agg_rep = 0.0
    n_match = n_days = 0
    details = []
    for u, d in days:
        lv = live.get((u, d.isoformat()), [])
        rp = _replay_day(u, d) or []
        lv_pnl, rp_pnl = sum(t["pnl"] for t in lv), sum(t["pnl"] for t in rp)
        agg_live += lv_pnl
        agg_rep += rp_pnl
        n_days += 1
        # crude direction-set match
        lv_dirs = sorted(t["dir"] for t in lv)
        rp_dirs = sorted(t["dir"] for t in rp)
        same_n = len(lv) == len(rp)
        same_sign = (lv_pnl > 0) == (rp_pnl > 0)
        dirs_ok = lv_dirs == rp_dirs
        ok = same_n and dirs_ok and same_sign
        if ok:
            n_match += 1
            verdict = "MATCH"
        elif same_sign and same_n:
            verdict = "~close"
        elif not same_sign:
            verdict = "SIGN FLIP"
        else:
            verdict = "MISMATCH"
        print(f"{u+' '+d.isoformat():>17}  {len(lv):>2}/₹{lv_pnl:>+9,.0f}   "
              f"{len(rp):>2}/₹{rp_pnl:>+9,.0f}   {str(dirs_ok):>10}  {verdict}")
        details.append((u, d.isoformat(), lv, rp, verdict))

    print("-" * 78)
    print(f"{'AGGREGATE':>17}  {'':>2} ₹{agg_live:>+9,.0f}   {'':>2} ₹{agg_rep:>+9,.0f}   "
          f"exact-match days: {n_match}/{n_days}")
    print(f"\nPARITY SCORE: {n_match}/{n_days} days match on (count ∧ directions ∧ P&L sign).")
    print(f"P&L divergence: live ₹{agg_live:+,.0f} vs replay ₹{agg_rep:+,.0f} "
          f"= ₹{agg_rep-agg_live:+,.0f} ({'replay optimistic' if agg_rep>agg_live else 'replay pessimistic'})")

    # persist a machine-readable ledger so this is a STANDING check, not a one-off:
    # any replay-based A/B should first confirm its days are FAITHFUL here.
    import json
    ledger = {
        "parity_score": f"{n_match}/{n_days}",
        "live_total": agg_live, "replay_total": agg_rep,
        "days": [{"underlying": u, "day": d, "verdict": v,
                  "live_n": len(lv), "replay_n": len(rp),
                  "live_pnl": sum(t["pnl"] for t in lv),
                  "replay_pnl": sum(t["pnl"] for t in rp),
                  "live_dirs": sorted(t["dir"] for t in lv),
                  "replay_dirs": sorted(t["dir"] for t in rp),
                  "faithful": v == "MATCH"}
                 for (u, d, lv, rp, v) in details],
    }
    Path("validation_reports").mkdir(exist_ok=True)
    Path("validation_reports/replay_parity_ledger.json").write_text(json.dumps(ledger, indent=2))
    print("\n  wrote validation_reports/replay_parity_ledger.json (standing faithfulness ledger)")

    # 06-29 forensic (the monster day)
    print("\n-- 06-29 SENSEX forensic (memory's +44k monster) --")
    for u, d, lv, rp, v in details:
        if d == "2026-06-29" and u == "SENSEX":
            print(f"  LIVE ({len(lv)} trades):")
            for t in lv:
                print(f"    {t['entry']}->{t['exit']} {t['contract']:22} {t['dir']:5} "
                      f"lots={t['lots']} ₹{t['pnl']:+,.0f}  {t['reason']}")
            print(f"  REPLAY ({len(rp)} trades):")
            for t in rp:
                print(f"    {t['entry']}->{t['exit']} {t['contract']:22} {t['dir']:5} "
                      f"lots={t['lots']} ₹{t['pnl']:+,.0f}  {t['reason']}")


if __name__ == "__main__":
    raise SystemExit(main())
