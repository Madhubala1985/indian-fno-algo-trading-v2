#!/usr/bin/env python3
"""
orderflow_scorecard.py — batch-4 §4.1 pre-registered test. Joins logs/orderflow_shadow.jsonl
(per-bar CVD/depth) to pt_structure_events (BOS/CHOCH with side) in trades.db.

Hypothesis: a CHOCH/BOS arriving WITH same-side flow imbalance is REAL; without it, a trap.
  bullish break + CVD>0 (or depth_imbalance>0) = CONFIRMED ; else UNCONFIRMED.
Then (when trades exist near confirmed vs unconfirmed breaks) compares outcomes. Ready now;
prints a readiness notice until ORDERFLOW_SHADOW=1 has accrued a corpus.

NOTE (2026-07-14 night-explorer fix): the shadow was re-pointed from the spot-index feed
(symbol="NIFTY50"/"BANKNIFTY"/"SENSEX", which Kite never carries real depth/volume for —
source stayed "kite_ws_no_depth", cvd trivially 0 every bar) to the FRONT-MONTH FUTURES
contract (symbol="NIFTY_FUT"/"BANKNIFTY_FUT"/"SENSEX_FUT", real traded volume+depth,
source="kite_ws") on 2026-07-14. pt_structure_events.instrument_id is still the spot name.
Joining on the literal symbol therefore matched ONLY the pre-07-14 no-depth rows (100%
UNCONFIRMED by construction — cvd is always 0 there) and silently dropped every real-depth
row from 07-14 onward (0 matches). Map spot -> futures symbol so the real data is used.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from pathlib import Path

_SPOT_TO_FUT = {"NIFTY50": "NIFTY_FUT", "BANKNIFTY": "BANKNIFTY_FUT", "SENSEX": "SENSEX_FUT"}


def _flow_index():
    idx = {}
    p = Path("logs/orderflow_shadow.jsonl")
    if not p.exists():
        return idx
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        idx.setdefault(d["symbol"], []).append(d)
    for s in idx:
        idx[s].sort(key=lambda r: r["ts"])
    return idx


def _flow_at(idx, symbol, ts):
    rec = idx.get(symbol)
    if not rec:
        return None
    tss = [r["ts"] for r in rec]
    i = bisect_left(tss, ts)
    for j in (i, i - 1):                      # exact bar or the one just before
        if 0 <= j < len(rec) and rec[j]["ts"][:16] == ts[:16]:   # same minute
            return rec[j]
    return None


def main():
    flow = _flow_index()
    n_flow = sum(len(v) for v in flow.values())
    print("=" * 88)
    print(f"  §4.1 ORDER-FLOW SCORECARD — {n_flow} flow bars logged")
    print("=" * 88)
    if n_flow == 0:
        print("  logs/orderflow_shadow.jsonl empty. The shadow is wired but DEFAULT OFF.")
        print("  To accrue: set ORDERFLOW_SHADOW=1 (after confirming the live tick carries")
        print("  volume+depth / MODE_FULL), run sessions, then re-run this scorecard.")
        print("=" * 88)
        return 0

    c = sqlite3.connect("logs/trades.db")
    conf = unconf = 0
    by_source = {}
    rows = c.execute("""select instrument_id, bar_ts, event_type, side
                        from pt_structure_events
                        where event_type in ('BOS','CHOCH') order by bar_ts""").fetchall()
    for inst, ts, et, side in rows:
        f = _flow_at(flow, _SPOT_TO_FUT.get(inst, inst), ts or "")
        if not f:
            f = _flow_at(flow, inst, ts or "")   # fall back to legacy no-depth spot rows
        if not f:
            continue
        cvd = f.get("cvd") or 0
        imb = f.get("depth_imbalance")
        flow_bull = (cvd > 0) or (imb is not None and imb > 0)
        flow_bear = (cvd < 0) or (imb is not None and imb < 0)
        want_bull = str(side).lower() == "bullish"
        confirmed = (want_bull and flow_bull) or ((not want_bull) and flow_bear)
        conf += confirmed
        unconf += (not confirmed)
        src = f.get("source", "?")
        sc, su = by_source.get(src, (0, 0))
        by_source[src] = (sc + confirmed, su + (not confirmed))
    tot = conf + unconf
    print(f"  BOS/CHOCH with matching flow bar: {tot}")
    if tot:
        print(f"  CONFIRMED (same-side flow):   {conf} ({100*conf//tot}%)")
        print(f"  UNCONFIRMED (no/opp flow):    {unconf} ({100*unconf//tot}%)")
        print("  by source:")
        for src, (sc, su) in sorted(by_source.items()):
            st = sc + su
            print(f"    {src:20s} n={st:4d}  confirmed={sc} ({100*sc//st if st else 0}%)"
                  f"  (kite_ws_no_depth rows are FLAT/cvd=0 by construction — not a real test)")
    print("  NEXT: once trades accumulate near confirmed vs unconfirmed breaks, compare their")
    print("  P&L/WR here — that is the promotion test for a flow-confirmation entry gate.")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
