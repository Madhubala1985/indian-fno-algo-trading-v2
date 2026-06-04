#!/usr/bin/env python3
"""
fetch_index_history.py

Fetch 1-minute OHLCV history for BANKNIFTY, NIFTY, or SENSEX from the
Zerodha Kite historical API and write a CSV to data/.

Usage:
    PYTHONPATH=. python3 scripts/fetch_index_history.py \
        --symbol NIFTY --from 2026-04-28 --to 2026-05-29 --interval minute

Output:
    data/{symbol_lower}_1m_{from}_{to}.csv

Exits with code 1 and prints TOKEN_INVALID if the Kite session is stale.
Does NOT trade, modify DB/state, start WebSocket, or write kite_config.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

# ── Instrument tokens (NSE/BSE indexes) ──────────────────────────────────────
INSTRUMENT_TOKENS: dict[str, int] = {
    "BANKNIFTY": 260105,
    "NIFTY":     256265,   # NIFTY50 feed token
    "SENSEX":    265,
}

CONFIG_PATH = Path("configs/kite_config.json")
DATA_DIR    = Path("data")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch index OHLCV history from Kite")
    p.add_argument("--symbol",   required=True, choices=list(INSTRUMENT_TOKENS),
                   help="Index symbol: BANKNIFTY | NIFTY | SENSEX")
    p.add_argument("--from",     dest="from_date", required=True,
                   metavar="YYYY-MM-DD", help="Start date (inclusive)")
    p.add_argument("--to",       dest="to_date",   required=True,
                   metavar="YYYY-MM-DD", help="End date (inclusive)")
    p.add_argument("--interval", default="minute",
                   choices=["minute", "3minute", "5minute", "10minute",
                            "15minute", "30minute", "60minute", "day"],
                   help="Candle interval (default: minute)")
    return p.parse_args()


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(1)
    with CONFIG_PATH.open() as f:
        return json.load(f)


def _output_path(symbol: str, from_date: str, to_date: str) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / f"{symbol.lower()}_1m_{from_date}_{to_date}.csv"


def main() -> int:
    args = _parse_args()

    try:
        from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
        to_dt   = datetime.strptime(args.to_date,   "%Y-%m-%d")
    except ValueError as exc:
        print(f"ERROR: bad date format — {exc}", file=sys.stderr)
        return 1

    config = _load_config()
    api_key      = config.get("api_key", "")
    access_token = config.get("access_token", "")
    if not api_key or not access_token:
        print("TOKEN_INVALID: api_key or access_token missing in kite_config.json",
              file=sys.stderr)
        return 1

    try:
        from platform_core.data.historical.zerodha_historical_fetcher import (
            ZerodhaHistoricalFetcher,
        )
    except ImportError as exc:
        print(f"ERROR: cannot import ZerodhaHistoricalFetcher — {exc}", file=sys.stderr)
        return 1

    token   = INSTRUMENT_TOKENS[args.symbol]
    fetcher = ZerodhaHistoricalFetcher(api_key=api_key, access_token=access_token)

    print(f"Fetching {args.symbol} (token={token}) "
          f"{args.from_date} → {args.to_date} [{args.interval}] …")

    try:
        candles = fetcher.fetch_ohlc(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval=args.interval,
        )
    except Exception as exc:
        msg = str(exc)
        if any(k in msg.lower() for k in ("invalid token", "token expired",
                                           "access_token", "403", "401",
                                           "tokenexception")):
            print(f"TOKEN_INVALID: {msg}", file=sys.stderr)
            return 1
        print(f"ERROR fetching data: {msg}", file=sys.stderr)
        return 1

    if not candles:
        print("ERROR: API returned zero candles — check date range or market calendar",
              file=sys.stderr)
        return 1

    out = _output_path(args.symbol, args.from_date, args.to_date)
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            ts = c["date"]
            writer.writerow([
                ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                c["open"], c["high"], c["low"], c["close"], c["volume"],
            ])

    print(f"Saved {len(candles):,} rows → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
