#!/usr/bin/env python3
"""H6 verdict — EXIT_BOS_MIN_PEAK_PCT=0.04 neighborhood robustness (night 2026-07-19).

Pre-registered in logs/night_explorer_2026-07-19.md before computation:
corpus = 30 pairs (NIFTY 13 + BANKNIFTY 2 @ floor 0.05, SENSEX 15 @ 0.015).
Composition rule (H1): a "portfolio" variant total sums the N/B rows at 0.05 and
the SENSEX rows at 0.015; composites use delta+gexspot+wideNN on N/B and
gexspot+wideNN on SENSEX.

ROBUST iff BOTH
 (a) standalone wide03 AND wide05 net >= +16,455 (>=50% of wide04's +13,999
     improvement over base +9,455) AND ex-best-day > +737 (base ex-best);
 (b) composite wide03 AND wide05 each >= +32,338 (prior-best gexspot+wide04).
Else FRAGILE.
"""
from __future__ import annotations

import json
from collections import defaultdict

WIDESENS = "logs/study_ab_20260719_widesens.jsonl"
AUTH = "logs/study_ab_20260719_authoritative.jsonl"
COMBOS = "logs/study_ab_20260719_combos.jsonl"
DELTA05 = "logs/study_ab_20260719_delta05.jsonl"

FLOORS = {"NIFTY": 0.05, "BANKNIFTY": 0.05, "SENSEX": 0.015}


def load(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            return [json.loads(l) for l in fh]
    except FileNotFoundError:
        return []


def portfolio(rows: list[dict], nb_variant: str, sensex_variant: str) -> dict | None:
    """Compose per-day nets: nb_variant on NIFTY/BANKNIFTY, sensex_variant on SENSEX."""
    by_day: dict[str, float] = defaultdict(float)
    seen: set[tuple[str, str]] = set()
    for r in rows:
        u = r["underlying"]
        want = nb_variant if u in ("NIFTY", "BANKNIFTY") else sensex_variant
        if r["variant"] != want or r["floor"] != FLOORS[u]:
            continue
        key = (u, r["day"])
        if key in seen:          # duplicate guard (resumed sweeps may re-append)
            continue
        seen.add(key)
        by_day[r["day"]] += r["net"]
    if not seen:
        return None
    total = sum(by_day.values())
    best = max(by_day, key=by_day.get)
    return {"pairs": len(seen), "days": len(by_day), "net": round(total),
            "best_day": best, "ex_best": round(total - by_day[best])}


def main() -> int:
    rows = load(WIDESENS) + load(AUTH) + load(COMBOS) + load(DELTA05)

    print(f"{'variant (N/B | SENSEX)':44} {'pairs':>5} {'net':>9} {'ex-best':>9}  best-day")
    results = {}
    specs = [
        ("base", "base", "base"),
        ("wide02", "wide02", "wide02"),
        ("wide03", "wide03", "wide03"),
        ("wide04", "wide04", "wide04"),
        ("wide05", "wide05", "wide05"),
        ("wide06", "wide06", "wide06"),
        ("wide08", "wide08", "wide08"),
        ("combo wide03", "delta+gexspot+wide03", "gexspot+wide03"),
        ("combo wide04 (delta05+gexspot+wide04)", "delta+gexspot+wide04", "gexspot+wide04"),
        ("combo wide05", "delta+gexspot+wide05", "gexspot+wide05"),
    ]
    for label, nbv, sv in specs:
        p = portfolio(rows, nbv, sv)
        results[label] = p
        if p:
            print(f"{label:44} {p['pairs']:>5} {p['net']:>+9} {p['ex_best']:>+9}  {p['best_day']}")
        else:
            print(f"{label:44} {'—':>5}")

    base, w3, w5 = results["base"], results["wide03"], results["wide05"]
    c3, c5 = results["combo wide03"], results["combo wide05"]
    print("\n── pre-registered criteria ──")
    ok_a = all(p and p["net"] >= 16455 and p["ex_best"] > 737 for p in (w3, w5))
    ok_b = all(p and p["net"] >= 32338 for p in (c3, c5))
    print(f"(a) wide03/wide05 net>=+16,455 and ex-best>+737: {'PASS' if ok_a else 'FAIL'}")
    print(f"(b) combo wide03/wide05 net>=+32,338:            {'PASS' if ok_b else 'FAIL'}")
    print(f"\nH6 VERDICT: {'ROBUST' if ok_a and ok_b else 'FRAGILE'}")
    if base:
        print(f"(sanity: composed base = {base['net']:+}, pre-reg expects +9,455)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
