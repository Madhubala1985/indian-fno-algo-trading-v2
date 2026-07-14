#!/usr/bin/env python3
"""
crossasset_scorecard.py — the PROMOTION DECISION tool for the §4.2 cross-asset signal.

Question: is the pre-open cross-asset bias (crossasset_shadow.jsonl `crossasset_bias`, from
GIFT/US500/Brent) a BETTER directional input for the open-drive window than the model's own
bias — ESPECIALLY on the days the model loses (wrong-way longs)?

Joins three sources, all real (no replay):
  * logs/crossasset_shadow.jsonl  — pre_open crossasset_bias per (underlying, session_date)
  * logs/trades.db pt_trades      — what the model actually did (direction) + P&L (the truth)
  * index CSV bars                — realized 09:15–09:35 IST open-drive direction

Outputs the metrics that decide promotion:
  1. Directional accuracy: crossasset_bias vs realized  vs  model direction vs realized.
  2. VETO VALUE: P&L of LONG trades the cross-asset bias CONTRADICTED (would block/flip) —
     split into losers-saved vs winners-wrongly-killed. Net > 0 and driven by the loser bucket
     = the edge that turns the −₹36k long bleed into avoided losses.
  3. Confusion: model-direction × crossasset-bias, avg P&L per cell (the money cell is
     model=LONG ∧ xa=SHORT).
Promotion rule (printed): ≥~15 sessions of data AND cross-asset out-calls the model on the
disagreement days AND the veto's net P&L is positive and loser-driven → promote to a live
veto/override on longs that contradict the pre-open cross-asset state.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import timezone
from pathlib import Path

from straddle_study import _CFG, _load_all_bars

_PROMOTE_MIN_SESSIONS = 15


def _crossasset_preopen():
    """{(underlying, day): record} — last pre_open per session."""
    out: dict[tuple, dict] = {}
    p = Path("logs/crossasset_shadow.jsonl")
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("kind") != "pre_open":
            continue
        day = (d.get("session_date") or d.get("logged_at", "")[:10])
        if d.get("underlying") and day:
            out[(d["underlying"], day)] = d
    return out


def _trades_by_session():
    c = sqlite3.connect("logs/trades.db")
    out = defaultdict(list)
    for r in c.execute("select underlying, date(entry_ts), direction, pnl from pt_trades"):
        out[(r[0], r[1])].append({"dir": r[2], "pnl": r[3] or 0.0})
    return out


def _open_drive_dir(underlying: str) -> dict:
    """{day: 'LONG'/'SHORT'/'FLAT'} from the 09:15–09:35 IST (03:45–04:05 UTC) index move."""
    cfg = _CFG.get(underlying)
    if not cfg:
        return {}
    bars = _load_all_bars(underlying, cfg["feed"])
    byday: dict[str, list] = defaultdict(list)
    for b in bars:
        t = b.start_timestamp.astimezone(timezone.utc)
        if (t.hour, t.minute) >= (3, 45) and (t.hour, t.minute) <= (4, 5):
            byday[t.date().isoformat()].append(b)
    out = {}
    for day, bs in byday.items():
        bs.sort(key=lambda x: x.start_timestamp)
        mv = bs[-1].close_price - bs[0].open_price
        out[day] = "LONG" if mv > 0 else "SHORT" if mv < 0 else "FLAT"
    return out


def main():
    xa = _crossasset_preopen()
    trades = _trades_by_session()
    realized = {u: _open_drive_dir(u) for u in _CFG}

    sessions = sorted(xa.keys())
    print("=" * 94)
    print(f"  CROSS-ASSET SCORECARD (§4.2 promotion decision) — {len(sessions)} sessions with pre-open data")
    print("=" * 94)
    if not sessions:
        print("  No crossasset_shadow pre_open records yet. Tool is READY — re-run after live")
        print("  sessions accumulate (need ~%d). Health-check: logs/crossasset_shadow.jsonl source=kite." % _PROMOTE_MIN_SESSIONS)
        print("=" * 94)
        return 0

    # per-session ledger + accuracy
    xa_hits = xa_n = mdl_hits = mdl_n = 0
    print(f"  {'session':>18} {'xa_bias':>8} {'realized':>8} {'model_dir':>9} {'sess_pnl':>9}")
    veto_saved = veto_killed = 0.0
    veto_saved_n = veto_killed_n = 0
    confusion = defaultdict(lambda: [0, 0.0])  # (model_dir, xa) -> [n, pnl]
    for key in sessions:
        u, day = key
        rec = xa[key]
        xab = rec.get("crossasset_bias")
        real = realized.get(u, {}).get(day)
        tl = trades.get(key, [])
        mdir = max(set(t["dir"] for t in tl), key=lambda d: sum(1 for t in tl if t["dir"] == d)) if tl else None
        spnl = sum(t["pnl"] for t in tl)
        if xab in ("LONG", "SHORT") and real in ("LONG", "SHORT"):
            xa_n += 1; xa_hits += (xab == real)
        if mdir in ("LONG", "SHORT") and real in ("LONG", "SHORT"):
            mdl_n += 1; mdl_hits += (mdir == real)
        # trade-level veto: cross-asset contradicts the trade's direction
        for t in tl:
            if xab in ("LONG", "SHORT") and t["dir"] in ("LONG", "SHORT") and xab != t["dir"]:
                confusion[(t["dir"], xab)][0] += 1
                confusion[(t["dir"], xab)][1] += t["pnl"]
                if t["pnl"] < 0:
                    veto_saved += -t["pnl"]; veto_saved_n += 1
                else:
                    veto_killed += t["pnl"]; veto_killed_n += 1
        print(f"  {u+' '+day:>18} {str(xab):>8} {str(real):>8} {str(mdir):>9} {spnl:>+9,.0f}")

    print("-" * 94)
    print("  1) DIRECTIONAL ACCURACY vs realized open-drive:")
    print(f"     cross-asset : {xa_hits}/{xa_n}" + (f"  ({100*xa_hits//xa_n}%)" if xa_n else ""))
    print(f"     model bias  : {mdl_hits}/{mdl_n}" + (f"  ({100*mdl_hits//mdl_n}%)" if mdl_n else ""))
    print("  2) VETO VALUE (longs+shorts the cross-asset bias contradicts):")
    print(f"     losers it would SAVE     : ₹{veto_saved:+,.0f}  (n={veto_saved_n})")
    print(f"     winners it would KILL    : ₹{-veto_killed:+,.0f}  (n={veto_killed_n})")
    print(f"     NET veto P&L             : ₹{veto_saved - veto_killed:+,.0f}")
    print("  3) CONFUSION (model_dir × xa_bias → n, avg P&L):")
    for (md, xab), (n, pnl) in sorted(confusion.items()):
        print(f"     model={md:5} xa={xab:5}: n={n:>2}  avg ₹{pnl/n:>+8,.0f}")

    print("-" * 94)
    ready = len(sessions) >= _PROMOTE_MIN_SESSIONS
    edge = (xa_n and mdl_n and xa_hits / xa_n > mdl_hits / mdl_n
            and (veto_saved - veto_killed) > 0 and veto_saved > veto_killed)
    verdict = ("PROMOTE — cross-asset out-calls the model AND the veto is net-positive/loser-driven"
               if (ready and edge) else
               f"HOLD — need ≥{_PROMOTE_MIN_SESSIONS} sessions ({len(sessions)} so far)" if not ready else
               "HOLD — cross-asset not yet beating the model / veto not loser-driven")
    print(f"  PROMOTION VERDICT: {verdict}")
    print("=" * 94)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
