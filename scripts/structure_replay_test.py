from datetime import datetime, timedelta, timezone

from platform_core.analytics.structure.structure_engine import (
    StructureEngine,
    StructureEngineConfig,
    StructureEngineInput,
)

from platform_core.analytics.structure.liquidity_sweep_detector import (
    LiquiditySweepDetector,
    LiquiditySweepDetectorConfig,
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

from platform_core.analytics.structure.support_resistance_engine import (
    SupportResistanceEngine,
    SupportResistanceEngineConfig,
)

from platform_core.analytics.structure.models import (
    StructureBar,
)


print("\n===================================")
print("STRUCTURE REPLAY TEST")
print("===================================\n")


instrument_id = "BANKNIFTY"

timeframe = "MIN_1"


# =========================================================
# ENGINE INITIALIZATION
# =========================================================

structure_engine = StructureEngine(

    config=StructureEngineConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    ),

    liquidity_sweep_detector=LiquiditySweepDetector(
        config=LiquiditySweepDetectorConfig(
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
    ),

    bos_detector=BOSDetector(
        config=BOSDetectorConfig(
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
    ),

    choch_detector=CHOCHDetector(
        config=CHOCHDetectorConfig(
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
    ),

    fvg_engine=FVGEngine(
        config=FVGEngineConfig(
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
    ),

    support_resistance_engine=SupportResistanceEngine(
        config=SupportResistanceEngineConfig(
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
    ),
)


# =========================================================
# SYNTHETIC MARKET DATA
# =========================================================

start_time = datetime.now(timezone.utc)

synthetic_bars = [

    (100, 102, 99, 101),
    (101, 104, 100, 103),
    (103, 106, 102, 105),
    (105, 108, 104, 107),
    (107, 109, 105, 106),
    (106, 110, 105, 109),
    (109, 112, 108, 111),
    (111, 113, 107, 108),
    (108, 106, 103, 104),
    (104, 101, 99, 100),
    (100, 98, 95, 96),
    (96, 99, 94, 98),

]


# =========================================================
# REPLAY LOOP
# =========================================================

for idx, (o, h, l, c) in enumerate(synthetic_bars):

    current_timestamp = (
        start_time + timedelta(minutes=idx)
    )

    bar = StructureBar(

        instrument_id=instrument_id,

        timeframe=timeframe,

        bar_timestamp=current_timestamp,

        open_price=float(o),

        high_price=float(h),

        low_price=float(l),

        close_price=float(c),

        volume=1000.0 + idx * 10,

        trade_count=100 + idx,

        open_interest=5000.0 + idx * 25,

        vwap=float((h + l + c) / 3.0),

        event_sequence_number=idx,

    )

    payload = StructureEngineInput(
        bar=bar,
    )

    result = structure_engine.process(
        payload=payload
    )

    print("\n-----------------------------------")
    print(f"BAR {idx + 1}")
    print("-----------------------------------")

    print(
        f"O={o} "
        f"H={h} "
        f"L={l} "
        f"C={c}"
    )

    print(
        f"Processed Bars: "
        f"{result.processed_bar_count}"
    )

    print(
        f"Inferred Structure Side: "
        f"{result.inferred_structure_side}"
    )

    print(
        f"Events Emitted: "
        f"{len(result.emitted_events)}"
    )

    # =====================================================
    # BOS
    # =====================================================

    if result.bos_result:

        print("\nBOS RESULT:")

        print(result.bos_result)

    # =====================================================
    # CHOCH
    # =====================================================

    if result.choch_result:

        print("\nCHOCH RESULT:")

        print(result.choch_result)

    # =====================================================
    # FVG
    # =====================================================

    if result.fvg_result:

        print("\nFVG RESULT:")

        print(result.fvg_result)

    # =====================================================
    # LIQUIDITY SWEEP
    # =====================================================

    if result.liquidity_sweep_result:

        print("\nLIQUIDITY SWEEP:")

        print(result.liquidity_sweep_result)

    # =====================================================
    # SUPPORT / RESISTANCE
    # =====================================================

    if result.support_resistance_result:

        print("\nSUPPORT/RESISTANCE:")

        print(result.support_resistance_result)

    # =====================================================
    # EMITTED EVENTS
    # =====================================================

    if result.emitted_events:

        print("\nEMITTED EVENTS:")

        for event in result.emitted_events:

            print(event)


print("\n===================================")
print("REPLAY TEST COMPLETE")
print("===================================\n")