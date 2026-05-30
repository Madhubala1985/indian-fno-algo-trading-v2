from __future__ import annotations

from platform_core.analytics.structure.models import (
    StructureBar,
)

from platform_core.data.bar_builder import (
    LiveBar,
)


class LiveStructureAdapter:

    def __init__(self) -> None:

        self._sequence = 0

    def convert_bar(
        self,
        *,
        live_bar: LiveBar,
    ) -> StructureBar:

        self._sequence += 1

        return StructureBar(
            instrument_id=live_bar.symbol,

            timeframe=live_bar.timeframe,

            bar_timestamp=(
                live_bar.end_timestamp
            ),

            open_price=(
                live_bar.open_price
            ),

            high_price=(
                live_bar.high_price
            ),

            low_price=(
                live_bar.low_price
            ),

            close_price=(
                live_bar.close_price
            ),

            volume=live_bar.volume,

            trade_count=None,

            open_interest=None,

            vwap=None,

            event_sequence_number=(
                self._sequence
            ),
        )
