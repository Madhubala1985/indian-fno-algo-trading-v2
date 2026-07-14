#!/usr/bin/env python3
"""
probe_gift_symbols.py — logs-only probe: does Kite expose GIFT Nifty / global-index feeds?

Downloads the full Kite instrument dump, greps for GIFT / GIFTNIFTY and global-index
tracking symbols (Dow / S&P / Nasdaq), then attempts kite.ltp / kite.quote on each
candidate. Reports which symbols resolve and sample quotes. If a real GIFT feed exists it
becomes the first-choice gift_gap source in crossasset_shadow (superior to the gap-at-open
proxy: causal, fetched pre-open with a timestamp). Read-only; touches nothing else.

Usage:  PYTHONPATH=. python3 scripts/probe_gift_symbols.py
"""
from __future__ import annotations

from platform_core.data.live.kite_adapter import KiteAdapter

_NEEDLES = ("GIFT", "GIFTNIFTY", "SGX", "DOW", "DJIA", "DJI", "NASDAQ", "NDX",
            "S&P", "SPX", "SP500", "US500", "US30", "US100", "USTECH", "GLOBAL")


def main() -> int:
    a = KiteAdapter()
    insts = a.kite.instruments()
    print(f"[probe] total instruments: {len(insts)}")

    hits = []
    for i in insts:
        ts = str(i.get("tradingsymbol", "")).upper()
        nm = str(i.get("name", "")).upper()
        if any(n in ts or n in nm for n in _NEEDLES):
            hits.append(i)
    print(f"[probe] candidate matches: {len(hits)}\n")

    # group by exchange for readability
    by_exch: dict[str, list] = {}
    for i in hits:
        by_exch.setdefault(i.get("exchange", "?"), []).append(i)
    candidate_keys = []
    for exch in sorted(by_exch):
        rows = by_exch[exch]
        print(f"── {exch}  ({len(rows)} matches) ──")
        for i in rows[:40]:
            key = f"{i.get('exchange')}:{i.get('tradingsymbol')}"
            candidate_keys.append(key)
            print(f"  {key:34} name={i.get('name'):16} type={i.get('instrument_type')} "
                  f"seg={i.get('segment')} expiry={i.get('expiry')}")
        if len(rows) > 40:
            print(f"  … +{len(rows) - 40} more")
        print()

    if not candidate_keys:
        print("[probe] NO GIFT / global-index symbols in the Kite dump → gap-at-open proxy stays primary.")
        return 0

    # de-dup and attempt live quotes (GIFT trades 06:30 IST — live before our 09:00 window)
    uniq = sorted(set(candidate_keys))
    print(f"[probe] attempting kite.quote on {len(uniq)} unique candidate keys…\n")
    resolved = []
    for i in range(0, len(uniq), 200):
        batch = uniq[i:i + 200]
        try:
            q = a.kite.quote(batch) or {}
        except Exception as e:
            print(f"  quote batch failed: {type(e).__name__}: {str(e)[:100]}")
            q = {}
        for k in batch:
            row = q.get(k)
            if isinstance(row, dict) and row.get("last_price"):
                ohlc = row.get("ohlc") or {}
                resolved.append((k, row.get("last_price"), ohlc.get("close")))

    print("── RESOLVED (live quote available) ──")
    if resolved:
        for k, lp, prev in resolved:
            gap = (100.0 * (lp - prev) / prev) if (lp and prev) else None
            print(f"  {k:34} last={lp:<12} prev_close={prev}  "
                  f"gap_vs_prev={f'{gap:+.2f}%' if gap is not None else 'n/a'}")
        print(f"\n[probe] → set gift_symbol in crossasset_shadow to the best GIFT-Nifty key above.")
    else:
        print("  none returned a live quote (market closed for these, or no data entitlement).")
        print("  → re-run during GIFT morning session (06:30–09:00 IST); proxy stays primary meanwhile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
