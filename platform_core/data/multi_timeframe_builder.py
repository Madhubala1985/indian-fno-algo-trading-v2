from __future__ import annotations

from collections import defaultdict

from platform_core.data.bar_builder import (
    LiveBar,
)


class MultiTimeframeBuilder:

    def __init__(self) -> None:

        self._buffers = defaultdict(list)

    def update_bar(
        self,
        *,
        bar: LiveBar,
    ) -> dict[str, LiveBar]:

        outputs = {}

        self._buffers[
            (bar.symbol, "MIN_5")
        ].append(bar)

        self._buffers[
            (bar.symbol, "MIN_15")
        ].append(bar)

        self._buffers[
            (bar.symbol, "HOUR_1")
        ].append(bar)

        self._buffers[
            (bar.symbol, "HOUR_4")
        ].append(bar)

        self._build(
            outputs=outputs,
            symbol=bar.symbol,
            timeframe="MIN_5",
            count=5,
        )

        self._build(
            outputs=outputs,
            symbol=bar.symbol,
            timeframe="MIN_15",
            count=15,
        )

        self._build(
            outputs=outputs,
            symbol=bar.symbol,
            timeframe="HOUR_1",
            count=60,
        )

        self._build(
            outputs=outputs,
            symbol=bar.symbol,
            timeframe="HOUR_4",
            count=240,
        )

        return outputs

    def _build(
        self,
        *,
        outputs,
        symbol,
        timeframe,
        count,
    ):

        buffer = self._buffers[
            (symbol, timeframe)
        ]

        if len(buffer) < count:
            return

        bars = buffer[:count]

        del buffer[:count]

        outputs[timeframe] = LiveBar(
            symbol=symbol,

            timeframe=timeframe,

            start_timestamp=(
                bars[0].start_timestamp
            ),

            end_timestamp=(
                bars[-1].end_timestamp
            ),

            open_price=(
                bars[0].open_price
            ),

            high_price=max(
                bar.high_price
                for bar in bars
            ),

            low_price=min(
                bar.low_price
                for bar in bars
            ),

            close_price=(
                bars[-1].close_price
            ),

            volume=sum(
                bar.volume
                for bar in bars
            ),
        )
