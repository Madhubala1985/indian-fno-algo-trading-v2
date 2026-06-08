"""
refresh_mcb_warmstart_data.py
──────────────────────────────────────────────────────────────────────
Fetch recent 1-minute OHLCV for BANKNIFTY / NIFTY / SENSEX and save to
data/ so MCB warm-start uses fresh context on the next live session.

Default: last 10 trading days up to the most recent completed session.

Kite API notes:
  - historical_data expects IST strings (no UTC conversion).
  - 1-minute interval supports up to 60-day windows per call.
  - Access token expires daily; regenerate with generate_kite_session.py.

Output files:
  data/banknifty_1m_<start>_<end>.csv
  data/nifty_1m_<start>_<end>.csv
  data/sensex_1m_<start>_<end>.csv

Do NOT stage these files in git.

Usage:
  PYTHONPATH=. python3 scripts/refresh_mcb_warmstart_data.py
  PYTHONPATH=. python3 scripts/refresh_mcb_warmstart_data.py --days 10
  PYTHONPATH=. python3 scripts/refresh_mcb_warmstart_data.py --symbols BANKNIFTY
  PYTHONPATH=. python3 scripts/refresh_mcb_warmstart_data.py --overwrite
  PYTHONPATH=. python3 scripts/refresh_mcb_warmstart_data.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path("configs/kite_config.json")
DATA_DIR    = Path("data")

_INSTRUMENTS: dict[str, int] = {
    "BANKNIFTY": 260105,
    "NIFTY":     256265,
    "SENSEX":    265,
}

_IST = timezone(timedelta(hours=5, minutes=30))

# 1-min bars per trading session (09:15 – 15:29 inclusive = 375 bars)
_BARS_PER_DAY  = 375
_MIN_BARS_WARN = 300   # warn below this per day in the fetched range


def _latest_completed_session() -> date:
    """Return the most recent date where the full trading session has closed."""
    now = datetime.now(_IST)
    mclose = now.replace(hour=15, minute=30, second=0, microsecond=0)
    end = now.date() if now >= mclose else now.date() - timedelta(days=1)
    while end.weekday() >= 5:   # skip Saturday=5, Sunday=6
        end -= timedelta(days=1)
    return end


def _n_trading_days_before(ref: date, n: int) -> date:
    """Return the trading date N trading days before ref (weekdays only)."""
    d = ref
    counted = 0
    while counted < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            counted += 1
    return d


def _parse_cfg() -> tuple[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg["api_key"], cfg.get("access_token", "")


def _ts_str(candle: dict) -> str:
    ts = candle.get("date", "")
    if hasattr(ts, "isoformat"):
        return ts.isoformat()[:19]
    return str(ts)[:19]


def _save_csv(sym: str, candles: list[dict], start: date, end: date) -> Path:
    out = DATA_DIR / f"{sym.lower()}_1m_{start}_{end}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            ts = c["date"]
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            writer.writerow([ts, c["open"], c["high"], c["low"], c["close"], c["volume"]])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh MCB warm-start OHLCV data from Kite")
    parser.add_argument("--days",      type=int, default=25,
                        help="Number of trading days to fetch (default: 25; need ≥22 for 4h/1d MCB readiness)")
    parser.add_argument("--symbols",   default="BANKNIFTY,NIFTY,SENSEX",
                        help="Comma-separated symbols")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing file if already present for same date range")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print what would be done without fetching or writing")
    args = parser.parse_args()

    symbols    = [s.strip().upper() for s in args.symbols.split(",")]
    end_date   = _latest_completed_session()
    start_date = _n_trading_days_before(end_date, args.days - 1)

    print(f"[REFRESH] Date range : {start_date} → {end_date}  ({args.days} trading days)")
    print(f"[REFRESH] Symbols    : {', '.join(symbols)}")
    print(f"[REFRESH] Output dir : {DATA_DIR}/")
    if args.dry_run:
        print("[REFRESH] DRY-RUN — no files will be fetched or written")
        for sym in symbols:
            out = DATA_DIR / f"{sym.lower()}_1m_{start_date}_{end_date}.csv"
            print(f"[DRY-RUN] {sym}: would write {out}")
        return 0

    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError:
        print("[ERROR] pykiteconnect not installed — pip install kiteconnect")
        return 1

    if not CONFIG_PATH.exists():
        print(f"[ERROR] {CONFIG_PATH} not found")
        return 1

    api_key, access_token = _parse_cfg()
    if not access_token:
        print("[ERROR] No access_token in configs/kite_config.json")
        print("        Run: PYTHONPATH=. python3 scripts/generate_kite_session.py")
        return 1

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    any_error = False
    for sym in symbols:
        token = _INSTRUMENTS.get(sym)
        if token is None:
            print(f"[SKIP] {sym}: no instrument token configured")
            continue

        out_path = DATA_DIR / f"{sym.lower()}_1m_{start_date}_{end_date}.csv"
        if out_path.exists() and not args.overwrite:
            print(f"[SKIP] {sym}: {out_path.name} already exists (pass --overwrite to replace)")
            continue

        from_str = f"{start_date} 09:15:00"
        to_str   = f"{end_date} 15:30:00"

        print(f"[FETCH] {sym} (token={token})  {from_str} → {to_str} ...", end=" ", flush=True)
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=from_str,
                to_date=to_str,
                interval="minute",
                continuous=False,
            )
        except Exception as exc:
            print(f"FAILED: {exc}")
            any_error = True
            continue

        if not candles:
            print(f"EMPTY — no bars returned")
            any_error = True
            continue

        n          = len(candles)
        first_ts   = _ts_str(candles[0])
        last_ts    = _ts_str(candles[-1])
        expected   = args.days * _BARS_PER_DAY

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _save_csv(sym, candles, start_date, end_date)

        status = "OK"
        if n < args.days * _MIN_BARS_WARN:
            status = "WARN"
            any_error = True

        print(f"{status}  {n} bars  [{first_ts} → {last_ts}]  (expected ~{expected})  → {out_path}")
        if status == "WARN":
            print(f"  !! Only {n} bars — expected ~{expected}. Check for holidays or API limits.")

    if any_error:
        print("\n[WARN] One or more symbols had issues. Check logs above.")
        return 1

    print(f"\n[DONE] Warm-start data refreshed. Restart live pipeline to pick up new files.")
    print(f"  Next live run will auto-select the freshest CSV in data/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
