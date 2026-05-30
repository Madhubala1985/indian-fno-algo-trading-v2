"""
scripts/record_option_chains.py
────────────────────────────────────────────────────────────────────────
Records live option-chain snapshots to CSV for historical replay in BacktestRunner.

Run this during active market hours with a valid Kite session.  Snapshots are
written to data/option_chains/{SYMBOL}/{expiry_date}.csv using the CSV_HEADER
defined in platform_core.data.historical.option_chain_feed.

Usage:
    python scripts/record_option_chains.py                          # defaults
    python scripts/record_option_chains.py --symbol NIFTY --interval 1
    python scripts/record_option_chains.py --symbol BANKNIFTY --interval 5

The resulting CSV files can be passed to BacktestRunner via:
    BacktestConfig(symbol="BANKNIFTY", chain_data_dir="data/option_chains")
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: pip install kiteconnect")
    sys.exit(1)

from platform_core.data.live.kite_adapter import KiteAdapter
from platform_core.data.synthetic_chain_builder import ExpiryProvider
from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.core.enums import UnderlyingFamily
from platform_core.data.historical.option_chain_feed import CSV_HEADER, snapshot_to_rows

_IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_OPEN  = (9, 15)
_SESSION_CLOSE = (15, 30)

_UNDERLYING_FAMILY = {
    "BANKNIFTY": UnderlyingFamily.BANKNIFTY,
    "NIFTY":     UnderlyingFamily.NIFTY,
    "FINNIFTY":  UnderlyingFamily.FINNIFTY,
    "SENSEX":    UnderlyingFamily.SENSEX,
}

_EXCHANGE = {
    "BANKNIFTY": "NFO",
    "NIFTY":     "NFO",
    "FINNIFTY":  "NFO",
    "SENSEX":    "BFO",
}


def _in_session() -> bool:
    now = datetime.now(_IST)
    h, m = now.hour, now.minute
    tod = h * 60 + m
    return (9 * 60 + 15) <= tod <= (15 * 60 + 30)


def _open_writer(out_dir: Path, expiry_str: str) -> tuple[csv.DictWriter, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{expiry_str}.csv"
    exists = path.exists()
    f = open(path, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
    if not exists:
        writer.writeheader()
    return writer, f


def main() -> None:
    p = argparse.ArgumentParser(description="Record live option chain snapshots")
    p.add_argument("--symbol",   default="BANKNIFTY")
    p.add_argument("--interval", type=int, default=1, help="minutes between snapshots")
    p.add_argument("--strikes",  type=int, default=10, help="strikes around ATM")
    p.add_argument("--out",      default="data/option_chains", help="output root dir")
    args = p.parse_args()

    symbol = args.symbol.upper()
    family = _UNDERLYING_FAMILY.get(symbol)
    if family is None:
        print(f"Unsupported symbol: {symbol}. Supported: {list(_UNDERLYING_FAMILY)}")
        sys.exit(1)

    lot_size   = REFERENCE_LOT_SIZES[family]
    strike_step = float(REFERENCE_STRIKE_STEPS[family])
    exchange   = _EXCHANGE[symbol]

    adapter = KiteAdapter()
    expiry_provider = ExpiryProvider(symbol)

    out_root = Path(args.out) / symbol
    writers: dict[str, tuple] = {}  # expiry_str → (writer, file)

    print(f"Recording {symbol} option chains every {args.interval}m → {out_root}/")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            if not _in_session():
                print("Outside session, waiting 60s...")
                time.sleep(60)
                continue

            now = datetime.now(_IST)
            today = now.date()

            # Determine active expiries
            try:
                weekly   = expiry_provider.get_weekly_expiry(today)
                biweekly = expiry_provider.get_biweekly_expiry(today)
            except Exception as e:
                print(f"Expiry compute error: {e}")
                time.sleep(args.interval * 60)
                continue

            expiries = list(dict.fromkeys([weekly, biweekly]))  # deduplicate

            for expiry in expiries:
                expiry_str = expiry.isoformat()
                try:
                    snap = adapter.fetch_option_chain(
                        underlying=symbol,
                        expiry_date=expiry,
                        spot_price=0.0,  # will be filled from ATM detection
                        lot_size=lot_size,
                        exchange=exchange,
                        strikes_around_atm=args.strikes,
                        strike_step=strike_step,
                    )
                except Exception as e:
                    print(f"  [{expiry_str}] fetch error: {e}")
                    continue

                rows = snapshot_to_rows(snap, lot_size)
                if expiry_str not in writers:
                    writers[expiry_str] = _open_writer(out_root, expiry_str)
                writer, _ = writers[expiry_str]
                writer.writerows(rows)
                writers[expiry_str][1].flush()  # flush to disk

                print(f"  [{now.strftime('%H:%M')}] {expiry_str}: {len(rows)} contracts recorded")

            time.sleep(args.interval * 60)

    except KeyboardInterrupt:
        print("\nStopping recorder.")
    finally:
        for _, f in writers.values():
            f.close()


if __name__ == "__main__":
    main()
