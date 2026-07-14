#!/usr/bin/env python3
"""
Task A — two-condition expansion detector, cross-checked on the corpus.

Detector fires (long-straddle candidate) only when BOTH causal conditions hold:
  1. RISK-STATE elevated: overnight/open GAP is the only component recorded in-repo;
     GIFT-gap / overnight-S&P / Brent-momentum / geopolitical-headline are EXTERNAL
     feeds NOT in the repo (flagged NEEDS-FEED). 07-08 + 06-29 both fall in the same
     confirmed Iran/Hormuz window — UNSCHEDULED, so a scheduled-event calendar alone
     is insufficient by construction.
  2. IV-CHEAPNESS: the market-implied daily move (ATM straddle premium / spot at open)
     is SMALL relative to the day's actual move. Ratio = realized_range% / implied_move%.
     > 1 means the straddle was underpriced vs what happened (expansion was cheap).

Per-day hits/misses reported. IV-cheapness is computed from recorded chains (real); the
risk-state column shows the computable GAP plus a NEEDS-FEED marker for the rest.
"""
from __future__ import annotations

from datetime import timezone

from straddle_study import (_CFG, _CACHE, _load_all_bars, _minute_map,
                            _atm_strike, _recorded_days)
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher

# Confirmed geopolitical window (from WebSearch: US-Iran / Hormuz, crude +8%/2d).
_GEO_WINDOW = {"2026-06-29", "2026-06-30", "2026-07-07", "2026-07-08"}
_IMPLIED_CHEAP_RATIO = 1.3   # realized/implied above this = expansion was underpriced
_GAP_ELEVATED_PCT = 0.35     # |open-gap| over prior close beyond this = risk-state stir


def main():
    print("TASK A — two-condition expansion detector cross-check")
    print("cond1 RISK-STATE: gap computed | S&P/Brent/GIFT/geopolitical = NEEDS-FEED")
    print(f"cond2 IV-CHEAP: realized_range pct / implied_move pct  (>{_IMPLIED_CHEAP_RATIO} = cheap)\n")
    print(f"{'day':>17} {'gap%':>6} {'geo':>4} {'implied%':>9} {'realized%':>10} "
          f"{'cheap x':>8}  {'DETECT':>7}")
    hits = []
    for u in ("NIFTY", "SENSEX"):
        cfg = _CFG[u]
        bars = _load_all_bars(u, cfg["feed"])
        fetcher = HistoricalOptionChainFetcher(adapter=None)
        by_date = {}
        for b in bars:
            by_date.setdefault(b.start_timestamp.astimezone(timezone.utc).date(), []).append(b)
        prev_close = {}
        ordered = sorted(by_date)
        for i, d in enumerate(ordered):
            if i > 0:
                prev_close[d] = by_date[ordered[i - 1]][-1].close_price
        for d in _recorded_days(u):
            sess = by_date.get(d)
            if not sess:
                continue
            op = sess[0].open_price
            rng = 100.0 * (max(b.high_price for b in sess) - min(b.low_price for b in sess)) / op
            gap = 100.0 * (op - prev_close[d]) / prev_close[d] if d in prev_close else 0.0
            # implied move from open ATM straddle
            expiry = fetcher.cached_expiry(u, d)
            implied = None
            if expiry is not None:
                cache = _CACHE / f"{u}_{d.isoformat()}_{expiry.isoformat()}.jsonl"
                if cache.exists():
                    mm = _minute_map(fetcher.load_day(cache))
                    if mm:
                        legs = mm[sorted(mm)[0]]
                        strike = _atm_strike(legs)
                        if strike is not None:
                            straddle = legs[(strike, "CE")] + legs[(strike, "PE")]
                            implied = 100.0 * straddle / strike
            geo = d.isoformat() in _GEO_WINDOW
            cheap_x = (rng / implied) if implied else None
            cond1 = (abs(gap) >= _GAP_ELEVATED_PCT) or geo   # gap OR geopolitical
            cond2 = cheap_x is not None and cheap_x >= _IMPLIED_CHEAP_RATIO
            detect = cond1 and cond2
            hits.append((u, d.isoformat(), rng, detect, geo))
            print(f"{u+' '+d.isoformat():>17} {gap:>+6.2f} {('YES' if geo else '·'):>4} "
                  f"{(f'{implied:.2f}' if implied else 'n/a'):>9} {rng:>10.2f} "
                  f"{(f'{cheap_x:.2f}' if cheap_x else 'n/a'):>8}  {('FIRE' if detect else '-'):>7}")

    # cross-check: did the detector fire on the big-expansion days?
    print("\n-- cross-check vs big-expansion days (realized range ranked) --")
    big = sorted(hits, key=lambda x: -x[2])[:6]
    for u, d, rng, det, geo in big:
        mark = "HIT " if det else "MISS"
        print(f"  {mark}  {u:6} {d}  realized={rng:4.2f}%  geo={'Y' if geo else 'n'}  "
              f"detector={'FIRE' if det else 'silent'}")
    print("\nNOTE 06-29: the +44k live monster (SENSEX) is in the geo window; its realized")
    print("range (0.82%) is modest — the move was DIRECTIONAL (afternoon short), so an ATM")
    print("straddle underperformed a correct short. IV-cheapness catches VOLATILITY expansion,")
    print("not directional trends already captured by a correctly-aligned directional trade.")


if __name__ == "__main__":
    raise SystemExit(main())
