"""
fetch_june8_ohlcv.py
──────────────────────────────────────────────────────────────────────
Fetch 2026-06-08 intraday OHLCV for BANKNIFTY / NIFTY / SENSEX
from Zerodha Kite and save to data/ for backtest replay.

After running this script, use:
  PYTHONPATH=. python3 scripts/run_backtest.py   (single symbol)
  PYTHONPATH=. python3 scripts/run_batch_backtest.py  (all CSVs)

Read-only. No orders placed.

Usage:
  PYTHONPATH=. python3 scripts/fetch_june8_ohlcv.py
  PYTHONPATH=. python3 scripts/fetch_june8_ohlcv.py --date 2026-06-08
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path("configs/kite_config.json")
DATA_DIR    = Path("data")

# Kite instrument tokens (NSE index tokens for historical data)
_INSTRUMENTS = {
    "BANKNIFTY": 260105,   # NSE:NIFTY BANK
    "NIFTY":     256265,   # NSE:NIFTY 50
    "SENSEX":    265,      # BSE:SENSEX  (BFO token for BSE Sensex)
}

# Map to Kite exchange:tradingsymbol for quotes (for token lookup fallback)
_KITE_TRADINGSYMBOL = {
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "NIFTY":     ("NSE", "NIFTY 50"),
    "SENSEX":    ("BSE", "SENSEX"),
}


def _parse_cfg() -> tuple[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg["api_key"], cfg.get("access_token", "")


def _fetch_1m(kite, instrument_token: int, from_dt: datetime, to_dt: datetime) -> list[dict]:
    """Fetch 1-minute OHLCV candles for the given token and date range."""
    candles = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        interval="minute",
        continuous=False,
    )
    return candles


def _save_csv(sym: str, candles: list[dict], date_str: str) -> Path:
    out_path = DATA_DIR / f"{sym.lower()}_1m_{date_str}_{date_str}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            ts = c["date"]
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            writer.writerow([ts, c["open"], c["high"], c["low"], c["close"], c["volume"]])
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch intraday OHLCV for backtest")
    parser.add_argument("--date", default="2026-06-08",
                        help="Trading date to fetch (YYYY-MM-DD)")
    parser.add_argument("--symbols", default="BANKNIFTY,NIFTY,SENSEX",
                        help="Comma-separated symbols")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date)
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError:
        print("[ERROR] pykiteconnect not installed.")
        print("        Install: pip install kiteconnect")
        return

    api_key, access_token = _parse_cfg()
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Market session: 9:15 to 15:30 IST (UTC+5:30 = UTC +0:30 offset for IST)
    ist_offset = timedelta(hours=5, minutes=30)

    from_dt = datetime(target_date.year, target_date.month, target_date.day,
                       9, 15, 0) - ist_offset  # to UTC
    to_dt   = datetime(target_date.year, target_date.month, target_date.day,
                       15, 30, 0) - ist_offset

    date_str = target_date.isoformat()
    print(f"[FETCH] Date: {date_str}  "
          f"Window: {from_dt.strftime('%H:%M')} – {to_dt.strftime('%H:%M')} UTC")

    for sym in symbols:
        token = _INSTRUMENTS.get(sym)
        if token is None:
            print(f"[SKIP] {sym}: no token configured")
            continue
        print(f"[FETCH] {sym} (token={token}) ...", end=" ", flush=True)
        try:
            candles = _fetch_1m(kite, token, from_dt, to_dt)
            out = _save_csv(sym, candles, date_str)
            print(f"OK  {len(candles)} bars → {out}")
        except Exception as exc:
            print(f"FAILED: {exc}")

    print("\n[DONE] Run backtest:")
    print("  PYTHONPATH=. python3 scripts/run_backtest.py")
    print("  # or for all CSVs:")
    print("  PYTHONPATH=. python3 scripts/run_batch_backtest.py")


if __name__ == "__main__":
    main()
