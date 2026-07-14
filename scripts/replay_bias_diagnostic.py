#!/usr/bin/env python3
"""
replay_bias_diagnostic.py — PRECISELY locate why SurgicalReplay diverges from live.

The replay's trades come from the MCB directional bias. The live model recorded its bias every
bar in trades.db pt_decisions. This feeds the replay's MCB the same session bars (CSV warm-start,
the current replay method) and compares its per-bar bias to the live per-bar bias — pinpointing
whether the divergence is at WARM-START (bar 0 = bias engine seed) or mid-session.

If replay bias ≠ live bias from the first shared bar → the root cause is the warm-start state
(replay seeds higher timeframes from CSV; live seeds from persisted state). That is the exact
failure the boot MCB-snapshot + SurgicalReplay.warm_start_from_state fixes going forward.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import timezone

from straddle_study import _CFG, _load_all_bars

from platform_core.backtesting.surgical_replay import SurgicalReplay


def _norm(bias) -> str:
    s = str(bias).upper()
    for k in ("SHORT", "LONG", "NEUTRAL"):
        if k in s:
            return k
    return s


def _live_bias(day, underlying):
    c = sqlite3.connect("logs/trades.db")
    out = {}
    for ts, b in c.execute("""select bar_ts, bias from pt_decisions
                              where date(bar_ts)=? and underlying=? order by bar_ts""",
                           (day, underlying)):
        out[ts[:16]] = _norm(b)   # key by minute
    return out


def diagnose(underlying, day):
    cfg = _CFG[underlying]
    bars = _load_all_bars(underlying, cfg["feed"])
    from datetime import date as _date
    d = _date.fromisoformat(day)
    warm = [b for b in bars if b.start_timestamp.astimezone(timezone.utc).date() < d]
    session = [b for b in bars if b.start_timestamp.astimezone(timezone.utc).date() == d]
    if not session:
        print(f"  {underlying} {day}: no session bars"); return
    rep = SurgicalReplay(underlying_symbol=underlying, feed_symbol=cfg["feed"])
    rep.warm_start(warm, cfg["feed"])
    warm_bias = _norm(rep._ctx.build().bias)   # bias AT boot, before any session bar
    replay_bias = {}
    for b in session:
        rep._feed_bar(b)
        replay_bias[b.start_timestamp.astimezone(timezone.utc).isoformat()[:16]] = _norm(rep._ctx.build().bias)

    live = _live_bias(day, underlying)
    shared = sorted(set(replay_bias) & set(live))
    print(f"\n  ── {underlying} {day} ──  replay warm-start bias@boot = {warm_bias}")
    if not shared:
        print("    (no overlapping bars with pt_decisions)"); return
    match = sum(1 for m in shared if replay_bias[m] == live[m])
    first_div = next((m for m in shared if replay_bias[m] != live[m]), None)
    print(f"    live bias @ first shared bar {shared[0][11:]}: {live[shared[0]]}   "
          f"replay: {replay_bias[shared[0]]}")
    print(f"    per-bar bias agreement: {match}/{len(shared)} ({100*match//len(shared)}%)")
    if first_div:
        i = shared.index(first_div)
        print(f"    FIRST DIVERGENCE at {first_div[11:]} (shared bar #{i}): "
              f"live={live[first_div]} vs replay={replay_bias[first_div]}"
              + ("  → diverges from the START = WARM-START (bias-seed) root cause"
                 if i == 0 else "  → diverges mid-session"))
    else:
        print("    NO divergence — replay bias matches live all day ✓")


def main():
    pairs = sys.argv[1:] or ["SENSEX:2026-06-29", "SENSEX:2026-07-02", "NIFTY:2026-07-01",
                             "SENSEX:2026-07-01", "SENSEX:2026-07-06"]
    print("=" * 88)
    print("  REPLAY BIAS DIAGNOSTIC — per-bar replay bias vs live pt_decisions")
    print("=" * 88)
    for p in pairs:
        u, d = p.split(":")
        try:
            diagnose(u, d)
        except Exception as e:
            print(f"  {p}: {type(e).__name__}: {e}")
    print("\n" + "=" * 88)


if __name__ == "__main__":
    raise SystemExit(main())
