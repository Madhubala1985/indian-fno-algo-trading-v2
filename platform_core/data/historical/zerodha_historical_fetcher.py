
from __future__ import annotations

from datetime import datetime

from kiteconnect import KiteConnect


class ZerodhaHistoricalFetcher:

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
    ) -> None:

        self.kite = KiteConnect(
            api_key=api_key
        )

        self.kite.set_access_token(
            access_token
        )

    def fetch_ohlc(

        self,

        *,

        instrument_token: int,

        from_date: datetime,

        to_date: datetime,

        interval: str = "minute",

    ):

        data = (
            self.kite.historical_data(
                instrument_token=(
                    instrument_token
                ),

                from_date=from_date,

                to_date=to_date,

                interval=interval,
            )
        )

        return data

