from __future__ import annotations

import csv
from datetime import datetime

from platform_core.data.bar_builder import (
    LiveBar,
)


class HistoricalReplayFeed:

    def load_csv(
        self,
        *,
        path: str,
        symbol: str,
        timeframe: str = "MIN_1",
    ) -> list[LiveBar]:

        bars = []

        with open(path, "r") as f:

            reader = csv.DictReader(f)

            for row in reader:

                timestamp = datetime.fromisoformat(
                    row["timestamp"]
                )

                bars.append(
                    LiveBar(
                        symbol=symbol,

                        timeframe=timeframe,

                        start_timestamp=timestamp,

                        end_timestamp=timestamp,

                        open_price=float(
                            row["open"]
                        ),

                        high_price=float(
                            row["high"]
                        ),

                        low_price=float(
                            row["low"]
                        ),

                        close_price=float(
                            row["close"]
                        ),

                        volume=float(
                            row["volume"]
                        ),
                    )
                )

        return bars

