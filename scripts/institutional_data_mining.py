#!/usr/bin/env python3
"""
institutional_data_mining.py — proactive mining of data we already hold (2026-07-14).

Institutional premise (user directive): the opponents' book IS the data. Option OI is the one
place retail can see institutional positioning — writers (sellers) are overwhelmingly the
institutions; they position where they expect price NOT to go, and they harvest fixed stops
and theta from buyers. We recorded 22 day-symbols of REAL per-minute OI and never read it as
opponent flow. Also: our own book uses fixed %-stops — exactly the harvestable pattern.

STUDY A — "Are WE the harvested?" For every losing exit on a recorded-chain day, follow the
same contract's real premium for 60 min after our exit. If it recovers above our ENTRY, the
market took our stop/exit and went where we said — we were the liquidity.

STUDY B — "Trade WITH the writers." Per day: cumulative ΔOI (calls vs puts) from open to a
causal 11:30 IST read. Classic writer read: puts being written faster than calls (ΔOI_PE >
ΔOI_CE) = institutions selling downside = they expect UP (and vice versa). Score that read
against the 11:30→close move. Report per-day hits, both indices, no cherry-picking.

STUDY C — the theta toll: estimate the total theta we PAID the writers across the live book.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from straddle_study import _CFG, _CACHE, _load_all_bars, _recorded_days
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher

IST = timezone(timedelta(hours=5, minutes=30))


def _load_chain(u, d):
    f = HistoricalOptionChainFetcher(adapter=None)
    exp = f.cached_expiry(u, d)
    if exp is None:
        return None
    p = _CACHE / f"{u}_{d.isoformat()}_{exp.isoformat()}.jsonl"
    if not p.exists():
        return None
    rows = []
    for ln in p.read_text().splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass
    return rows


# ── STUDY A ────────────────────────────────────────────────────────────────────
def study_a():
    print("=" * 92)
    print("  STUDY A — are WE the harvested liquidity? (real premium path AFTER our losing exits)")
    print("=" * 92)
    c = sqlite3.connect("logs/trades.db")
    trades = list(c.execute(
        """select underlying, contract_symbol, entry_premium, exit_premium, exit_ts, pnl,
                  exit_reason, date(entry_ts) from pt_trades
           where pnl < 0 and exit_ts is not null order by entry_ts"""))
    chains = {}
    n = harvested = bounced = 0
    tot_missed = 0.0
    for u, sym, ep, xp, xts, pnl, reason, d in trades:
        try:
            day = date.fromisoformat(d)
        except Exception:
            continue
        key = (u, d)
        if key not in chains:
            chains[key] = _load_chain(u, day)
        rows = chains[key]
        if not rows or not xp:
            continue
        try:
            t0 = datetime.fromisoformat(xts)
        except Exception:
            continue
        path = sorted((r["ts"], float(r["ltp"])) for r in rows
                      if r.get("tradingsymbol") == sym and r.get("ltp"))
        post = [ltp for ts, ltp in path
                if t0 <= datetime.fromisoformat(ts) <= t0 + timedelta(minutes=60)]
        if len(post) < 5:
            continue
        n += 1
        mx = max(post)
        if mx > ep:                      # premium came back ABOVE our entry after we bled out
            harvested += 1
            tot_missed += (mx - float(xp))
        elif mx > float(xp) * 1.15:      # >15% bounce off our exit
            bounced += 1
    print(f"  losing exits followed on real chains: {n}")
    if n:
        print(f"  HARVESTED (premium exceeded our ENTRY within 60min of our exit): "
              f"{harvested}/{n} ({100*harvested//n}%)")
        print(f"  bounced >15% off our exit (but below entry):                    {bounced}/{n}")
        print(f"  premium move we handed over on harvested exits: ₹{tot_missed:,.0f}/unit "
              f"(× lot_size × lots each)")
        print("  MECHANISM: high harvest rate ⇒ our exits sit exactly where liquidity is taken —")
        print("  fixed %-stops and reactive structure exits are visible/late. Institutional fix:")
        print("  invalidation-based exits (thesis-level), not price-level %-stops.")
    print()


# ── STUDY B ────────────────────────────────────────────────────────────────────
def _writer_read(rows, cutoff_utc):
    """ΔOI calls vs puts, open→cutoff. Returns (signal, d_ce, d_pe)."""
    first, last = {}, {}
    for r in rows:
        if r.get("oi") is None:
            continue
        ts = r["ts"]
        if ts[11:16] > cutoff_utc:
            continue
        k = (r["tradingsymbol"],)
        if k not in first:
            first[k] = r
        last[k] = r
    d_ce = d_pe = 0
    for k, fr in first.items():
        lr = last[k]
        d = (lr["oi"] or 0) - (fr["oi"] or 0)
        if fr["type"] == "CE":
            d_ce += d
        else:
            d_pe += d
    if d_pe > d_ce:
        return "UP", d_ce, d_pe      # puts written faster → institutions sell downside → UP
    if d_ce > d_pe:
        return "DOWN", d_ce, d_pe
    return "FLAT", d_ce, d_pe


def study_b():
    print("=" * 92)
    print("  STUDY B — opponent positioning: writer ΔOI (open→11:30 IST) vs the 11:30→close move")
    print("=" * 92)
    hits = misses = 0
    ledger = []
    for u in ("NIFTY", "SENSEX"):
        cfg = _CFG[u]
        bars = _load_all_bars(u, cfg["feed"])
        byday = {}
        for b in bars:
            byday.setdefault(b.start_timestamp.astimezone(timezone.utc).date(), []).append(b)
        for d in _recorded_days(u):
            rows = _load_chain(u, d)
            sess = byday.get(d)
            if not rows or not sess:
                continue
            sig, dce, dpe = _writer_read(rows, "06:00")     # 11:30 IST = 06:00 UTC
            after = [b for b in sess if b.start_timestamp.astimezone(timezone.utc).hour >= 6]
            if not after or sig == "FLAT":
                continue
            mv = after[-1].close_price - after[0].open_price
            actual = "UP" if mv > 0 else "DOWN"
            ok = sig == actual
            hits += ok
            misses += (not ok)
            ledger.append((u, d.isoformat(), sig, actual, ok,
                           100.0 * mv / after[0].open_price, dce, dpe))
    for u, d, sig, act, ok, mvpct, dce, dpe in ledger:
        print(f"  {u:6} {d}  writers say {sig:4} → market went {act:4} {mvpct:+5.2f}%  "
              f"{'HIT ' if ok else 'MISS'}  (ΔOI ce {dce:+,} / pe {dpe:+,})")
    tot = hits + misses
    if tot:
        print("-" * 92)
        print(f"  WRITER-READ HIT RATE (11:30 IST read → close): {hits}/{tot} ({100*hits//tot}%)")
        print("  ≥60% on 20+ days = a causal opponent-flow signal worth a live shadow;")
        print("  ~50% = OI aggregate too coarse → refine to strike-level walls/velocity.")
    print()


# ── STUDY C ────────────────────────────────────────────────────────────────────
def study_c():
    print("=" * 92)
    print("  STUDY C — the theta toll we paid the writers (live book)")
    print("=" * 92)
    c = sqlite3.connect("logs/trades.db")
    tot_theta = tot_prem = 0.0
    n = 0
    for ep, lots, ls, bars, expd, ets, pnl in c.execute(
            """select entry_premium, lots, lot_size, bars_held, expiry_date, entry_ts, pnl
               from pt_trades where entry_premium>0 and pnl is not null"""):
        try:
            dte = max(((date.fromisoformat(expd[:10]) - date.fromisoformat(ets[:10])).days), 0) + 0.5
        except Exception:
            dte = 3.0
        qty = (lots or 0) * (ls or 0)
        theta = (0.5 / dte) * ((bars or 0) / 375.0) * ep * qty
        tot_theta += theta
        tot_prem += ep * qty
        n += 1
    print(f"  {n} trades: est. theta paid ≈ ₹{tot_theta:,.0f} on ₹{tot_prem:,.0f} premium deployed")
    print(f"  book net P&L ₹+9,607 → theta toll ≈ {abs(tot_theta)/9607:.1f}× the entire net edge.")
    print("  MECHANISM: as pure premium BUYERS we pay the institutions' rent every minute we sit")
    print("  in a trade. The futures sleeve (theta-zero) deployed today is the first counter;")
    print("  the writer-side sleeve (defined-risk selling) is the second — gated on Study B.")
    print()


if __name__ == "__main__":
    study_a()
    study_b()
    study_c()
