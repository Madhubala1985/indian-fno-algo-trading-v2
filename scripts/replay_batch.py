#!/usr/bin/env python3
"""
replay_batch.py — batch surgical replay over many real days, aggregated.

The first holistic validation of Part-1-A on REAL option premiums (no live-session-
per-day). Loops the fidelity-v2 surgical replay across a date range/underlyings and
reports win-rate, P&L, per-day breakdown, conviction separation, and (optionally) a
conviction-floor A/B. Days whose historical chain can't be fetched (expired expiry)
are skipped and listed — this is the known Kite current-only-instruments limit.

Usage:
  PYTHONPATH=. python3 scripts/replay_batch.py --underlyings SENSEX --start 2026-06-25 --end 2026-06-30
  PYTHONPATH=. python3 scripts/replay_batch.py --underlyings SENSEX,NIFTY --start 2026-06-25 --end 2026-06-30 --ab 0.40
"""
from __future__ import annotations

import argparse
from datetime import date

from platform_core.backtesting.batch_replay import (
    UNDERLYING_CFG, BatchReplayResult, run_batch, trading_days,
)


def _print_summary(batch: BatchReplayResult, label: str) -> None:
    trades = batch.trades
    wr = batch.win_rate
    wconv, lconv = batch.conviction_split()
    replayed = batch.replayed
    skipped = [d for d in batch.days if d.result is None]
    print("\n" + "=" * 78)
    print(f"  BATCH SUMMARY — {label}")
    print("=" * 78)
    print(f"  days replayed: {len(replayed)}   skipped: {len(skipped)}   "
          f"conv_floor: {batch.conv_floor}")
    print(f"  TRADES {len(trades)}   WINS {batch.wins}   WIN-RATE {wr*100:.1f}%   "
          f"TOTAL P&L ₹{batch.total_pnl:+,.0f}")
    if trades:
        avg = batch.total_pnl / len(trades)
        print(f"  avg P&L/trade ₹{avg:+,.0f}   "
              f"conviction: winners {wconv:.3f} vs losers {lconv:.3f}  "
              f"(sep {wconv - lconv:+.3f})")
    blocks = batch.block_totals()
    if blocks:
        print("  entry-gate blocks: " + "  ".join(
            f"{k}={v}" for k, v in sorted(blocks.items(), key=lambda kv: -kv[1])[:8]))
    if skipped:
        print("  skipped days:")
        for d in skipped:
            print(f"    {d.underlying:9} {d.day} — {d.skipped}")
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlyings", default="SENSEX",
                    help="comma-separated (SENSEX,NIFTY,BANKNIFTY)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--conv-floor", type=float, default=None,
                    help="single-pass conviction floor (skip setups below it)")
    ap.add_argument("--ab", type=float, default=None,
                    help="A/B: run floor=None vs floor=THIS and compare")
    ap.add_argument("--strikes", type=int, default=8)
    ap.add_argument("--max-dte", type=int, default=8,
                    help="skip a day if the nearest listed expiry is farther than this "
                         "(guards against trading a far expiry when the true nearest expired)")
    ap.add_argument("--capital", type=float, default=400000.0, help="sizing capital basis (₹)")
    ap.add_argument("--risk-frac", type=float, default=0.020,
                    help="per-trade risk fraction (live: 0.02 conservative / 0.04 balanced)")
    ap.add_argument("--max-lots", type=int, default=4, help="Tier-2 lot ceiling")
    ap.add_argument("--risk-compare", action="store_true",
                    help="run BOTH 2%% (conservative) and 4%% (balanced) and compare "
                         "(the recommended view — sizing is the biggest P&L lever)")
    args = ap.parse_args()
    print(f"[batch] sizing: capital=₹{args.capital:,.0f}  risk_frac={args.risk_frac:.1%}  "
          f"max_lots={args.max_lots}  (% -of-capital, not fixed lots)")

    unders = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    bad = [u for u in unders if u not in UNDERLYING_CFG]
    if bad:
        print(f"unknown underlying(s): {bad}  (known: {list(UNDERLYING_CFG)})")
        return 1
    days = trading_days(date.fromisoformat(args.start), date.fromisoformat(args.end))
    print(f"[batch] underlyings={unders}  days={len(days)} "
          f"({args.start}..{args.end}, weekdays)  strikes=±{args.strikes}")

    if args.risk_compare:
        # Sizing is the biggest P&L lever — show both profiles side by side.
        from platform_core.data.live.kite_adapter import KiteAdapter
        from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
        adapter = KiteAdapter()
        fetcher = HistoricalOptionChainFetcher(adapter)
        results = []
        for frac, name in ((0.020, "2% conservative (real-fund-safe)"),
                           (0.040, "4% balanced (paper/validation)")):
            print(f"\n[risk {frac:.0%}] {name}:")
            r = run_batch(underlyings=unders, days=days, conv_floor=args.conv_floor,
                          strikes=args.strikes, max_entry_dte=args.max_dte, capital=args.capital,
                          risk_frac=frac, max_lots=args.max_lots, adapter=adapter, fetcher=fetcher)
            results.append((name, r))
        for name, r in results:
            _print_summary(r, f"risk {name}")
        (n0, r0), (n1, r1) = results
        print("\n" + "-" * 78)
        print(f"  RISK DELTA (4% − 2%):  P&L ₹{r1.total_pnl - r0.total_pnl:+,.0f}   "
              f"(2%: ₹{r0.total_pnl:+,.0f}  |  4%: ₹{r1.total_pnl:+,.0f})   "
              f"capital=₹{args.capital:,.0f}")
        print("  → 2% is real-fund-safe; 4% is paper-only (>2.5% not approved for real funds).")
        print("-" * 78)
        return 0

    if args.ab is not None:
        # Build adapter/fetcher once; reuse across both passes (chains are cached).
        from platform_core.data.live.kite_adapter import KiteAdapter
        from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
        adapter = KiteAdapter()
        fetcher = HistoricalOptionChainFetcher(adapter)
        print("\n[A] conviction OFF (floor=None):")
        a = run_batch(underlyings=unders, days=days, conv_floor=None, strikes=args.strikes,
                      max_entry_dte=args.max_dte, capital=args.capital, risk_frac=args.risk_frac,
                      max_lots=args.max_lots, adapter=adapter, fetcher=fetcher)
        print(f"\n[B] conviction ON (floor={args.ab}):")
        b = run_batch(underlyings=unders, days=days, conv_floor=args.ab, strikes=args.strikes,
                      max_entry_dte=args.max_dte, capital=args.capital, risk_frac=args.risk_frac,
                      max_lots=args.max_lots, adapter=adapter, fetcher=fetcher)
        _print_summary(a, "A: conviction OFF")
        _print_summary(b, f"B: conviction floor={args.ab}")
        print("\n" + "-" * 78)
        print(f"  A/B DELTA (B − A):  trades {len(b.trades) - len(a.trades):+d}   "
              f"win-rate {(b.win_rate - a.win_rate)*100:+.1f}pp   "
              f"P&L ₹{b.total_pnl - a.total_pnl:+,.0f}")
        print(f"  → conviction floor {args.ab} "
              f"{'HELPS' if b.total_pnl > a.total_pnl else 'HURTS/NEUTRAL'} on this sample "
              f"(NOT out-of-sample proof — needs more days).")
        print("-" * 78)
        return 0

    batch = run_batch(underlyings=unders, days=days, conv_floor=args.conv_floor,
                      strikes=args.strikes, max_entry_dte=args.max_dte, capital=args.capital,
                      risk_frac=args.risk_frac, max_lots=args.max_lots)
    _print_summary(batch, f"floor={args.conv_floor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
