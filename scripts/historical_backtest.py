from platform_core.backtesting.historical_replay import (
    HistoricalReplayFeed,
)

from platform_core.data.live.live_structure_adapter import (
    LiveStructureAdapter,
)

from platform_core.analytics.structure.structure_engine import (
    StructureEngine,
    StructureEngineConfig,
    StructureEngineInput,
)

from platform_core.analytics.structure.bos_detector import (
    BOSDetector,
    BOSDetectorConfig,
)

from platform_core.analytics.structure.choch_detector import (
    CHOCHDetector,
    CHOCHDetectorConfig,
)

from platform_core.analytics.structure.fvg_engine import (
    FVGEngine,
    FVGEngineConfig,
)

from platform_core.analytics.structure.liquidity_sweep_detector import (
    LiquiditySweepDetector,
    LiquiditySweepDetectorConfig,
)

from platform_core.analytics.structure.support_resistance_engine import (
    SupportResistanceEngine,
    SupportResistanceEngineConfig,
)


symbol = "BANKNIFTY"

timeframe = "MIN_1"


feed = HistoricalReplayFeed()

bars = feed.load_csv(
    path="data/sample_banknifty.csv",
    symbol=symbol,
)


adapter = LiveStructureAdapter()


engine = StructureEngine(

    config=StructureEngineConfig(
        instrument_id=symbol,
        timeframe=timeframe,
    ),

    liquidity_sweep_detector=(
        LiquiditySweepDetector(
            config=(
                LiquiditySweepDetectorConfig(
                    instrument_id=symbol,
                    timeframe=timeframe,
                )
            )
        )
    ),

    bos_detector=BOSDetector(
        config=BOSDetectorConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    ),

    choch_detector=CHOCHDetector(
        config=CHOCHDetectorConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    ),

    fvg_engine=FVGEngine(
        config=FVGEngineConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    ),

    support_resistance_engine=(
        SupportResistanceEngine(
            config=(
                SupportResistanceEngineConfig(
                    instrument_id=symbol,
                    timeframe=timeframe,
                )
            )
        )
    ),
)


print(
    "\nHISTORICAL BACKTEST STARTED\n"
)


for live_bar in bars:

    structure_bar = (
        adapter.convert_bar(
            live_bar=live_bar
        )
    )

    result = engine.process(
        payload=StructureEngineInput(
            bar=structure_bar
        )
    )

    print(
        f"\nBAR: "
        f"{live_bar.close_price}"
    )

    if result.emitted_events:

        print(
            "\nEVENTS:\n"
        )

        for event in (
            result.emitted_events
        ):

            print(event)

    else:

        print(
            "No events."
        )


print(
    "\nHISTORICAL BACKTEST COMPLETED\n"
)

