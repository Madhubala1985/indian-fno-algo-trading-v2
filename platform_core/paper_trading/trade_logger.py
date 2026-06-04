from __future__ import annotations

import csv
from pathlib import Path

from platform_core.paper_trading.paper_broker import (
    PaperTrade,
)
from platform_core.paper_trading.terminal_display import (
    print_trade_logged,
    print_duplicate_suppressed,
)


LOG_PATH = (
    "logs/paper_trades.csv"
)


def _trade_key(symbol: str, direction: str, entry_price: float,
               exit_price: float, quantity: int) -> tuple:
    """Stable string-based key matching the CSV round-trip representation."""
    return (symbol, direction, str(entry_price), str(exit_price), str(quantity))


class TradeLogger:

    def __init__(self) -> None:

        Path("logs").mkdir(
            exist_ok=True
        )

        if not Path(LOG_PATH).exists():

            with open(
                LOG_PATH,
                "w",
                newline="",
            ) as f:

                writer = csv.writer(f)

                writer.writerow(
                    [
                        "symbol",
                        "direction",
                        "entry_price",
                        "exit_price",
                        "quantity",
                        "pnl",
                    ]
                )

        # Set-based deduplication: seeded from any rows already in the CSV so
        # duplicates are blocked across process restarts, not just within a single
        # run.  Using string representations of numeric fields matches the
        # CSV round-trip exactly and avoids float equality hazards.
        self._logged_keys: set[tuple] = set()
        self._seed_from_csv()

    def _seed_from_csv(self) -> None:
        """Populate _logged_keys from the existing CSV (if any)."""
        try:
            p = Path(LOG_PATH)
            if not p.exists():
                return
            with p.open(newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = _trade_key(
                        row.get("symbol", ""),
                        row.get("direction", ""),
                        float(row["entry_price"]) if row.get("entry_price") else 0.0,
                        float(row["exit_price"])  if row.get("exit_price")  else 0.0,
                        int(float(row["quantity"])) if row.get("quantity")  else 0,
                    )
                    self._logged_keys.add(key)
        except Exception:
            # Unreadable CSV — start with an empty set; in-process guard still works.
            pass

    def log_trade(
        self,
        *,
        trade: PaperTrade,
    ) -> None:

        key = _trade_key(
            trade.symbol,
            trade.direction,
            trade.entry_price,
            trade.exit_price,
            trade.quantity,
        )

        if key in self._logged_keys:
            print_duplicate_suppressed(
                symbol=trade.symbol,
                direction=trade.direction,
                entry=trade.entry_price,
                exit_price=trade.exit_price,
            )
            return

        self._logged_keys.add(key)

        with open(
            LOG_PATH,
            "a",
            newline="",
        ) as f:

            writer = csv.writer(f)

            writer.writerow(
                [
                    trade.symbol,
                    trade.direction,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.pnl,
                ]
            )

        print_trade_logged(
            symbol=trade.symbol,
            direction=trade.direction,
            entry=trade.entry_price,
            exit_price=trade.exit_price,
            pnl=trade.pnl,
        )

