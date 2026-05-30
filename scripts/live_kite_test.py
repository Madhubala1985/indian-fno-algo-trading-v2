from platform_core.data.live.kite_adapter import (
    KiteAdapter,
)

from platform_core.data.live.instrument_registry import (
    INSTRUMENTS,
)


TOKEN_TO_SYMBOL = {
    value: key
    for key, value in INSTRUMENTS.items()
}


def on_ticks(ticks):

    print("\nLIVE TICKS:\n")

    for tick in ticks:

        token = tick["instrument_token"]

        symbol = TOKEN_TO_SYMBOL.get(
            token,
            str(token),
        )

        print(
            f"{symbol:<15} "
            f"{tick.get('last_price')}"
        )


adapter = KiteAdapter()

adapter.add_tick_callback(on_ticks)

adapter.connect(
    instrument_tokens=list(
        INSTRUMENTS.values()
    )
)

print("\nListening for live ticks...\n")

while True:
    pass
