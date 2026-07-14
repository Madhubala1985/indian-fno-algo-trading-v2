#!/usr/bin/env python3
"""
shadow_corpus_status.py — how much OOS data has each shadow accumulated?

Every unvalidated hypothesis lives in a shadow log (logs-only, no trade effect) until it has
enough out-of-sample sessions to judge. This summarises each shadow's record count, distinct
trading days, and (where relevant) whether its key field is populated — so we know when a
hypothesis is ready for a promotion test vs still starved.
"""
from __future__ import annotations

import json
from pathlib import Path

# shadow file → (key field to sanity-check non-null, one-line purpose)
_SHADOWS = {
    "logs/regime_shadow.jsonl":            ("vol_regime", "volatility/expansion regime per bar"),
    "logs/htf_shadow.jsonl":               ("is_aligned", "HTF alignment (drives LONG_ALIGNMENT_GATE)"),
    "logs/trap_shadow.jsonl":              ("trap_probability", "TrapProbability OOS corpus"),
    "logs/conviction_shadow.jsonl":        ("conviction", "conviction scoring (anti-predictive check)"),
    "logs/options_analytics_shadow.jsonl": ("gex_regime", "GEX/OI/dealer/premium analytics"),
    "logs/crossasset_shadow.jsonl":        ("source",     "GIFT/S&P/Brent/VIX + constituent flow (§4.2)"),
}


def _summ(path: str, key: str):
    p = Path(path)
    if not p.exists():
        return None
    n = 0
    days = set()
    nonnull = 0
    sources = {}
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        n += 1
        ts = d.get("ts") or d.get("logged_at") or d.get("session_date") or ""
        if isinstance(ts, str) and len(ts) >= 10:
            days.add(ts[:10])
        if d.get(key) is not None:
            nonnull += 1
        if key == "source":
            sources[d.get("source")] = sources.get(d.get("source"), 0) + 1
    return n, sorted(days), nonnull, sources


def main():
    print("=" * 90)
    print("  SHADOW CORPUS STATUS — OOS data accumulated per hypothesis")
    print("=" * 90)
    print(f"  {'shadow':38} {'recs':>7} {'days':>5} {'key-nonnull':>12}")
    print("  " + "-" * 86)
    for path, (key, purpose) in _SHADOWS.items():
        r = _summ(path, key)
        name = path.split("/")[-1]
        if r is None:
            print(f"  {name:38} {'—':>7} {'—':>5} {'(not created yet)':>12}   {purpose}")
            continue
        n, days, nonnull, sources = r
        pct = f"{100*nonnull//n if n else 0}%"
        print(f"  {name:38} {n:>7} {len(days):>5} {nonnull:>6} ({pct:>3})")
        print(f"  {'':38} span {days[0] if days else '?'}..{days[-1] if days else '?'}   {purpose}")
        if sources:
            print(f"  {'':38} sources: " + ", ".join(f"{k}={v}" for k, v in sources.items()))
    print("=" * 90)
    print("  readiness rule of thumb: a hypothesis needs ~20+ distinct OOS days with its key")
    print("  field populated before a promotion test is meaningful (see replay-parity ledger).")
    print("=" * 90)


if __name__ == "__main__":
    raise SystemExit(main())
