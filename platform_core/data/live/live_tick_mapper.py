from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone


@dataclass(slots=True)
class LiveTick:

    instrument_token: int

    symbol: str

    last_price: float

    last_quantity: float | None

    average_price: float | None

    volume: float | None

    buy_quantity: float | None

    sell_quantity: float | None

    open_price: float | None

    high_price: float | None

    low_price: float | None

    close_price: float | None

    exchange_timestamp: datetime | None


class LiveTickMapper:

    def __init__(
        self,
        *,
        token_to_symbol: dict[int, str],
    ) -> None:

        self._token_to_symbol = (
            token_to_symbol
        )

    def map_tick(
        self,
        raw_tick: dict,
    ) -> LiveTick:

        token = raw_tick[
            "instrument_token"
        ]

        return LiveTick(
            instrument_token=token,

            symbol=self._token_to_symbol.get(
                token,
                str(token),
            ),

            last_price=float(
                raw_tick.get(
                    "last_price",
                    0.0,
                )
            ),

            last_quantity=raw_tick.get(
                "last_quantity"
            ),

            average_price=raw_tick.get(
                "average_price"
            ),

            volume=raw_tick.get(
                "volume_traded"
            ),

            buy_quantity=raw_tick.get(
                "total_buy_quantity"
            ),

            sell_quantity=raw_tick.get(
                "total_sell_quantity"
            ),

            open_price=(
                raw_tick.get("ohlc", {})
                .get("open")
            ),

            high_price=(
                raw_tick.get("ohlc", {})
                .get("high")
            ),

            low_price=(
                raw_tick.get("ohlc", {})
                .get("low")
            ),

            close_price=(
                raw_tick.get("ohlc", {})
                .get("close")
            ),

            exchange_timestamp=(
                raw_tick.get(
                    "exchange_timestamp",
                    datetime.now(
                        timezone.utc
                    ),
                )
            ),
        )
