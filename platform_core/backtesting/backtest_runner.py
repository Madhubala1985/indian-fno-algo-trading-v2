# FILE: platform_core/backtesting/backtest_runner.py
"""
platform_core.backtesting.backtest_runner
═══════════════════════════════════════════════════════════════════════
Integrated historical backtest runner — options decision engine.

Full pipeline per bar:
    LiveBar stream
      → DailyBarAccumulator    (emits 1d bars when the session date rolls)
      → MultiTimeframeBuilder  (1m → 5m / 15m / 1h / 4h)
      → MarketContextBuilder   (RegimeClassifier per TF → bias + confidence)
      → SyntheticOptionChainBuilder (BSM chain from realized vol)
      → OptionsContextBuilder  (OI / GEX / IV / Dealer / Premium engines)
      → StrikeSelectionEngine  (7-factor scoring → StrikeRecommendation)
      → PositionSizer          (8-factor risk-adjusted lot sizing)
      → OptionPaperTrader      (BSM P&L simulation with equity tracking)

Changes from prior version:
  1. PositionSizer integrated — lots no longer fixed at 1.
     Sizing uses confidence, volatility ratio, liquidity, drawdown,
     portfolio heat, and cash availability.
  2. DailyBarAccumulator added — emits a LiveBar when the session date
     rolls. Fed to MarketContextBuilder("1d") so the daily slot (weight
     0.35 — the largest in the charter hierarchy) contributes to bias.
  3. Equity tracking added — equity, peak_equity, drawdown, open
     premium cost tracked across the replay.

Backward compatible:
    BacktestRunner(symbol="BANKNIFTY").run(csv_path=...) unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import floor

from platform_core.analytics.structure.bos_detector import BOSDetector, BOSDetectorConfig
from platform_core.analytics.structure.choch_detector import CHOCHDetector, CHOCHDetectorConfig
from platform_core.analytics.structure.fvg_engine import FVGEngine, FVGEngineConfig
from platform_core.analytics.structure.liquidity_sweep_detector import (
    LiquiditySweepDetector, LiquiditySweepDetectorConfig,
)
from platform_core.analytics.structure.structure_engine import (
    StructureEngine, StructureEngineConfig, StructureEngineInput,
)
from platform_core.analytics.structure.support_resistance_engine import (
    SupportResistanceEngine, SupportResistanceEngineConfig,
)
from platform_core.analytics.structure.swing_detector import SwingDetector, SwingDetectorConfig

from platform_core.backtesting.historical_replay import HistoricalReplayFeed
from platform_core.data.bar_builder import LiveBar
from platform_core.data.live.live_structure_adapter import LiveStructureAdapter
from platform_core.data.multi_timeframe_builder import MultiTimeframeBuilder
from platform_core.data.synthetic_chain_builder import (
    ExpiryProvider, SyntheticChainConfig, SyntheticOptionChainBuilder,
)
from platform_core.intelligence.market_context_builder import (
    MarketContextBuilder, MarketBias, MarketContext,
)
from platform_core.options.options_context import OptionsContextBuilder, OptionsContextBuilderConfig
from platform_core.options.strike_selection import (
    StrikeSelectionEngine, StrikeSelectionConfig, StrikeRecommendation,
)
from platform_core.options.greeks_engine import GreeksEngine, GreeksEngineConfig, OptionGreeksInput
from platform_core.paper_trading.paper_broker import PaperBroker, PaperTrade
from platform_core.paper_trading.performance_tracker import PerformanceTracker
from platform_core.paper_trading.terminal_display import (
    print_entry_block,
    print_exit_block,
    print_position_snapshot,
    print_heartbeat,
    print_warning_block,
    print_performance_summary,
    print_backtest_header,
    print_backtest_results,
)
from platform_core.risk.position_sizing import (
    PositionSizer, PositionSizingConfig,
    PositionSizingRequest, PositionSizingResult,
    PortfolioRiskSnapshot,
)
from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.analytics.structure.session_context import SessionContextEngine, SessionContext
from platform_core.analytics.structure.previous_day_levels import PreviousDayLevelsEngine, PreviousDayLevels
from platform_core.analytics.structure.opening_range import OpeningRangeEngine, OpeningRange
from platform_core.analytics.structure.fvg_scoring import (
    FVGQualityScorer, FVGQualityScore,
    FVG_QUALITY_BLOCK_THRESHOLD, FVG_QUALITY_STALENESS_BARS,
)
from platform_core.analytics.structure.fvg_engine import FVGEngine, FVGEngineConfig
from platform_core.analytics.structure.models import StructureEventType
from platform_core.analytics.structure.sweep_validation import (
    LiquiditySweepValidationEngine, SetupValidation,
)
from platform_core.backtesting.strike_validator import StrikeSelectionValidator
from platform_core.data.historical.option_chain_feed import HistoricalOptionChainFeed


# ── Multi-timeframe key normalisation ──────────────────────────────────────────

_MTF_KEY_MAP: dict[str, str] = {
    "MIN_5":  "5m",
    "MIN_15": "15m",
    "HOUR_1": "1h",
    "HOUR_4": "4h",
}


# ── DailyBarAccumulator ────────────────────────────────────────────────────────

class DailyBarAccumulator:
    """
    Accumulates 1m bars and emits a complete LiveBar at each session-date
    boundary.

    This feeds the "1d" slot of MarketContextBuilder, giving the daily
    RegimeClassifier (weight 0.35 in bias fusion) real price data.

    A partial day bar is emitted on flush() at end-of-data.
    """

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._current_date: date | None = None
        self._open: float | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._close: float | None = None
        self._volume: float = 0.0
        self._day_start_ts: datetime | None = None
        self._last_ts: datetime | None = None

    def update(self, bar: LiveBar) -> LiveBar | None:
        """
        Feed one 1m bar. Returns a completed daily LiveBar when the
        session date changes, otherwise returns None.
        """
        bar_date = _bar_date(bar)

        if self._current_date is None:
            self._start_day(bar, bar_date)
            return None

        if bar_date != self._current_date:
            completed = self._emit()
            self._start_day(bar, bar_date)
            return completed

        # Same day — accumulate
        self._high = max(self._high, bar.high_price)   # type: ignore[type-var]
        self._low  = min(self._low,  bar.low_price)    # type: ignore[type-var]
        self._close = bar.close_price
        self._volume += bar.volume
        self._last_ts = bar.end_timestamp
        return None

    def flush(self) -> LiveBar | None:
        """Return the current partial day as a LiveBar, or None if empty."""
        if self._current_date is None or self._open is None:
            return None
        return self._emit()

    def _start_day(self, bar: LiveBar, bar_date: date) -> None:
        self._current_date = bar_date
        self._open  = bar.open_price
        self._high  = bar.high_price
        self._low   = bar.low_price
        self._close = bar.close_price
        self._volume = bar.volume
        self._day_start_ts = bar.start_timestamp
        self._last_ts = bar.end_timestamp

    def _emit(self) -> LiveBar:
        return LiveBar(
            symbol=self._symbol,
            timeframe="DAY_1",
            start_timestamp=self._day_start_ts,
            end_timestamp=self._last_ts,
            open_price=self._open,
            high_price=self._high,
            low_price=self._low,
            close_price=self._close,
            volume=self._volume,
        )


# ── BacktestConfig ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BacktestConfig:
    """
    Full configuration for one backtest run.

    lot_size=0 and strike_step=0 trigger automatic lookup from
    REFERENCE_LOT_SIZES and REFERENCE_STRIKE_STEPS respectively.
    """

    symbol: str
    timeframe: str = "MIN_1"

    # Option chain parameters (0 = auto from core constants)
    lot_size: int = 0
    strike_step: int = 0
    strikes_around_atm: int = 10

    # MarketContext filtering
    min_confidence: float = 0.35

    # Risk management
    stop_loss_pct: float = 0.50        # exit when value ≤ entry × this
    take_profit_pct: float = 2.00      # exit when value ≥ entry × this
    min_dte_to_hold: int = 2

    # Greeks
    target_delta_min: float = 0.28
    target_delta_max: float = 0.52
    min_option_oi: int = 500

    # Market model
    risk_free_rate: float = 0.068
    dividend_yield: float = 0.0
    vol_lookback_bars: int = 20

    # Historical option chain data directory (empty = always use synthetic)
    # Path root: {chain_data_dir}/{symbol}/{expiry_date}.csv
    # Set to "data/option_chains" when real recorded chains are available.
    chain_data_dir: str = ""

    # MarketContextBuilder
    mtf_min_history_bars: int = 20

    # ── PositionSizer inputs ────────────────────────────────────────────
    initial_equity: float = 1_000_000.0      # starting capital (₹)
    max_drawdown_fraction: float = 0.15       # max tolerated drawdown
    max_risk_fraction_per_trade: float = 0.01 # 1% of equity per trade
    max_portfolio_heat_fraction: float = 0.05 # max 5% open premium / equity
    max_lot_count: int = 5                    # hard cap on lots per trade
    reference_iv: float = 0.18               # baseline IV for vol-ratio

    def effective_lot_size(self) -> int:
        return self.lot_size if self.lot_size > 0 else _lookup_lot_size(self.symbol)

    def effective_strike_step(self) -> int:
        return self.strike_step if self.strike_step > 0 else _lookup_strike_step(self.symbol)

    @classmethod
    def for_symbol(cls, symbol: str, timeframe: str = "MIN_1") -> "BacktestConfig":
        return cls(symbol=symbol, timeframe=timeframe)


# ── Option position and trade tracking ────────────────────────────────────────

@dataclass(slots=True)
class OptionPosition:
    underlying_symbol: str
    contract_symbol: str
    strike: float
    option_type: str
    expiry_date: date
    lot_size: int
    lots: int
    entry_premium: float
    entry_spot: float
    entry_iv: float
    direction: str
    bar_index: int


@dataclass(slots=True)
class OptionTrade:
    underlying_symbol: str
    contract_symbol: str
    strike: float
    option_type: str
    expiry_date: date
    direction: str
    lots: int
    lot_size: int
    entry_premium: float
    exit_premium: float
    pnl: float
    exit_reason: str
    bars_held: int

    def as_paper_trade(self) -> PaperTrade:
        return PaperTrade(
            symbol=self.contract_symbol,
            direction=self.direction,
            entry_price=self.entry_premium,
            exit_price=self.exit_premium,
            quantity=self.lots * self.lot_size,
            pnl=self.pnl,
        )


class OptionPaperTrader:
    """
    Tracks one open synthetic option position with BSM mark-to-market.

    Accepts a `lots` parameter on open() — previously hardcoded to 1.
    """

    def __init__(self, risk_free_rate: float = 0.068, dividend_yield: float = 0.0) -> None:
        self._greeks_engine = GreeksEngine(
            config=GreeksEngineConfig(theta_scale_per_day=True)
        )
        self._risk_free_rate = risk_free_rate
        self._dividend_yield = dividend_yield
        self._position: OptionPosition | None = None
        self.completed_trades: list[OptionTrade] = []

    def has_open_position(self) -> bool:
        return self._position is not None

    def open(
        self,
        *,
        recommendation: StrikeRecommendation,
        direction: str,
        bar_index: int,
        lots: int = 1,
    ) -> None:
        if self._position is not None:
            return
        lots = max(lots, 1)
        self._position = OptionPosition(
            underlying_symbol=recommendation.underlying_symbol,
            contract_symbol=recommendation.option_symbol,
            strike=recommendation.strike_price,
            option_type=recommendation.option_type,
            expiry_date=recommendation.expiry_date,
            lot_size=recommendation.lot_size,
            lots=lots,
            entry_premium=recommendation.last_traded_price,
            entry_spot=recommendation.spot_price,
            entry_iv=recommendation.implied_volatility,
            direction=direction,
            bar_index=bar_index,
        )

    def evaluate_and_close(
        self,
        *,
        current_spot: float,
        current_iv: float,
        current_date: date,
        bar_index: int,
        stop_loss_pct: float,
        take_profit_pct: float,
        min_dte: int,
        force_reason: str | None = None,
    ) -> OptionTrade | None:
        pos = self._position
        if pos is None:
            return None

        tte = max((pos.expiry_date - current_date).days, 1) / 365.25
        effective_iv = max(current_iv, 0.05)
        try:
            result = self._greeks_engine.compute(
                option_input=OptionGreeksInput(
                    instrument_id="mtm",
                    underlying_symbol=pos.underlying_symbol,
                    option_symbol=pos.contract_symbol,
                    option_type=pos.option_type,
                    spot_price=current_spot,
                    strike_price=pos.strike,
                    time_to_expiry_years=tte,
                    implied_volatility=effective_iv,
                    risk_free_rate=self._risk_free_rate,
                    dividend_yield=self._dividend_yield,
                    lot_size=pos.lot_size,
                )
            )
            current_premium = max(result.option_price, 0.01)
        except Exception:
            current_premium = pos.entry_premium * 0.5

        dte = (pos.expiry_date - current_date).days
        pct_of_entry = current_premium / max(pos.entry_premium, 0.001)

        exit_reason = force_reason
        if exit_reason is None and dte < min_dte:
            exit_reason = f"DTE_MINIMUM (dte={dte})"
        if exit_reason is None and pct_of_entry <= stop_loss_pct:
            exit_reason = f"STOP_LOSS ({pct_of_entry:.0%} of entry)"
        if exit_reason is None and pct_of_entry >= take_profit_pct:
            exit_reason = f"TAKE_PROFIT ({pct_of_entry:.0%} of entry)"
        if exit_reason is None:
            return None

        pnl = (current_premium - pos.entry_premium) * pos.lot_size * pos.lots
        trade = OptionTrade(
            underlying_symbol=pos.underlying_symbol,
            contract_symbol=pos.contract_symbol,
            strike=pos.strike,
            option_type=pos.option_type,
            expiry_date=pos.expiry_date,
            direction=pos.direction,
            lots=pos.lots,
            lot_size=pos.lot_size,
            entry_premium=pos.entry_premium,
            exit_premium=current_premium,
            pnl=pnl,
            exit_reason=exit_reason,
            bars_held=bar_index - pos.bar_index,
        )
        self.completed_trades.append(trade)
        self._position = None
        return trade


# ── BacktestRunner ─────────────────────────────────────────────────────────────

class BacktestRunner:
    """
    Integrated backtest runner.

    Backward-compatible:
        BacktestRunner(symbol='BANKNIFTY').run(csv_path=...)
    """

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str = "MIN_1",
        config: BacktestConfig | None = None,
    ) -> None:
        self.config = config or BacktestConfig.for_symbol(symbol, timeframe)
        self.symbol = self.config.symbol
        self.timeframe = self.config.timeframe

        lot_size = self.config.effective_lot_size()
        strike_step = self.config.effective_strike_step()

        # ── Structure analysis (unchanged) ────────────────────────────────
        self.feed = HistoricalReplayFeed()
        self.adapter = LiveStructureAdapter()
        self.swing_detector = SwingDetector(
            config=SwingDetectorConfig(instrument_id=self.symbol, timeframe=self.timeframe)
        )
        self.liquidity_detector = LiquiditySweepDetector(
            config=LiquiditySweepDetectorConfig(instrument_id=self.symbol, timeframe=self.timeframe)
        )
        self.engine = StructureEngine(
            config=StructureEngineConfig(instrument_id=self.symbol, timeframe=self.timeframe),
            liquidity_sweep_detector=self.liquidity_detector,
            bos_detector=BOSDetector(
                config=BOSDetectorConfig(instrument_id=self.symbol, timeframe=self.timeframe)
            ),
            choch_detector=CHOCHDetector(
                config=CHOCHDetectorConfig(instrument_id=self.symbol, timeframe=self.timeframe)
            ),
            fvg_engine=FVGEngine(
                config=FVGEngineConfig(instrument_id=self.symbol, timeframe=self.timeframe)
            ),
            support_resistance_engine=SupportResistanceEngine(
                config=SupportResistanceEngineConfig(instrument_id=self.symbol, timeframe=self.timeframe)
            ),
        )

        # ── Multi-timeframe and market context ────────────────────────────
        self.daily_acc = DailyBarAccumulator(self.symbol)
        self.mtf_builder = MultiTimeframeBuilder()
        self.market_ctx_builder = MarketContextBuilder(
            underlying_symbol=self.symbol,
            min_history_bars=self.config.mtf_min_history_bars,
        )

        # ── Option chain pipeline ─────────────────────────────────────────
        self.expiry_provider = ExpiryProvider(self.symbol)
        self.chain_builder = SyntheticOptionChainBuilder(
            config=SyntheticChainConfig(
                underlying_symbol=self.symbol,
                lot_size=lot_size,
                strike_step=strike_step,
                risk_free_rate=self.config.risk_free_rate,
                dividend_yield=self.config.dividend_yield,
                strikes_around_atm=self.config.strikes_around_atm,
                vol_lookback_bars=self.config.vol_lookback_bars,
            )
        )
        # Real historical option chain feed (primary); synthetic is the fallback
        self.chain_feed = HistoricalOptionChainFeed(
            data_dir=self.config.chain_data_dir,
            symbol=self.symbol,
        )
        if self.chain_feed.ready:
            print(f"  [CHAIN FEED] Real option chain data loaded for "
                  f"{len(self.chain_feed.expiries_loaded)} expiries — "
                  f"synthetic used only as fallback.")

        self.opt_ctx_builder = OptionsContextBuilder(
            config=OptionsContextBuilderConfig(
                risk_free_rate=self.config.risk_free_rate,
                dividend_yield=self.config.dividend_yield,
                compute_missing_greeks=True,
            )
        )
        self.strike_engine = StrikeSelectionEngine(
            config=StrikeSelectionConfig(
                top_n=3,
                target_delta_min=self.config.target_delta_min,
                target_delta_max=self.config.target_delta_max,
                min_open_interest=self.config.min_option_oi,
            )
        )

        # ── Position sizing ───────────────────────────────────────────────
        self.position_sizer = PositionSizer(
            config=PositionSizingConfig(
                max_risk_fraction_per_trade=self.config.max_risk_fraction_per_trade,
                max_portfolio_heat_fraction=self.config.max_portfolio_heat_fraction,
            )
        )

        # ── Session / PDH-PDL / Opening-Range / FVG-quality filters ────────
        self.session_engine = SessionContextEngine()
        self.pdl_engine     = PreviousDayLevelsEngine()
        self.or_engine      = OpeningRangeEngine()
        self.fvg_scorer     = FVGQualityScorer()

        self.sweep_validator = LiquiditySweepValidationEngine()
        self.strike_validator = StrikeSelectionValidator(
            risk_free_rate=self.config.risk_free_rate,
            dividend_yield=self.config.dividend_yield,
        )

        # MTF FVG engines — reuse FVGEngine class, separate instances per TF
        self.fvg_15m = FVGEngine(
            config=FVGEngineConfig(instrument_id=self.symbol, timeframe="MIN_15")
        )
        self.fvg_1h = FVGEngine(
            config=FVGEngineConfig(instrument_id=self.symbol, timeframe="HOUR_1")
        )
        self._fvg_quality: float = 1.0   # current FVG quality state
        self._fvg_quality_bar: int = -FVG_QUALITY_STALENESS_BARS - 1  # force stale initially

        # ── Trade execution and tracking ──────────────────────────────────
        self.option_trader = OptionPaperTrader(
            risk_free_rate=self.config.risk_free_rate,
            dividend_yield=self.config.dividend_yield,
        )
        self.performance_tracker = PerformanceTracker()
        self.paper_broker = PaperBroker()  # kept for BatchBacktester compatibility

        # ── Equity state ──────────────────────────────────────────────────
        self._equity: float = self.config.initial_equity
        self._peak_equity: float = self.config.initial_equity
        self._open_premium_cost: float = 0.0

    def run(self, *, csv_path: str) -> None:
        cfg = self.config
        bars = self.feed.load_csv(
            path=csv_path, symbol=self.symbol, timeframe=self.timeframe
        )

        print_backtest_header(
            symbol=self.symbol,
            bars=len(bars),
            csv_path=csv_path,
            initial_equity=cfg.initial_equity,
            risk_pct=cfg.max_risk_fraction_per_trade,
            max_lots=cfg.max_lot_count,
        )

        daily_bars_fed = 0
        trade_signals = 0
        entries_made = 0
        session_blocks = 0
        pdl_blocks = 0
        or_blocks = 0
        fvg_blocks = 0
        fvg_quality_filters = 0
        setup_blocks = 0

        # Carry active SR zones forward as LiquidityLevels for sweep detection
        _liq_levels: tuple = ()

        for bar_index, bar in enumerate(bars):
            bar_ts = _bar_timestamp(bar)
            bar_date = bar_ts.date()

            # ── 1. Update chain vol history ───────────────────────────────
            self.chain_builder.update_bar(bar)

            # ── 2. Daily bar accumulation → "1d" feed + PDH/PDL ──────────
            daily_bar = self.daily_acc.update(bar)
            if daily_bar is not None:
                self.market_ctx_builder.update(bar=daily_bar, timeframe="1d")
                self.pdl_engine.update_daily_bar(daily_bar)
                daily_bars_fed += 1

            # ── 2b. Session + Opening Range classification ────────────────
            session_ctx = self.session_engine.classify(bar_ts)
            self.or_engine.update_bar(bar)

            # ── 3. Structure analysis (with SR levels fed to sweep detector)
            structure_bar = self.adapter.convert_bar(live_bar=bar)
            swing_result = self.swing_detector.process_bar(structure_bar)
            structure_result = self.engine.process(
                payload=StructureEngineInput(
                    bar=structure_bar,
                    swing_points=swing_result.confirmed_points,
                    liquidity_levels=_liq_levels,
                )
            )
            _liq_levels = self.engine.active_liquidity_levels  # update for next bar

            # ── 3b. FVG quality scoring ───────────────────────────────────
            fvg_events = [
                e for e in structure_result.emitted_events
                if e.event_type == StructureEventType.FVG_CREATED
            ]
            _recent_sweeps = tuple(self.liquidity_detector.recent_sweeps)
            _htf_fvgs = {
                "MIN_15": self.fvg_15m.active_gaps,
                "HOUR_1": self.fvg_1h.active_gaps,
            }

            # Update setup validator with this bar's evidence
            self.sweep_validator.update(
                bar_index=bar_index,
                structure_events=structure_result.emitted_events,
                fvg_events=fvg_events,
            )

            if fvg_events:
                # Preliminary score with NEUTRAL bias (bias computed later in step 5)
                scores = [
                    self.fvg_scorer.score_event(
                        event=e,
                        market_bias="NEUTRAL",
                        recent_sweeps=_recent_sweeps,
                        active_htf_fvgs=_htf_fvgs,
                        mitigation_ratio=0.0,
                    )
                    for e in fvg_events
                ]
                best = max(scores, key=lambda s: s.total)
                self._fvg_quality = best.total
                self._fvg_quality_bar = bar_index

            # ── 4. Multi-timeframe aggregation ────────────────────────────
            mtf_result = self.mtf_builder.update_bar(bar=bar)
            self.market_ctx_builder.update(bar=bar, timeframe="1m")
            for tf_key, tf_bar in mtf_result.items():
                tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
                self.market_ctx_builder.update(bar=tf_bar, timeframe=tf_name)
                # Feed completed MTF bars to their dedicated FVGEngine instances
                if tf_key == "MIN_15":
                    self.fvg_15m.process_bar(
                        bar=self.adapter.convert_bar(live_bar=tf_bar)
                    )
                elif tf_key == "HOUR_1":
                    self.fvg_1h.process_bar(
                        bar=self.adapter.convert_bar(live_bar=tf_bar)
                    )

            # ── 5. Market context ─────────────────────────────────────────
            market_ctx = self.market_ctx_builder.build(valuation_timestamp=bar_ts)

            # ── 6. Option chain + analytics ───────────────────────────────
            # Priority: real historical chain → synthetic fallback.
            # Both paths produce OptionChainSnapshot (option_chain_ingestion).
            # OptionsContextBuilder.build() is identical regardless of source.
            weekly_expiry   = self.expiry_provider.get_weekly_expiry(bar_date)
            biweekly_expiry = self.expiry_provider.get_biweekly_expiry(bar_date)
            lot_size_val    = cfg.effective_lot_size()

            weekly_snap = (
                self.chain_feed.get_snapshot(bar_ts, weekly_expiry, bar.close_price, lot_size_val)
                if self.chain_feed.ready else None
            )
            if weekly_snap is None:
                weekly_snap = self.chain_builder.build_snapshot(
                    spot_price=bar.close_price,
                    expiry_date=weekly_expiry,
                    valuation_timestamp=bar_ts,
                )

            biweekly_snap = (
                self.chain_feed.get_snapshot(bar_ts, biweekly_expiry, bar.close_price, lot_size_val)
                if self.chain_feed.ready else None
            )
            if biweekly_snap is None:
                biweekly_snap = self.chain_builder.build_snapshot(
                    spot_price=bar.close_price,
                    expiry_date=biweekly_expiry,
                    valuation_timestamp=bar_ts,
                )

            options_ctx = self.opt_ctx_builder.build(
                snapshots=[weekly_snap, biweekly_snap],
                spot_price=bar.close_price,
                lot_size=lot_size_val,
            )

            # ── 7. Exit management ────────────────────────────────────────
            if self.option_trader.has_open_position():
                exit_reason = _detect_exit_signal(
                    structure_result.emitted_events, self.option_trader
                )
                closed = self.option_trader.evaluate_and_close(
                    current_spot=bar.close_price,
                    current_iv=options_ctx.atm_iv or 0.18,
                    current_date=bar_date,
                    bar_index=bar_index,
                    stop_loss_pct=cfg.stop_loss_pct,
                    take_profit_pct=cfg.take_profit_pct,
                    min_dte=cfg.min_dte_to_hold,
                    force_reason=exit_reason,
                )
                if closed is not None:
                    self._equity += closed.pnl
                    self._peak_equity = max(self._peak_equity, self._equity)
                    self._open_premium_cost = 0.0
                    self.strike_validator.record_exit(
                        exit_spot=bar.close_price,
                        exit_iv=options_ctx.atm_iv or 0.18,
                        exit_date=bar_date,
                        entry_price=closed.entry_premium,
                        exit_price=closed.exit_premium,
                        pnl=closed.pnl,
                        exit_reason=str(closed.exit_reason),
                    )
                    print_exit_block(
                        underlying=closed.underlying_symbol,
                        strike=closed.strike,
                        option_type=closed.option_type,
                        direction=closed.direction,
                        lots=closed.lots,
                        lot_size=closed.lot_size,
                        entry_premium=closed.entry_premium,
                        exit_premium=closed.exit_premium,
                        pnl=closed.pnl,
                        bars_held=closed.bars_held,
                        exit_reason=str(closed.exit_reason),
                        equity=self._equity,
                    )

            # ── 8. Effective confidence (session × PDH/PDL × OR × FVG) ───
            effective_confidence = market_ctx.confidence * session_ctx.confidence_multiplier

            pdl_mult, pdl_reason = (1.0, "")
            if self.pdl_engine.ready and market_ctx.bias != MarketBias.NEUTRAL:
                pdl_mult, pdl_reason = self.pdl_engine.levels.confidence_multiplier(
                    spot=bar.close_price, bias=str(market_ctx.bias)
                )
            effective_confidence *= pdl_mult

            or_mult, or_reason = (1.0, "")
            if self.or_engine.ready and market_ctx.bias != MarketBias.NEUTRAL:
                or_mult, or_reason = self.or_engine.current_range.confidence_multiplier(
                    spot=bar.close_price, bias=str(market_ctx.bias)
                )
            effective_confidence *= or_mult

            # FVG quality: re-score with actual bias if a fresh FVG scored this bar;
            # otherwise apply the stored score only while it is not stale.
            fvg_quality_for_entry = 1.0
            fvg_quality_reason = ""
            if fvg_events and market_ctx.bias != MarketBias.NEUTRAL:
                # Re-score with the now-known bias (step 3b used NEUTRAL)
                rescored = [
                    self.fvg_scorer.score_event(
                        event=e,
                        market_bias=str(market_ctx.bias),
                        recent_sweeps=_recent_sweeps,
                        active_htf_fvgs=_htf_fvgs,
                        mitigation_ratio=0.0,
                    )
                    for e in fvg_events
                ]
                best_rescored = max(rescored, key=lambda s: s.total)
                self._fvg_quality = best_rescored.total
                self._fvg_quality_bar = bar_index
                fvg_quality_for_entry = best_rescored.confidence_multiplier()
                if best_rescored.blocks_entry:
                    fvg_quality_reason = f"low FVG quality={best_rescored.total:.2f} (disp={best_rescored.displacement:.2f} sess={best_rescored.session:.2f} sweep={best_rescored.sweep_origin:.2f} htf={best_rescored.htf_alignment:.2f})"
            elif bar_index - self._fvg_quality_bar <= FVG_QUALITY_STALENESS_BARS:
                # Recent stored FVG quality still valid — apply it
                stored_score_obj = FVGQualityScore(
                    total=self._fvg_quality,
                    displacement=0.0, session=0.0, sweep_origin=0.0,
                    htf_alignment=0.0, freshness=0.0, volume=0.0,
                )
                fvg_quality_for_entry = stored_score_obj.confidence_multiplier()
                if self._fvg_quality < FVG_QUALITY_BLOCK_THRESHOLD:
                    fvg_quality_reason = f"stored low FVG quality={self._fvg_quality:.2f}"
            # else: stale — no FVG penalty, quality defaults to 1.0

            effective_confidence *= fvg_quality_for_entry

            # Setup validation: sequential sweep → BOS → displacement → FVG
            # Observability only — not applied to effective_confidence until
            # sweep detection is generating sufficient confirmed events.
            setup_val = self.sweep_validator.validate(
                bar_index=bar_index,
                bar_ts=bar_ts,
                bias=str(market_ctx.bias),
                recent_sweeps=_recent_sweeps,
            )
            # effective_confidence *= setup_val.setup_score  # disabled: sweep rarely fires

            # ── 9. Strike selection ───────────────────────────────────────
            strike_rec: StrikeRecommendation | None = None
            if (
                market_ctx.bias != MarketBias.NEUTRAL
                and effective_confidence >= cfg.min_confidence
                and session_ctx.allow_entry
                and not fvg_quality_reason  # FVG quality did not block
                and options_ctx.strike_candidates
                and not self.option_trader.has_open_position()
            ):
                trade_signals += 1
                try:
                    strike_result = self.strike_engine.select(
                        bias=market_ctx.bias,
                        candidates=list(options_ctx.strike_candidates),
                        valuation_timestamp=bar_ts,
                        oi_snapshot=options_ctx.oi_snapshot,
                        gex_snapshot=options_ctx.gex_snapshot,
                        atm_iv=options_ctx.atm_iv,
                    )
                    strike_rec = strike_result.best()
                except Exception:
                    strike_rec = None
            elif market_ctx.bias != MarketBias.NEUTRAL and not self.option_trader.has_open_position():
                # Log which filter fired
                if not session_ctx.allow_entry and session_ctx.filter_reason:
                    session_blocks += 1
                    if session_blocks <= 3 or bar_index % 100 == 0:
                        print_warning_block(
                            "SESSION BLOCK",
                            bar_index=bar_index,
                            reason=session_ctx.filter_reason,
                        )
                elif not setup_val.is_valid:
                    setup_blocks += 1
                    if setup_blocks <= 5:
                        sv = setup_val
                        print_warning_block(
                            "SETUP BLOCK",
                            bar_index=bar_index,
                            reason=sv.reason,
                            sweep="Y" if sv.sweep_ok else "N",
                            bos="Y" if sv.bos_ok else "N",
                            disp="Y" if sv.disp_ok else "N",
                        )
                elif fvg_quality_reason:
                    fvg_blocks += 1
                    if fvg_blocks <= 5:
                        print_warning_block(
                            "FVG QUALITY BLOCK",
                            bar_index=bar_index,
                            reason=fvg_quality_reason,
                        )
                elif pdl_reason:
                    pdl_blocks += 1
                    if pdl_blocks <= 5:
                        print_warning_block(
                            "PDL BLOCK",
                            bar_index=bar_index,
                            conf=f"{market_ctx.confidence:.2f}→{effective_confidence:.2f}",
                            reason=pdl_reason,
                        )
                elif or_reason:
                    or_blocks += 1
                    if or_blocks <= 5:
                        print_warning_block(
                            "OR BLOCK",
                            bar_index=bar_index,
                            conf=f"{market_ctx.confidence:.2f}→{effective_confidence:.2f}",
                            reason=or_reason,
                        )

            # ── 10. Position sizing and entry ─────────────────────────────
            if strike_rec is not None:
                sizing = self._size_position(strike_rec, market_ctx, options_ctx, bar_ts,
                                             effective_confidence=effective_confidence)
                if sizing.tradable and sizing.recommended_lots >= 1:
                    lots = sizing.recommended_lots
                    premium_cost = strike_rec.last_traded_price * strike_rec.lot_size * lots
                    self._open_premium_cost = premium_cost
                    self.option_trader.open(
                        recommendation=strike_rec,
                        direction=str(market_ctx.bias),
                        bar_index=bar_index,
                        lots=lots,
                    )
                    if fvg_quality_for_entry < 1.0:
                        fvg_quality_filters += 1  # entry allowed but confidence was reduced
                    self.strike_validator.record_entry(
                        trade_id=entries_made,
                        bar_index=bar_index,
                        strike_rec=strike_rec,
                        all_candidates=list(options_ctx.strike_candidates),
                        entry_spot=bar.close_price,
                        entry_iv=options_ctx.atm_iv or 0.18,
                        lots=lots,
                        direction=str(market_ctx.bias),
                    )
                    pdl_lbl = self.pdl_engine.levels.to_dict() if self.pdl_engine.ready else {}
                    or_lbl  = self.or_engine.current_range.to_dict() if self.or_engine.ready else {}
                    print_entry_block(
                        direction=str(market_ctx.bias),
                        underlying=strike_rec.underlying_symbol,
                        strike=strike_rec.strike_price,
                        option_type=strike_rec.option_type,
                        expiry=strike_rec.expiry_date,
                        lots=lots,
                        lot_size=strike_rec.lot_size,
                        premium=strike_rec.last_traded_price,
                        notional=premium_cost,
                        bar_index=bar_index,
                        session=str(session_ctx.phase),
                        conf_raw=market_ctx.confidence,
                        conf_eff=effective_confidence,
                        fvg_quality=self._fvg_quality,
                        setup_score=setup_val.setup_score,
                        vol_ratio=_vol_ratio(options_ctx, cfg),
                        liquidity=strike_rec.breakdown.liquidity,
                        drawdown=self._current_drawdown(),
                        pdh=pdl_lbl.get("pdh"),
                        pdl=pdl_lbl.get("pdl"),
                        or_low=or_lbl.get("or_low"),
                        or_high=or_lbl.get("or_high"),
                    )
                    entries_made += 1
                else:
                    if not sizing.tradable:
                        print_warning_block(
                            "SIZING REJECTED",
                            bar_index=bar_index,
                            equity=f"₹{self._equity:,.0f}",
                            dd=f"{self._current_drawdown():.1%}",
                            reason=sizing.rejection_reason or "",
                        )

            # ── 11. Progress reporting ────────────────────────────────────
            if bar_index % 500 == 0 and bar_index > 0:
                _completed = self.option_trader.completed_trades
                _wins = sum(1 for t in _completed if t.pnl > 0)
                print_heartbeat(
                    bar_index=bar_index,
                    equity=self._equity,
                    drawdown=self._current_drawdown(),
                    trades=len(_completed),
                    wins=_wins,
                    bias=str(market_ctx.bias),
                    confidence=market_ctx.confidence,
                    session=str(session_ctx.phase),
                    has_position=self.option_trader.has_open_position(),
                )
            if bar_index % 100 == 0 and self.option_trader.has_open_position():
                pos = self.option_trader._position
                if pos is not None:
                    print_position_snapshot(
                        underlying=pos.underlying_symbol,
                        strike=pos.strike,
                        option_type=pos.option_type,
                        direction=pos.direction,
                        lots=pos.lots,
                        entry_premium=pos.entry_premium,
                        bars_held=bar_index - pos.bar_index,
                        equity=self._equity,
                        drawdown=self._current_drawdown(),
                    )

        # ── Force-close at end of data ────────────────────────────────────
        if self.option_trader.has_open_position():
            last_bar = bars[-1]
            closed = self.option_trader.evaluate_and_close(
                current_spot=last_bar.close_price,
                current_iv=0.18,
                current_date=_bar_timestamp(last_bar).date(),
                bar_index=len(bars),
                stop_loss_pct=cfg.stop_loss_pct,
                take_profit_pct=cfg.take_profit_pct,
                min_dte=0,
                force_reason="END_OF_DATA",
            )
            if closed is not None:
                self._equity += closed.pnl
                self._open_premium_cost = 0.0
                print_exit_block(
                    underlying=closed.underlying_symbol,
                    strike=closed.strike,
                    option_type=closed.option_type,
                    direction=closed.direction,
                    lots=closed.lots,
                    lot_size=closed.lot_size,
                    entry_premium=closed.entry_premium,
                    exit_premium=closed.exit_premium,
                    pnl=closed.pnl,
                    bars_held=closed.bars_held,
                    exit_reason=str(closed.exit_reason),
                    equity=self._equity,
                )

        # ── Flush partial daily bar ───────────────────────────────────────
        partial_daily = self.daily_acc.flush()
        if partial_daily is not None:
            self.market_ctx_builder.update(bar=partial_daily, timeframe="1d")
            daily_bars_fed += 1

        # ── Summary ───────────────────────────────────────────────────────
        option_trades = [t.as_paper_trade() for t in self.option_trader.completed_trades]
        summary = self.performance_tracker.calculate_summary(trades=option_trades)

        print_backtest_results(
            bars=len(bars),
            daily_bars_fed=daily_bars_fed,
            trade_signals=trade_signals,
            entries_made=entries_made,
            session_blocks=session_blocks,
            pdl_blocks=pdl_blocks,
            or_blocks=or_blocks,
            fvg_blocks=fvg_blocks,
            fvg_filters=fvg_quality_filters,
            setup_blocks=setup_blocks,
            total_trades=summary.total_trades,
            wins=summary.winning_trades,
            losses=summary.losing_trades,
            win_rate=summary.win_rate,
            total_pnl=summary.total_pnl,
            avg_pnl=summary.average_pnl,
            equity=self._equity,
            initial_equity=cfg.initial_equity,
            fvg_block_threshold=FVG_QUALITY_BLOCK_THRESHOLD,
        )
        self.strike_validator.print_report()

    # ── Private helpers ────────────────────────────────────────────────────

    def _size_position(
        self,
        rec: StrikeRecommendation,
        market_ctx: MarketContext,
        options_ctx,
        bar_ts: datetime,
        *,
        effective_confidence: float | None = None,
    ) -> PositionSizingResult:
        cfg = self.config
        entry_premium = rec.last_traded_price
        stop_premium  = max(entry_premium * cfg.stop_loss_pct, 0.01)

        current_drawdown = self._current_drawdown()
        available_cash   = max(self._equity - self._open_premium_cost, 0.0)
        portfolio_heat   = min(self._open_premium_cost / max(self._equity, 1.0), 1.0)

        portfolio = PortfolioRiskSnapshot(
            equity=max(self._equity, 1.0),
            available_cash=available_cash,
            current_portfolio_heat=portfolio_heat,
            current_drawdown_fraction=current_drawdown,
            max_allowed_drawdown_fraction=cfg.max_drawdown_fraction,
            correlation_exposure_fraction=0.0,
            open_position_count=1 if self.option_trader.has_open_position() else 0,
        )

        try:
            return self.position_sizer.size_position(
                request=PositionSizingRequest(
                    underlying_symbol=rec.underlying_symbol,
                    timestamp=bar_ts,
                    direction=str(market_ctx.bias),
                    instrument_type="OPTION",
                    entry_price=entry_premium,
                    stop_price=stop_premium,
                    contract_multiplier=1.0,
                    lot_size=rec.lot_size,
                    minimum_lot_count=1,
                    maximum_lot_count=cfg.max_lot_count,
                    confidence_score=min(effective_confidence if effective_confidence is not None else market_ctx.confidence, 1.0),
                    actionability_score=min(rec.composite_score, 1.0),
                    realized_volatility_ratio=_vol_ratio(options_ctx, cfg),
                    liquidity_score=min(rec.breakdown.liquidity, 1.0),
                    spread_bps=50.0,
                    slippage_bps_estimate=10.0,
                    expected_correlation_to_book=0.0,
                    portfolio=portfolio,
                ),
                valuation_timestamp=bar_ts,
            )
        except Exception as exc:
            # Sizing failed — fall back to minimum viable lot
            from platform_core.risk.position_sizing import (
                PositionSizingBreakdown, PositionSizingResult,
            )
            return PositionSizingResult(
                underlying_symbol=rec.underlying_symbol,
                timestamp=bar_ts,
                direction=str(market_ctx.bias),
                recommended_units=rec.lot_size,
                recommended_lots=1,
                notional_exposure=entry_premium * rec.lot_size,
                estimated_risk_amount=entry_premium * rec.lot_size * 0.5,
                estimated_round_trip_cost=entry_premium * rec.lot_size * 0.006,
                tradable=True,
                rejection_reason=None,
                breakdown=PositionSizingBreakdown(
                    base_risk_budget=0.0, confidence_scale=1.0,
                    volatility_scale=1.0, liquidity_scale=1.0,
                    drawdown_scale=1.0, correlation_scale=1.0,
                    portfolio_heat_scale=1.0, cash_scale=1.0,
                    effective_risk_budget=0.0, per_unit_risk=0.0,
                    preliminary_units=1.0, capped_units=1.0,
                ),
                metadata={"fallback_reason": str(exc)},
            )

    def _current_drawdown(self) -> float:
        return max(0.0, (self._peak_equity - self._equity) / max(self._peak_equity, 1.0))


# ── Module helpers ─────────────────────────────────────────────────────────────

def _detect_exit_signal(emitted_events: tuple, option_trader: OptionPaperTrader) -> str | None:
    pos = option_trader._position
    if pos is None:
        return None
    for event in emitted_events:
        evt_val  = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        side_val = event.side.value if hasattr(event.side, "value") else str(event.side)
        is_choch = "CHOCH" in evt_val
        is_bos   = evt_val == "BOS"
        if pos.direction == "LONG"  and (is_choch or is_bos) and side_val == "BEARISH":
            return f"STRUCTURE_EXIT ({evt_val})"
        if pos.direction == "SHORT" and (is_choch or is_bos) and side_val == "BULLISH":
            return f"STRUCTURE_EXIT ({evt_val})"
    return None


def _bar_timestamp(bar: LiveBar) -> datetime:
    ts = bar.start_timestamp
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _bar_date(bar: LiveBar) -> date:
    return _bar_timestamp(bar).date()


def _vol_ratio(options_ctx, cfg: BacktestConfig) -> float:
    iv = options_ctx.atm_iv or cfg.reference_iv
    return max(iv / max(cfg.reference_iv, 0.001), 0.1)


def _lookup_lot_size(symbol: str) -> int:
    for underlying, lot_size in REFERENCE_LOT_SIZES.items():
        if symbol.upper() == underlying.value:
            return lot_size
    return 1


def _lookup_strike_step(symbol: str) -> int:
    for underlying, step in REFERENCE_STRIKE_STEPS.items():
        if symbol.upper() == underlying.value:
            return int(step)
    return 50


__all__ = [
    "BacktestConfig",
    "BacktestRunner",
    "DailyBarAccumulator",
    "OptionPaperTrader",
    "OptionTrade",
]
