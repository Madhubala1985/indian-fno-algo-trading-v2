from platform_core.orchestration.event_bus import EventBus

from platform_core.analytics.structure.structure_engine import (
    StructureEngine,
    StructureEngineConfig,
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

from platform_core.risk.position_sizing import (
    PositionSizer,
    PositionSizingConfig,
)

from platform_core.execution.order_manager import (
    OrderManager,
)


print("\n===================================")
print("RUNTIME BOOT TEST")
print("===================================\n")


class MockBroker:

    def place_order(self, *args, **kwargs):

        print("[MockBroker] place_order called")

        return {
            "status": "SUCCESS",
            "order_id": "MOCK_ORDER_001",
        }

    def modify_order(self, *args, **kwargs):

        print("[MockBroker] modify_order called")

        return {
            "status": "SUCCESS",
        }

    def cancel_order(self, *args, **kwargs):

        print("[MockBroker] cancel_order called")

        return {
            "status": "SUCCESS",
        }

    def get_orders(self, *args, **kwargs):

        print("[MockBroker] get_orders called")

        return []


def main():

    print("Initializing Event Bus...")
    event_bus = EventBus()
    print("Event Bus initialized.")

    instrument_id = "BANKNIFTY"

    timeframe = "MIN_1"

    print("\nInitializing Structure Configs...")

    structure_config = StructureEngineConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    liquidity_config = LiquiditySweepDetectorConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    bos_config = BOSDetectorConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    choch_config = CHOCHDetectorConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    fvg_config = FVGEngineConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    sr_config = SupportResistanceEngineConfig(
        instrument_id=instrument_id,
        timeframe=timeframe,
    )

    print("Structure Configs initialized.")

    print("\nInitializing Detectors...")

    liquidity_detector = LiquiditySweepDetector(
        config=liquidity_config
    )

    bos_detector = BOSDetector(
        config=bos_config
    )

    choch_detector = CHOCHDetector(
        config=choch_config
    )

    fvg_engine = FVGEngine(
        config=fvg_config
    )

    sr_engine = SupportResistanceEngine(
        config=sr_config
    )

    print("Detectors initialized.")

    print("\nInitializing Structure Engine...")

    structure_engine = StructureEngine(
        config=structure_config,
        liquidity_sweep_detector=liquidity_detector,
        bos_detector=bos_detector,
        choch_detector=choch_detector,
        fvg_engine=fvg_engine,
        support_resistance_engine=sr_engine,
    )

    print("Structure Engine initialized.")

    print("\nInitializing Risk Engine...")

    risk_config = PositionSizingConfig()

    risk_engine = PositionSizer(
        config=risk_config
    )

    print("Risk Engine initialized.")

    print("\nInitializing Order Manager...")

    mock_broker = MockBroker()

    order_manager = OrderManager(
        broker=mock_broker
    )

    print("Order Manager initialized.")

    print("\n===================================")
    print("FULL RUNTIME BOOT SUCCESSFUL")
    print("===================================\n")


if __name__ == "__main__":
    main()
