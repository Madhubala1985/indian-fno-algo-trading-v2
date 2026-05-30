
from datetime import datetime
import json
import csv

from platform_core.data.historical.zerodha_historical_fetcher import (
    ZerodhaHistoricalFetcher,
)

BANKNIFTY_TOKEN = 260105


with open(
    "configs/kite_config.json",
    "r",
) as f:

    config = json.load(f)


fetcher = ZerodhaHistoricalFetcher(

    api_key=config["api_key"],

    access_token=config["access_token"],
)


data = fetcher.fetch_ohlc(

    instrument_token=(
        BANKNIFTY_TOKEN
    ),

    from_date=datetime(
        2025,
        5,
        1,
    ),

    to_date=datetime(
        2025,
        5,
        7,
    ),

    interval="minute",
)


output_path = (
    "data/banknifty_real.csv"
)


with open(
    output_path,
    "w",
    newline="",
) as f:

    writer = csv.writer(f)

    writer.writerow(
        [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    )

    for candle in data:

        writer.writerow(
            [
                candle["date"].isoformat(),
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            ]
        )

print(
    "\nREAL HISTORICAL DATA SAVED:\n"
)

print(output_path)

