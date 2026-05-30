from platform_core.data.bar_builder import (
    BarBuilder,
)

from platform_core.data.live.kite_adapter import (
    KiteAdapter,
)

from platform_core.data.live.live_tick_mapper import (
    LiveTickMapper,
)

from platform_core.data.live.instrument_registry import (
    INSTRUMENTS,
)


TOKEN_TO_SYMBOL = {
    value: key
    for key, value in INSTRUMENTS.items()
}


mapper = LiveTickMapper(
    token_to_symbol=TOKEN_TO_SYMBOL
)

bar_builder = BarBuilder()

adapter = KiteAdapter()


def on_ticks(raw_ticks):

    for raw_tick in raw_ticks:

        tick = mapper.map_tick(raw_tick)

        completed_bar = (
            bar_builder.update_tick(
                tick=tick
            )
        )

        if completed_bar is not None:

            print("\nCOMPLETED BAR:\n")

            print(completed_bar)


adapter.add_tick_callback(on_ticks)

adapter.connect(
    instrument_tokens=list(
        INSTRUMENTS.values()
    )
)

print(
    "\nLIVE MARKET STREAM ACTIVE\n"
)

while True:
    pass
