from __future__ import annotations

import csv
from pathlib import Path

from platform_core.paper_trading.paper_broker import (
    PaperTrade,
)


LOG_PATH = (
    "logs/paper_trades.csv"
)


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

        # Deduplication guard: tracks the key of the last trade written in this
        # process run.  If log_trade() is called twice with identical fields the
        # second call is suppressed and a warning is printed.  Resets to None on
        # each new process start, so trades from distinct sessions are unaffected.
        self._last_logged_key: tuple | None = None

    def log_trade(
        self,
        *,
        trade: PaperTrade,
    ) -> None:

        key = (
            trade.symbol,
            trade.direction,
            trade.entry_price,
            trade.exit_price,
            trade.quantity,
        )

        if key == self._last_logged_key:
            print(
                f"\n[LOGGER] Duplicate suppressed — trade already logged this session: "
                f"{trade.symbol} {trade.direction} entry={trade.entry_price} "
                f"exit={trade.exit_price}"
            )
            return

        self._last_logged_key = key

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

        print(
            "\n[LOGGER] Trade persisted."
        )

