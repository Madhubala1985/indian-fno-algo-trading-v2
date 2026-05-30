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

    def log_trade(
        self,
        *,
        trade: PaperTrade,
    ) -> None:

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

