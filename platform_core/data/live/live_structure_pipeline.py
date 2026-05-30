from datetime import datetime, timezone

from platform_core.intelligence.market_context_builder import (
    MarketContextBuilder,
)
from platform_core.options.options_context import (
    OptionsContextBuilder,
    OptionsContextBuilderConfig,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
)
from platform_core.data.synthetic_chain_builder import ExpiryProvider
from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.core.enums import UnderlyingFamily

from platform_core.adaptive.market_memory import (
    MarketMemory,
    MarketMemoryRecord,
)

from platform_core.analytics.structure.swing_detector import (
    SwingDetector,
    SwingDetectorConfig,
)

from platform_core.intelligence.bias_detector import (
    BiasDetector,
)

from platform_core.intelligence.htf_bias_engine import (
    HTFBiasEngine,
)


from platform_core.data.multi_timeframe_builder import (
    MultiTimeframeBuilder,
)


from platform_core.adaptive.reflection_engine import (
    ReflectionEngine,
)

from platform_core.adaptive.adaptive_policy_engine import (
    AdaptivePolicyEngine,
)

from platform_core.adaptive.regime_engine import (
    RegimeEngine,
)

from platform_core.signals.signal_engine import (
    SignalEngine,
)

from platform_core.paper_trading.paper_broker import (
    PaperBroker,
)

from platform_core.paper_trading.performance_tracker import (
    PerformanceTracker,
)

from platform_core.paper_trading.trade_logger import (
    TradeLogger,
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

from platform_core.analytics.structure.structure_engine import (
    StructureEngine,
    StructureEngineConfig,
    StructureEngineInput,
)

from platform_core.analytics.structure.support_resistance_engine import (
    SupportResistanceEngine,
    SupportResistanceEngineConfig,
)

from platform_core.data.bar_builder import (
    BarBuilder,
)

from platform_core.data.live.instrument_registry import (
    INSTRUMENTS,
)

from platform_core.data.live.kite_adapter import (
    KiteAdapter,
)

from platform_core.data.live.live_structure_adapter import (
    LiveStructureAdapter,
)

from platform_core.data.live.live_tick_mapper import (
    LiveTickMapper,
)


TOKEN_TO_SYMBOL = {
    value: key
    for key, value in INSTRUMENTS.items()
}


mapper = LiveTickMapper(
    token_to_symbol=TOKEN_TO_SYMBOL
)

bar_builder = BarBuilder()

structure_adapter = (
    LiveStructureAdapter()
)

adapter = KiteAdapter()

signal_engine = SignalEngine()

bias_detector = BiasDetector()

htf_bias_engine = (
    HTFBiasEngine()
)

paper_broker = PaperBroker()

performance_tracker = (
    PerformanceTracker()
)

trade_logger = TradeLogger()

market_memory = MarketMemory()

reflection_engine = (
    ReflectionEngine()
)


regime_engine = RegimeEngine()

adaptive_policy_engine = (
    AdaptivePolicyEngine()
)

multi_timeframe_builder = (
    MultiTimeframeBuilder()
)

# ── Options pipeline (BANKNIFTY target) ───────────────────────────────────────

_OPTION_TARGET = "BANKNIFTY"
_LOT_SIZE = REFERENCE_LOT_SIZES[UnderlyingFamily.BANKNIFTY]
_STRIKE_STEP = float(REFERENCE_STRIKE_STEPS[UnderlyingFamily.BANKNIFTY])

# MTF key normalisation (matches BacktestRunner convention)
_MTF_KEY_MAP: dict[str, str] = {
    "MIN_5":  "5m",
    "MIN_15": "15m",
    "HOUR_1": "1h",
    "HOUR_4": "4h",
}

_market_ctx_builder = MarketContextBuilder(underlying_symbol=_OPTION_TARGET)
_opt_ctx_builder = OptionsContextBuilder(
    config=OptionsContextBuilderConfig(compute_missing_greeks=True)
)
_strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=3))
_expiry_provider = ExpiryProvider(_OPTION_TARGET)


ENGINES = {}
SWING_DETECTORS = {}
LATEST_HTF_BARS = {}


def build_structure_engine(
    symbol: str,
):

    timeframe = "MIN_1"

    structure_config = (
        StructureEngineConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    )

    liquidity_detector = (
        LiquiditySweepDetector(
            config=(
                LiquiditySweepDetectorConfig(
                    instrument_id=symbol,
                    timeframe=timeframe,
                )
            )
        )
    )

    bos_detector = BOSDetector(
        config=BOSDetectorConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    )

    choch_detector = (
        CHOCHDetector(
            config=CHOCHDetectorConfig(
                instrument_id=symbol,
                timeframe=timeframe,
            )
        )
    )

    fvg_engine = FVGEngine(
        config=FVGEngineConfig(
            instrument_id=symbol,
            timeframe=timeframe,
        )
    )

    sr_engine = (
        SupportResistanceEngine(
            config=(
                SupportResistanceEngineConfig(
                    instrument_id=symbol,
                    timeframe=timeframe,
                )
            )
        )
    )

    return StructureEngine(
        config=structure_config,

        liquidity_sweep_detector=(
            liquidity_detector
        ),

        bos_detector=bos_detector,

        choch_detector=(
            choch_detector
        ),

        fvg_engine=fvg_engine,

        support_resistance_engine=(
            sr_engine
        ),
    )


for symbol in INSTRUMENTS.keys():

    ENGINES[symbol] = (
        build_structure_engine(
            symbol
        )
    )

    SWING_DETECTORS[symbol] = (
        SwingDetector(
            config=SwingDetectorConfig(
                instrument_id=symbol,
                timeframe="MIN_1",
            )
        )
    )


def on_ticks(raw_ticks):

    for raw_tick in raw_ticks:

        tick = mapper.map_tick(raw_tick)

        completed_bar = (
            bar_builder.update_tick(
                tick=tick
            )
        )

        if completed_bar is None:
            continue

        print(
            "\n======================"
        )

        print(
            f"COMPLETED BAR: "
            f"{completed_bar.symbol}"
        )

        print(
            "======================"
        )

        print(completed_bar)

        structure_bar = (
            structure_adapter.convert_bar(
                live_bar=completed_bar
            )
        )

        mtf_bars = (
            multi_timeframe_builder.update_bar(
                bar=completed_bar
            )
        )

        for timeframe, mtf_bar in (
            mtf_bars.items()
        ):

            LATEST_HTF_BARS[
                (
                    completed_bar.symbol,
                    timeframe,
                )
            ] = mtf_bar

            print(
                f"\n[MTF] "
                f"{completed_bar.symbol} "
                f"{timeframe}"
            )

            print(mtf_bar)

        # ── Options pipeline (runs only for the designated target symbol) ─────
        if completed_bar.symbol == _OPTION_TARGET:
            _market_ctx_builder.update(bar=completed_bar, timeframe="1m")
            for tf_key, tf_bar in mtf_bars.items():
                tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
                _market_ctx_builder.update(bar=tf_bar, timeframe=tf_name)

            market_ctx = _market_ctx_builder.build(
                valuation_timestamp=datetime.now(timezone.utc)
            )
            print(
                f"\n[OPTIONS] bias={market_ctx.bias}  "
                f"conf={market_ctx.confidence:.2f}"
            )

            if str(market_ctx.bias) != "NEUTRAL":
                try:
                    spot = completed_bar.close_price
                    bar_date = datetime.now(timezone.utc).date()
                    expiry = _expiry_provider.get_weekly_expiry(bar_date)

                    live_chain = adapter.fetch_option_chain(
                        underlying=_OPTION_TARGET,
                        expiry_date=expiry,
                        spot_price=spot,
                        lot_size=_LOT_SIZE,
                        strike_step=_STRIKE_STEP,
                    )

                    options_ctx = _opt_ctx_builder.build(
                        snapshots=[live_chain],
                        spot_price=spot,
                        lot_size=_LOT_SIZE,
                        valuation_timestamp=datetime.now(timezone.utc),
                    )

                    if options_ctx.strike_candidates:
                        result = _strike_engine.select(
                            bias=market_ctx.bias,
                            candidates=list(options_ctx.strike_candidates),
                            valuation_timestamp=datetime.now(timezone.utc),
                            oi_snapshot=options_ctx.oi_snapshot,
                            gex_snapshot=options_ctx.gex_snapshot,
                            atm_iv=options_ctx.atm_iv,
                        )
                        best = result.best()
                        print(
                            f"[STRIKE] {best.option_symbol}  "
                            f"δ={best.delta:.2f}  "
                            f"score={best.composite_score:.2f}  "
                            f"iv={best.implied_volatility:.1%}"
                        )
                except Exception as exc:
                    print(f"[OPTIONS] Pipeline error: {exc}")
        # ─────────────────────────────────────────────────────────────────────

        h4_bar = LATEST_HTF_BARS.get(
            (
                completed_bar.symbol,
                "HOUR_4",
            )
        )

        h1_bar = LATEST_HTF_BARS.get(
            (
                completed_bar.symbol,
                "HOUR_1",
            )
        )

        m15_bar = LATEST_HTF_BARS.get(
            (
                completed_bar.symbol,
                "MIN_15",
            )
        )

        biases = (
            bias_detector.detect_multi_timeframe_bias(
                h4_bar=h4_bar,
                h1_bar=h1_bar,
                m15_bar=m15_bar,
            )
        )

        market_context = (
            htf_bias_engine.build_context(
                daily_bias="NEUTRAL",
                h4_bias=(
                    biases["h4_bias"]
                ),
                h1_bias=(
                    biases["h1_bias"]
                ),
                m15_bias=(
                    biases["m15_bias"]
                ),
                volatility_regime="NORMAL",
            )
        )

        if mtf_bars:

            print(
                "\nMARKET CONTEXT:\n"
            )

            print(market_context)

        

        engine = ENGINES[
            completed_bar.symbol
        ]

        swing_detector = (
            SWING_DETECTORS[
                completed_bar.symbol
            ]
        )

        swing_result = (
            swing_detector.process_bar(
                structure_bar
            )
        )

        result = engine.process(
            payload=StructureEngineInput(
                bar=structure_bar,
                swing_points=(
                    swing_result.confirmed_points
                ),
            )
        )

        if result.emitted_events:

            print(
                "\nSTRUCTURE EVENTS:\n"
            )

            for event in (
                result.emitted_events
            ):

                print(event)

            bar_range = (
                completed_bar.high_price
                - completed_bar.low_price
            )

            average_range = max(
                bar_range,
                1.0,
            )

            regime = (
                regime_engine.classify(
                    bar_range=bar_range,
                    average_range=average_range,
                )
            )

            print(
                "\nMARKET REGIME:\n"
            )

            print(regime)

            event_names = tuple(
                str(event.event_type)
                for event in (
                    result.emitted_events
                )
            )

            adaptive_confidence = (
                adaptive_policy_engine.confidence(
                    events=event_names,
                    regime=(
                        regime.volatility_regime
                    ),
                )
            )

            signals = (
                signal_engine.generate_signals(
                    symbol=completed_bar.symbol,
                    events=result.emitted_events,
                    regime=regime,
                    market_context=market_context,
                )
            )

            for signal in signals:

                signal.confidence = (
                    signal.confidence
                    * adaptive_confidence
                )

            if signals:

                print(
                    "\nTRADE SIGNALS:\n"
                )

                for signal in signals:

                    print(signal)

                    if signal.action == "OPEN":

                        if not (
                            paper_broker.has_open_position(
                                symbol=signal.symbol
                            )
                        ):

                            paper_broker.open_position(
                                symbol=signal.symbol,
                                direction=signal.direction,
                                entry_price=(
                                    completed_bar.close_price
                                ),
                                quantity=1,
                            )

                    elif signal.action == "CLOSE":

                        if (
                            paper_broker.has_open_position(
                                symbol=signal.symbol
                            )
                        ):

                            paper_broker.close_position(
                                symbol=signal.symbol,
                                exit_price=(
                                    completed_bar.close_price
                                ),
                            )

                        if paper_broker.completed_trades:
                            latest_trade = (
                                paper_broker.completed_trades[-1]
                            )

                            memory_record = (
                                MarketMemoryRecord(

                                    symbol=latest_trade.symbol,

                                    direction=latest_trade.direction,

                                    confidence=signal.confidence,

                                    events=tuple(
                                        str(event.event_type)
                                        for event in (
                                            result.emitted_events
                                        )
                                    ),

                                    entry_price=(
                                        latest_trade.entry_price
                                    ),

                                    exit_price=(
                                        latest_trade.exit_price
                                    ),

                                    pnl=latest_trade.pnl,

                                     timestamp=datetime.utcnow(),

                                    outcome=(
                                        "WIN"
                                        if latest_trade.pnl > 0
                                        else "LOSS"
                                    ),
                                 )
                            )

                            market_memory.remember_trade(
                                record=memory_record
                            )

                            reflection = (
                                reflection_engine.reflect(
                                    memory=memory_record
                                )
                            )

                            adaptive_policy_engine.reinforce(

                                events=memory_record.events,

                                regime=signal.regime,

                                pnl=latest_trade.pnl,
                            )

                            print(
                                "\nREFLECTION ENGINE:\n"
                            )

                            print(reflection)


                        


                            summary = (
                                performance_tracker.calculate_summary(
                                    trades=(
                                        paper_broker.completed_trades
                                    )
                                )
                            )

                            print(
                                "\nPERFORMANCE SUMMARY:\n"
                            )

                            print(summary)

        else:

            print(
                "\nNo structure events."
            )


adapter.add_tick_callback(on_ticks)

adapter.connect(
    instrument_tokens=list(
        INSTRUMENTS.values()
    )
)

print(
    "\nLIVE STRUCTURE PIPELINE ACTIVE\n"
)

import time

while True:
    time.sleep(1)

