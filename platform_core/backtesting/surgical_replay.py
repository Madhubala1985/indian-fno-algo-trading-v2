"""
surgical_replay.py — Phase-B2 replay driver (surgical, real-chain).

Replays the CORE live decision path on REAL historical data, so the model can be
validated on past option days without a live session:

    1m index bars → MultiTimeframeBuilder + DailyBarAccumulator → MarketContextBuilder
    (bias) → on a directional bias: OptionsContextBuilder + StrikeSelectionEngine on
    the REAL historical chain (B1) → score_margin gate → PositionSizer → ENTER at the
    real historical LTP → manage each minute, EXIT at the real historical LTP on a
    real StructureEngine BOS/CHOCH reversal, a stop (30%), a take-profit (2x), or EOD.

It composes the SAME engine classes the live pipeline uses (same configs), so the
strike selection / sizing / conviction logic is faithful. Entry AND exit use the
real recorded option premium (no Black-Scholes) — the thing we were flying blind
on. Fidelity v2: exits now run the real SwingDetector->StructureEngine stack and fire on
the same BOS/CHOCH events the live model exits on (the v1 bias-flip proxy is gone),
with the live evaluate_and_close precedence (structure->stop->take-profit). Still
approximated vs on_ticks: the full live ENTRY gate stack (contradiction cooldown,
chop brake, warm-up, routing) and bias timing -- refined next (1b/1c).

This is a validation tool, not the live path — it will not be byte-identical, but
it is faithful to the decisions that drive P&L.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta

from platform_core.data.multi_timeframe_builder import MultiTimeframeBuilder
from platform_core.backtesting.backtest_runner import DailyBarAccumulator, LiveBar
from platform_core.intelligence.market_context_builder import MarketContextBuilder
from platform_core.options.options_context.builder import (
    OptionsContextBuilder, OptionsContextBuilderConfig,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine, StrikeSelectionConfig,
)
from platform_core.data.live.conviction_shadow import ConvictionScorer
from platform_core.risk.position_sizing import (
    PositionSizer, PositionSizingConfig, PositionSizingRequest, PortfolioRiskSnapshot,
)

# ── Real StructureEngine stack (fidelity v2: BOS/CHOCH exits, not a bias proxy) ──
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
from platform_core.data.live.live_structure_adapter import LiveStructureAdapter

# Shared, import-safe context-contradiction cooldown (SAME semantics as live).
from platform_core.execution.context_cooldown import (
    contradiction_cooldown_active as _contradiction_cooldown_active,
    cooldown_bars_from_env as _cooldown_bars_from_env,
)
# Shared ENTRY gates (same code path as live — no drift): time-of-day cutoff +
# same-contract session loss limit. The OR engine below is the live class too.
from platform_core.execution.entry_gates import (
    DirectionEntryCounter as _DirectionEntryCounter,
    SameContractLossTracker as _SameContractLossTracker,
    entry_cutoff_hour as _entry_cutoff_hour,
    time_cutoff_blocked as _time_cutoff_blocked,
)
from platform_core.analytics.structure.opening_range import OpeningRangeEngine as _OREngine
from platform_core.analytics.structure.session_context import (
    SessionContextEngine as _SessionEngine,
)
# GEX dealer-positioning gate — same module + env knobs as live [A7].
from platform_core.options.gex_gate import gex_wall_block as _gex_wall_block

_GEX_REGIME_GATE: bool = (os.environ.get("GEX_REGIME_GATE", "0") == "1")
_GEX_WALL_PROXIMITY_PCT: float = float(os.environ.get("GEX_WALL_PROXIMITY_PCT", "0.005") or 0.005)
_GEX_REGIME_VETO: bool = (os.environ.get("GEX_REGIME_VETO", "1") == "1")

# ── Live ENTRY-gate constants (fidelity v2, mirrored from live_structure_pipeline
# so the replay applies the SAME gate stack). Same env vars + defaults keep a single
# behavioural source of truth; if live retunes these, set the same env var here. ──
_LIVE_BARS_BEFORE_ENTRY: int = 20               # warm-up: N session bars before first entry
_SAME_UNDERLYING_STOP_COOLDOWN_BARS: int = 5    # after STOP_LOSS: any contract on underlying
_SAME_CONTRACT_STOP_COOLDOWN_BARS:   int = 15   # after STOP_LOSS: exact same contract
_SAME_UNDERLYING_STRUCT_COOLDOWN_BARS: int = int(os.environ.get("STRUCT_EXIT_UNDERLYING_COOLDOWN_BARS", "3")  or 3)
_SAME_CONTRACT_STRUCT_COOLDOWN_BARS:   int = int(os.environ.get("STRUCT_EXIT_CONTRACT_COOLDOWN_BARS",   "12") or 12)
_MAX_REENTRY_LOSSES:   int = int(os.environ.get("REENTRY_MAX_LOSSES", "3")   or 3)   # chop brake
_REENTRY_LOCKOUT_BARS: int = int(os.environ.get("REENTRY_LOCKOUT_BARS", "60") or 60)
_CONTEXT_CONTRADICTION_COOLDOWN_BARS: int = _cooldown_bars_from_env()               # default 5

# ── Live position-sizing constants (fidelity v3, mirrored from live Tier-2) ──────
# A fresh session sizes at Tier 2 (_determine_risk_tier: no drawdown, session didn't
# open above the gain gate). We replicate that: risk-fraction × capital, conviction/
# confidence/vol/liquidity scaling, lot ceilings + per-symbol caps + 10% heat — NOT
# a fixed lot count. Defaults match the live conservative profile; override per run.
_DEFAULT_CAPITAL: float = float(os.environ.get("REPLAY_CAPITAL", "400000") or 400000)  # ₹4L
_TIER2_RISK_FRAC_DEFAULT: float = float(os.environ.get("REPLAY_RISK_FRAC", "0.020") or 0.020)  # 2%
_TIER2_MAX_LOTS_DEFAULT: int = int(os.environ.get("REPLAY_MAX_LOTS", "4") or 4)
_PER_SYMBOL_MAX_LOTS: dict[str, int] = {"NIFTY": int(os.environ.get("PAPER_NIFTY_MAX_LOTS", "5") or 5)}
_MAX_HEAT_FRACTION: float = 0.10
_MAX_CONF_SCALE: float = 1.25
_MIN_CONF_SCALE: float = 0.25
_MAX_ACCOUNT_DD: float = 0.15
_REFERENCE_IV: float = 0.18
# Capital-scaled lot ceiling — MUST match live (_LOT_CAP_SCALE_BASE_CAPITAL) so the
# replay and live size identically. Ceiling grows as capital exceeds the baseline.
_LOT_CAP_SCALE_BASE: float = float(os.environ.get("LOT_CAP_SCALE_BASE_CAPITAL", "400000") or 400000)
# %-of-capital sizing rails — MUST match live: single-position premium cap (an
# option buyer's true worst case is the whole premium) + exchange freeze-qty rail.
_MAX_POSITION_PREMIUM_FRAC: float = float(
    os.environ.get("PAPER_MAX_POSITION_PREMIUM_FRAC", "0.15") or 0.15
)
_FREEZE_QTY_BY_SYM: dict[str, int] = {
    "NIFTY":     int(os.environ.get("FREEZE_QTY_NIFTY", "1800") or 1800),
    "BANKNIFTY": int(os.environ.get("FREEZE_QTY_BANKNIFTY", "900") or 900),
    "SENSEX":    int(os.environ.get("FREEZE_QTY_SENSEX", "1000") or 1000),
}
# Time-of-day entry cutoff (measured; promoted to live 2026-07-02 after the OOS
# confirmation — the live 07-02 afternoon entries lost -6,884 of -9,754). Default
# now matches live (12 IST) via the shared entry_gates module; env
# REPLAY_ENTRY_CUTOFF_IST=0 turns it off for A/B.
_ENTRY_CUTOFF_IST_HOUR: int = _entry_cutoff_hour("REPLAY_ENTRY_CUTOFF_IST")

# ── Profit-protection exits (breakeven ratchet + trail) — A/B knobs ──────────
# RATCHET: once premium reaches entry×(1+arm), the position can no longer close
# below entry (exit at breakeven if it round-trips). TRAIL: once armed, exit if
# premium gives back more than trail_frac from its peak. 0 = off. MUST match the
# live OptionPaperTrader semantics once promoted.
# Ratchet default 0.08 = live default (backtest_runner._RATCHET_ARM_PCT); the
# trail stays 0 — measured: structure exits already capture ~53% of peak and a
# mechanical trail cuts the big runners earlier (winners peak +17.9%/exit +9.4%).
_RATCHET_ARM_PCT: float = float(os.environ.get("EXIT_RATCHET_ARM_PCT", "0.08") or 0.08)
_TRAIL_GIVEBACK_FRAC: float = float(os.environ.get("EXIT_TRAIL_GIVEBACK_FRAC", "0") or 0)

_IST = timezone(timedelta(hours=5, minutes=30))
# Map MultiTimeframeBuilder OUTPUT keys → MCB timeframe slots. The builder emits
# MIN_5/MIN_15/HOUR_1/HOUR_4 (NOT "15m"/"1h"/"4h"), so this MUST key on those raw
# keys — mirrors live _MTF_KEY_MAP / BacktestRunner. MIN_5 is intentionally omitted
# (5m is not an MCB SUPPORTED_TIMEFRAMES slot). Bug (fixed 1c): the old map keyed on
# "15m"/"1h"/"4h", matched NOTHING, so 15m/1h/4h were NEVER fed to the MCB — bias ran
# on 1m+1d only, causing the replay's bias to diverge (late-LONG) from live.
_TF_NAME = {"MIN_15": "15m", "HOUR_1": "1h", "HOUR_4": "4h"}


def _structure_exit_reason(emitted_events, direction: str) -> str | None:
    """Real STRUCTURE_EXIT signal — a CHoCH/BOS that reverses the open position.

    Mirrors live `_option_exit_signal` (live_structure_pipeline.py) and
    `_detect_exit_signal` (backtest_runner.py) EXACTLY: a bearish BOS/CHOCH exits a
    LONG, a bullish BOS/CHOCH exits a SHORT. StructureSide enum values are lowercase
    ("bullish"/"bearish") — the .lower() is load-bearing (2026-06-04 bug: uppercase
    comparisons silently never fired). This replaces the v1 bias-flip proxy so the
    replay exits on the SAME structural events the live model exits on.
    """
    for event in emitted_events:
        evt = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        side = (event.side.value if hasattr(event.side, "value") else str(event.side)).lower()
        is_reversal = "CHOCH" in evt or evt == "BOS"
        if not is_reversal:
            continue
        if direction == "LONG" and side == "bearish":
            return f"STRUCTURE_EXIT ({evt})"
        if direction == "SHORT" and side == "bullish":
            return f"STRUCTURE_EXIT ({evt})"
    return None


def _contradicts_bias(emitted_events, bias: str) -> str | None:
    """Entry-side twin of _structure_exit_reason: a BOS/CHOCH this bar whose side
    OPPOSES the directional MCB bias (mirrors live [A2] CONTEXT_CONTRADICTION gate,
    live_structure_pipeline.py ~3779). A bullish break contradicts SHORT; a bearish
    break contradicts LONG. Returns "<evt>_<SIDE>" for the block reason, else None.
    NEUTRAL bias is never contradicted (matches live)."""
    b = bias.upper()
    if b not in ("LONG", "SHORT"):
        return None
    for event in emitted_events:
        evt = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        side = (event.side.value if hasattr(event.side, "value") else str(event.side)).lower()
        if not ("CHOCH" in evt or evt == "BOS"):
            continue
        if b == "SHORT" and side == "bullish":
            return f"{evt}_BULLISH"
        if b == "LONG" and side == "bearish":
            return f"{evt}_BEARISH"
    return None


@dataclass
class ReplayTrade:
    underlying: str
    contract: str
    direction: str
    entry_ts: str
    entry_prem: float
    exit_ts: str = ""
    exit_prem: float = 0.0
    lots: int = 0
    lot_size: int = 0
    pnl: float = 0.0
    reason: str = ""
    conviction: float = 0.0
    mcb_conf: float = 0.0      # MCB confidence at entry (saturation diagnostic)
    trend_score: float = 0.0   # pre-clip weighted directional score at entry
    or_ext: float = 0.0        # spot extension beyond the OR boundary in the trade
                               # direction, in OR-width units (trend-day signature)
    peak_prem: float = 0.0     # max favorable premium seen while open (MFE)


@dataclass
class ReplayResult:
    underlying: str
    day: str
    trades: list = field(default_factory=list)
    blocks: dict = field(default_factory=dict)   # entry-gate block counts by reason

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)


class SurgicalReplay:
    """Replay the core decision path for one underlying/day on real chains."""

    STOP_PCT = 0.70            # exit at ≤70% of entry (30% stop), like live (_STOP_LOSS_PCT)
    TAKE_PROFIT_PCT = 2.00     # exit at ≥200% of entry (premium doubles), like live (_TAKE_PROFIT_PCT)
    MIN_SCORE_MARGIN = 0.015   # SENSEX-style margin gate

    def __init__(self, *, underlying_symbol: str, feed_symbol: str | None = None,
                 conviction_floor: float | None = None,
                 capital: float = _DEFAULT_CAPITAL,
                 risk_frac: float = _TIER2_RISK_FRAC_DEFAULT,
                 max_lots: int = _TIER2_MAX_LOTS_DEFAULT) -> None:
        # The structure stack validates each bar's instrument_id against its config,
        # and the adapter tags StructureBars with bar.symbol = the FEED symbol (e.g.
        # NIFTY50, not NIFTY). So swing/structure MUST be built on the feed symbol;
        # only the MCB keys on the underlying. Defaults to underlying_symbol for the
        # SENSEX case where feed == underlying.
        struct_symbol = feed_symbol or underlying_symbol
        self._mtf = MultiTimeframeBuilder()
        self._daily = None
        self._ctx = MarketContextBuilder(underlying_symbol=underlying_symbol, min_history_bars=20)
        self._opt = OptionsContextBuilder(config=OptionsContextBuilderConfig(compute_missing_greeks=True))
        self._strike = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))
        self._conv = ConvictionScorer()
        self._conv_floor = conviction_floor

        # ── Position sizing (fidelity v3): % of capital, not fixed lots ──────
        self._capital = capital
        self._max_lots = min(max_lots, _PER_SYMBOL_MAX_LOTS.get(underlying_symbol, max_lots))
        self._sizer = PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=risk_frac,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
            max_confidence_scale=_MAX_CONF_SCALE,
            min_confidence_scale=_MIN_CONF_SCALE,
        ))
        # Min-lot override budget (live _PAPER_MAX_SINGLE_TRADE_RISK_FRAC = risk_frac):
        # when the sizer rejects a setup for insufficient budget, live still trades 1
        # lot if that single lot's risk fits this cap and heat fits.
        self._max_single_trade_frac = risk_frac

        # ── Real structure stack (fidelity v2) ───────────────────────────────
        # Built once per SurgicalReplay instance = once per replayed day. Live
        # rebuilds/gap-resets structure state every morning (the overnight >10m
        # gap trips _STRUCTURE_GAP_RESET_MIN), so a fresh engine per day matches
        # live: swings/BOS/CHOCH accumulate only from the day's own bars. Warm-
        # start bars feed MCB/MTF/daily (bias) but NOT structure — same as live.
        _tf = "MIN_1"
        self._adapter = LiveStructureAdapter()
        self._swing = SwingDetector(
            config=SwingDetectorConfig(instrument_id=struct_symbol, timeframe=_tf)
        )
        self._engine = StructureEngine(
            config=StructureEngineConfig(instrument_id=struct_symbol, timeframe=_tf),
            liquidity_sweep_detector=LiquiditySweepDetector(
                config=LiquiditySweepDetectorConfig(instrument_id=struct_symbol, timeframe=_tf)
            ),
            bos_detector=BOSDetector(
                config=BOSDetectorConfig(instrument_id=struct_symbol, timeframe=_tf)
            ),
            choch_detector=CHOCHDetector(
                config=CHOCHDetectorConfig(instrument_id=struct_symbol, timeframe=_tf)
            ),
            fvg_engine=FVGEngine(
                config=FVGEngineConfig(instrument_id=struct_symbol, timeframe=_tf)
            ),
            support_resistance_engine=SupportResistanceEngine(
                config=SupportResistanceEngineConfig(instrument_id=struct_symbol, timeframe=_tf)
            ),
        )

    def _feed_structure(self, bar: LiveBar):
        """Feed one 1m bar through swing→structure, returning emitted_events.

        Identical sequence to live on_ticks and BacktestRunner: adapter.convert_bar
        → swing_detector.process_bar → engine.process(StructureEngineInput). Called
        on EVERY session bar (regardless of bias/position) so structure state is
        continuous, exactly like live.
        """
        sbar = self._adapter.convert_bar(live_bar=bar)
        swing = self._swing.process_bar(sbar)
        result = self._engine.process(
            payload=StructureEngineInput(bar=sbar, swing_points=swing.confirmed_points)
        )
        return result.emitted_events

    def _size_lots(self, *, best, underlying: str, bias: str, atm_iv, conf: float,
                   equity: float, peak: float, session_open: float,
                   open_premium_cost: float, lot_size: int, bar_ts) -> int:
        """% -of-capital lot sizing — mirrors live _size_live_position (Tier 2) and
        BacktestRunner._size_position. lots = risk_frac × capital × conf/vol/liq/dd
        scales ÷ per-unit-risk, capped by max_lots (+ per-symbol cap) and 10% heat.
        Returns recommended lots (0 = not tradable → skip). Replaces the old fixed 3."""
        entry_premium = float(best.last_traded_price)
        stop_premium = max(entry_premium * self.STOP_PCT, 0.01)   # 30% stop, like live
        sizing_equity = max(min(equity, session_open), 1.0)       # opening-equity basis
        # Lot rails — mirrors live _size_live_position exactly:
        #   max_lots 0 = pure %-of-capital (no fixed ceiling); else capital-scaled.
        #   Then freeze-qty rail + single-position premium cap (tail defense).
        eff_max_lots = self._max_lots
        if eff_max_lots <= 0:
            eff_max_lots = 10_000
        elif _LOT_CAP_SCALE_BASE > 0.0:
            eff_max_lots = int(self._max_lots * max(1.0, session_open / _LOT_CAP_SCALE_BASE))
        _freeze_qty = _FREEZE_QTY_BY_SYM.get(underlying, 0)
        if _freeze_qty > 0 and lot_size > 0:
            eff_max_lots = min(eff_max_lots, max(_freeze_qty // lot_size, 1))
        if _MAX_POSITION_PREMIUM_FRAC > 0.0 and entry_premium > 0.0 and lot_size > 0:
            eff_max_lots = min(eff_max_lots, int(
                (_MAX_POSITION_PREMIUM_FRAC * session_open) // (entry_premium * lot_size)
            ))
        if eff_max_lots < 1:
            return 0    # one lot already exceeds the premium cap — reject
        drawdown = max(0.0, (peak - equity) / max(peak, 1.0))
        heat = min(open_premium_cost / max(equity, 1.0), 1.0)
        portfolio = PortfolioRiskSnapshot(
            equity=sizing_equity,
            available_cash=max(sizing_equity - open_premium_cost, 0.0),
            current_portfolio_heat=heat,
            current_drawdown_fraction=drawdown,
            max_allowed_drawdown_fraction=_MAX_ACCOUNT_DD,
            correlation_exposure_fraction=0.0,
            open_position_count=0,
        )
        try:
            res = self._sizer.size_position(
                request=PositionSizingRequest(
                    underlying_symbol=underlying,
                    timestamp=bar_ts,
                    direction=bias,
                    instrument_type="OPTION",
                    entry_price=entry_premium,
                    stop_price=stop_premium,
                    contract_multiplier=1.0,
                    lot_size=lot_size,
                    minimum_lot_count=1,
                    maximum_lot_count=eff_max_lots,
                    confidence_score=min(conf, 1.0),
                    actionability_score=min(best.composite_score, 1.0),
                    realized_volatility_ratio=max((atm_iv or _REFERENCE_IV) / max(_REFERENCE_IV, 0.001), 0.1),
                    liquidity_score=min(best.breakdown.liquidity, 1.0),
                    spread_bps=30.0,
                    slippage_bps_estimate=10.0,
                    expected_correlation_to_book=0.0,
                    portfolio=portfolio,
                ),
                valuation_timestamp=bar_ts,
            )
        except Exception:
            return 0
        if res.tradable and res.recommended_lots >= 1:
            return int(res.recommended_lots)
        # ── Min-lot override (mirrors live ~2457-2490) ───────────────────────
        # Sizer rejected for insufficient budget: still allow exactly 1 lot when
        # its absolute risk fits the single-trade cap AND premium heat fits 10%.
        one_lot_risk = (entry_premium - stop_premium) * lot_size
        one_lot_heat = (open_premium_cost + entry_premium * lot_size) / max(equity, 1.0)
        if (one_lot_risk <= self._max_single_trade_frac * sizing_equity
                and one_lot_heat <= _MAX_HEAT_FRACTION):
            return 1
        return 0

    def _feed_bar(self, bar: LiveBar) -> None:
        self._ctx.update(bar=bar, timeframe="1m")
        for _tf, _b in self._mtf.update_bar(bar=bar).items():
            name = _TF_NAME.get(_tf)
            if name:
                self._ctx.update(bar=_b, timeframe=name)
        db = self._daily.update(bar)
        if db is not None:
            self._ctx.update(bar=db, timeframe="1d")

    def warm_start(self, warm_bars: list[LiveBar], symbol: str) -> None:
        self._daily = DailyBarAccumulator(symbol=symbol)
        for b in warm_bars:
            self._feed_bar(b)

    def run_day(self, *, underlying: str, feed_symbol: str, exchange: str, expiry: date,
                lot_size: int, strike_step: float, day: date,
                session_bars: list[LiveBar], chain_by_min: dict, fetcher) -> ReplayResult:
        res = ReplayResult(underlying=underlying, day=day.isoformat())
        pos: ReplayTrade | None = None

        # ── Live ENTRY-gate state (fidelity v2, reset per day — matches live's
        # fresh-each-session state). Names/semantics mirror live UnderlyingState. ─
        bars_seen = 0                       # session bars processed (live_bars_seen)
        last_stop_bar = -9999               # bar of last STOP_LOSS exit
        last_stop_contract = ""
        last_struct_exit_bar = -9999        # bar of last STRUCTURE_EXIT exit
        last_struct_exit_contract = ""
        last_contradiction_bar: int | None = None
        reentry_loss_streak = 0             # consecutive losing exits (chop brake)
        reentry_lockout_until_bar = -1      # entries blocked until this bar
        contract_loss_tracker = _SameContractLossTracker()  # same-contract session loss limit
        direction_counter = _DirectionEntryCounter()        # same-direction session entry cap
        or_engine = _OREngine()             # live OR gate (buffer + extension chase)
        session_engine = _SessionEngine()   # live session phases (blocks 15:00+ CLOSING)

        # ── Capital/equity state for % sizing (session basis = starting capital) ─
        equity = self._capital
        peak_equity = self._capital
        session_open = self._capital
        open_premium_cost = 0.0

        def _block(reason_key: str) -> None:
            res.blocks[reason_key] = res.blocks.get(reason_key, 0) + 1

        def ltp_of(contract: str, minute_iso: str):
            for c in chain_by_min.get(minute_iso, []):
                if c["tradingsymbol"] == contract and c.get("ltp"):
                    return float(c["ltp"])
            return None

        for bar_idx, bar in enumerate(session_bars):
            bars_seen = bar_idx + 1
            self._feed_bar(bar)
            or_engine.update_bar(bar)
            emitted = self._feed_structure(bar)   # real BOS/CHOCH events this bar
            spot = bar.close_price
            minute_iso = bar.start_timestamp.astimezone(timezone.utc).isoformat()
            ctx = self._ctx.build(valuation_timestamp=bar.start_timestamp)
            bias = str(ctx.bias)

            # ── manage open position ────────────────────────────────────────
            # Exit precedence mirrors live evaluate_and_close EXACTLY:
            #   STRUCTURE_EXIT (force_reason) → STOP_LOSS → TAKE_PROFIT.
            # All marked at the REAL historical LTP (no BSM). The v1 bias-flip
            # proxy is gone: we now exit on the same BOS/CHOCH events the live
            # model exits on. (DTE_MINIMUM is inert here — replay_day selects the
            # expiry with min_dte>=2, so dte is never < 2 on the replayed day.)
            if pos is not None:
                cur = ltp_of(pos.contract, minute_iso)
                if cur is not None:
                    pos.peak_prem = max(pos.peak_prem, cur)
                    pct = cur / max(pos.entry_prem, 0.001)
                    _armed = (_RATCHET_ARM_PCT > 0.0
                              and pos.peak_prem >= pos.entry_prem * (1.0 + _RATCHET_ARM_PCT))
                    reason = _structure_exit_reason(emitted, pos.direction)
                    if reason is None and pct <= self.STOP_PCT:
                        reason = f"STOP_LOSS ({1.0 - pct:.0%} loss)"
                    # Profit protection (same precedence as live once promoted):
                    # breakeven ratchet, then peak-giveback trail, then TP.
                    if reason is None and _armed and cur <= pos.entry_prem:
                        reason = "BREAKEVEN_RATCHET (was +{:.0%}, back to entry)".format(
                            pos.peak_prem / pos.entry_prem - 1.0)
                    if (reason is None and _armed and _TRAIL_GIVEBACK_FRAC > 0.0
                            and cur <= pos.peak_prem * (1.0 - _TRAIL_GIVEBACK_FRAC)):
                        reason = "TRAIL_STOP (peak +{:.0%}, gave back {:.0%})".format(
                            pos.peak_prem / pos.entry_prem - 1.0, _TRAIL_GIVEBACK_FRAC)
                    if reason is None and pct >= self.TAKE_PROFIT_PCT:
                        reason = f"TAKE_PROFIT ({pct:.0%} of entry)"
                    if reason:
                        pos.exit_ts, pos.exit_prem = minute_iso, cur
                        pos.pnl = (cur - pos.entry_prem) * pos.lot_size * pos.lots
                        pos.reason = reason
                        # ── Arm re-entry cooldowns (mirrors live ~3484-3507) ──
                        if "STOP_LOSS" in reason:
                            last_stop_bar, last_stop_contract = bar_idx, pos.contract
                        if "STRUCTURE_EXIT" in reason:
                            last_struct_exit_bar, last_struct_exit_contract = bar_idx, pos.contract
                        contract_loss_tracker.record_exit(
                            contract=pos.contract, direction=pos.direction, pnl=pos.pnl)
                        # Chop brake: a win resets the streak (trend); consecutive
                        # losing exits lock the underlying out briefly.
                        if pos.pnl > 0:
                            reentry_loss_streak = 0
                        else:
                            reentry_loss_streak += 1
                            if reentry_loss_streak >= _MAX_REENTRY_LOSSES:
                                reentry_lockout_until_bar = bar_idx + _REENTRY_LOCKOUT_BARS
                                reentry_loss_streak = 0
                        # Roll equity forward so subsequent sizing sees realised P&L
                        # (drawdown/heat evolve like live). Freed premium → heat 0.
                        equity += pos.pnl
                        peak_equity = max(peak_equity, equity)
                        open_premium_cost = 0.0
                        res.trades.append(pos); pos = None
                continue

            # ── look for an entry ───────────────────────────────────────────
            if bias not in ("LONG", "SHORT"):
                continue
            contracts = chain_by_min.get(minute_iso)
            if not contracts:
                continue
            snap = fetcher.snapshot_at(underlying=underlying, exchange=exchange,
                                       expiry=expiry, minute_iso=minute_iso, contracts=contracts)
            if snap is None:
                continue
            try:
                octx = self._opt.build(snapshots=[snap], spot_price=spot,
                                       lot_size=lot_size, valuation_timestamp=bar.start_timestamp)
                cands = list(octx.strike_candidates)
                if not cands:
                    continue
                sel = self._strike.select(bias=bias, candidates=cands,
                                          valuation_timestamp=bar.start_timestamp,
                                          oi_snapshot=octx.oi_snapshot,
                                          gex_snapshot=octx.gex_snapshot, atm_iv=octx.atm_iv)
                best = sel.best()
                if best is None:
                    continue

                # ── Live ENTRY gate stack (fidelity v2) ──────────────────────
                # Same order as live on_ticks pre-entry checks (live_structure_
                # pipeline.py ~3738-3896): first matching gate blocks the entry;
                # the contradiction gate arms its cooldown as a side effect. These
                # are the gates that suppress the same-strike re-entry churn the
                # v1 replay (no gates) exhibited. Quality gate (margin) is below,
                # after these — matching live (contradiction precedes margin).
                _blk: str | None = None
                if _time_cutoff_blocked(bar.start_timestamp, _ENTRY_CUTOFF_IST_HOUR):
                    _blk = f"TIME_CUTOFF(>={_ENTRY_CUTOFF_IST_HOUR}IST)"   # afternoon-chop guard
                # [SESSION] Live session-phase gate: PRE_OPEN/OPENING/CLOSING block
                # entries (live_structure_pipeline gates on session_ctx.allow_entry).
                # Fidelity fix: the replay previously allowed 15:00+ entries live
                # would never take.
                if _blk is None and not session_engine.classify(bar.start_timestamp).allow_entry:
                    _blk = "SESSION_PHASE_NO_ENTRY"
                # [OR] Opening-range filter (live: zeroes/penalizes effective_conf
                # below every tier min-conf, so both cases BLOCK). Uses the same
                # OpeningRange class incl. the breakout buffer.
                if _blk is None and or_engine.ready:
                    _or_mult, _or_reason = or_engine.current_range.confidence_multiplier(
                        spot=spot, bias=bias)
                    if _or_mult == 0.0:
                        _blk = "OR_FILTER_INSIDE"
                    elif _or_mult < 1.0:
                        _blk = "OR_FILTER_CONTRADICTION"
                # [A-contract] Same-contract session loss limit (shared with live).
                if _blk is None and contract_loss_tracker.blocked(
                        contract=best.option_symbol, direction=bias):
                    _blk = "SAME_CONTRACT_SESSION_BLOCK"
                # [A-direction] Same-direction session entry cap (shared with live).
                if _blk is None and direction_counter.blocked(direction=bias):
                    _blk = "DIRECTION_ENTRY_CAP"
                # [A7] GEX dealer-positioning gate (same module as live; opt-in).
                if _blk is None and _GEX_REGIME_GATE:
                    _gex_blk = _gex_wall_block(
                        bias=bias, spot=spot, gex=octx.gex_snapshot,
                        proximity_pct=_GEX_WALL_PROXIMITY_PCT,
                        block_long_gamma=_GEX_REGIME_VETO)
                    if _gex_blk:
                        _blk = _gex_blk.split()[0]
                if _blk is None and bar_idx < reentry_lockout_until_bar:        # [A-chop]
                    _blk = "CHOP_REENTRY_LOCKOUT"
                if _blk is None and bars_seen < _LIVE_BARS_BEFORE_ENTRY:        # [A] warm-up
                    _blk = "WARM_START_LOCKOUT"
                if _blk is None:                                               # [A2] contradiction
                    _contra = _contradicts_bias(emitted, bias)
                    if _contra is not None:
                        last_contradiction_bar = bar_idx   # arm the cooldown
                        _blk = f"CONTEXT_CONTRADICTION({_contra})"
                if _blk is None:                                               # [A3] contra cooldown
                    _cd_blocked, _ = _contradiction_cooldown_active(
                        bar_idx, last_contradiction_bar, _CONTEXT_CONTRADICTION_COOLDOWN_BARS)
                    if _cd_blocked:
                        _blk = "CONTEXT_COOLDOWN"
                _bars_since_stop = bar_idx - last_stop_bar
                if _blk is None and _bars_since_stop < _SAME_UNDERLYING_STOP_COOLDOWN_BARS:   # [B1]
                    _blk = "STOP_COOLDOWN_underlying"
                if (_blk is None and best.option_symbol == last_stop_contract              # [B2]
                        and _bars_since_stop < _SAME_CONTRACT_STOP_COOLDOWN_BARS):
                    _blk = "STOP_COOLDOWN_contract"
                _bars_since_struct = bar_idx - last_struct_exit_bar
                if _blk is None and _bars_since_struct < _SAME_UNDERLYING_STRUCT_COOLDOWN_BARS:  # [B1s]
                    _blk = "STRUCT_EXIT_COOLDOWN_underlying"
                if (_blk is None and best.option_symbol == last_struct_exit_contract          # [B2s]
                        and _bars_since_struct < _SAME_CONTRACT_STRUCT_COOLDOWN_BARS):
                    _blk = "STRUCT_EXIT_COOLDOWN_contract"
                if _blk is not None:
                    _block(_blk)
                    continue

                margin = 0.0
                recs = sel.recommendations if hasattr(sel, "recommendations") else []
                if len(recs) >= 2:
                    margin = recs[0].composite_score - recs[1].composite_score
                if margin < self.MIN_SCORE_MARGIN:
                    continue
                conv, _ = self._conv.score(
                    composite=best.composite_score, score_margin=margin,
                    liquidity=best.breakdown.liquidity,
                    vol_ratio=max((octx.atm_iv or 0.15) / 0.18, 0.1),
                    fvg_quality=1.0)
                if self._conv_floor is not None and conv < self._conv_floor:
                    continue
                entry = float(best.last_traded_price)
                if entry <= 0:
                    continue
                # % -of-capital sizing (mirrors live Tier 2) — NOT a fixed lot count.
                lots = self._size_lots(
                    best=best, underlying=underlying, bias=bias, atm_iv=octx.atm_iv,
                    conf=ctx.confidence, equity=equity, peak=peak_equity,
                    session_open=session_open, open_premium_cost=open_premium_cost,
                    lot_size=lot_size, bar_ts=bar.start_timestamp)
                if lots < 1:
                    _block("SIZING_ZERO_LOTS")
                    continue
                _or_ext = 0.0
                if or_engine.ready:
                    _r = or_engine.current_range
                    _w = max(_r.or_high - _r.or_low, 1e-9)
                    _or_ext = ((spot - _r.or_high) if bias == "LONG"
                               else (_r.or_low - spot)) / _w
                pos = ReplayTrade(underlying=underlying, contract=best.option_symbol,
                                  direction=bias, entry_ts=minute_iso, entry_prem=entry,
                                  lots=lots, lot_size=lot_size, conviction=round(conv, 3),
                                  mcb_conf=round(float(ctx.confidence), 3),
                                  trend_score=round(float(getattr(ctx, "trend_score", 0.0)), 3),
                                  or_ext=round(_or_ext, 3), peak_prem=entry)
                direction_counter.record_entry(direction=bias)
                open_premium_cost = entry * lot_size * lots
            except Exception:
                continue

        # EOD close any open position at last real LTP
        if pos is not None and session_bars:
            last_iso = session_bars[-1].start_timestamp.astimezone(timezone.utc).isoformat()
            cur = ltp_of(pos.contract, last_iso) or pos.entry_prem
            pos.exit_ts, pos.exit_prem = last_iso, cur
            pos.pnl = (cur - pos.entry_prem) * pos.lot_size * pos.lots
            pos.reason = "EOD"
            res.trades.append(pos)
        return res
