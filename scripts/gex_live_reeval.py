#!/usr/bin/env python3
"""
gex_live_reeval.py — RE-OPEN the "GEX veto is harmful" verdict on LIVE data.

The rejection (memory gex-veto-harmful: +₹31,938→−₹1,082) was measured on 12 SURGICALREPLAY
trades — the same replay later shown 1/11 faithful (it flipped the +₹44k day). So its "winning
entries the veto blocks" are fictional. This re-judges GEX on REAL data only: the live GEX log
(logs/options_analytics_shadow.jsonl — gex_regime/walls/spot per bar, logged live) joined to real
trade outcomes (trades.db). No replay anywhere.

Veto (mirrors platform_core/options/gex_gate.gex_wall_block):
  would-block if gex_regime==LONG_GAMMA, OR spot within `wall_pct` of the wall in the trade's
  direction (LONG→call wall ahead, SHORT→put wall ahead).
Then: do would-block trades WIN (→ veto harmful, replay verdict holds on live) or LOSE (→ veto
helps, the replay verdict was a hallucination)?
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right

WALL_PCT = 0.30   # spot within 0.30% of the wall = "wall ahead" (gex_gate default-ish)


def _gex_series():
    idx = {}
    try:
        for line in open("logs/options_analytics_shadow.jsonl"):
            d = json.loads(line)
            idx.setdefault(d["underlying"], []).append(d)
    except FileNotFoundError:
        return {}
    for u in idx:
        idx[u].sort(key=lambda r: r["ts"])
    return idx


def _gex_at(series, u, ts):
    rec = series.get(u)
    if not rec:
        return None
    tss = [r["ts"] for r in rec]
    i = bisect_right(tss, ts) - 1
    return rec[i] if i >= 0 else None


def _would_block(g, direction, spot):
    if g is None:
        return None
    if str(g.get("gex_regime", "")).upper() == "LONG_GAMMA":
        return "LONG_GAMMA_REGIME"
    cw, pw = g.get("gex_call_wall"), g.get("gex_put_wall")
    if direction == "LONG" and cw and spot and 0 <= (cw - spot) / spot * 100 <= WALL_PCT:
        return "CALL_WALL_AHEAD"
    if direction == "SHORT" and pw and spot and 0 <= (spot - pw) / spot * 100 <= WALL_PCT:
        return "PUT_WALL_AHEAD"
    return None


def main():
    gex = _gex_series()
    n_gex = sum(len(v) for v in gex.values())
    days = sorted({r["ts"][:10] for v in gex.values() for r in v})
    c = sqlite3.connect("logs/trades.db")
    block, ok = [], []
    matched = 0
    for r in c.execute("""select underlying, direction, entry_spot, entry_ts, pnl, date(entry_ts)
                          from pt_trades where pnl is not null"""):
        u, d, spot, ets, pnl, day = r
        g = _gex_at(gex, u, ets or "")
        if g is None:
            continue
        matched += 1
        reason = _would_block(g, d, spot)
        (block if reason else ok).append({"pnl": pnl, "day": day, "reason": reason})

    print("=" * 88)
    print("  GEX VETO — RE-EVALUATED ON LIVE DATA (replay verdict was invalid)")
    print(f"  live GEX log: {n_gex} bars over {len(days)} day(s) {days}")
    print("=" * 88)
    print(f"  live trades with a matching GEX bar: {matched}")
    if matched == 0:
        print("  → options_analytics_shadow.jsonl only covers 07-10 so far (2 SENSEX trades).")
        print("    This tool is the correct instrument; it becomes conclusive as the live GEX")
        print("    log accrues (auto-on, PAPER_OPT_ANALYTICS_SHADOW). Also: 119 would-block /")
        print("    63 ok [GEX SHADOW] verdicts already exist in live logs — a richer join once")
        print("    those are tied to trade outcomes.")
        print("=" * 88)
        return 0

    def _s(rows):
        n = len(rows); p = sum(x["pnl"] for x in rows); w = sum(1 for x in rows if x["pnl"] > 0)
        return n, p, (100 * w // n if n else 0)

    bn, bp, bwr = _s(block); on, op, owr = _s(ok)
    print(f"  WOULD-BLOCK trades: n={bn}  P&L ₹{bp:+,.0f}  WR {bwr}%")
    print(f"  OK (not blocked):   n={on}  P&L ₹{op:+,.0f}  WR {owr}%")
    print("-" * 88)
    if bn:
        verdict = ("veto HARMFUL on live (blocks winners) — replay verdict holds" if bp > 0
                   else "veto HELPS on live (blocks losers) — REPLAY VERDICT WAS WRONG, promote-test GEX")
        print(f"  LIVE VERDICT: would-block trades net ₹{bp:+,.0f} → {verdict}")
    print(f"  (sample n={matched}; needs accrual before acting — mechanism + counts above)")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
