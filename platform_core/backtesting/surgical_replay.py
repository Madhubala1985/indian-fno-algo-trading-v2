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

_IST = timezone(timedelta(hours=5, minutes=30))
# MarketContextBuilder supports 1m/15m/1h/4h/1d — the MTF builder also emits 5m,
# which is skipped (not a supported MCB slot).
_TF_NAME = {"15m": "15m", "1h": "1h", "4h": "4h"}


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

    def __init__(self, *, underlying_symbol: str, conviction_floor: float | None = None) -> None:
        self._mtf = MultiTimeframeBuilder()
        self._daily = None
        self._ctx = MarketContextBuilder(underlying_symbol=underlying_symbol, min_history_bars=20)
        self._opt = OptionsContextBuilder(config=OptionsContextBuilderConfig(compute_missing_greeks=True))
        self._strike = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))
        self._conv = ConvictionScorer()
        self._conv_floor = conviction_floor

        # ── Real structure stack (fidelity v2) ───────────────────────────────
        # Built once per SurgicalReplay instance = once per replayed day. Live
        # rebuilds/gap-resets structure state every morning (the overnight >10m
        # gap trips _STRUCTURE_GAP_RESET_MIN), so a fresh engine per day matches
        # live: swings/BOS/CHOCH accumulate only from the day's own bars. Warm-
        # start bars feed MCB/MTF/daily (bias) but NOT structure — same as live.
        _tf = "MIN_1"
        self._adapter = LiveStructureAdapter()
        self._swing = SwingDetector(
            config=SwingDetectorConfig(instrument_id=underlying_symbol, timeframe=_tf)
        )
        self._engine = StructureEngine(
            config=StructureEngineConfig(instrument_id=underlying_symbol, timeframe=_tf),
            liquidity_sweep_detector=LiquiditySweepDetector(
                config=LiquiditySweepDetectorConfig(instrument_id=underlying_symbol, timeframe=_tf)
            ),
            bos_detector=BOSDetector(
                config=BOSDetectorConfig(instrument_id=underlying_symbol, timeframe=_tf)
            ),
            choch_detector=CHOCHDetector(
                config=CHOCHDetectorConfig(instrument_id=underlying_symbol, timeframe=_tf)
            ),
            fvg_engine=FVGEngine(
                config=FVGEngineConfig(instrument_id=underlying_symbol, timeframe=_tf)
            ),
            support_resistance_engine=SupportResistanceEngine(
                config=SupportResistanceEngineConfig(instrument_id=underlying_symbol, timeframe=_tf)
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
                    pct = cur / max(pos.entry_prem, 0.001)
                    reason = _structure_exit_reason(emitted, pos.direction)
                    if reason is None and pct <= self.STOP_PCT:
                        reason = f"STOP_LOSS ({1.0 - pct:.0%} loss)"
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
                        # Chop brake: a win resets the streak (trend); consecutive
                        # losing exits lock the underlying out briefly.
                        if pos.pnl > 0:
                            reentry_loss_streak = 0
                        else:
                            reentry_loss_streak += 1
                            if reentry_loss_streak >= _MAX_REENTRY_LOSSES:
                                reentry_lockout_until_bar = bar_idx + _REENTRY_LOCKOUT_BARS
                                reentry_loss_streak = 0
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
                if bar_idx < reentry_lockout_until_bar:                         # [A-chop]
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
                pos = ReplayTrade(underlying=underlying, contract=best.option_symbol,
                                  direction=bias, entry_ts=minute_iso, entry_prem=entry,
                                  lots=3, lot_size=lot_size, conviction=round(conv, 3))
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
