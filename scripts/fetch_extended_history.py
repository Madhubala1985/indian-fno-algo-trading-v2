"""
scripts/fetch_extended_history.py
──────────────────────────────────────────────────────────────────────
Fetch multi-month BANKNIFTY 1-minute historical data from Kite Connect.

Kite enforces a 60-calendar-day limit per minute-interval request.
This script splits any date range into 60-day chunks, fetches each,
deduplicates, filters to NSE trading hours, and writes:

  data/banknifty_1m_<YYYY-MM-DD>_<YYYY-MM-DD>.csv   ← single large file
  data/monthly/<YYYY-MM>.csv                          ← per-month files

Both formats are compatible with existing BacktestRunner.run() and
BatchBacktester.run_directory().  No strategy logic is touched.

Usage:
  python scripts/fetch_extended_history.py
  python scripts/fetch_extended_history.py --from 2024-06-01 --to 2025-05-31
  python scripts/fetch_extended_history.py --months 3
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: pykiteconnect not installed — pip install kiteconnect")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

BANKNIFTY_TOKEN = 260105
IST = timezone(timedelta(hours=5, minutes=30))

# NSE session: 09:15–15:29 inclusive (1-minute bars on open of each minute)
SESSION_START_MINUTE = 9 * 60 + 15   # 555
SESSION_END_MINUTE   = 15 * 60 + 29  # 929

# Kite hard limit per minute-interval request
CHUNK_DAYS = 59   # stay under 60-day limit with a 1-day safety margin

CSV_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_kite() -> KiteConnect:
    config_path = Path(__file__).parent.parent / "configs" / "kite_config.json"
    with open(config_path) as f:
        cfg = json.load(f)
    kite = KiteConnect(api_key=cfg["api_key"])
    kite.set_access_token(cfg["access_token"])
    return kite


def _date_chunks(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """Split [from_date, to_date] into CHUNK_DAYS-wide windows."""
    chunks = []
    cursor = from_date
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), to_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _in_session(dt: datetime) -> bool:
    ist = dt.astimezone(IST)
    tod = ist.hour * 60 + ist.minute
    return SESSION_START_MINUTE <= tod <= SESSION_END_MINUTE


def _candle_to_row(candle: dict) -> list:
    return [
        candle["date"].isoformat(),
        candle["open"],
        candle["high"],
        candle["low"],
        candle["close"],
        candle["volume"],
    ]


def _month_key(dt: datetime) -> str:
    ist = dt.astimezone(IST)
    return f"{ist.year:04d}-{ist.month:02d}"


# ── Core fetch ─────────────────────────────────────────────────────────────────

def fetch_range(
    kite: KiteConnect,
    from_date: date,
    to_date: date,
    token: int = BANKNIFTY_TOKEN,
    delay_seconds: float = 0.35,
) -> list[dict]:
    """
    Fetch all 1-minute candles between from_date and to_date,
    chunking into CHUNK_DAYS windows to stay within Kite limits.
    """
    chunks = _date_chunks(from_date, to_date)
    all_candles: list[dict] = []
    seen_timestamps: set[str] = set()

    print(f"\nFetching {token} from {from_date} to {to_date}")
    print(f"  Chunks: {len(chunks)}  (each up to {CHUNK_DAYS} days)")

    for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
        print(f"  [{i:2d}/{len(chunks)}] {chunk_start} → {chunk_end} ... ", end="", flush=True)
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=datetime(chunk_start.year, chunk_start.month, chunk_start.day),
                to_date=datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59),
                interval="minute",
                oi=0,
            )
        except Exception as exc:
            print(f"ERROR: {exc}")
            time.sleep(2.0)
            continue

        before = len(all_candles)
        for c in candles:
            if not _in_session(c["date"]):
                continue
            ts = c["date"].isoformat()
            if ts in seen_timestamps:
                continue
            seen_timestamps.add(ts)
            all_candles.append(c)

        added = len(all_candles) - before
        print(f"{len(candles):5d} raw → {added:5d} session bars  total={len(all_candles):6d}")
        if i < len(chunks):
            time.sleep(delay_seconds)

    all_candles.sort(key=lambda c: c["date"])
    return all_candles


# ── Save ───────────────────────────────────────────────────────────────────────

def save_single_csv(candles: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for c in candles:
            w.writerow(_candle_to_row(c))
    print(f"\nSaved {len(candles):,} bars → {out_path}")


def save_monthly_csvs(candles: list[dict], monthly_dir: Path) -> dict[str, int]:
    monthly_dir.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = defaultdict(list)
    for c in candles:
        by_month[_month_key(c["date"])].append(c)

    counts = {}
    for month_key in sorted(by_month.keys()):
        rows = by_month[month_key]
        out_path = monthly_dir / f"{month_key}.csv"
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for c in rows:
                w.writerow(_candle_to_row(c))
        counts[month_key] = len(rows)
        print(f"  {month_key}: {len(rows):5d} bars → {out_path.name}")
    return counts


# ── Statistics ────────────────────────────────────────────────────────────────

def _report_stats(candles: list[dict], label: str) -> None:
    if not candles:
        print(f"\n{label}: no data")
        return
    dates = sorted({c["date"].astimezone(IST).date() for c in candles})
    print(f"\n{'─'*60}")
    print(f"  Dataset: {label}")
    print(f"  Bars:          {len(candles):,}")
    print(f"  Trading days:  {len(dates)}")
    print(f"  First bar:     {candles[0]['date'].isoformat()}")
    print(f"  Last bar:      {candles[-1]['date'].isoformat()}")
    print(f"  Date range:    {dates[0]} → {dates[-1]}")
    trade_rate = 4.0 / 3.0  # measured from 3-day sample
    est_trades = int(len(dates) * trade_rate)
    print(f"  Est. trades:   ~{est_trades}  (at {trade_rate:.2f} trades/day from 3-day baseline)")
    print(f"{'─'*60}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    today = date.today()
    default_to = today - timedelta(days=1)
    default_from = default_to - timedelta(days=365)

    p = argparse.ArgumentParser(description="Fetch BANKNIFTY 1m history from Kite")
    p.add_argument("--from", dest="from_date", default=str(default_from),
                   help="Start date YYYY-MM-DD  (default: 1 year ago)")
    p.add_argument("--to", dest="to_date", default=str(default_to),
                   help="End date YYYY-MM-DD    (default: yesterday)")
    p.add_argument("--months", type=int, default=0,
                   help="Shortcut: fetch last N months (overrides --from/--to)")
    p.add_argument("--symbol", default="BANKNIFTY",
                   help="Symbol name (used for filenames)")
    p.add_argument("--token", type=int, default=BANKNIFTY_TOKEN,
                   help="Instrument token")
    p.add_argument("--no-monthly", action="store_true",
                   help="Skip per-month CSV output")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    to_date   = date.fromisoformat(args.to_date)
    if args.months > 0:
        from_date = to_date - timedelta(days=args.months * 31)
    else:
        from_date = date.fromisoformat(args.from_date)

    kite = _load_kite()

    # Verify session
    try:
        profile = kite.profile()
        print(f"Kite session: {profile.get('user_name', 'ok')}")
    except Exception as e:
        print(f"Kite session invalid: {e}")
        sys.exit(1)

    candles = fetch_range(kite, from_date, to_date, token=args.token)

    if not candles:
        print("No data returned.")
        sys.exit(1)

    _report_stats(candles, f"{args.symbol} {from_date}→{to_date}")

    data_dir = Path(__file__).parent.parent / "data"
    symbol_lc = args.symbol.lower()

    # Single large CSV
    single_path = data_dir / f"{symbol_lc}_1m_{from_date}_{to_date}.csv"
    save_single_csv(candles, single_path)

    # Per-month CSVs
    if not args.no_monthly:
        monthly_dir = data_dir / "monthly"
        print(f"\nPer-month files → {monthly_dir}/")
        save_monthly_csvs(candles, monthly_dir)

    print("\nDone.")


if __name__ == "__main__":
    main()
