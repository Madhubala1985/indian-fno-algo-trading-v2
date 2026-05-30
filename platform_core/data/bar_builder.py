from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta


@dataclass(slots=True)
class LiveBar:

    symbol: str

    timeframe: str

    start_timestamp: datetime

    end_timestamp: datetime

    open_price: float

    high_price: float

    low_price: float

    close_price: float

    volume: float


class BarBuilder:

    def __init__(
        self,
        *,
        timeframe_seconds: int = 60,
    ) -> None:

        self._timeframe_seconds = (
            timeframe_seconds
        )

        self._active_bars: dict[
            str,
            LiveBar,
        ] = {}

    def update_tick(
        self,
        *,
        tick,
    ) -> LiveBar | None:

        symbol = tick.symbol

        timestamp = (
            tick.exchange_timestamp
        )

        bucket_start = timestamp.replace(
            second=0,
            microsecond=0,
        )

        bucket_end = (
            bucket_start
            + timedelta(
                seconds=self._timeframe_seconds
            )
        )

        existing_bar = (
            self._active_bars.get(symbol)
        )

        if existing_bar is None:

            self._active_bars[symbol] = (
                LiveBar(
                    symbol=symbol,
                    timeframe="MIN_1",
                    start_timestamp=bucket_start,
                    end_timestamp=bucket_end,
                    open_price=tick.last_price,
                    high_price=tick.last_price,
                    low_price=tick.last_price,
                    close_price=tick.last_price,
                    volume=tick.volume or 0,
                )
            )

            return None

        if (
            timestamp
            >= existing_bar.end_timestamp
        ):

            completed_bar = existing_bar

            self._active_bars[symbol] = (
                LiveBar(
                    symbol=symbol,
                    timeframe="MIN_1",
                    start_timestamp=bucket_start,
                    end_timestamp=bucket_end,
                    open_price=tick.last_price,
                    high_price=tick.last_price,
                    low_price=tick.last_price,
                    close_price=tick.last_price,
                    volume=tick.volume or 0,
                )
            )

            return completed_bar

        existing_bar.high_price = max(
            existing_bar.high_price,
            tick.last_price,
        )

        existing_bar.low_price = min(
            existing_bar.low_price,
            tick.last_price,
        )

        existing_bar.close_price = (
            tick.last_price
        )

        existing_bar.volume = (
            tick.volume or 0
        )

        return None
