#!/usr/bin/env python3
"""
participant_oi_study.py — the OPPONENT'S ACTUAL BOOK: NSE participant-wise OI (daily, public).

FII/Pro = the institutions; Client = retail (the harvested crowd). Fetches the daily CSVs for
the corpus window, then tests causally (position known at close T, predicts day T+1):
  H1: FII index-futures NET direction (and day-over-day Δ) → next-day index direction.
  H2: retail(Client)-vs-FII divergence — when retail is stretched long vs FII, next day bleeds.
  H3: join with OUR live trade days — were our losing LONG days days when FII was net short?

Data → data/source/participant_oi/. Study prints per-day ledger + hit rates (n small; mechanism
+ counts, no promotion claims).
"""
from __future__ import annotations

import csv
import io
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

OUT = Path("data/source/participant_oi")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
START, END = date(2026, 6, 2), date(2026, 7, 13)


def fetch_all():
    OUT.mkdir(parents=True, exist_ok=True)
    d = START
    got = 0
    while d <= END:
        if d.weekday() < 5:
            p = OUT / f"{d.isoformat()}.csv"
            if not p.exists() or p.stat().st_size < 300:
                url = (f"https://nsearchives.nseindia.com/content/nsccl/"
                       f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv")
                try:
                    r = subprocess.run(["curl", "-sL", "--max-time", "20", "-A", UA, url],
                                       capture_output=True, timeout=25)
                    body = r.stdout
                    if body and b"Client Type" in body:
                        p.write_bytes(body)
                        got += 1
                    time.sleep(0.4)
                except Exception:
                    pass
        d += timedelta(days=1)
    files = sorted(OUT.glob("*.csv"))
    print(f"[fetch] {len(files)} participant-OI days on disk (+{got} new)")
    return files


def parse(p: Path):
    """{participant: {"fl","fs","cl","pl","cs","ps"}} — index futures/options contracts."""
    out = {}
    try:
        rows = list(csv.reader(io.StringIO(p.read_text())))
        for r in rows:
            if r and r[0].strip() in ("Client", "DII", "FII", "Pro"):
                out[r[0].strip()] = {
                    "fl": int(r[1]), "fs": int(r[2]),
                    "cl": int(r[5]), "pl": int(r[6]), "cs": int(r[7]), "ps": int(r[8]),
                }
    except Exception:
        pass
    return out


def main():
    files = fetch_all()
    days = {}
    for p in files:
        d = parse(p)
        if d:
            days[p.stem] = d
    dates = sorted(days)
    if len(dates) < 5:
        print("insufficient days fetched")
        return 1

    # next-day NIFTY direction from local index CSVs
    import sys
    sys.path.insert(0, "scripts")
    from straddle_study import _load_all_bars
    bars = _load_all_bars("NIFTY", "NIFTY50")
    from datetime import timezone
    daily = {}
    for b in bars:
        dd = b.start_timestamp.astimezone(timezone.utc).date().isoformat()
        o, c = daily.get(dd, (b.open_price, b.close_price))
        daily[dd] = (o, b.close_price)

    print("=" * 100)
    print("  PARTICIPANT-WISE OI — the institutions' actual book vs next-day NIFTY")
    print("=" * 100)
    print(f"  {'date':>10} {'FII netF':>9} {'ΔnetF':>8} {'FII_putbias':>11} {'retail netF':>11} "
          f"{'nextday':>8} {'H1':>4}")
    h1_hit = h1_n = 0
    diverg = []
    prev_net = None
    ledger = []
    for i, ds in enumerate(dates[:-1]):
        nd = dates[i + 1] if i + 1 < len(dates) else None
        fii = days[ds].get("FII")
        cli = days[ds].get("Client")
        if not fii or not cli:
            continue
        net = fii["fl"] - fii["fs"]
        dnet = (net - prev_net) if prev_net is not None else 0
        prev_net = net
        # FII options stance: long puts + short calls = bearish exposure
        put_bias = (fii["pl"] + fii["cs"]) - (fii["cl"] + fii["ps"])
        retail_net = cli["fl"] - cli["fs"]
        # next trading day's move
        nxt = None
        for k in sorted(daily):
            if k > ds:
                o, c = daily[k]
                nxt = 100.0 * (c - o) / o
                break
        if nxt is None or dnet == 0:
            continue
        sig = "UP" if dnet > 0 else "DOWN"
        act = "UP" if nxt > 0 else "DOWN"
        ok = sig == act
        h1_hit += ok
        h1_n += 1
        ledger.append((ds, net, dnet, put_bias, retail_net, nxt, ok))
        print(f"  {ds:>10} {net:>9,} {dnet:>+8,} {put_bias:>11,} {retail_net:>11,} "
              f"{nxt:>+7.2f}% {'HIT' if ok else 'MISS':>4}")
        if retail_net > 2.5 * max(net, 1):
            diverg.append((ds, nxt))
    print("-" * 100)
    if h1_n:
        print(f"  H1 FII Δnet-futures → next-day direction: {h1_hit}/{h1_n} ({100*h1_hit//h1_n}%)")
    big = [(ds, nxt, ok) for ds, net, dnet, pb, rn, nxt, ok in ledger if abs(nxt) >= 0.6]
    if big:
        bh = sum(1 for _, _, ok in big if ok)
        print(f"  H1 on BIG next-days (|move|≥0.6%): {bh}/{len(big)} — the days that matter")
    if diverg:
        avg = sum(x for _, x in diverg) / len(diverg)
        print(f"  H2 retail-stretched days (retail netF > 2.5× FII): n={len(diverg)}, "
              f"avg next-day {avg:+.2f}%")
    # H3: our losing-LONG days vs FII stance that morning (prior close)
    import sqlite3
    c = sqlite3.connect("logs/trades.db")
    print("-" * 100)
    print("  H3 our LONG-day P&L vs FII net-futures stance at prior close:")
    for ds, und, pnl in c.execute(
            """select date(entry_ts), underlying, sum(pnl) from pt_trades
               where direction='LONG' group by 1,2 order by 1"""):
        prior = max((k for k in dates if k < ds), default=None)
        if prior and "FII" in days.get(prior, {}):
            f = days[prior]["FII"]
            net = f["fl"] - f["fs"]
            stance = "NET-LONG" if net > 0 else "NET-SHORT"
            tag = ("WITH institutions" if net > 0 else "AGAINST institutions")
            print(f"    {ds} {und:7} LONG P&L ₹{pnl:>+8,.0f}  FII {stance} ({net:+,}) → we were {tag}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
