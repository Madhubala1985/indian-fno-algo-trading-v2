"""
fetch_june8_ohlcv.py
──────────────────────────────────────────────────────────────────────
Fetch a full intraday OHLCV day for BANKNIFTY / NIFTY / SENSEX
from Zerodha Kite and save to data/ for backtest replay.

Root cause of previous 45-bar issue: times were incorrectly converted
to UTC before being passed to the Kite API. Kite historical_data expects
dates in IST (exchange time), not UTC. Fixed: IST times are sent as-is.

After running this script, use:
  PYTHONPATH=. python3 scripts/run_backtest.py   (single symbol)
  PYTHONPATH=. python3 scripts/run_batch_backtest.py  (all CSVs)

Read-only. No orders placed.

Usage:
  PYTHONPATH=. python3 scripts/fetch_june8_ohlcv.py
  PYTHONPATH=. python3 scripts/fetch_june8_ohlcv.py --date 2026-06-08
  PYTHONPATH=. python3 scripts/fetch_june8_ohlcv.py --date 2026-06-08 --symbols BANKNIFTY
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

CONFIG_PATH = Path("configs/kite_config.json")
DATA_DIR    = Path("data")

# Kite instrument tokens
_INSTRUMENTS = {
    "BANKNIFTY": 260105,   # NSE:NIFTY BANK
    "NIFTY":     256265,   # NSE:NIFTY 50
    "SENSEX":    265,      # BSE:SENSEX
}

# Full NSE/BSE trading day: 09:15 to 15:30 IST = 375 1-minute bars
_FULL_SESSION_BARS_MIN = 300   # fail loudly if fewer than this (allows for holidays/early close)
_FULL_SESSION_BARS_EXPECTED = 375


def _parse_cfg() -> tuple[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text())
    return cfg["api_key"], cfg.get("access_token", "")


def _fetch_1m(kite, instrument_token: int, target_date: date) -> list[dict]:
    """
    Fetch 1-minute OHLCV candles for the full trading day.

    Kite historical_data expects from_date / to_date in IST (exchange time)
    as 'YYYY-MM-DD HH:MM:SS' strings. Do NOT convert to UTC — that was the
    root cause of the previous 45-bar truncation.
    """
    from_str = f"{target_date} 09:15:00"   # IST — passed directly to Kite
    to_str   = f"{target_date} 15:30:00"   # IST — passed directly to Kite
    candles = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_str,
        to_date=to_str,
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


def _ts_str(candle: dict) -> str:
    ts = candle.get("date", "")
    if hasattr(ts, "isoformat"):
        return ts.isoformat()[:19]
    return str(ts)[:19]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch full-day intraday OHLCV for backtest")
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
        print("[ERROR] pykiteconnect not installed — pip install kiteconnect")
        return 1

    api_key, access_token = _parse_cfg()
    if not access_token:
        print("[ERROR] No access_token in configs/kite_config.json")
        print("        Run: PYTHONPATH=. python3 scripts/generate_kite_session.py")
        return 1

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    date_str = target_date.isoformat()
    print(f"[FETCH] Date: {date_str}  Session: 09:15–15:30 IST  Expected: ~{_FULL_SESSION_BARS_EXPECTED} bars")
    print(f"[NOTE]  Kite API receives IST times directly (no UTC conversion)")

    any_partial = False
    for sym in symbols:
        token = _INSTRUMENTS.get(sym)
        if token is None:
            print(f"[SKIP] {sym}: no token configured")
            continue
        print(f"[FETCH] {sym} (token={token}) ...", end=" ", flush=True)
        try:
            candles = _fetch_1m(kite, token, target_date)
        except Exception as exc:
            print(f"FAILED: {exc}")
            any_partial = True
            continue

        if not candles:
            print(f"EMPTY — no bars returned (check date: is {date_str} a trading day?)")
            any_partial = True
            continue

        n = len(candles)
        first_ts = _ts_str(candles[0])
        last_ts  = _ts_str(candles[-1])
        out      = _save_csv(sym, candles, date_str)

        status = "OK" if n >= _FULL_SESSION_BARS_MIN else "PARTIAL"
        if status == "PARTIAL":
            any_partial = True
        print(f"{status}  {n} bars  [{first_ts} → {last_ts}]  → {out}")

        if n < _FULL_SESSION_BARS_MIN:
            print(f"  !! WARNING: only {n} bars fetched (expected ~{_FULL_SESSION_BARS_EXPECTED})")
            print(f"  !! Possible causes: access token expired, non-trading day, API limit")
            print(f"  !! Regenerate token: PYTHONPATH=. python3 scripts/generate_kite_session.py")

    if any_partial:
        print("\n[WARN] One or more symbols returned partial data. Do not run replay.")
        print("       Regenerate access token and re-fetch before running:")
        print("       PYTHONPATH=. python3 scripts/replay_gate_calibration_20260608.py")
        return 1

    print(f"\n[DONE] Full-day data ready. Run replay:")
    print(f"  PYTHONPATH=. python3 scripts/replay_gate_calibration_20260608.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
