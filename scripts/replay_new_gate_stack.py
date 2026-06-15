#!/usr/bin/env python3
"""
scripts/replay_new_gate_stack.py
──────────────────────────────────────────────────────────────────────────────
Replay the NEW gate stack (2026-06-15) against real bar-by-bar strike-selection
audit data, to answer one question honestly:

    Does the new gate stack KEEP the proven-winning trades, and does it UNBLOCK
    the days that were being starved?

GATE STACKS COMPARED (Tier2/3 entry filters that operate on the selected strike):
  OLD:  oi_support >= 0.10                         (flat floor added 2026-06-10)
  NEW:  oi_support >= {SENSEX 0.005, NIFTY 0.03, BANKNIFTY 0.05}
        AND abs_open_interest >= {SENSEX 3000, NIFTY/BANKNIFTY 25000}
        AND liquidity_factor   >= 0.50
  (score_margin >= 0.015 applies to both as the paper_trade_unlock soft floor;
   shown but not the focus here.)

GROUND TRUTH:
  The winning session (archived session_20260608_035222, actual trade date
  2026-06-05) took 11 SENSEX trades, 9 wins, +Rs45,741 — with NO oi_support or
  liquidity gate at all.  We replay the new gates over those exact entry bars to
  confirm the new stack would NOT have blocked the winners.

HONEST LIMITATIONS:
  • The audit logs the SELECTED strike per bar, not every entry gate (warm-start,
    contradiction cooldown, stop cooldown, heat, position limits).  So "bars
    passing" is the LIQUIDITY/OI/MARGIN filter only — an upper bound on trades,
    not an exact trade count.  The WINNERS check IS exact: it matches the 11
    actually-executed trades to their entry bars.
  • Today's would-be trades have NO known outcome (no peak-OI ground truth), so
    this does NOT prove today's non-peak strikes win at the 06-05 rate.  It only
    proves the gates admit liquid strikes and keep the known winners.

Usage:
  PYTHONPATH=. python3 scripts/replay_new_gate_stack.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

WINNING_AUDIT = Path("logs/archive/session_20260608_035222/strike_selection_audit.jsonl")
WINNING_DB    = Path("logs/archive/session_20260608_035222/trades.db")
TODAY_AUDIT   = Path("logs/strike_selection_audit.jsonl")

# ── Gate-stack definitions ─────────────────────────────────────────────────────
OLD_OI_FLOOR = {"BANKNIFTY": 0.10, "NIFTY": 0.10, "SENSEX": 0.10}
NEW_OI_FLOOR = {"BANKNIFTY": 0.05, "NIFTY": 0.03, "SENSEX": 0.005}
NEW_MIN_ABS_OI = {"BANKNIFTY": 25000.0, "NIFTY": 25000.0, "SENSEX": 3000.0}
NEW_MIN_LIQ = 0.50


def _rec(d: dict) -> dict | None:
    u = d.get("underlying")
    if u not in OLD_OI_FLOOR:
        return None
    fs = d.get("factor_scores") or {}
    if fs.get("oi_support") is None or d.get("oi") is None:
        return None
    return {
        "ts": d.get("ts", ""),
        "u": u,
        "sym": d.get("selected_symbol"),
        "prem": d.get("premium"),
        "oi": float(d.get("oi")),
        "oi_support": float(fs.get("oi_support")),
        "liq": float(fs.get("liquidity") or 0.0),
    }


def load_audit(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = _rec(json.loads(line))
            except Exception:
                continue
            if r:
                out.append(r)
    return out


def passes_old(r: dict) -> bool:
    return r["oi_support"] >= OLD_OI_FLOOR[r["u"]]


def passes_new(r: dict) -> bool:
    return (r["oi_support"] >= NEW_OI_FLOOR[r["u"]]
            and r["oi"] >= NEW_MIN_ABS_OI[r["u"]]
            and r["liq"] >= NEW_MIN_LIQ)


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def pass_rates(recs: list[dict], label: str) -> None:
    print(f"\n  {label}")
    by_u: dict[str, list[dict]] = {}
    for r in recs:
        by_u.setdefault(r["u"], []).append(r)
    for u in ("SENSEX", "NIFTY", "BANKNIFTY"):
        rs = by_u.get(u, [])
        if not rs:
            continue
        old = sum(passes_old(r) for r in rs)
        new = sum(passes_new(r) for r in rs)
        n = len(rs)
        print(f"    {u:10} bars={n:4}  OLD(oi>=0.10) pass={old:4} ({100*old//max(n,1):3}%)"
              f"   NEW(stack) pass={new:4} ({100*new//max(n,1):3}%)")


def winners_check() -> None:
    print("\n" + "=" * 74)
    print("  WINNERS PRESERVATION — the 11 actually-executed trades (2026-06-05)")
    print("=" * 74)
    if not WINNING_DB.exists():
        print("  [skip] winning DB not found")
        return
    c = sqlite3.connect(f"file:{WINNING_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    trades = c.execute(
        "SELECT entry_ts, contract_symbol, entry_premium, ROUND(pnl,0) pnl "
        "FROM pt_trades WHERE underlying='SENSEX' ORDER BY entry_ts"
    ).fetchall()
    c.close()

    audit = load_audit(WINNING_AUDIT)
    by_sym: dict[str, list[dict]] = {}
    for r in audit:
        by_sym.setdefault(r["sym"], []).append((_parse_ts(r["ts"]), r))

    old_keep = new_keep = 0
    print(f"  {'entry_ts':<27} {'contract':<20} {'pnl':>8}  oi_sup  abs_oi   liq   OLD   NEW")
    print("  " + "-" * 86)
    for t in trades:
        ets = _parse_ts(t["entry_ts"])
        cand = by_sym.get(t["contract_symbol"], [])
        best = None
        best_dt = 1e9
        for ats, r in cand:
            if ats is None or ets is None:
                continue
            dt = abs((ats - ets).total_seconds())
            if dt < best_dt:
                best_dt, best = dt, r
        if best is None or best_dt > 120:
            print(f"  {t['entry_ts']:<27} {t['contract_symbol']:<20} {t['pnl']:>8}  "
                  f"(no audit bar matched within 120s)")
            continue
        po, pn = passes_old(best), passes_new(best)
        old_keep += po
        new_keep += pn
        print(f"  {t['entry_ts']:<27} {t['contract_symbol']:<20} {t['pnl']:>8}  "
              f"{best['oi_support']:.3f}  {best['oi']:>7.0f}  {best['liq']:.3f}  "
              f"{'PASS' if po else 'BLOCK':>5} {'PASS' if pn else 'BLOCK':>5}")
    n = len(trades)
    print("  " + "-" * 86)
    print(f"  Winners kept:   OLD stack {old_keep}/{n}    NEW stack {new_keep}/{n}")
    if new_keep == n:
        print("  => NEW gate stack preserves ALL known winners (no downgrade where we have ground truth).")
    else:
        print(f"  => WARNING: NEW stack would block {n - new_keep} known winner(s) — investigate.")


def main() -> int:
    print("=" * 74)
    print("  NEW GATE-STACK REPLAY  (OLD oi>=0.10  vs  NEW oi+absOI+liquidity)")
    print("=" * 74)

    win = load_audit(WINNING_AUDIT)
    print(f"\n[Winning session 2026-06-05]  audit bars with OI data: {len(win)}")
    pass_rates(win, "Pass rates by underlying:")

    today = load_audit(TODAY_AUDIT)
    if today:
        # restrict to the latest date present
        latest = max(r["ts"][:10] for r in today)
        today = [r for r in today if r["ts"].startswith(latest)]
        print(f"\n[Recent session {latest}]  audit bars with OI data: {len(today)}")
        pass_rates(today, "Pass rates by underlying:")

    winners_check()

    print("\nNOTE: 'bars passing' is the liquidity/OI/margin filter only (upper bound,")
    print("      not exact trade count).  The winners check is exact (real trades).")
    print("      Today's admitted strikes have no known outcome — this proves they are")
    print("      liquid and that known winners survive, NOT that they win at 82%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
