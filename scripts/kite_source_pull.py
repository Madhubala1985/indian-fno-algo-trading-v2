#!/usr/bin/env python3
"""
kite_source_pull.py — AUTONOMOUS morning source-pull (VM cron 08:50 IST, after the 08:45 token
refresh, before the 09:10 model boot). Goes to the SOURCE instead of waiting for sessions:

  1. GIFT NIFTY + GLOBAL:US500 + INDIA VIX live quotes — the pending premarket-feed confirmation,
     taken during GIFT's live morning session (it trades from 06:30 IST).
  2. FUTURES 1-minute HISTORY backfill (45 days, front month, all three indices) — volume-bearing
     minute bars from the source. Signed-volume CVD proxy over 45 days multiplies the order-flow
     corpus ~40× instead of waiting 40 live sessions.
  3. INDIA VIX daily history (120d) — vol-regime context for every study.

Paced (~0.4s/req, <25 requests total) — negligible vs Kite's 3 req/s; finished well before boot.
Report → logs/kite_source_pull_<date>.log; data → data/source/.
"""
from __future__ import annotations

import csv
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path("data/source")
LOG = Path(f"logs/kite_source_pull_{date.today().isoformat()}.log")
_lines = []


def log(msg):
    print(msg)
    _lines.append(str(msg))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    from platform_core.data.live.kite_adapter import KiteAdapter
    a = KiteAdapter()
    log(f"[pull] {datetime.now(IST):%Y-%m-%d %H:%M IST}")

    # ── 1. premarket quotes: GIFT / US500 / VIX (the pending confirmation) ────
    for key in ("NSEIX:GIFT NIFTY", "GLOBAL:US500", "GLOBAL:US30", "NSE:INDIA VIX"):
        try:
            q = (a.kite.quote([key]) or {}).get(key) or {}
            lp, prev = q.get("last_price"), (q.get("ohlc") or {}).get("close")
            log(f"  QUOTE {key:22} last={lp}  prev_close={prev}  "
                f"{'RESOLVED ✓' if lp else 'EMPTY (no entitlement/closed)'}")
        except Exception as e:
            log(f"  QUOTE {key:22} FAILED: {type(e).__name__}: {str(e)[:80]}")
        time.sleep(0.4)

    # ── 2. futures minute-history backfill (the 40× order-flow corpus) ────────
    today = date.today()
    for exch, name in (("NFO", "NIFTY"), ("NFO", "BANKNIFTY"), ("BFO", "SENSEX")):
        try:
            futs = [i for i in a._get_instruments(exch)
                    if i.get("name") == name and i.get("instrument_type") == "FUT"
                    and i.get("expiry") and i["expiry"] >= today]
            if not futs:
                log(f"  FUT {name}: no live contract found")
                continue
            front = min(futs, key=lambda i: i["expiry"])
            rows = []
            # minute data limited to ~60d/request-window; chunk 3×15 trading-day windows
            for back in (45, 30, 15):
                frm = today - timedelta(days=back)
                to = today - timedelta(days=max(back - 15, 0))
                try:
                    rows += a.kite.historical_data(front["instrument_token"],
                                                   from_date=frm.isoformat(),
                                                   to_date=to.isoformat(),
                                                   interval="minute") or []
                except Exception as e:
                    log(f"  FUT {name} chunk {back}d: {type(e).__name__}")
                time.sleep(0.5)
            if rows:
                p = OUT / f"{name}_FUT_1m_45d.csv"
                with p.open("w", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                    seen = set()
                    for r in rows:
                        ts = str(r["date"])
                        if ts in seen:
                            continue
                        seen.add(ts)
                        w.writerow([ts, r["open"], r["high"], r["low"], r["close"], r["volume"]])
                log(f"  FUT {name}: {len(seen)} minute bars → {p} "
                    f"({front['tradingsymbol']}, exp {front['expiry']})")
        except Exception as e:
            log(f"  FUT {name}: FAILED {type(e).__name__}: {str(e)[:80]}")

    # ── 2b. NSE participant-wise OI (yesterday's institutional book — public, no Kite auth) ──
    try:
        import subprocess
        poi_dir = Path("data/source/participant_oi")
        poi_dir.mkdir(parents=True, exist_ok=True)
        d = today - timedelta(days=1)
        tries = 0
        while d.weekday() >= 5 and tries < 4:      # roll back to last weekday
            d -= timedelta(days=1)
            tries += 1
        p = poi_dir / f"{d.isoformat()}.csv"
        if not p.exists():
            url = (f"https://nsearchives.nseindia.com/content/nsccl/"
                   f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv")
            r = subprocess.run(["curl", "-sL", "--max-time", "20", "-A",
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", url],
                               capture_output=True, timeout=25)
            if r.stdout and b"Client Type" in r.stdout:
                p.write_bytes(r.stdout)
        log(f"  PARTICIPANT-OI: {p.name} {'✓' if p.exists() else 'unavailable'}")
    except Exception as e:
        log(f"  PARTICIPANT-OI: FAILED {type(e).__name__}")

    # ── 3. India VIX daily (vol context) ──────────────────────────────────────
    try:
        vix = a.kite.historical_data(264969, from_date=(today - timedelta(days=120)).isoformat(),
                                     to_date=today.isoformat(), interval="day") or []
        p = OUT / "indiavix_daily_120d.csv"
        with p.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close"])
            for r in vix:
                w.writerow([str(r["date"])[:10], r["open"], r["high"], r["low"], r["close"]])
        log(f"  VIX: {len(vix)} daily bars → {p}")
    except Exception as e:
        log(f"  VIX: FAILED {type(e).__name__}: {str(e)[:80]}")

    LOG.write_text("\n".join(_lines) + "\n")
    log("[pull] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
