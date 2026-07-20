import atexit
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from platform_core.intelligence.market_context_builder import (
    MarketBias,
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
from platform_core.options.strike_selection_audit import (
    StrikeAuditLogger,
    log_selection as _audit_selection,
)
from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.core.enums import UnderlyingFamily

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


from platform_core.paper_trading.performance_tracker import (
    PerformanceTracker,
)

from platform_core.paper_trading.terminal_display import (
    print_entry_block,
    print_exit_block,
    print_warning_block,
    print_performance_summary,
    print_heartbeat,
)
from platform_core.persistence.trade_database import TradeDatabase

from platform_core.paper_trading.trade_logger import (
    TradeLogger,
)

from platform_core.backtesting.backtest_runner import OptionPaperTrader, OptionPosition, DailyBarAccumulator
from platform_core.risk.position_sizing import (
    PortfolioRiskSnapshot,
    PositionSizer,
    PositionSizingBreakdown,
    PositionSizingConfig,
    PositionSizingRequest,
    PositionSizingResult,
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

from platform_core.analytics.structure.session_context import (
    SessionContextEngine,
)

from platform_core.analytics.structure.previous_day_levels import (
    PreviousDayLevelsEngine,
)

from platform_core.execution.entry_gates import (
    DirectionEntryCounter as _DirectionEntryCounter,
    SameContractLossTracker as _SameContractLossTracker,
    entry_cutoff_hour as _entry_cutoff_hour,
    time_cutoff_blocked as _time_cutoff_blocked,
)
from platform_core.analytics.structure.opening_range import (
    OpeningRangeEngine,
)

from platform_core.analytics.structure.fvg_scoring import (
    FVGQualityScore,
    FVGQualityScorer,
    FVG_QUALITY_BLOCK_THRESHOLD,
    FVG_QUALITY_STALENESS_BARS,
)

from platform_core.analytics.structure.models import StructureEventType

from platform_core.data.bar_builder import (
    BarBuilder,
    LiveBar,
)

from platform_core.data.live.instrument_registry import (
    INSTRUMENTS,
    OPTION_CHAIN_CONFIG,
    FEED_TO_OPTION_UNDERLYING,
    OPTION_UNDERLYING_TO_FEED,
)

from platform_core.data.live.kite_adapter import (
    KiteAdapter,
)

from platform_core.data.live.live_structure_adapter import (
    LiveStructureAdapter,
)

# Approach-A live chain recorder (off by default; env RECORD_OPTION_CHAIN=1).
# Exception-safe + no extra Kite calls — serialises a chain the live path already
# fetched. See platform_core/data/live/option_chain_recorder.py.
from platform_core.data.live.option_chain_recorder import record_chain as _record_chain

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

bias_detector = BiasDetector()

htf_bias_engine = (
    HTFBiasEngine()
)

performance_tracker = (
    PerformanceTracker()
)

trade_logger = TradeLogger()

multi_timeframe_builder = (
    MultiTimeframeBuilder()
)

# ── Options pipeline ──────────────────────────────────────────────────────────
# _PRIMARY_OPTION_TARGET    — canonical reference underlying (BANKNIFTY).
# _OPTION_TARGETS_ENABLED   — underlyings that EXECUTE trades. BANKNIFTY
#                             re-enabled 2026-07-15 (user directive: full-breadth
#                             unlock — all subscribed indices trade, options +
#                             futures sleeve; it was confirmation-only since
#                             2026-06-15). Enabling it also makes the futures
#                             sleeve's on_direction_signal path reachable for it
#                             (the call sits inside the enabled-targets block —
#                             night-explorer 07-14 §3 gap, closed here without
#                             moving the call site). NIFTY/BANKNIFTY trade on
#                             NFO; SENSEX on BFO. Env-overridable for a clean
#                             per-underlying rollback, e.g.
#                             OPTION_TARGETS_ENABLED=NIFTY,SENSEX reverts this.
# _OPTION_TARGETS_SHADOW    — empty; shadow infrastructure retained for future use.
# _OPTION_TARGETS_AVAILABLE — all underlyings with complete option-chain config
#                             (drives _runtime_by_underlying / MCB for all three).

_PRIMARY_OPTION_TARGET:    str       = "BANKNIFTY"
_OPTION_TARGETS_ENABLED:   frozenset = frozenset(
    s.strip().upper()
    for s in (
        os.environ.get("OPTION_TARGETS_ENABLED") or "BANKNIFTY,NIFTY,SENSEX"
    ).split(",")
    if s.strip()
)
_OPTION_TARGETS_SHADOW:    frozenset = frozenset()  # no active shadow targets
_OPTION_TARGETS_AVAILABLE: frozenset = frozenset({"BANKNIFTY", "NIFTY", "SENSEX"})
# Raw index/feed names that must never appear as a contract symbol
_RAW_INDEX_SYMBOLS:        frozenset = frozenset({"BANKNIFTY", "NIFTY", "NIFTY50", "SENSEX", "FINNIFTY"})
# Guard: ENABLED must be a subset of AVAILABLE — an env typo (e.g. "NIFTY5O")
# silently enabling a target with no chain config would crash mid-session.
_unknown_targets = _OPTION_TARGETS_ENABLED - _OPTION_TARGETS_AVAILABLE
if _unknown_targets:
    print(f"[CONFIG WARNING] OPTION_TARGETS_ENABLED contains unknown targets "
          f"{sorted(_unknown_targets)} — ignored (available: {sorted(_OPTION_TARGETS_AVAILABLE)})")
    _OPTION_TARGETS_ENABLED = _OPTION_TARGETS_ENABLED & _OPTION_TARGETS_AVAILABLE
_overlap = _OPTION_TARGETS_ENABLED & _OPTION_TARGETS_SHADOW
if _overlap:
    print(f"[CONFIG WARNING] Targets in both ENABLED and SHADOW sets: {_overlap} — shadow suppressed for those.")
_enabled_str = ",".join(sorted(_OPTION_TARGETS_ENABLED))
_shadow_str  = ",".join(sorted(_OPTION_TARGETS_SHADOW)) or "none"
print(f"[OPTIONS TARGETS] enabled={_enabled_str}  shadow={_shadow_str}")
_LOT_SIZE  = REFERENCE_LOT_SIZES[UnderlyingFamily.BANKNIFTY]
_STRIKE_STEP = float(REFERENCE_STRIKE_STEPS[UnderlyingFamily.BANKNIFTY])


def feed_symbol_to_option_underlying(feed_symbol: str) -> str:
    """
    Translate a WebSocket feed symbol (BarBuilder key) to the Kite option-chain
    underlying name.  Example: "NIFTY50" → "NIFTY".
    Returns feed_symbol unchanged if no mapping is defined.
    """
    return FEED_TO_OPTION_UNDERLYING.get(feed_symbol, feed_symbol)


def option_underlying_to_feed_symbol(option_underlying: str) -> str:
    """
    Inverse of feed_symbol_to_option_underlying.
    Example: "NIFTY" → "NIFTY50".
    Returns option_underlying unchanged if no mapping is defined.
    """
    return OPTION_UNDERLYING_TO_FEED.get(option_underlying, option_underlying)


def get_option_chain_config(option_underlying: str):
    """
    Return OptionChainMeta for option_underlying, or None if not configured.
    Use this instead of hardcoding exchange/lot_size/strike_step per target.
    """
    return OPTION_CHAIN_CONFIG.get(option_underlying)


def is_option_target_enabled(option_underlying: str) -> bool:
    """
    Return True when option_underlying is in the live-enabled set.

    Authoritative execution gate: only enabled targets may fetch chains,
    run strike selection, size positions, or open/close trades.
    """
    return option_underlying in _OPTION_TARGETS_ENABLED


def is_option_target_shadow(option_underlying: str) -> bool:
    """Return True when underlying is in shadow-only mode (chain+score audit, no trades)."""
    return option_underlying in _OPTION_TARGETS_SHADOW


def _bias_direction(bias: object) -> str:
    """
    Normalise any bias label to UP / DOWN / NEUTRAL so signals using different
    vocabularies (LONG/SHORT vs BULLISH/BEARISH) can be compared.  Used for the
    BANKNIFTY confirmation double-check.
    """
    u = str(bias).upper()
    if "LONG" in u or "BULL" in u:
        return "UP"
    if "SHORT" in u or "BEAR" in u:
        return "DOWN"
    return "NEUTRAL"


def _banknifty_confirmation(option_underlying: str, entry_bias: object) -> tuple[str, str]:
    """
    Return (banknifty_bias_str, verdict) comparing the NIFTY/SENSEX entry
    direction against BANKNIFTY's current MCB bias.  BANKNIFTY is confirmation-
    only (not traded), so this is a non-blocking market-view double-check.
    verdict ∈ {AGREES, DIVERGES, BANKNIFTY_NEUTRAL, UNAVAILABLE}.
    """
    try:
        _bnk_rt = _runtime_by_underlying.get("BANKNIFTY")
        if _bnk_rt is None:
            return "UNAVAILABLE", "UNAVAILABLE"
        _bnk_bias = _bnk_rt.market_ctx_builder.build().bias
    except Exception:
        return "UNAVAILABLE", "UNAVAILABLE"
    es, bs = _bias_direction(entry_bias), _bias_direction(_bnk_bias)
    if bs == "NEUTRAL":
        verdict = "BANKNIFTY_NEUTRAL"
    elif es == bs:
        verdict = "AGREES"
    else:
        verdict = "DIVERGES"
    return str(_bnk_bias), verdict


def can_run_option_intelligence(option_underlying: str) -> bool:
    """Return True when option chain fetch and strike scoring may run (enabled or shadow)."""
    return is_option_target_enabled(option_underlying) or is_option_target_shadow(option_underlying)


def can_execute_option_trade(option_underlying: str) -> bool:
    """Return True when position sizing and trade entry/exit may run (enabled targets only)."""
    return is_option_target_enabled(option_underlying)


# ── Per-underlying runtime state ───────────────────────────────────────────────

@dataclass
class UnderlyingRuntimeState:
    """
    All per-underlying runtime objects for one options execution target.

    One instance exists per entry in _runtime_by_underlying.
    Currently only BANKNIFTY is instantiated.

    Static config fields (feed_symbol … strike_step) are set once at
    startup from OPTION_CHAIN_CONFIG.  Stateful engine fields are
    instantiated per-underlying so each target has its own price history,
    PDL levels, opening range, and FVG state.

    Per-bar capture fields (captured_market_ctx, captured_strike_rec) are
    reset to None at the top of every bar tick and set within the options
    pipeline block.  They must be None-checked before any downstream use.
    """

    # ── Static config ──────────────────────────────────────────────────────
    feed_symbol:       str    # key in INSTRUMENTS / BarBuilder output
    option_underlying: str    # Kite instrument master name for chain queries
    exchange:          str    # "NFO" or "BFO"
    lot_size:          int
    strike_step:       float

    # ── Stateful engines ───────────────────────────────────────────────────
    market_ctx_builder: MarketContextBuilder
    option_trader:      OptionPaperTrader
    daily_acc:          DailyBarAccumulator
    pdl_engine:         PreviousDayLevelsEngine
    or_engine:          OpeningRangeEngine
    fvg_15m:            FVGEngine
    fvg_1h:             FVGEngine

    # ── Mutable per-underlying state ───────────────────────────────────────
    last_atm_iv:      float                     # updated each bar from live chain
    last_bar_close:   float = 0.0               # most recent 1m close price — used for EOD BSM
    last_stop_bar:    int   = -9999             # _bar_count at last stop-loss fire
    last_stop_contract: str = ""               # contract symbol of last stop-loss
    last_struct_exit_bar:      int = -9999      # _bar_count at last STRUCTURE_EXIT (chop filter)
    last_struct_exit_contract: str = ""        # contract symbol of last structure exit
    reentry_loss_streak:       int = 0          # consecutive losing exits (chop re-entry brake)
    reentry_lockout_until_bar: int = -1         # block new entries on this underlying until this bar
    # Same-contract session loss limit: N losing exits on one contract+direction
    # block that pair for the rest of the session (same-strike churn killer).
    contract_loss_tracker: _SameContractLossTracker = field(default_factory=_SameContractLossTracker)
    # Same-direction session entry cap: the day's initiative move gets harvested
    # at most N times; later same-direction re-entries chase the tail (measured).
    direction_entry_counter: _DirectionEntryCounter = field(default_factory=_DirectionEntryCounter)
    live_bars_seen:   int   = 0                 # live 1m bars received this session
    bar_history:      dict = field(default_factory=dict)  # {tf: [bar_dict, ...]}
    bars_since_save:  int  = 0                  # bars since last state-file write
    fvg_quality:      float = 1.0
    fvg_quality_bar:  int   = 0                 # set to -FVG_QUALITY_STALENESS_BARS-1
    sizers:           dict  = field(default_factory=dict)  # {0..3: PositionSizer}
    # Entry-gate inputs stashed each bar (2026-07-10). Defaults are permissive so
    # a gate never fires before real data sets these (backward-compatible w/ saved state).
    last_is_aligned:  bool  = True              # legacy 3-of-4 alignment (shadow-logged only)
    last_dir_regime:  str   = "UNKNOWN"         # regime de-size gate input (inert until set)
    # HTF OPPOSITION (2026-07-14 gate redesign): the gate now blocks only when the HTF
    # ACTIVELY OPPOSES the trade direction (≥2 of h4/h1/m15 against). NEUTRAL never blocks —
    # the old is_aligned needed 3-of-4 agreeing with daily hardcoded NEUTRAL, i.e. it was a
    # de-facto blanket long ban (cost the whole +0.8% 07-13 session). Defaults permissive.
    last_htf_opposed_long:  bool = False        # ≥2 of h4/h1/m15 BEARISH → oppose LONG
    last_htf_opposed_short: bool = False        # ≥2 of h4/h1/m15 BULLISH → oppose SHORT

    # ── Per-bar captures — reset to None each bar ──────────────────────────
    captured_market_ctx:   object | None = None
    captured_strike_rec:   object | None = None
    captured_score_margin: float  | None = None  # rank1 - rank2 composite score
    captured_gex_snapshot: object | None = None  # dealer gamma (GEX) snapshot at capture
    captured_tiebreak_pass: bool = False         # delta tie-break resolved a near-tie

    # ── MCB adaptation watch ───────────────────────────────────────────────
    contra_bos_count:  int = 0   # BOS events opposing current MCB bias (session total)

    # ── Context-contradiction cooldown (HARD gate, profile-independent) ────
    last_contradiction_bar:  int | None = None   # _bar_count at last context-contradiction rejection
    last_contradiction_meta: dict = field(default_factory=dict)  # ts/reason/structure/mcb_bias


# MTF key normalisation (matches BacktestRunner convention)
_MTF_KEY_MAP: dict[str, str] = {
    "MIN_5":  "5m",
    "MIN_15": "15m",
    "HOUR_1": "1h",
    "HOUR_4": "4h",
}

# ── Stateless shared engines (safe to share across all underlyings) ──────────
_opt_ctx_builder = OptionsContextBuilder(
    config=OptionsContextBuilderConfig(compute_missing_greeks=True)
)
# top_n=5 so the audit logger has rank-2..5 margin and top-candidate analysis
_strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))
_session_engine    = SessionContextEngine()
_fvg_scorer        = FVGQualityScorer()
_IST               = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time
_SESSION_CLOSE_TOD = 15 * 60 + 30  # 15:30 IST — market-close shutdown trigger (minutes since midnight)

# ── Virtual account — capital model ──────────────────────────────────────────
# Compounding by default: each session starts from the balance carried forward
# in the state file, so realised profits compound into the capital base and
# position sizing scales with the growing account. An explicit
# PAPER_STARTING_CAPITAL always overrides (fresh account / fixed-capital A-B
# runs); set PAPER_COMPOUND_CAPITAL=0 to disable roll-forward.
# For real account paper runs: PAPER_STARTING_CAPITAL=300000
# At < ₹3,00,000 the system sizes to 1 lot per trade and caps simultaneous
# open positions to 2 to keep portfolio heat under 10%.
# At < ₹5,00,000 open positions are capped at 2; otherwise the default is 3.
_STATE_FILE_PATH:   str  = "logs/paper_trading_state.json"
_COMPOUND_CAPITAL:  bool = (os.environ.get("PAPER_COMPOUND_CAPITAL", "1") == "1")
_EXPLICIT_CAPITAL:  bool = (os.environ.get("PAPER_STARTING_CAPITAL") is not None)

# score_margin on PRE-DTE-multiplier weighted_raw. The DTE multiplier is the
# same for every candidate in a chain (shared expiry), so it never changes the
# ranking — but it scales the rank1−rank2 margin (×0.20 at DTE≤2), pushing
# decisive short-DTE SENSEX selections below a floor calibrated at mult 1.0.
# Shared env name with surgical_replay. Default OFF = current behaviour.
_MARGIN_PRE_DTE:    bool = (os.environ.get("STRIKE_MARGIN_PRE_DTE", "0") == "1")

# ── Strike score-margin tie-break (live port of the surgical_replay P1 knob) ──
# A near-tie between the top-2 strikes is a coin the scorer cannot call: the
# dominant OI wall pumps the OTM candidate's oi_support to 1.00, exactly
# offsetting its worse delta/theta, and the margin gate reads the resulting tie
# as "no conviction" and blocks. "delta" enters the higher-|delta| of the two
# instead — the better *vehicle* for a directional trade.
#   Evidence: night 2026-07-19 H1 (delta05), 30 (underlying,day) replay pairs at
#   per-symbol live floors, costs included. delta05+gexspot+wide04 composite:
#   net +41,928 / ex-best-day +23,454 vs base +9,455 / +737, gain spread over
#   9 days (7 positive) and robust to dropping the best changed day.
# MIN_FLOOR guard (mandatory): the tie-break PASSES only at symbols whose gate
# floor is >= this threshold (NIFTY/BANKNIFTY at 0.05) and DAMAGES SENSEX at its
# loose 0.015 floor, where it unlocks junk near-ties. 0.0 = unrestricted.
# The adoption config is STRIKE_TIEBREAK=delta with STRIKE_TIEBREAK_MIN_FLOOR=0.05;
# delta MUST NOT reach SENSEX. Both default OFF = current live behaviour.
_STRIKE_TIEBREAK:  str = os.environ.get("STRIKE_TIEBREAK", "").strip().lower()
_STRIKE_TIEBREAK_MIN_FLOOR: float = float(
    os.environ.get("STRIKE_TIEBREAK_MIN_FLOOR", "0") or 0)

# Pre-arm structure-exit berth (live port of the surgical_replay wide04 knob).
# See _option_exit_signal() for the mechanism and evidence. 0 = off (legacy).
_EXIT_BOS_MIN_PEAK_PCT: float = float(
    os.environ.get("EXIT_BOS_MIN_PEAK_PCT", "0") or 0)


# The tie-break decision lives in an import-safe module so it can be unit-tested
# without importing this pipeline (which opens a Kite WebSocket at import).
from platform_core.options.strike_tiebreak import delta_tiebreak_pick as _delta_tiebreak_pick


def _resolve_initial_equity() -> float:
    """Capital basis for this session.

    Explicit PAPER_STARTING_CAPITAL wins. Otherwise, in compounding mode, roll
    forward from the state file's current equity (the balance we ended the last
    session on). Falls back to the fixed default only when there is no state.
    """
    if _EXPLICIT_CAPITAL:
        return float(os.environ["PAPER_STARTING_CAPITAL"])
    if _COMPOUND_CAPITAL:
        try:
            _eq = float(json.loads(Path(_STATE_FILE_PATH).read_text())["equity"])
            if _eq > 0:
                return _eq
        except Exception:
            pass
    return 1_000_000.0


_INITIAL_EQUITY: float = _resolve_initial_equity()

# Capital-tier simultaneous position limit.  Prevents portfolio heat breaching
# 10% at small capital (e.g. 2 × 1-lot SENSEX ≈ 7.6% heat at ₹300k).
_MAX_OPEN_POSITIONS: int = int(os.environ.get(
    "MAX_OPEN_POSITIONS",
    "1" if _INITIAL_EQUITY < 300_000 else "2" if _INITIAL_EQUITY < 500_000 else "3",
))
_REFERENCE_IV:   float = 0.18         # baseline IV for vol-ratio scaling

# ── Per-underlying runtime state ───────────────────────────────────────────────
# All three prepared underlyings are instantiated and in _OPTION_TARGETS_ENABLED.
# BANKNIFTY and NIFTY trade on NSE (NFO).  SENSEX trades on BSE (BFO).
# The shadow infrastructure (_run_shadow_option_intelligence etc.) is retained
# for future use but no active shadow targets remain.
#
# Execution guard: is_option_target_enabled(state.option_underlying) must return
# True before any execution path (chain fetch / sizing / trade open/close) runs.


def _build_underlying_state(key: str) -> UnderlyingRuntimeState:
    """Instantiate one UnderlyingRuntimeState from OPTION_CHAIN_CONFIG."""
    cfg = OPTION_CHAIN_CONFIG[key]
    return UnderlyingRuntimeState(
        feed_symbol       = cfg.feed_symbol,
        option_underlying = cfg.option_underlying,
        exchange          = cfg.exchange,
        lot_size          = cfg.lot_size,
        strike_step       = cfg.strike_step,
        market_ctx_builder = MarketContextBuilder(
            underlying_symbol=cfg.option_underlying, min_history_bars=20
        ),
        option_trader = OptionPaperTrader(),
        daily_acc     = DailyBarAccumulator(symbol=cfg.feed_symbol),
        pdl_engine    = PreviousDayLevelsEngine(),
        or_engine     = OpeningRangeEngine(),
        fvg_15m       = FVGEngine(config=FVGEngineConfig(
            instrument_id=cfg.feed_symbol, timeframe="MIN_15"
        )),
        fvg_1h        = FVGEngine(config=FVGEngineConfig(
            instrument_id=cfg.feed_symbol, timeframe="HOUR_1"
        )),
        last_atm_iv     = _REFERENCE_IV,
        fvg_quality_bar = -FVG_QUALITY_STALENESS_BARS - 1,
    )


_runtime_by_underlying: dict[str, UnderlyingRuntimeState] = {
    key: _build_underlying_state(key)
    for key in _OPTION_TARGETS_AVAILABLE   # BANKNIFTY, NIFTY, SENSEX
}

# Reverse lookup: feed_symbol ("NIFTY50", "BANKNIFTY", "SENSEX") → state
# Allows per-bar state routing without string manipulation in the hot path.
_state_by_feed: dict[str, UnderlyingRuntimeState] = {
    state.feed_symbol: state
    for state in _runtime_by_underlying.values()
}

# Risk tier parameters — tier is selected at runtime from account state
# Tier 1 (Defensive): entered when drawdown is elevated
# Tier 2 (Normal):    default operating mode
# Tier 3 (Aggressive): only when equity is above initial, low drawdown, high confidence
_TIER1_RISK_FRAC: float = 0.010   # 1.0% of equity at risk — fixed (never profile-adjusted)

# ── Paper risk profile — small-capital calibration ───────────────────────────
# PAPER_RISK_PROFILE: conservative (default, real-fund safe) | balanced | aggressive
# balanced/aggressive raise risk fractions suitable for paper-only accounts.
# At ₹300k the default 2% budget (₹6k) is below the 1-lot floor for all
# near-ATM index options (SENSEX ₹6,624 / NIFTY ₹6,825 / BANKNIFTY ₹8,550).
# balanced mode lifts Tier2/Tier3 to 4%, enabling SENSEX + NIFTY at ₹300k.
_PAPER_RISK_PROFILE: str = os.environ.get("PAPER_RISK_PROFILE", "conservative").lower()
if _PAPER_RISK_PROFILE not in ("conservative", "balanced", "aggressive"):
    _PAPER_RISK_PROFILE = "conservative"

_TIER2_RISK_FRAC: float = (
    float(os.environ["PAPER_TIER2_RISK_FRAC"])
    if os.environ.get("PAPER_TIER2_RISK_FRAC")
    else 0.040 if _PAPER_RISK_PROFILE in ("balanced", "aggressive")
    else 0.020
)
_TIER3_RISK_FRAC: float = (
    float(os.environ["PAPER_TIER3_RISK_FRAC"])
    if os.environ.get("PAPER_TIER3_RISK_FRAC")
    else 0.040 if _PAPER_RISK_PROFILE in ("balanced", "aggressive")
    else 0.035
)
# Max single-trade risk as fraction of session-opening equity used by the
# min-lot override.  All other safety gates still apply.
_PAPER_MAX_SINGLE_TRADE_RISK_FRAC: float = (
    float(os.environ["PAPER_MAX_SINGLE_TRADE_RISK_FRAC"])
    if os.environ.get("PAPER_MAX_SINGLE_TRADE_RISK_FRAC")
    else _TIER2_RISK_FRAC
)

# ── Per-trade hard MAX-LOSS cap (batch-4 §1a) — safety, default ON all modes ──
# Caps lots so a single trade's EX-ANTE worst-case loss cannot exceed this fraction of equity.
# Unlike the premium cap (bounds outlay), this bounds LOSS = outlay × adverse_frac. The −₹15,972
# trade (3.8% of equity) is exactly what this prevents. Set frac=0 to disable.
_PER_TRADE_MAX_LOSS_FRAC: float = float(os.environ.get("PER_TRADE_MAX_LOSS_FRAC", "0.015") or 0.015)
_PER_TRADE_ADVERSE_FRAC:  float = float(os.environ.get("PER_TRADE_ADVERSE_FRAC", "0.40") or 0.40)
from platform_core.risk.per_trade_loss_cap import max_loss_capped_lots as _max_loss_capped_lots
_TIER1_MAX_LOTS:  int   = 1
# Conviction-scaled lot ceilings (2026-06-15). Raised so high-conviction setups
# can use their FULL existing risk budget instead of being clipped by a low
# ceiling. This does NOT change per-trade risk: the sizer still caps lots at
# min(risk_budget_units, ceiling*lot_size), where risk_budget = equity *
# tier_risk_frac * confidence_scale(0.25..1.25) * haircuts. Raising the ceiling
# only lets the risk budget (not an arbitrary cap) be the binding constraint, so
# strong setups (high confidence + composite_score) book more; marginal setups
# still size down via confidence_scale. Portfolio heat (10%) + drawdown haircuts
# + daily-loss halt remain hard. Env-overridable for tuning.
# 0 = NO fixed ceiling (pure %-of-capital sizing; user directive 2026-07-03):
# lots derive from risk budget + premium cap + heat + freeze-qty rails only.
_TIER2_MAX_LOTS:  int   = int(os.environ.get("PAPER_TIER2_MAX_LOTS", "4") or 4)
_TIER3_MAX_LOTS:  int   = int(os.environ.get("PAPER_TIER3_MAX_LOTS", "6") or 6)

# ── %-of-capital sizing rails (replace fixed lot-count ceilings) ─────────────
# Single-position premium cap: for an option BUYER the true worst case is the
# ENTIRE premium (gap/halt through the stop), not the stop distance. At 8% risk
# and a 30% stop the sizer would otherwise deploy 26.7% of equity in ONE
# position — a single catastrophe event would erase months of compounding.
# Cap the premium outlay per position as a fraction of session-opening equity.
# Applies in ALL modes (tail-defense, never loosened by profiles).
_MAX_POSITION_PREMIUM_FRAC: float = float(
    os.environ.get("PAPER_MAX_POSITION_PREMIUM_FRAC", "0.15") or 0.15
)
# Correlated same-direction heat: SENSEX/NIFTY/BANKNIFTY are ~0.9-correlated —
# two same-direction option positions are ONE doubled bet, not diversification.
# Cap the COMBINED same-direction premium outlay across underlyings at this
# fraction of session equity (sits inside the 30% total heat cap; opposite-
# direction pairs are true hedges and are not restricted). 0 disables.
_CORRELATED_HEAT_FRACTION: float = float(
    os.environ.get("CORRELATED_HEAT_FRACTION", "0.20") or 0.20
)
# Exchange freeze-quantity rail (NSE/BSE per-order limits — an exchange rule,
# the only legitimate FIXED-number cap). Defensive: binds only when capital
# grows far beyond the current base. Env-overridable if exchange revises.
_FREEZE_QTY_BY_SYM: dict[str, int] = {
    "NIFTY":     int(os.environ.get("FREEZE_QTY_NIFTY", "1800") or 1800),
    "BANKNIFTY": int(os.environ.get("FREEZE_QTY_BANKNIFTY", "900") or 900),
    "SENSEX":    int(os.environ.get("FREEZE_QTY_SENSEX", "1000") or 1000),
}
# Capital-scaled lot ceiling (user directive 2026-07-01): the tier ceilings above are
# the BASE at _LOT_CAP_SCALE_BASE_CAPITAL; as the account compounds past it, the
# ceiling grows proportionally so sizing keeps scaling with capital instead of being
# frozen at a fixed cap. Floored at 1.0x (never REDUCES the base ceiling below the
# baseline). The 10-30% portfolio-heat cap + per-trade risk budget remain the real
# safety limits. Env-tunable; LOT_CAP_SCALE_BASE_CAPITAL=0 disables (fixed ceiling).
_LOT_CAP_SCALE_BASE_CAPITAL: float = float(
    os.environ.get("LOT_CAP_SCALE_BASE_CAPITAL", "400000") or 400000
)
_TIER1_MIN_CONF:  float = 0.55    # higher conviction bar in defensive mode
_TIER2_MIN_CONF:  float = 0.40
_TIER3_MIN_CONF:  float = 0.35    # tier 3 eligibility enforced at tier selection

# ── SENSEX-primary routing (user directive 2026-06-17) ───────────────────────
# SENSEX is the primary, historically-profitable option target.  NIFTY has
# repeatedly churned the same strike for net losses in range-bound tape
# (2026-06-17: NIFTY -Rs11,235 across 6 trades vs SENSEX +Rs14,982 in 1).
# NIFTY now enters ONLY when (a) SENSEX holds no open position (SENSEX gets first
# call on the opportunity) AND (b) NIFTY's effective confidence clears a
# very-strong bar.  This is a pure ADDITIONAL restriction on NIFTY entries —
# it never loosens any gate, and leaves SENSEX/BANKNIFTY paths unchanged.
# Env-tunable / reversible: SENSEX_PRIMARY_ROUTING=0 disables; NIFTY_VERY_STRONG_CONF
# adjusts the NIFTY bar (default 0.95; 2026-06-17 NIFTY chop ran ~0.91, so filtered).
# RE-ENABLED 2026-06-18 (default on): over the user's larger 3-4 day sample NIFTY
# is the net loser, and in symmetric mode whichever index fires first takes the
# single budget slot — so NIFTY firing first BLOCKS SENSEX (observed today as
# SENSEX SIZING_REJECTED).  SENSEX-primary stops NIFTY from blocking SENSEX:
# NIFTY defers when SENSEX holds a position and otherwise only enters on a
# very-strong (>=NIFTY_VERY_STRONG_CONF) setup.  NOTE: this restricts NIFTY, not
# SENSEX — it does not prevent SENSEX's own losing days (the gap-stop fix caps
# those).  A complete same-bar-race fix = evaluate SENSEX before NIFTY each bar.
# Set SENSEX_PRIMARY_ROUTING=0 to neutralize.
_SENSEX_PRIMARY_ROUTING:     bool  = (os.environ.get("SENSEX_PRIMARY_ROUTING", "1") == "1")
_NIFTY_VERY_STRONG_CONF:     float = float(os.environ.get("NIFTY_VERY_STRONG_CONF", "0.95") or 0.95)
_SENSEX_PRIORITY_UNDERLYING: str   = "SENSEX"

# ── GEX wall-proximity entry gate (PROTOTYPE 2026-06-18, opt-in / default OFF) ──
# Dealer-gamma "walls" act as intraday resistance/support: price tends to stall or
# reverse INTO a call wall (resistance) or put wall (support).  Chasing a momentum
# entry into a wall is the classic "entered right before the reversal" failure
# (e.g. 2026-06-18 SENSEX longs that reversed -40%).  When enabled, veto LONG
# entries within GEX_WALL_PROXIMITY_PCT below a call wall, and SHORT within that
# distance above a put wall.  Fail-OPEN (missing GEX never blocks); gex_snapshot is
# already computed in the options context.  Default OFF — enable with
# GEX_REGIME_GATE=1 for a deliberate A/B paper session before trusting it.
_GEX_REGIME_GATE:        bool  = (os.environ.get("GEX_REGIME_GATE", "0") == "1")
# MEASURED 2026-07-03 (batch 06-24..07-01, real OI): the veto DESTROYS the edge
# in both configs (full: +31,938 -> -1,082; wall-only: -6,918, 20% WR). The
# model's winners are momentum THROUGH walls (initiative flow), not fades — do
# NOT enable the veto. Shadow mode logs the verdict per entry evaluation
# without ever blocking, so live data keeps accumulating for re-evaluation.
_GEX_GATE_SHADOW:        bool  = (os.environ.get("GEX_GATE_SHADOW", "1") == "1")
# Widened 2026-06-23: at 0.25% the wall veto never triggered (0 blocks over two
# 06-19 sessions). 0.5% makes "near a wall" actually meaningful.
_GEX_WALL_PROXIMITY_PCT: float = float(os.environ.get("GEX_WALL_PROXIMITY_PCT", "0.005") or 0.005)

# ── Edge gates (2026-07-10, dry-run test — UNVALIDATED, env-reversible) ────────
# Evidence (66 trades): the LONG book bleeds −₹36k @34% WR while SHORT makes +₹50k
# @57% WR — the model over-trades the losing direction (bias-staleness). These two
# gates target that. Both default ON for the dry-run experiment; set to 0 to revert.
#   LONG_ALIGNMENT_GATE: block LONG entries when HTF structure is NOT aligned
#     (is_aligned=False) — the misaligned longs are the losers. SHORT untouched.
#   REGIME_DESIZE_MULT:  size multiplier applied when dir_regime is BALANCED (the
#     regime that lost 5/7 days this week). 1.0 disables; 0.5 halves size.
_LONG_ALIGNMENT_GATE: bool  = (os.environ.get("LONG_ALIGNMENT_GATE", "1") != "0")
_REGIME_DESIZE_MULT:  float = float(os.environ.get("REGIME_DESIZE_MULT", "0.5") or 0.5)
# NIFTY-long gate (2026-07-10, dry-run test): NIFTY-LONG is the single worst bucket
# (−₹21.8k @29% WR over 27 trades) while NIFTY-SHORT makes money (+₹13.7k @40%).
# Block NIFTY longs, keep NIFTY shorts. SENSEX untouched. NIFTY_LONG_GATE=0 reverts.
_NIFTY_LONG_GATE:     bool  = (os.environ.get("NIFTY_LONG_GATE", "1") != "0")
_override_printed: dict = {}   # (underlying, dir) -> printed flag (throttle: log state changes only)
_last_filter_reason: dict = {}  # underlying -> last ENTRY FILTER reason written to pt_decisions
# LONG_GAMMA regime veto — the bigger lever: when the GEX gate is enabled, also
# skip momentum chase-entries in a mean-reverting/pinning (long-gamma) regime.
# On by default whenever GEX_REGIME_GATE=1; set GEX_REGIME_VETO=0 to test wall-only.
_GEX_REGIME_VETO:        bool  = (os.environ.get("GEX_REGIME_VETO", "1") == "1")


from platform_core.options.gex_gate import gex_wall_block as _gex_wall_block

# Tier transition thresholds (deterministic, evaluated per bar)
# Defensive Tier 1 (hard-capped at _TIER1_MAX_LOTS=1) only engages on a genuine
# deeper drawdown. Raised 0.05 -> 0.10 on 2026-06-23 (user): a lingering ~5%
# drawdown from a week-old peak was holding the account in 1-lot defensive mode
# even on high-conviction setups, suppressing full multi-lot (Tier 2) sizing.
# The per-trade 30% stop, daily-loss circuit breaker, portfolio-heat and
# max-open-position caps remain the active risk controls below this threshold.
_TIER1_DD_ENTRY:  float = float(os.environ.get("TIER1_DD_ENTRY", "0.10") or 0.10)
_TIER3_MIN_GAIN:  float = 0.03    # Tier 3 requires equity > initial × 1.03
_TIER3_MAX_DD:    float = 0.025   # Tier 3 ineligible when drawdown ≥ 2.5%
_TIER3_CONF_GATE: float = 0.65    # Tier 3 requires confidence ≥ 65%

# Portfolio heat and trade management
# Max open-premium / equity. For option BUYING the premium is the full outlay,
# so this is the REAL binding constraint on lot count — it gates "more lots"
# more than risk-fraction or lot-ceiling do. Env-overridable; real_armed keeps
# the conservative 0.10 default, paper/dry-run raises it via the supervisor.
_MAX_HEAT_FRACTION: float = float(os.environ.get("PAPER_MAX_HEAT_FRACTION", "0.10") or 0.10)
# Conviction-scaled sizing: best setups get a larger risk-budget multiplier (so
# they take MORE lots); normal/weak setups size down. The sizer multiplies the
# risk budget by confidence_scale in [min, max]. Env-overridable (paper).
_MAX_CONF_SCALE: float = float(os.environ.get("PAPER_MAX_CONF_SCALE", "1.25") or 1.25)
_MIN_CONF_SCALE: float = float(os.environ.get("PAPER_MIN_CONF_SCALE", "0.25") or 0.25)
# Per-symbol lot ceiling (allocation control). NIFTY is a proven net loser
# (37% WR / net -ve vs SENSEX 69% / net +ve), so cap it well below the winner
# so it cannot out-size SENSEX even at the raised ceilings. Applies in ALL
# modes (defensive — safe for real funds too). Env-overridable.
_PER_SYMBOL_MAX_LOTS: dict[str, int] = {
    "NIFTY": int(os.environ.get("PAPER_NIFTY_MAX_LOTS", "5") or 5),
}
# Stop-loss: max loss from entry premium before a forced exit (premium must remain
# >= (1 - frac) of entry). History: 50% → 30% (tightened), env-tunable 2026-07-15
# (user directive — the supervisor live profile widens it to 0.45 to test the
# HARVESTED-EXIT finding: 55% of losing exits saw premium recover above entry
# within 60 min, i.e. our exits sit where liquidity gets taken; a wider stop is
# the direct A/B on that shakeout mechanism). Still subject to bar-close gap risk;
# the per-trade max-loss cap sizes lots off PER_TRADE_ADVERSE_FRAC, which must be
# kept >= this stop so the ex-ante loss bound stays honest.
_MAX_OPTION_PREMIUM_LOSS_FRACTION: float = float(
    os.environ.get("PAPER_OPTION_STOP_LOSS_FRAC", "0.30") or 0.30
)
_STOP_LOSS_PCT:     float = 1.0 - _MAX_OPTION_PREMIUM_LOSS_FRACTION   # 0.70 remaining
_TAKE_PROFIT_PCT:   float = 2.00
_MIN_DTE_TO_HOLD:   int   = 2
# Expiry-selection DTE floor: trade the NEAREST expiry with at least this many
# days of runway (best value — cheapest, highest gamma, chart-matching), rolling
# forward only when the front contract is about to expire. Replaces the prior
# "fetch nearest + next and rank both by score" (which paid up for the far expiry
# and added no proven edge per the backtest). Must be >= _MIN_DTE_TO_HOLD so we
# never enter an expiry already at the hold-exit threshold. Env-tunable.
_MIN_EXPIRY_DTE:    int   = max(int(os.environ.get("PAPER_MIN_EXPIRY_DTE", "2") or 2), _MIN_DTE_TO_HOLD)

# Stop-loss cooldown: prevent immediate re-entry after a loss exit.
# Proven defect: SENSEX re-entered the same contract at the exact bar of stop-loss exit.
_SAME_UNDERLYING_STOP_COOLDOWN_BARS: int = 5    # any contract on this underlying
_SAME_CONTRACT_STOP_COOLDOWN_BARS:   int = 15   # exact same contract symbol

# Structure-exit re-entry cooldown (chop filter, 2026-06-17).  STRUCTURE_EXIT
# (BOS/CHOCH) exits did NOT set the stop cooldown above, so NIFTY re-bought the
# SAME strike 1-2 bars after each BOS exit and churned the account in range-bound
# tape (NIFTY -Rs11,235 across 6 same-strike 24100CE round-trips).  Apply a
# re-entry cooldown after structure exits too.  Same-contract is the primary
# churn-killer (blocks re-buying the identical strike); same-underlying is short.
# Env-tunable; pure additional restriction — never loosens any gate.
_SAME_UNDERLYING_STRUCT_COOLDOWN_BARS: int = int(os.environ.get("STRUCT_EXIT_UNDERLYING_COOLDOWN_BARS", "3")  or 3)
_SAME_CONTRACT_STRUCT_COOLDOWN_BARS:   int = int(os.environ.get("STRUCT_EXIT_CONTRACT_COOLDOWN_BARS",   "12") or 12)

# Chop re-entry brake (2026-06-29). The bar-count cooldowns above were too loose:
# NIFTY bled across many re-entries on choppy days. The discriminator is consecutive
# LOSING exits (chop) vs wins (trend): after this many consecutive losing exits on an
# underlying, lock new entries on it for a window; a winning exit resets the streak,
# so trending re-entries (e.g. SENSEX 3 wins in a row) are never blocked. Symmetric,
# pure additional restriction, env-tunable.
# DELIBERATELY CONSERVATIVE: threshold = 3 (mirrors the existing GLOBAL 3-consec-loss
# halt — defensible as risk discipline, not P&L-tuned). On the 9-day NIFTY sample,
# more aggressive variants looked far better in backtest (2-consec +6,080; daily
# -3,000 loss-cap flips NIFTY to net-positive +7,445) but are textbook OVERFITTING
# on ~27 trades — and the 2-consec rule WOULD block the 06-11 recovery winner
# (-1,576,-4,435,+4,778). The 3-consec rule changed only 06-17 (+2,894) and made
# ZERO days worse; it never blocks a 2-loss-then-win recovery. Re-tune only with a
# much larger out-of-sample trade count.
_MAX_REENTRY_LOSSES:   int = int(os.environ.get("REENTRY_MAX_LOSSES", "3")   or 3)
_REENTRY_LOCKOUT_BARS: int = int(os.environ.get("REENTRY_LOCKOUT_BARS", "60") or 60)

# Confluence gate for Tier2/Tier3: require minimum OI support on the selected strike.
# Proven defect: SENSEX entry had oi_support=0.034 (near-zero OI = liquidity risk).
_MIN_OI_SUPPORT_TIER23: float = 0.10

# Live bar warmup gate: require at least this many live bars before allowing entry.
# Prevents entries driven purely by warm-start historical MCB state (no live confirmation).
_LIVE_BARS_BEFORE_ENTRY: int = 20

# Position reconciliation cadence (real_armed only): reconcile broker vs local
# positions every N completed bars; a HIGH/CRITICAL break halts trading.
_RECONCILE_EVERY_BARS: int = int(os.environ.get("RECONCILE_EVERY_BARS", "15") or 15)

# Context-contradiction cooldown: after a context-contradiction rejection fires
# for an underlying, block new entries on that underlying for N bars.  HARD gate —
# applies to every profile and every execution mode (paper/real_dry_run/real_armed).
# Proven defect (2026-06-10): NIFTY re-entered LONG 1 and 5 bars after bearish-BOS
# contradictions; session ended at 3 consecutive losses and a capital halt.
from platform_core.execution.context_cooldown import (
    contradiction_cooldown_active as _contradiction_cooldown_active,
    cooldown_bars_from_env as _cooldown_bars_from_env,
)
_CONTEXT_CONTRADICTION_COOLDOWN_BARS: int = _cooldown_bars_from_env()

# Time-of-day entry cutoff — default OFF. The afternoon leak turned out to be
# EXTENSION CHASING, not a time effect (per-trade replay: all afternoon losers
# entered >=1.6 OR-widths into an extended move; a fresh afternoon breakout won).
# The opening_range.py extension-chase gate (OR_MAX_EXTENSION=2.0) replaces this;
# LIVE_ENTRY_CUTOFF_IST=<hour> re-arms a hard cutoff for A/B if ever needed.
_ENTRY_CUTOFF_IST_HOUR: int = _entry_cutoff_hour("LIVE_ENTRY_CUTOFF_IST")

# Strike quality gate for Tier2/3: reject low-quality strikes that passed OI filter.
# Proven defect (2026-06-04): SENSEX iv_attr=0.000 and all entries SCORE_MARGIN_LOW.
_MIN_SCORE_MARGIN_TIER23: float = 0.05   # min rank-1 minus rank-2 score gap
# DEMOTED 2026-06-26: iv_attr hard veto → soft score input. iv_attractiveness
# remains a weighted factor in the composite strike score (it still ranks
# strikes), but it no longer hard-blocks an entry. A broken atm_iv reference
# (computed ~half the true near-ATM value at low vol) zeroed iv_attr for every
# SENSEX strike from 06-09 onward, killing ALL SENSEX trades despite healthy
# 06-05/06-08 days — a single fragile computed metric should not hold a veto.
# Default 0.0 = gate off (iv_attr is clipped to [0,1], never < 0.0, so the
# check below is inert). Re-enable as a hard gate via PAPER_MIN_IV_ATTR=0.05.
_MIN_IV_ATTR_TIER23:      float = float(os.environ.get("PAPER_MIN_IV_ATTR", "0.0") or 0.0)

# ── Paper-only gate profiles and per-underlying overrides ─────────────────────
# PAPER_GATE_PROFILE selects a named threshold set (paper sessions only).
# Individual env vars (PAPER_BANKNIFTY_SCORE_MARGIN_MIN etc.) override the profile.
# When no profile or override is set, all defaults reproduce current live thresholds.
#
# Profile = default:              score_margin=0.05, oi_support=0.10 for all underlyings
# Profile = banknifty_experiment: BANKNIFTY score_margin=0.03 (others unchanged)
# Profile = small_capital_experiment: BNK+NIFTY margin=0.03, NIFTY oi=0.08, SENSEX strict
# Profile = paper_trade_unlock:   margin=0.015 all, oi=0.10 — paper/real_dry_run ONLY,
#                                 rejected fail-closed under real_armed
#
# Profile tables live in platform_core/execution/gate_profiles.py (import-safe).
# SENSEX is always kept at default strict unless PAPER_SENSEX_* is explicitly set.
#
# Usage:
#   export PAPER_GATE_PROFILE=banknifty_experiment
#   export PAPER_RISK_PROFILE=balanced
#   PYTHONPATH=. python3 platform_core/data/live/live_structure_pipeline.py

import os as _os
from platform_core.execution.execution_router import ExecutionRouter as _ExecutionRouter
# Real round-trip toll — paper/dry-run P&L is netted inside OptionPaperTrader;
# these imports cover the real_armed fill-override paths (statutory only).
from platform_core.execution.transaction_costs import apply_costs as _apply_txn_costs
from platform_core.execution.position_reconciler import reconcile_positions
from platform_core.execution.gate_profiles import (
    PROFILE_GATE_TABLES as _PROFILE_GATE_TABLES,
    validate_profile_mode as _validate_profile_mode,
)

# ── Execution mode ────────────────────────────────────────────────────────────
# paper        — default; paper trading only; no broker API calls
# real_dry_run — creates real OrderIntent, validates, logs, no place_order call
# real_armed   — fail-closed unless all real-money interlocks pass
_EXECUTION_MODE: str = _os.environ.get("EXECUTION_MODE", "paper").lower()
if _EXECUTION_MODE not in ("paper", "real_dry_run", "real_armed"):
    print(f"[EXECUTION MODE] WARNING: unknown mode={_EXECUTION_MODE!r} — falling back to paper")
    _EXECUTION_MODE = "paper"

_PAPER_GATE_PROFILE: str = _os.environ.get("PAPER_GATE_PROFILE", "default").lower()

# Paper-only profiles must never run armed — fail closed before anything starts.
_profile_ok, _profile_reject_reason = _validate_profile_mode(
    _PAPER_GATE_PROFILE, _EXECUTION_MODE
)
if not _profile_ok:
    print(f"[GATE PROFILE BLOCKED] {_profile_reject_reason}")
    raise SystemExit(2)

# Fall back to default for unknown profile names
_active_profile_table = _PROFILE_GATE_TABLES.get(
    _PAPER_GATE_PROFILE, _PROFILE_GATE_TABLES["default"]
)
_profile_is_non_default = (_PAPER_GATE_PROFILE != "default")


def _get_gate_val(sym: str, metric: str, env_key: str) -> tuple[float, str]:
    """Return (value, source_str) for one gate threshold.
    Priority: explicit env var > profile > hardcoded default.
    """
    profile_val = _active_profile_table.get(sym, {}).get(
        metric, _MIN_SCORE_MARGIN_TIER23 if metric == "score_margin" else _MIN_OI_SUPPORT_TIER23
    )
    raw = _os.environ.get(env_key)
    if raw is not None:
        try:
            return float(raw), "env"
        except ValueError:
            pass
    _default_val = _MIN_SCORE_MARGIN_TIER23 if metric == "score_margin" else _MIN_OI_SUPPORT_TIER23
    src = "profile" if profile_val != _default_val else "default"
    return profile_val, src


# Build final per-underlying dicts with source tracking
_gate_sm: dict[str, tuple[float, str]] = {
    s: _get_gate_val(s, "score_margin", f"PAPER_{s}_SCORE_MARGIN_MIN")
    for s in ("BANKNIFTY", "NIFTY", "SENSEX")
}
_gate_oi: dict[str, tuple[float, str]] = {
    s: _get_gate_val(s, "oi_support", f"PAPER_{s}_OI_SUPPORT_MIN")
    for s in ("BANKNIFTY", "NIFTY", "SENSEX")
}

_GATE_SCORE_MARGIN_BY_SYM: dict[str, float] = {s: v for s, (v, _) in _gate_sm.items()}
_GATE_OI_SUPPORT_BY_SYM:   dict[str, float] = {s: v for s, (v, _) in _gate_oi.items()}

# ── Absolute liquidity hard gate (Tier2/3) ────────────────────────────────────
# oi_support is a RELATIVE ratio (selected_strike_OI / chain_peak_OI); it can
# falsely reject a perfectly liquid strike just because it is not THE single
# most-popular strike (proven 2026-06-15: SENSEX liquid strikes volume ~35k,
# spread ~0.65%, liquidity-factor 0.94, yet oi_support ~0.01).  This gate adds
# ABSOLUTE tradeability checks the relative ratio cannot express:
#   • minimum absolute open interest on the selected strike (per-underlying), and
#   • minimum liquidity factor (tight bid/ask spread + real volume, [0,1]).
# It is a STRENGTHENING hard gate: it applies to every profile and every
# execution mode (paper / real_dry_run / real_armed) and can only make entries
# safer — it never relaxes any existing gate.  Thresholds are env-overridable
# (PAPER_<SYM>_MIN_ABS_OI, PAPER_MIN_LIQUIDITY) and calibrated so they admit the
# 2026-06-08 SENSEX winners (OI 560k+, liq 0.97) and today's liquid SENSEX/NIFTY
# strikes while rejecting genuinely thin strikes (e.g. OI<1k).
_DEFAULT_MIN_ABS_OI_BY_SYM: dict[str, float] = {
    "BANKNIFTY": 25000.0,   # confirmation-only, kept for completeness
    "NIFTY":     25000.0,
    "SENSEX":     3000.0,    # BSE chain OI is structurally lower than NSE
}


def _get_min_abs_oi(sym: str) -> float:
    raw = _os.environ.get(f"PAPER_{sym}_MIN_ABS_OI")
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_MIN_ABS_OI_BY_SYM.get(sym, 3000.0)


_MIN_ABS_OI_BY_SYM: dict[str, float] = {
    s: _get_min_abs_oi(s) for s in ("BANKNIFTY", "NIFTY", "SENSEX")
}
try:
    _MIN_LIQUIDITY_TIER23: float = float(_os.environ.get("PAPER_MIN_LIQUIDITY", "0.50") or 0.50)
except ValueError:
    _MIN_LIQUIDITY_TIER23 = 0.50

_gate_experiment_active: bool = (
    _profile_is_non_default
    or any(src == "env" for _, src in list(_gate_sm.values()) + list(_gate_oi.values()))
)


def _print_gate_startup() -> None:
    """Print full gate configuration table at startup."""
    print(
        f"[GATE PROFILE] profile={_PAPER_GATE_PROFILE}"
        f"  paper_only=true"
        f"  real_funds_approved={'false' if _gate_experiment_active else 'true_if_default'}"
    )
    for sym in ("BANKNIFTY", "NIFTY", "SENSEX"):
        sm_val, sm_src = _gate_sm[sym]
        oi_val, oi_src = _gate_oi[sym]
        print(
            f"[GATE CONFIG] {sym:12s}"
            f"  score_margin_min={sm_val:.3f} ({sm_src})"
            f"  oi_support_min={oi_val:.3f} ({oi_src})"
            f"  min_abs_oi={_MIN_ABS_OI_BY_SYM[sym]:,.0f}"
        )
    print(
        f"[LIQUIDITY GATE] min_liquidity={_MIN_LIQUIDITY_TIER23:.2f}  "
        f"min_abs_oi=BANKNIFTY:{_MIN_ABS_OI_BY_SYM['BANKNIFTY']:,.0f} "
        f"NIFTY:{_MIN_ABS_OI_BY_SYM['NIFTY']:,.0f} "
        f"SENSEX:{_MIN_ABS_OI_BY_SYM['SENSEX']:,.0f}  "
        f"(absolute tradeability — applies to all profiles/modes)"
    )
    from platform_core.analytics.structure.opening_range import (
        _BREAKOUT_BUFFER_FRAC as _or_buf,
        _MAX_EXTENSION as _or_max_ext,
    )
    print(
        f"[CHOP GUARDS] or_breakout_buffer_frac={_or_buf:.2f}  "
        f"or_max_extension={_or_max_ext:.1f} (chase guard)  "
        f"same_contract_session_max_losses={_SameContractLossTracker().max_losses}  "
        f"same_direction_session_max_entries={_DirectionEntryCounter().max_entries}  "
        f"entry_cutoff={_ENTRY_CUTOFF_IST_HOUR}:00 IST (0=off)"
    )
    if _PAPER_GATE_PROFILE == "paper_trade_unlock":
        print("[WARNING] Paper trade-unlock experiment — not approved for real funds.")
    elif _gate_experiment_active:
        print("[WARNING] Paper gate experiment active — not approved for real funds.")


# MCB adaptation watch: log diagnostic when MCB stays anchored while structure opposes
_MCB_CONTRA_BOS_WARN_THRESHOLD: int = 3  # warn after this many opposing BOS events per session

# Terminal mode: set LIVE_TERMINAL_MODE=debug for full StructureEvent repr.
# Default compact mode suppresses verbose event output.
_TERMINAL_COMPACT: bool = _os.environ.get("LIVE_TERMINAL_MODE", "compact").lower() != "debug"

# Capital protection limits
# Env-overridable so the halt thresholds stay COHERENT with the sizing: per-trade
# risk must sit below the soft-halt, and the daily hard-halt must allow the
# intended number of full-size losses before stopping. real_armed keeps the
# conservative defaults (2% / 4% / 15%); paper/dry-run raises them via the
# supervisor to match aggressive sizing (e.g. 8-10 lots -> 3/2 losses to halt).
# Soft halt — defensive mode, not a full stop
_SOFT_HALT_DAILY_LOSS_FRAC: float = float(os.environ.get("PAPER_SOFT_HALT_DAILY_LOSS", "0.020") or 0.020)
_SOFT_HALT_RISK_FRAC:       float = 0.007   # 0.7% risk — below Tier 1's 1%
_SOFT_HALT_MAX_LOTS:        int   = 1
_SOFT_HALT_MIN_CONF:        float = 0.65    # higher bar than Tier 1's 0.55
# Hard halt — no new trades
_HARD_HALT_DAILY_LOSS_FRAC: float = float(os.environ.get("PAPER_HARD_HALT_DAILY_LOSS", "0.040") or 0.040)
# 0 = DISABLED (no streak halt; rely on the daily-loss floor + smooth sizer taper).
_HARD_HALT_CONSEC_LOSSES:   int   = int(os.environ.get("PAPER_HARD_HALT_CONSEC_LOSSES", "3") or 3)
_MAX_ACCOUNT_DD:            float = float(os.environ.get("PAPER_MAX_ACCOUNT_DD", "0.150") or 0.150)

# ── Per-underlying PositionSizer sets ────────────────────────────────────────
# Each underlying gets independent instances so sizer internal tracking
# (_last_underlying_symbol, _processed_requests) never bleeds across symbols.
for _sizer_init_state in _runtime_by_underlying.values():
    _sizer_init_state.sizers = {
        0: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_SOFT_HALT_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
            max_confidence_scale=_MAX_CONF_SCALE,
            min_confidence_scale=_MIN_CONF_SCALE,
        )),
        1: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER1_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
            max_confidence_scale=_MAX_CONF_SCALE,
            min_confidence_scale=_MIN_CONF_SCALE,
        )),
        2: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER2_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
            max_confidence_scale=_MAX_CONF_SCALE,
            min_confidence_scale=_MIN_CONF_SCALE,
        )),
        3: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER3_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
            max_confidence_scale=_MAX_CONF_SCALE,
            min_confidence_scale=_MIN_CONF_SCALE,
        )),
    }


# ── One-global-position helpers ──────────────────────────────────────────────

def has_any_open_position() -> bool:
    """Return True if any prepared underlying currently holds an open position."""
    return any(
        und_s.option_trader.has_open_position()
        for und_s in _runtime_by_underlying.values()
    )


def get_open_position_state() -> "UnderlyingRuntimeState | None":
    """Return the UnderlyingRuntimeState whose option_trader has an open position, or None."""
    for und_s in _runtime_by_underlying.values():
        if und_s.option_trader.has_open_position():
            return und_s
    return None


def _held_option_ltp(state: "UnderlyingRuntimeState") -> float | None:
    """
    Real last-traded price of the currently-held option for `state`, from Kite.

    Used to mark the position to the LIVE market at exit instead of a Black-Scholes
    recompute (which produced fictional exit P&L). Returns None on any error or in
    paper-only contexts without a live quote, so the caller falls back to BSM.
    Never raises into the exit path.
    """
    try:
        pos = state.option_trader._position
        if pos is None:
            return None
        return adapter.fetch_ltp(exchange=state.exchange, tradingsymbol=pos.contract_symbol)
    except Exception:
        return None


def _robust_atm_iv(options_ctx) -> float:
    """
    A trustworthy ATM IV for the underlying.

    Prefers the near-ATM strike's OWN raw IV (directly implied from the real
    option price) over the surface-INTERPOLATED ``options_ctx.atm_iv``, which has
    been observed to mis-interpolate on the SENSEX/BSE chain (the atm_iv bug:
    e.g. ~half the true value at low vol, or a few points off vs the near strike).
    This value feeds the sizing volatility scale (realized_volatility_ratio), so a
    wrong IV mis-sizes positions. Falls back to the surface value, then the
    reference IV. Never raises.
    """
    try:
        atm_strike = getattr(options_ctx, "atm_strike", None)
        cands = getattr(options_ctx, "strike_candidates", None) or ()
        if atm_strike is not None and cands:
            near = min(
                (c for c in cands if getattr(c, "implied_volatility", 0.0) > 0.0),
                key=lambda c: abs(c.strike_price - atm_strike),
                default=None,
            )
            if near is not None and near.implied_volatility > 0.0:
                return float(near.implied_volatility)
        surface = getattr(options_ctx, "atm_iv", None)
        if surface is not None and surface > 0.0:
            return float(surface)
    except Exception:
        pass
    return _REFERENCE_IV


def _compute_open_premium_cost() -> float:
    """Sum entry_premium × lot_size × lots across every currently-open position."""
    total = 0.0
    for und_s in _runtime_by_underlying.values():
        pos = und_s.option_trader._position
        if pos is not None:
            total += pos.entry_premium * pos.lot_size * pos.lots
    return total


# ── Mutable account state (portfolio-level) ───────────────────────────────────
_equity:            float = _INITIAL_EQUITY
_peak_equity:       float = _INITIAL_EQUITY
_open_premium_cost: float = 0.0
_bar_count:         int   = 0

# Capital protection state
_session_date:           date | None = None
_session_opening_equity: float       = _INITIAL_EQUITY
_daily_pnl:              float       = 0.0
_trading_halted:         bool        = False
_halt_reason:            str         = ""
_soft_halted:            bool        = False
_soft_halt_reason:       str         = ""
_consecutive_losses:     int         = 0
_shutdown_triggered:     bool        = False  # set True by _handle_market_close_shutdown
# Serializes the market-close shutdown trigger. _handle_market_close_shutdown is
# reachable from BOTH the WS thread (on_ticks bar-level 15:30 check) and the main
# thread (watchdog clock), so the check-then-set on _shutdown_triggered must be
# atomic or both could run _eod_force_close_all() → double-booked EOD close.
_shutdown_lock:          "threading.Lock" = threading.Lock()
# NOTE: a previous attempt to serialize WS-thread bar processing against the
# main-thread EOD close via an _account_lock held across the whole on_ticks batch
# DEADLOCKED after a WS reconnect (two ticker threads contend on the lock; the
# adapter's pre-callback liveness stamp hid it from the stall watchdog). Reverted
# 2026-06-24. The market-close double-invocation is still guarded atomically by
# _shutdown_lock above; full bar/EOD serialization needs a non-blocking design
# (e.g. a single-consumer tick queue), not a lock around the callback.
_shadow_last_bar:        dict        = {}     # {underlying: _bar_count} — shadow rate-limit
_shadow_counters:        dict        = {}     # {underlying: {attempts,success,no_candidate,failures,last_symbol,last_score}}
_SHADOW_SUMMARY_EVERY:   int         = 10     # print [SHADOW SUMMARY] every N attempts per underlying

# Operational state
_STATE_FILE:         Path = Path("logs/paper_trading_state.json")
_PID_FILE:           Path = Path("logs/live_pipeline.pid")
_SHADOW_AUDIT_PATH:  Path = Path("logs/strike_selection_shadow.jsonl")
_shadow_auditor      = StrikeAuditLogger(audit_path=_SHADOW_AUDIT_PATH, mode="shadow")
# Phase-1 intelligence wiring (docs/intelligence_wiring_plan.md): RegimeEngine in
# SHADOW mode — logs the volatility regime per bar to logs/regime_shadow.jsonl,
# never affects the trade path. Disable with PAPER_REGIME_SHADOW=0.
from platform_core.data.live.regime_shadow import RegimeShadow as _RegimeShadow
_regime_shadow = (
    _RegimeShadow() if _os.environ.get("PAPER_REGIME_SHADOW", "1") != "0" else None
)
# ── BIAS INVALIDATION ENGINE (2026-07-15) — the Fable adaptivity model ────────
# Evidence-weighted intraday override of the MCB execution bias. The MCB's
# 4h/1d weights (~60%) mean a start-of-day bias can NEVER flip intraday —
# 2026-07-13 shorted the low of a +0.8% trend day and then logged 231
# consecutive OR-contradiction blocks, frozen SHORT all day. This engine
# treats the prior as a hypothesis: sustained contradiction (price beyond the
# opposite OR extreme, opposite BOS/CHOCH, opposing constituent flow) first
# NEUTRALIZES the bias (stand aside), then FLIPS it at humble confidence.
# Validated on real recorded days (scripts/validate_bias_invalidation_days.py,
# 15/15: flips 07-13 LONG@10:00 +87pts-to-close, flips 07-08 SHORT@12:42
# pre-waterfall +294pts, ZERO false flips on the 07-14 chop day).
# Applied UPSTREAM of strike selection so CE/PE side, gates, sizing and the
# futures sleeve all see ONE consistent effective bias. BIAS_INVALIDATION=0
# kills it dead. Transitions print loudly + log to bias_invalidation.jsonl.
from platform_core.data.live.bias_invalidation import (
    BiasInvalidationEngine as _BiasInvalidationEngine,
)
_bias_inval = _BiasInvalidationEngine()
_BIAS_INVAL_LOG_PATH = Path("logs/bias_invalidation.jsonl")

# Structure events of the LAST finalized bar, per symbol. The invalidation
# block runs before this bar's structure engine, so it must read the previous
# bar's BOS/CHOCH — causal, one-bar lag, never this bar's (not yet computed).
_LAST_STRUCT_EVENTS: dict = {}


def _bias_inval_log(rec: dict) -> None:
    """Append one JSONL record; never raises into the tick path."""
    try:
        with _BIAS_INVAL_LOG_PATH.open("a") as _f:
            _f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


# Order-flow shadow (batch-4 §4.1) — DEFAULT ON. 2026-07-14 FIX: order flow comes from INDEX
# FUTURES, not spot — a spot index is a computed number with no traded volume and no book
# (07-13 collected all-zero CVD/null depth from spot; a wasted day of data). Front-month
# futures tokens are resolved dynamically at boot (they change every expiry) and subscribed
# alongside the spot feeds; their ticks feed ONLY this shadow, never the bar/structure path.
from platform_core.data.live.orderflow_shadow import OrderFlowShadow as _OrderFlowShadow
_orderflow_shadow = (
    _OrderFlowShadow() if _os.environ.get("ORDERFLOW_SHADOW", "1") != "0" else None
)

_FUT_TOKEN_TO_SYMBOL: dict[int, str] = {}   # ws token -> "NIFTY_FUT" etc.
_FEED_TO_FUT_SYMBOL = {"NIFTY50": "NIFTY_FUT", "SENSEX": "SENSEX_FUT",
                       "BANKNIFTY": "BANKNIFTY_FUT"}
if _orderflow_shadow is not None:
    try:
        from datetime import date as _date_of
        for _exch, _name, _fsym in (("NFO", "NIFTY", "NIFTY_FUT"),
                                    ("NFO", "BANKNIFTY", "BANKNIFTY_FUT"),
                                    ("BFO", "SENSEX", "SENSEX_FUT")):
            _futs = [i for i in adapter._get_instruments(_exch)
                     if i.get("name") == _name and i.get("instrument_type") == "FUT"
                     and i.get("expiry") and i["expiry"] >= _date_of.today()]
            if _futs:
                _front = min(_futs, key=lambda i: i["expiry"])
                _FUT_TOKEN_TO_SYMBOL[int(_front["instrument_token"])] = _fsym
                print(f"[ORDERFLOW] {_fsym} = {_front['tradingsymbol']} "
                      f"(exp {_front['expiry']}, token {_front['instrument_token']})")
        if not _FUT_TOKEN_TO_SYMBOL:
            print("[ORDERFLOW] WARNING: no futures tokens resolved — order flow will be empty")
    except Exception as _fut_exc:
        print(f"[ORDERFLOW] futures resolution failed ({_fut_exc}) — order flow will be empty")

# ── FUTURES DRY-RUN SLEEVE (2026-07-14) — paper index-futures book alongside options ──────────
# MIRROR entries (same signal, linear expression, no theta) + TURN entries (constituent-flow
# turn events the options path can't take). Marked to the real futures ticks above. Pure paper:
# no orders anywhere; fully self-contained; failures never disturb the options path.
# Disable with FUTURES_SLEEVE=0.
_futures_sleeve = None
if _os.environ.get("FUTURES_SLEEVE", "1") != "0" and _FUT_TOKEN_TO_SYMBOL:
    try:
        from platform_core.data.live.futures_sleeve import FuturesSleeve as _FuturesSleeve
        _futures_sleeve = _FuturesSleeve(
            # Same compounding capital basis as the options book (2026-07-15;
            # was hardcoded 400000 — diverges as equity compounds).
            capital=_INITIAL_EQUITY,
            contracts={
                _fs: {"underlying": _fs.replace("_FUT", ""),
                      # lot sizes from the system registry (futures lots == option lots here)
                      "lot_size": (OPTION_CHAIN_CONFIG[_fs.replace("_FUT", "")].lot_size
                                   if _fs.replace("_FUT", "") in OPTION_CHAIN_CONFIG else 0)}
                for _fs in _FUT_TOKEN_TO_SYMBOL.values()
            },
        )
        print(f"[FUTURES SLEEVE] dry-run active: {sorted(_FUT_TOKEN_TO_SYMBOL.values())} "
              f"(mirror + turn entries, paper only)")
    except Exception as _fsl_exc:
        print(f"[FUTURES SLEEVE] not started ({_fsl_exc})")
# Phase-1 intelligence wiring: HTF-bias alignment in SHADOW mode — logs how the
# execution bias aligns with the (already-computed) daily/h4/h1/m15 HTF biases to
# logs/htf_shadow.jsonl, never affects the trade path. Disable with PAPER_HTF_SHADOW=0.
from platform_core.data.live.htf_shadow import HTFShadow as _HTFShadow
_htf_shadow = (
    _HTFShadow() if _os.environ.get("PAPER_HTF_SHADOW", "1") != "0" else None
)
# Phase-1 intelligence wiring (2026-07-08): TrapProbabilityModel in SHADOW mode —
# scores whether the CURRENT opening-range break attempt (UP or DOWN — inferred
# from the ACTUAL break, direction-agnostic, never assumes a side) is a trap, and
# logs per break-bar to logs/trap_shadow.jsonl. Never affects the trade path.
# Accumulates OOS evidence for the trap_prob>0.8 tail-filter lead before any
# promotion. Disable with PAPER_TRAP_SHADOW=0.
from platform_core.data.live.trap_shadow import TrapShadow as _TrapShadow
_trap_shadow = (
    _TrapShadow() if _os.environ.get("PAPER_TRAP_SHADOW", "1") != "0" else None
)
# Phase-2 intelligence wiring: transparent, graded setup-conviction in SHADOW —
# logs a per-decision conviction score (real inputs, cannot saturate) to
# logs/conviction_shadow.jsonl, never affects the trade path. Replaces the
# saturated MCB conf (1.00) / ML ConfidenceEngine (0.99). Disable PAPER_CONV_SHADOW=0.
from platform_core.data.live.conviction_shadow import ConvictionShadow as _ConvictionShadow
from platform_core.data.live.conviction_shadow import ConvictionScorer as _ConvictionScorer
_conviction_shadow = (
    _ConvictionShadow() if _os.environ.get("PAPER_CONV_SHADOW", "1") != "0" else None
)
# Phase-3 intelligence wiring (2026-07-10): the 4 options-analytics engines
# (GEX / OI / DealerHedging / PremiumBehavior) are ALREADY computed every bar
# inside OptionsContext but their results are discarded. SHADOW mode logs each
# engine's summary per option-context build to logs/options_analytics_shadow.jsonl,
# never affecting the trade path — to test whether any separates winners from
# losers before it ever gates a trade. Disable with PAPER_OPT_ANALYTICS_SHADOW=0.
from platform_core.data.live.options_analytics_shadow import (
    OptionsAnalyticsShadow as _OptionsAnalyticsShadow,
)
_opt_analytics_shadow = (
    _OptionsAnalyticsShadow()
    if _os.environ.get("PAPER_OPT_ANALYTICS_SHADOW", "1") != "0" else None
)
# Phase-3 intelligence wiring (2026-07-10): ContinuationClassifier in SHADOW —
# assembles the 22-field observation from 1m bar history + Dealer/OI/Premium
# snapshots + ATM IV, runs the classifier, logs continuation_probability per bar
# to logs/continuation_shadow.jsonl. Never affects trades. Tests whether the
# continuation signal separates real breakouts from fakes (the momentum-vs-chop
# question behind strike-rotation). Disable with PAPER_CONTINUATION_SHADOW=0.
from platform_core.data.live.continuation_shadow import ContinuationShadow as _ContinuationShadow
_continuation_shadow = (
    _ContinuationShadow()
    if _os.environ.get("PAPER_CONTINUATION_SHADOW", "1") != "0" else None
)
# Phase-3 intelligence wiring (2026-07-10): ReversalClassifier in SHADOW — the
# near-twin of the continuation shadow (reuses shadow_feature_kit). Logs a
# reversal_probability per bar to logs/reversal_shadow.jsonl. Never affects
# trades. Pairs with continuation to characterise breakout follow-through vs
# exhaustion. Disable with PAPER_REVERSAL_SHADOW=0.
from platform_core.data.live.reversal_shadow import ReversalShadow as _ReversalShadow
_reversal_shadow = (
    _ReversalShadow()
    if _os.environ.get("PAPER_REVERSAL_SHADOW", "1") != "0" else None
)

# ── Phase-2 LIVE conviction wiring (paper / real_dry_run ONLY) ────────────────
# Wire the transparent conviction score into live sizing + routing. real_armed
# keeps the old MCB-conf behaviour until conviction is validated on a larger
# sample (this is ONE-day evidence — bounded + reversible by design).
#  - SIZING: feed conviction into the sizer's EXISTING confidence_scale (a bug
#    fix — that input was the saturated MCB conf ≈1.0, so the scale did nothing;
#    now weak setups size DOWN, strong UP, all within the existing 1.5× cap — no
#    new risk envelope).
#  - FLOOR: skip setups below a conservative conviction floor (pure risk cut).
#  - NIFTY ROUTING: gate NIFTY on conviction instead of the saturated MCB conf
#    (the 0.95 MCB gate blocked a high-conviction NIFTY while a weak SENSEX
#    traded and lost on 2026-06-30).
# All env-tunable; set PAPER_CONV_LIVE=0 to fall back to pure MCB behaviour.
_conviction_scorer = _ConvictionScorer()
# REVERTED to shadow-only 2026-07-01 (default OFF, opt-in PAPER_CONV_LIVE=1). Two
# live days were 1-1: 06-30 conviction ranked NIFTY>SENSEX and SENSEX lost (looked
# right); 07-01 conviction ranked NIFTY>SENSEX again but NIFTY LOST and SENSEX won
# (wrong) — and the pure-probability routing let NIFTY trade and cost -1,788 (85%
# of the day's loss). Conviction is NOT validated (1-1), so it returns to SHADOW
# and SENSEX-priority routing is restored. Re-enable only after it earns it on a
# real sample.
# Parity (2026-07-05): mode-INDEPENDENT — the supervisor builds an identical
# env for all modes, so if this experiment is ever enabled it behaves the same
# in dry-run and real_armed instead of silently diverging. Default 0 = shadow
# everywhere (conviction is not validated — hard-rule: stays shadow).
_CONV_LIVE: bool = _os.environ.get("PAPER_CONV_LIVE", "0") == "1"

# Open-drive window (gate forensics 2026-07-06: the warm-up/session-phase
# blocked set ran 72% WR / +₹21,788 over 12 days; batch A/B +317 → +17,972 —
# in-sample; this week's dry-runs are the zero-risk OOS test). Entries are
# allowed through the session-phase and warm-up gates ONLY while ALL hold:
#   (a) knob on, (b) warm-start verified seeded 4h+1d (NEVER on a blind
#   start — the original reason those blocks existed), (c) IST clock inside
#   09:15–09:35. Morning-only by construction: the 15:00+ CLOSING block is
#   unreachable. Every other gate (OR, margin, caps, cooldowns, halts, stop/
#   ratchet management) stays fully armed. Mode-independent (parity).
_OPEN_DRIVE_WINDOW: bool = (
    _os.environ.get("PAPER_OPEN_DRIVE_WINDOW", "1") == "1"
)


def _open_drive_pass_now() -> bool:
    """True while the seeded opening-drive entry window is active."""
    if not (_OPEN_DRIVE_WINDOW and _WARMSTART_SEEDED_OK):
        return False
    _n = datetime.now(_IST)
    return (_n.hour, _n.minute) >= (9, 15) and (_n.hour, _n.minute) < (9, 36)


# Re-entry risk haircut (measured 2026-07-06, replay A/B on 14 day-samples:
# net −1,354 → +317 with the SAME 28-trade set — pure sizing effect, no
# selection). 2nd+ same-direction session entry sizes at this fraction of the
# sizer's lots. Mode-independent (parity). 0 disables.
_REENTRY_RISK_HAIRCUT: float = float(
    _os.environ.get("PAPER_REENTRY_RISK_HAIRCUT", "0.5") or 0.5
)
_CONV_FLOOR:     float = float(_os.environ.get("PAPER_CONV_FLOOR", "0.40") or 0.40)
_CONV_NIFTY_MIN: float = float(_os.environ.get("PAPER_CONV_NIFTY_MIN", "0.60") or 0.60)


def _live_conviction(state, rec) -> float:
    """Transparent conviction in [0,1] for this decision. Fail-OPEN (return 1.0
    on any error) so a scoring fault never silently shrinks/skips a trade."""
    try:
        conv, _parts = _conviction_scorer.score(
            composite=getattr(rec, "composite_score", 0.0),
            score_margin=(state.captured_score_margin or 0.0),
            liquidity=getattr(getattr(rec, "breakdown", None), "liquidity", 0.0),
            vol_ratio=max(state.last_atm_iv / max(_REFERENCE_IV, 0.001), 0.1),
            fvg_quality=state.fvg_quality,
        )
        return conv
    except Exception:
        return 1.0

_kite_session_valid: bool = True   # set False when token expiry is detected
_kite_session_error: str  = ""

# Persistence layer — fire-and-forget, never affects trading logic
_DB_PATH: Path = Path("logs/trades.db")
_db: TradeDatabase = TradeDatabase(str(_DB_PATH))

# MCB warm-start constants
_MCB_HISTORY_BARS:  int = 30   # bars per timeframe kept (> min_history_bars=20)
_MCB_SAVE_INTERVAL: int = 15   # write state every N BANKNIFTY 1-min bars
# Note: bar_history and bars_since_save are now per-underlying in UnderlyingRuntimeState.

# CSV warm-start: maps option_underlying key → filename prefix for data/*.csv discovery.
# "NIFTY" maps to "nifty" because fetch_index_history.py writes nifty_1m_*.csv (not nifty50_*).
_MCB_WARMSTART_CSV_PREFIX: dict[str, str] = {
    "BANKNIFTY": "banknifty",
    "NIFTY":     "nifty",
    "SENSEX":    "sensex",
}


# ── Single-instance lock ───────────────────────────────────────────────────────

def _proc_start_time(pid: int) -> float | None:
    """
    Return the Unix epoch start time for *pid* using /proc/{pid}/stat.

    /proc/{pid}/stat field 22 (starttime) is in clock ticks since boot.
    Boot time is read from /proc/stat btime.  This method is reliable on
    Linux and WSL where /proc/{pid} directory ctime is not trustworthy.

    Returns None if any read fails.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        # Format: pid (comm) state ppid pgrp session tty_nr tpgid flags
        #         minflt cminflt majflt cmajflt utime stime cutime cstime
        #         priority nice num_threads itrealvalue starttime ...
        # starttime is the 22nd field (1-indexed).  The (comm) field may
        # contain spaces/brackets, so split after the closing ')'.
        after_name = stat_text[stat_text.rfind(")") + 2:]
        fields = after_name.split()
        starttime_ticks = int(fields[19])   # 0-indexed within after_name portion

        btime: int | None = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime"):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None

        clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        return btime + starttime_ticks / clk_tck
    except Exception:
        return None


def _pid_is_live_pipeline(pid: int) -> bool:
    """
    Return True only if the process at *pid* could be a live pipeline that
    legitimately owns the PID file.

    Uses a temporal check: a genuine running pipeline must have started
    BEFORE the PID file was last written (the pipeline starts, then writes
    the PID file as module-level code runs).  If the process started more
    than 60 s AFTER the PID file was written, the OS reused the PID after
    an earlier pipeline exited without cleanup — the lock is stale and
    startup should proceed.

    The 60 s tolerance absorbs time between process start and module-level
    _acquire_pid_lock() execution.

    Falls back to True (conservative — block startup) when:
      - /proc is unavailable or unreadable (non-Linux, permission error)
      - process start time cannot be determined for any other reason
    This ensures the single-instance guarantee is never weakened by an
    exception path.
    """
    try:
        proc_start = _proc_start_time(pid)
        if proc_start is None:
            return True    # cannot determine — conservative: block
        pid_file_mtime = _PID_FILE.stat().st_mtime
        # If the process started more than 60 s after the PID file was
        # written, it cannot be the process that wrote the file → stale.
        if proc_start > pid_file_mtime + 60.0:
            return False
        return True
    except FileNotFoundError:
        return False   # /proc/{pid}/stat absent → process is gone
    except Exception:
        return True    # unreadable — conservative: block startup


def _acquire_pid_lock() -> None:
    """
    Write a PID file so that a second invocation of this script detects the
    running instance and refuses to start.

    Stale PID files (left after SIGKILL or power loss) are handled via:
      1. os.kill(pid, 0) — if the PID is dead, overwrite immediately.
      2. _pid_is_live_pipeline() — if the PID is alive but belongs to an
         unrelated process (PID reused by the OS after the pipeline exited
         without cleaning up), overwrite and continue.  This prevents import-
         time writes (e.g. "from live_structure_pipeline import X" in test
         scripts) from permanently blocking restarts when those processes exit
         without running their atexit handler.
    """
    if _PID_FILE.exists():
        try:
            existing_pid = int(_PID_FILE.read_text().strip())
            os.kill(existing_pid, 0)   # signal 0 = existence check, no kill
            # PID is alive — verify it is actually live_structure_pipeline.py.
            if not _pid_is_live_pipeline(existing_pid):
                # PID reused by an unrelated process — stale lock, overwrite.
                pass
            else:
                # Genuine live pipeline instance — refuse to start.
                print(
                    f"\n{'='*60}\n"
                    f"[PID LOCK] Another instance is already running (PID {existing_pid}).\n"
                    f"  Only one live pipeline process may run at a time.\n"
                    f"  If the previous run crashed without cleanup, delete:\n"
                    f"    {_PID_FILE}\n"
                    f"  then restart.\n"
                    f"{'='*60}"
                )
                sys.exit(1)
        except ProcessLookupError:
            pass   # recorded PID is dead — stale lock, overwrite
        except PermissionError:
            pass   # PID reused by a different user's process — treat as stale
        except (ValueError, OSError):
            pass   # unreadable or corrupt PID file — overwrite

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_pid_lock)


def _release_pid_lock() -> None:
    """Remove the PID file on clean exit (atexit handler)."""
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink()
    except Exception:
        pass


# ── State persistence (defined before ENGINES so startup restore runs first) ──

def _bar_to_dict(bar: LiveBar) -> dict:
    """Serialise a LiveBar to a JSON-safe dict."""
    return {
        "symbol":    bar.symbol,
        "timeframe": bar.timeframe,
        "start":     bar.start_timestamp.isoformat(),
        "end":       bar.end_timestamp.isoformat(),
        "open":      bar.open_price,
        "high":      bar.high_price,
        "low":       bar.low_price,
        "close":     bar.close_price,
        "volume":    bar.volume,
    }


def _dict_to_bar(d: dict) -> LiveBar:
    """Reconstruct a LiveBar from a serialised dict."""
    ts = datetime.fromisoformat(d["start"])
    te = datetime.fromisoformat(d["end"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if te.tzinfo is None:
        te = te.replace(tzinfo=timezone.utc)
    return LiveBar(
        symbol          = d["symbol"],
        timeframe       = d["timeframe"],
        start_timestamp = ts,
        end_timestamp   = te,
        open_price      = float(d["open"]),
        high_price      = float(d["high"]),
        low_price       = float(d["low"]),
        close_price     = float(d["close"]),
        volume          = float(d["volume"]),
    )


def _save_state() -> bool:
    """
    Persist mutable account state and recent bar history to disk atomically.
    _open_premium_cost is not stored directly — re-derived from
    open_position.entry_premium × lot_size × lots on restore.
    bar_history is stored as-is for MCB warm-start rehydration.

    Returns True on success, False if the write failed.  Callers that must
    guarantee persistence (e.g. post-trade logging) should check the return
    value and skip downstream side-effects if False.
    """
    _bnk = _runtime_by_underlying[_PRIMARY_OPTION_TARGET]
    _pos_state = get_open_position_state()
    pos = _pos_state.option_trader._position if _pos_state is not None else None
    state = {
        "starting_capital":      _INITIAL_EQUITY,
        "equity":                _equity,
        "peak_equity":           _peak_equity,
        "daily_pnl":             _daily_pnl,
        "consecutive_losses":    _consecutive_losses,
        "trading_halted":        _trading_halted,
        "halt_reason":           _halt_reason,
        "soft_halted":           _soft_halted,
        "soft_halt_reason":      _soft_halt_reason,
        "session_date":          _session_date.isoformat() if _session_date else None,
        "session_opening_equity": _session_opening_equity,
        "last_atm_iv":           _bnk.last_atm_iv,
        # Legacy single-position key — kept for backward compat; source of truth is open_positions.
        "open_position": {
            "underlying_symbol": pos.underlying_symbol,
            "contract_symbol":   pos.contract_symbol,
            "strike":            pos.strike,
            "option_type":       pos.option_type,
            "expiry_date":       pos.expiry_date.isoformat(),
            "lot_size":          pos.lot_size,
            "lots":              pos.lots,
            "entry_premium":     pos.entry_premium,
            "entry_spot":        pos.entry_spot,
            "entry_iv":          pos.entry_iv,
            "direction":         pos.direction,
            "bar_index":         pos.bar_index,
        } if pos is not None else None,
        # Multi-underlying positions — authoritative source of truth.
        "open_positions": {
            und: {
                "underlying_symbol": _p.underlying_symbol,
                "contract_symbol":   _p.contract_symbol,
                "strike":            _p.strike,
                "option_type":       _p.option_type,
                "expiry_date":       _p.expiry_date.isoformat(),
                "lot_size":          _p.lot_size,
                "lots":              _p.lots,
                "entry_premium":     _p.entry_premium,
                "entry_spot":        _p.entry_spot,
                "entry_iv":          _p.entry_iv,
                "direction":         _p.direction,
                "bar_index":         _p.bar_index,
                "peak_premium":      _p.peak_premium,
            } if (_p := und_s.option_trader._position) is not None else None
            for und, und_s in _runtime_by_underlying.items()
        },
        "bar_history": _bnk.bar_history,
        # Phase B3B: multi-underlying context persistence (BANKNIFTY compat preserved above)
        "bar_history_by_underlying": {
            und: und_state.bar_history
            for und, und_state in _runtime_by_underlying.items()
        },
        "last_atm_iv_by_underlying": {
            und: und_state.last_atm_iv
            for und, und_state in _runtime_by_underlying.items()
        },
        # Context-contradiction cooldown — intra-session only.  Restored after a
        # same-session restart when still fresh; never carried across days.
        "last_context_contradiction_by_underlying": {
            und: (und_state.last_contradiction_meta or None)
            if und_state.last_contradiction_bar is not None else None
            for und, und_state in _runtime_by_underlying.items()
        },
        # Same-contract session loss counts — intra-session only. Restored on a
        # same-day restart so a crash-restart cannot reset the churn brake;
        # ignored (fresh) on the next session.
        "contract_session_losses_by_underlying": {
            und: und_state.contract_loss_tracker.to_dict()
            for und, und_state in _runtime_by_underlying.items()
        },
        "direction_session_entries_by_underlying": {
            und: und_state.direction_entry_counter.to_dict()
            for und, und_state in _runtime_by_underlying.items()
        },
        # Finalized opening range — same-day crash-restarts only. Without it a
        # restarted process has NO opening range for the rest of the day and
        # the OR guards (buffer/extension/inside) silently deactivate.
        "opening_range_by_underlying": {
            und: (und_state.or_engine.current_range.to_dict()
                  if und_state.or_engine.ready else None)
            for und, und_state in _runtime_by_underlying.items()
        },
    }
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        # Flush + fsync before the atomic rename so a power loss between write and
        # rename cannot leave a torn/empty state file (the account's source of
        # truth for equity and open positions). replace() is atomic on POSIX.
        with open(tmp, "w") as _f:
            _f.write(json.dumps(state, indent=2))
            _f.flush()
            os.fsync(_f.fileno())
        tmp.replace(_STATE_FILE)
        return True
    except Exception as exc:
        print(f"[STATE] Write failed: {exc}")
        return False


def _load_state() -> None:
    """
    Restore account state from disk if a state file exists.
    All values are validated into locals first; globals are only written if
    the entire parse succeeds, so a partial/corrupt file leaves state intact.
    """
    global _equity, _peak_equity, _daily_pnl, _consecutive_losses
    global _trading_halted, _halt_reason, _soft_halted, _soft_halt_reason
    global _session_date, _session_opening_equity
    global _open_premium_cost
    # last_atm_iv, bar_history, option_trader are on the BANKNIFTY runtime state

    if not _STATE_FILE.exists():
        print(f"[STATE] No state file — starting fresh.  equity=₹{_INITIAL_EQUITY:,.0f}")
        print(f"[STATE CAPITAL] equity=₹{_INITIAL_EQUITY:,.0f} peak=₹{_INITIAL_EQUITY:,.0f}")
        return
    try:
        s = json.loads(_STATE_FILE.read_text())
        # Parse into locals first — globals are only written after full success
        eq     = float(s["equity"])
        peak   = float(s["peak_equity"])
        dpnl   = float(s["daily_pnl"])
        consec = int(s["consecutive_losses"])
        halted = bool(s["trading_halted"])
        hreason= str(s["halt_reason"])
        soft   = bool(s["soft_halted"])
        sreason= str(s["soft_halt_reason"])
        seq    = float(s["session_opening_equity"])
        iv     = float(s.get("last_atm_iv", _REFERENCE_IV))  # routed to runtime state below
        sd     = s.get("session_date")
        sdate  = date.fromisoformat(sd) if sd else None
        # Parse open position keys (both formats; parsing is deferred to restore section)
        pos_raw           = s.get("open_position")    # legacy single-position key
        new_positions_raw = s.get("open_positions")   # new multi-underlying key (None if old file)
    except Exception as exc:
        print(f"[STATE] Restore failed ({exc}) — starting fresh.  equity=₹{_INITIAL_EQUITY:,.0f}")
        return

    # ── Capital consistency guard ─────────────────────────────────────────
    # The state file records the starting capital it was created with
    # (legacy files predate the field — peak_equity is the best available
    # proxy).  A session must NEVER silently continue on an equity basis
    # that conflicts with the requested PAPER_STARTING_CAPITAL: either the
    # operator explicitly archives the old state, or startup is refused.
    _state_capital_raw = s.get("starting_capital")
    _state_capital     = (
        float(_state_capital_raw) if _state_capital_raw is not None else peak
    )
    _state_capital_src = "state" if _state_capital_raw is not None else "inferred_from_peak"
    if _COMPOUND_CAPITAL and not _EXPLICIT_CAPITAL:
        # Compounding: the session basis was already rolled forward to the
        # state's current equity at startup, so continuing is intended — no
        # fixed-capital match is required. Realised P&L compounds into the base.
        print(
            f"[STATE CAPITAL] compounding — session starts from carried balance "
            f"₹{eq:,.0f} (peak ₹{peak:,.0f}, basis ₹{_INITIAL_EQUITY:,.0f})"
        )
    elif abs(_state_capital - _INITIAL_EQUITY) > 0.5:
        _has_open_positions = bool(pos_raw) or any(
            v for v in (new_positions_raw or {}).values()
        )
        _mismatch_policy = os.environ.get(
            "PAPER_STATE_CAPITAL_MISMATCH", "refuse"
        ).lower().strip()
        print(
            f"\n{'='*62}\n"
            f"[STATE CAPITAL MISMATCH]\n"
            f"  requested  : ₹{_INITIAL_EQUITY:,.0f} (PAPER_STARTING_CAPITAL)\n"
            f"  state file : ₹{_state_capital:,.0f} ({_state_capital_src})  "
            f"equity=₹{eq:,.0f}  peak=₹{peak:,.0f}\n"
            f"  open_positions_in_state={_has_open_positions}\n"
            f"{'='*62}"
        )
        if _has_open_positions:
            print(
                "[STATE CAPITAL MISMATCH] State holds OPEN positions — refusing "
                "to start.  Close/flatten via the original capital config or "
                "resolve the state file manually."
            )
            raise SystemExit(3)
        if _mismatch_policy == "archive_reset":
            _arch_dir = _STATE_FILE.parent / "archive"
            _arch_dir.mkdir(parents=True, exist_ok=True)
            _arch_path = _arch_dir / (
                f"paper_trading_state_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
            )
            _STATE_FILE.rename(_arch_path)
            print(
                f"[STATE CAPITAL MISMATCH] policy=archive_reset — state archived "
                f"to {_arch_path} — starting fresh at ₹{_INITIAL_EQUITY:,.0f}"
            )
            print(f"[STATE CAPITAL] equity=₹{_INITIAL_EQUITY:,.0f} peak=₹{_INITIAL_EQUITY:,.0f}")
            return
        print(
            "[STATE CAPITAL MISMATCH] Refusing to start (fail-closed).  Either:\n"
            "  - rerun with the state file's capital, or\n"
            "  - archive/reset the state for a new session "
            "(PAPER_STATE_CAPITAL_MISMATCH=archive_reset or the supervised "
            "runner's state-capital resolution), or\n"
            "  - move logs/paper_trading_state.json aside manually."
        )
        raise SystemExit(3)

    # All values parsed successfully — assign atomically
    _equity                 = eq
    _peak_equity            = peak
    _daily_pnl              = dpnl
    _consecutive_losses     = consec
    _trading_halted         = halted
    _halt_reason            = hreason
    _soft_halted            = soft
    _soft_halt_reason       = sreason
    _session_opening_equity = seq
    _session_date           = sdate
    _runtime_by_underlying[_PRIMARY_OPTION_TARGET].last_atm_iv = iv
    # Phase B3B: per-underlying IV restore — overwrites BANKNIFTY if new field present
    _iv_by_und = s.get("last_atm_iv_by_underlying")
    if _iv_by_und is not None:
        for _und, _und_state in _runtime_by_underlying.items():
            _raw_iv = _iv_by_und.get(_und)
            if _raw_iv is not None:
                try:
                    _und_state.last_atm_iv = float(_raw_iv)
                except (TypeError, ValueError):
                    pass  # keep default for this underlying
    # Context-contradiction cooldown restore — intra-session restarts only.
    # Re-arm the cooldown from bar 0 when the stored contradiction is still
    # fresh in wall-clock terms (1m bars ≈ minutes); stale/next-day records
    # are ignored so no fragile cross-session blocking can occur.
    _contra_by_und = s.get("last_context_contradiction_by_underlying") or {}
    for _und, _contra_meta in _contra_by_und.items():
        _und_state = _runtime_by_underlying.get(_und)
        if _und_state is None or not _contra_meta:
            continue
        try:
            _contra_ts  = datetime.fromisoformat(str(_contra_meta.get("ts", "")))
            _age_min    = (datetime.now(timezone.utc) - _contra_ts).total_seconds() / 60.0
        except Exception:
            continue
        if 0.0 <= _age_min <= float(_CONTEXT_CONTRADICTION_COOLDOWN_BARS):
            _und_state.last_contradiction_bar  = 0
            _und_state.last_contradiction_meta = dict(_contra_meta)
            print(
                f"[STATE] {_und}: context-contradiction cooldown re-armed "
                f"(age={_age_min:.1f}m < {_CONTEXT_CONTRADICTION_COOLDOWN_BARS} bars)"
            )
    # Same-contract session loss counts — same-day restarts only (never
    # carried to the next session; a new day starts with a clean slate).
    _today_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    if str(s.get("session_date") or "") == _today_ist:
        _losses_by_und = s.get("contract_session_losses_by_underlying") or {}
        for _und, _loss_data in _losses_by_und.items():
            _und_state = _runtime_by_underlying.get(_und)
            if _und_state is not None and _loss_data:
                _und_state.contract_loss_tracker.restore(_loss_data)
                print(f"[STATE] {_und}: same-contract session loss counts restored: {_loss_data}")
        _dirs_by_und = s.get("direction_session_entries_by_underlying") or {}
        for _und, _dir_data in _dirs_by_und.items():
            _und_state = _runtime_by_underlying.get(_und)
            if _und_state is not None and _dir_data:
                _und_state.direction_entry_counter.restore(_dir_data)
                print(f"[STATE] {_und}: same-direction session entry counts restored: {_dir_data}")
        _or_by_und = s.get("opening_range_by_underlying") or {}
        for _und, _or_data in _or_by_und.items():
            _und_state = _runtime_by_underlying.get(_und)
            if (_und_state is None or not _or_data
                    or str(_or_data.get("trading_date")) != _today_ist):
                continue  # OR is strictly same-day — never carried across sessions
            try:
                _und_state.or_engine.restore_range(
                    or_high=float(_or_data["or_high"]),
                    or_low=float(_or_data["or_low"]),
                    trading_date=str(_or_data["trading_date"]),
                )
                print(
                    f"[STATE] {_und}: opening range restored "
                    f"({_or_data['or_low']:.0f}–{_or_data['or_high']:.0f}) — OR guards stay active"
                )
            except Exception:
                pass  # a bad OR record must never block startup
    print(
        f"[STATE] Restored: equity=₹{_equity:,.0f}  peak=₹{_peak_equity:,.0f}  "
        f"daily_pnl=₹{_daily_pnl:+,.0f}  consec={_consecutive_losses}  "
        f"halted={_trading_halted}  soft={_soft_halted}  session={_session_date}"
    )
    print(f"[STATE CAPITAL] equity=₹{_equity:,.0f} peak=₹{_peak_equity:,.0f}")
    # Restore open positions — multi-underlying (new format) or single migration (old format).
    # Each position is parsed and restored independently; one failure does not block others.
    # _open_premium_cost is accumulated from all successfully restored positions.
    _positions_to_restore: dict[str, dict] = {}
    if new_positions_raw is not None:
        # New format: open_positions dict keyed by underlying
        for _und_key_nf, _pos_entry_nf in new_positions_raw.items():
            if _pos_entry_nf is not None:
                _positions_to_restore[_und_key_nf] = _pos_entry_nf
    elif pos_raw is not None:
        # Old format: migrate single open_position to per-underlying dict
        _legacy_und = pos_raw.get("underlying_symbol", _PRIMARY_OPTION_TARGET)
        _positions_to_restore[_legacy_und] = pos_raw

    for _und_key_r, _pos_raw_r in _positions_to_restore.items():
        _pos_target_r = _runtime_by_underlying.get(_und_key_r)
        if _pos_target_r is None:
            print(f"[STATE] WARNING: unknown underlying '{_und_key_r}' in open_positions — skipping restore.")
            continue
        try:
            _pos_data_r = dict(
                underlying_symbol = str(_pos_raw_r["underlying_symbol"]),
                contract_symbol   = str(_pos_raw_r["contract_symbol"]),
                strike            = float(_pos_raw_r["strike"]),
                option_type       = str(_pos_raw_r["option_type"]),
                expiry_date       = date.fromisoformat(str(_pos_raw_r["expiry_date"])),
                lot_size          = int(_pos_raw_r["lot_size"]),
                lots              = int(_pos_raw_r["lots"]),
                entry_premium     = float(_pos_raw_r["entry_premium"]),
                entry_spot        = float(_pos_raw_r["entry_spot"]),
                entry_iv          = float(_pos_raw_r["entry_iv"]),
                direction         = str(_pos_raw_r["direction"]),
                bar_index         = int(_pos_raw_r["bar_index"]),
                # Ratchet peak survives a same-day restart (missing in older
                # state files → falls back to entry = ratchet re-arms from live marks).
                peak_premium      = float(_pos_raw_r.get("peak_premium")
                                          or _pos_raw_r["entry_premium"]),
            )
            _pos_target_r.option_trader._position = OptionPosition(**_pos_data_r)
            _open_premium_cost += (
                _pos_data_r["entry_premium"] * _pos_data_r["lot_size"] * _pos_data_r["lots"]
            )
            print(
                f"[STATE] Open position restored ({_und_key_r}): {_pos_data_r['contract_symbol']}  "
                f"lots={_pos_data_r['lots']}  entry=₹{_pos_data_r['entry_premium']:.2f}  "
                f"direction={_pos_data_r['direction']}  expiry={_pos_data_r['expiry_date']}"
            )
            # Re-link the DB rowid so write_trade_exit() can update the correct row.
            # Called before _db.start() — recover_open_trade_row() connects directly.
            _db.recover_open_trade_row(
                underlying=_und_key_r,
                contract_symbol=_pos_data_r["contract_symbol"],
                direction=_pos_data_r["direction"],
                entry_premium=_pos_data_r["entry_premium"],
                entry_bar_index=_pos_data_r["bar_index"],
            )
        except Exception as _exc_r:
            print(f"[STATE] Position restore for {_und_key_r} failed ({_exc_r}) — skipping.")
    _bnk = _runtime_by_underlying[_PRIMARY_OPTION_TARGET]  # needed for legacy bar_history path below
    # Rehydrate MarketContextBuilder from persisted bar history.
    # Separated from account-state restore so corrupt bar history never blocks position restore.
    # Phase B3B: supports both new per-underlying format and legacy BANKNIFTY-only format.
    _hist_by_und = s.get("bar_history_by_underlying")
    if _hist_by_und is not None:
        # New format — restore each underlying independently; failures are isolated
        _restore_summary: dict[str, str] = {}
        for _und, _und_state in _runtime_by_underlying.items():
            _hist_raw = _hist_by_und.get(_und)
            if not _hist_raw:
                _restore_summary[_und] = "tf=0"
                continue
            try:
                _bars_by_tf: dict[str, list[LiveBar]] = {
                    tf: [_dict_to_bar(b) for b in raw_bars]
                    for tf, raw_bars in _hist_raw.items()
                }
                _und_state.bar_history.clear()
                for tf, raw_bars in _hist_raw.items():
                    _und_state.bar_history[tf] = list(raw_bars)
                for tf in sorted(_bars_by_tf.keys()):
                    for bar in _bars_by_tf[tf]:
                        _und_state.market_ctx_builder.update(bar=bar, timeframe=tf)
                _restore_summary[_und] = f"restored_history_tfs={len(_bars_by_tf)}"
            except Exception as exc:
                print(f"[STATE] {_und} history restore failed ({exc}) — cold start for {_und}.")
                _restore_summary[_und] = "FAILED"
        _summary_parts = ", ".join(f"{u} {info}" for u, info in _restore_summary.items())
        print(f"[STATE] Multi-underlying history restored: {_summary_parts}")
        try:
            _bnk_ctx = _runtime_by_underlying[_PRIMARY_OPTION_TARGET].market_ctx_builder.build()
            print(
                f"[STATE] BANKNIFTY MCB pre-warmstart: bias={_bnk_ctx.bias}  "
                f"conf={_bnk_ctx.confidence:.2f}  "
                f"mcb_ready_timeframes={list(_bnk_ctx.timeframes_with_history)}"
            )
        except Exception:
            pass
    else:
        # Legacy format — BANKNIFTY-only bar_history; NIFTY/SENSEX start cold
        try:
            _hist_raw_legacy = s.get("bar_history", {})
            if _hist_raw_legacy:
                _bars_by_tf_legacy: dict[str, list[LiveBar]] = {
                    tf: [_dict_to_bar(b) for b in raw_bars]
                    for tf, raw_bars in _hist_raw_legacy.items()
                }
                _bnk.bar_history.clear()
                for tf, raw_bars in _hist_raw_legacy.items():
                    _bnk.bar_history[tf] = list(raw_bars)
                _total = sum(len(v) for v in _bars_by_tf_legacy.values())
                for tf in sorted(_bars_by_tf_legacy.keys()):
                    for bar in _bars_by_tf_legacy[tf]:
                        _bnk.market_ctx_builder.update(bar=bar, timeframe=tf)
                _ctx_legacy = _bnk.market_ctx_builder.build()
                print(
                    f"[STATE] Legacy BANKNIFTY history restored: {_total} bars "
                    f"across {len(_bars_by_tf_legacy)} timeframe(s)  "
                    f"bias={_ctx_legacy.bias}  conf={_ctx_legacy.confidence:.2f}  "
                    f"ready={list(_ctx_legacy.timeframes_with_history)}"
                )
        except Exception as exc:
            print(f"[STATE] MCB warm-start skipped ({exc}) — cold start required.")


# ── MCB CSV warm-start ────────────────────────────────────────────────────────
# Feeds each underlying's MarketContextBuilder with historical 1m data from CSV
# so all timeframes (15m, 1h, 4h, 1d) start ready — eliminating the cold-start
# NEUTRAL lock that otherwise persists for 4-5 hours after a pipeline restart.
#
# Uses private MTF builder and DailyBarAccumulator per underlying so the live
# instances are not contaminated.  Does NOT touch equity, risk, or exec state.
# Does NOT force bias — MCB builds its own classification from the data.

def _csv_end_date_key(p: "Path") -> str:
    """
    Extract end-date string 'YYYY-MM-DD' from a warm-start filename.

    Format: {prefix}_1m_{start-date}_{end-date}.csv
    Returns '' when unparseable so unparseable files sort last.
    """
    try:
        return p.stem.split("_")[-1]   # last segment is the end date
    except Exception:
        return ""


def _csv_last_line_ts(p: "Path") -> "datetime | None":
    """Return the timestamp from the last row with a parseable timestamp, or None."""
    try:
        import csv as _csv2
        last_valid: "datetime | None" = None
        with p.open(newline="", encoding="utf-8") as fh:
            for row in _csv2.DictReader(fh):
                ts_raw = row.get("timestamp") or row.get("date") or ""
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    last_valid = ts
                except Exception:
                    pass   # skip non-timestamp rows (padding, comments)
        return last_valid
    except Exception:
        return None


def _trading_days_since(last: "date", today: "date") -> int:
    """Weekday count from day after last up to today (approximates trading days; ignores NSE holidays)."""
    count = 0
    d = last + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


_MCB_WARMSTART_STALE_DAYS: int = 3   # warn if warm-start data is older than this many trading days


def _find_warmstart_csv(option_underlying: str) -> "Path | None":
    """
    Return the warm-start CSV with the latest end-date among all matching files.

    Selection:
      - File must match {prefix}_1m_*.csv in data/.
      - File must be >= 50 KB (filters out single-day or stub files).
      - Among qualifying files, selects the one whose end-date in the filename
        is most recent. When end-dates tie, larger file wins (more history).
    """
    prefix = _MCB_WARMSTART_CSV_PREFIX.get(option_underlying)
    if not prefix:
        return None
    data_dir = Path("data")
    if not data_dir.exists():
        return None
    candidates = [
        p for p in data_dir.glob(f"{prefix}_1m_*.csv")
        if p.stat().st_size >= 50_000
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (_csv_end_date_key(p), p.stat().st_size))


def _csv_bars_to_livebars(csv_path: "Path", feed_symbol: str) -> "list[LiveBar]":
    """Parse a 1m OHLCV CSV (timestamp,open,high,low,close,volume) into LiveBars."""
    import csv as _csv
    bars: list[LiveBar] = []
    try:
        with csv_path.open(newline="") as fh:
            for row in _csv.DictReader(fh):
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    bars.append(LiveBar(
                        symbol          = feed_symbol,
                        timeframe       = "MIN_1",
                        start_timestamp = ts,
                        end_timestamp   = ts + timedelta(minutes=1),
                        open_price      = float(row["open"]),
                        high_price      = float(row["high"]),
                        low_price       = float(row["low"]),
                        close_price     = float(row["close"]),
                        volume          = float(row.get("volume") or 0),
                    ))
                except Exception:
                    pass
    except Exception as exc:
        print(f"[MCB WARMSTART] Failed to read {csv_path}: {exc}")
    return bars


def _warm_start_mcb_from_csv() -> None:
    """
    Pre-seed every enabled underlying's MCB with historical 1m CSV data.
    Called once between _load_state() and _db.start().

    For each underlying:
      - Skips if the state file already has >= 20 bars of 15m history.
      - Finds the latest matching CSV in data/.
      - Saves any intraday bars that were restored from state (June 8 context).
      - Resets the MCB classifiers so older CSV timestamps are accepted without
        rejection. (Without reset, state bars from June 8 set _last_timestamp in
        RegimeClassifier, causing all April-May CSV bars to be silently dropped.)
      - Feeds CSV bars through a private MultiTimeframeBuilder → 15m, 1h, 4h bars.
      - Feeds CSV bars through a private DailyBarAccumulator → 1d bars.
      - Re-feeds saved intraday state bars on top (newer timestamps → accepted).
      - Prints a truthful readiness summary including WARN for any loaded-but-not-ready TF.
    """
    for key, und_state in _runtime_by_underlying.items():
        existing_15m = len(und_state.bar_history.get("15m", []))
        # Only skip the CSV if the state ALSO seeds the HIGHER timeframes (4h + 1d,
        # which carry 60% of the MCB bias weight — 1d 0.35, 4h 0.25). The state
        # persists 1m/15m/1h but NOT 4h/1d, so N 15m bars (~hours) cannot build
        # daily/4h context — skipping the CSV then leaves the higher TFs BLIND and
        # the MCB stuck NEUTRAL for hours (observed 2026-07-01: ~4h delay to first
        # trade). Load the CSV whenever 4h or 1d is missing so previous-days'
        # candles seed the higher timeframes near open, as the model did before.
        try:
            _tfs_ready = set(und_state.market_ctx_builder.build().timeframes_with_history)
        except Exception:
            _tfs_ready = set()
        _higher_ready = {"4h", "1d"}.issubset(_tfs_ready)
        if existing_15m >= 20 and _higher_ready:
            try:
                _ctx = und_state.market_ctx_builder.build()
                print(
                    f"[MCB WARMSTART] {key}: state has {existing_15m} 15m bars + 4h/1d "
                    f"ready — skipping CSV  bias={_ctx.bias}  conf={_ctx.confidence:.2f}"
                )
            except Exception:
                pass
            continue
        if existing_15m >= 20 and not _higher_ready:
            print(
                f"[MCB WARMSTART] {key}: state has {existing_15m} 15m bars but 4h/1d "
                f"NOT seeded — loading CSV to warm the higher timeframes (avoids the "
                f"NEUTRAL-bias startup delay)."
            )

        csv_path = _find_warmstart_csv(key)
        if csv_path is None:
            print(
                f"[MCB WARMSTART] {key}: no CSV found in data/ — "
                f"cold start (bias=NEUTRAL until bars accumulate)"
            )
            print(
                f"[MCB WARMSTART SOURCE] {key}: no file — "
                f"run scripts/refresh_mcb_warmstart_data.py to fetch warm-start data"
            )
            continue

        # Print source info and staleness before loading.
        _src_last_ts = _csv_last_line_ts(csv_path)
        if _src_last_ts is not None:
            _today = datetime.now(timezone.utc).date()
            _age_td = _trading_days_since(_src_last_ts.date(), _today)
            print(
                f"[MCB WARMSTART SOURCE] {key}"
                f"  file={csv_path.name}"
                f"  last_ts={_src_last_ts.isoformat()[:19]}"
                f"  age_trading_days={_age_td}"
            )
            if _age_td > _MCB_WARMSTART_STALE_DAYS:
                print(
                    f"[MCB WARMSTART STALE WARN] {key}: warm-start data is {_age_td} trading days old"
                    f" — run scripts/refresh_mcb_warmstart_data.py before next live session"
                )
        else:
            print(f"[MCB WARMSTART SOURCE] {key}  file={csv_path.name}  last_ts=unknown")

        bars_1m = _csv_bars_to_livebars(csv_path, und_state.feed_symbol)
        if not bars_1m:
            print(f"[MCB WARMSTART] {key}: CSV {csv_path.name} is empty or unreadable — cold start")
            continue

        # Collect intraday state bars before resetting the MCB.
        # These carry the most recent session context and will be re-fed on top of the CSV.
        _state_bars_to_replay: list[tuple[str, "LiveBar"]] = []
        for _tf in sorted(und_state.bar_history.keys()):
            for _raw in und_state.bar_history.get(_tf, []):
                try:
                    _state_bars_to_replay.append((_tf, _dict_to_bar(_raw)))
                except Exception:
                    pass

        # Reset so CSV bars (older timestamps) are not rejected by the strictly-increasing
        # timestamp invariant that RegimeClassifier enforces per timeframe.
        und_state.market_ctx_builder.reset()

        warmup_mtf   = MultiTimeframeBuilder()
        warmup_daily = DailyBarAccumulator(symbol=und_state.feed_symbol)
        counts: dict[str, int] = {"1m": 0, "15m": 0, "1h": 0, "4h": 0, "1d": 0}

        for bar_1m in bars_1m:
            und_state.market_ctx_builder.update(bar=bar_1m, timeframe="1m")
            counts["1m"] += 1

            for tf_key, tf_bar in warmup_mtf.update_bar(bar=bar_1m).items():
                tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
                if tf_name in ("15m", "1h", "4h"):   # 5m not in MCB SUPPORTED_TIMEFRAMES
                    und_state.market_ctx_builder.update(bar=tf_bar, timeframe=tf_name)
                    counts[tf_name] += 1

            daily_bar = warmup_daily.update(bar_1m)
            if daily_bar is not None:
                und_state.market_ctx_builder.update(bar=daily_bar, timeframe="1d")
                counts["1d"] += 1

        # Flush the CSV's final day into the 1d classifier. The accumulator only
        # emits day N when day N+1 arrives, so without this the most recent daily
        # bar never lands and the 0.35-weight 1d classification trades a full day
        # staler than the data (2026-07-15: frozen at Fri 07-11 BULLISH → LONG
        # conf=1.00 all day through a breakdown). BacktestRunner flushes; parity.
        _final_daily = warmup_daily.flush()
        if _final_daily is not None:
            und_state.market_ctx_builder.update(bar=_final_daily, timeframe="1d")
            counts["1d"] += 1

        # Re-feed intraday state bars (newer timestamps accepted on top of CSV).
        # update() returns False on rejection (e.g. older-than-CSV timestamps),
        # so _refed counts bars the classifiers actually took.
        _refed = 0
        for _stf, _sbar in _state_bars_to_replay:
            try:
                if und_state.market_ctx_builder.update(bar=_sbar, timeframe=_stf):
                    _refed += 1
            except Exception:
                pass

        print(
            f"[MCB WARMSTART] {key}: loaded {csv_path.name}  "
            f"1m={counts['1m']}  15m={counts['15m']}  "
            f"1h={counts['1h']}  4h={counts['4h']}  1d={counts['1d']}"
            + (f"  (+{_refed} intraday state bars re-fed)" if _refed else "")
        )
        try:
            _ctx = und_state.market_ctx_builder.build()
            _tfs = sorted(_ctx.timeframes_with_history) if _ctx.timeframes_with_history else []
            print(
                f"[MCB WARMSTART] {key}: MCB ready  "
                f"bias={_ctx.bias}  conf={_ctx.confidence:.2f}  "
                f"timeframes_ready={_tfs}"
            )
            _ready_set = set(_tfs)
            for _etf in sorted({"1m", "15m", "1h", "4h", "1d"} - _ready_set):
                _loaded = counts.get(_etf, 0)
                if _loaded > 0:
                    print(
                        f"[MCB WARMSTART WARN] {key} {_etf}: loaded={_loaded} bars"
                        f" but not ready — check CSV coverage"
                    )
        except Exception as exc:
            print(f"[MCB WARMSTART] {key}: MCB build check failed ({exc})")


_acquire_pid_lock()   # must run before _load_state so state is never shared by two instances
print(
    f"[CAPITAL CONFIG] starting_capital=₹{_INITIAL_EQUITY:,.0f}"
    f"  max_open_positions={_MAX_OPEN_POSITIONS}"
    f"  source={'env:PAPER_STARTING_CAPITAL' if os.environ.get('PAPER_STARTING_CAPITAL') else 'default'}"
)
print(
    f"[RISK CONFIG] profile={_PAPER_RISK_PROFILE}"
    f"  capital=₹{_INITIAL_EQUITY:,.0f}"
    f"  tier2={_TIER2_RISK_FRAC * 100:.1f}%"
    f"  tier3={_TIER3_RISK_FRAC * 100:.1f}%"
    f"  max_single_trade={_PAPER_MAX_SINGLE_TRADE_RISK_FRAC * 100:.1f}%"
)
from platform_core.backtesting.backtest_runner import _RATCHET_ARM_PCT as _ratchet_arm
print(
    f"[EXIT PROTECTION] breakeven_ratchet_arm=+{_ratchet_arm:.0%}"
    f" (armed winner can't become a loser; armed position frees its risk slot;"
    f" EXIT_RATCHET_ARM_PCT=0 disables)"
)
print(
    f"[STRIKE ADOPTION] tiebreak={_STRIKE_TIEBREAK or 'OFF'}"
    f"  tiebreak_min_floor={_STRIKE_TIEBREAK_MIN_FLOOR:.3f}"
    f"  gex_spotside={'ON' if os.environ.get('STRIKE_GEX_SPOTSIDE', '0') == '1' else 'OFF'}"
    f"  exit_bos_min_peak={_EXIT_BOS_MIN_PEAK_PCT:.0%}"
    + ("" if _STRIKE_TIEBREAK != "delta" or _STRIKE_TIEBREAK_MIN_FLOOR > 0 else
       "  !! WARNING: delta tie-break UNGUARDED — will reach SENSEX (measured LOSS)")
)
print(
    f"[SIZING RAILS] lot_ceilings="
    f"{'pure-%-of-capital (no fixed ceiling)' if _TIER2_MAX_LOTS <= 0 else f'T2={_TIER2_MAX_LOTS} T3={_TIER3_MAX_LOTS} (capital-scaled)'}"
    f"  position_premium_cap={_MAX_POSITION_PREMIUM_FRAC:.0%} of equity"
    f"  correlated_same_dir_cap={_CORRELATED_HEAT_FRACTION:.0%}"
    f"  heat_cap={_MAX_HEAT_FRACTION:.0%}"
    f"  freeze_qty=N:{_FREEZE_QTY_BY_SYM['NIFTY']} B:{_FREEZE_QTY_BY_SYM['BANKNIFTY']} S:{_FREEZE_QTY_BY_SYM['SENSEX']}"
    f"  per_trade_max_loss={_PER_TRADE_MAX_LOSS_FRAC:.1%}×adverse{_PER_TRADE_ADVERSE_FRAC:.2f}"
    f"  regime_desize_mult={_REGIME_DESIZE_MULT:g}"
    f"  reentry_haircut={_REENTRY_RISK_HAIRCUT:g}"
)
if _TIER2_RISK_FRAC > 0.025:
    print(
        f"[WARNING] Paper risk profile '{_PAPER_RISK_PROFILE}' uses "
        f"{_TIER2_RISK_FRAC * 100:.1f}% Tier2 risk — not approved for real funds."
    )
# Spot index instrument tokens (Kite) — for seeding the OR on a late/crash start.
_SPOT_INDEX_TOKENS: dict[str, int] = {"NIFTY": 256265, "SENSEX": 265, "BANKNIFTY": 260105}


def _seed_opening_ranges_if_missing() -> None:
    """Fail-safe for a start AFTER the 09:15–09:30 opening-range window.

    The OR engine only builds from LIVE ticks during 09:15–09:30; warm-start CSV
    bars do NOT feed it, and a same-day OR is only restorable from state if a
    PRIOR session that day already established it. So a late first start (or a
    crash-restart before any OR was persisted) leaves every OR guard
    (inside-block / buffer / extension-chase) silently inactive — the model then
    trades chop blind. Root cause of 2026-07-08 −₹2,893: the session started
    10:29 IST, had no OR, and took 4 inside-OR entries a seeded OR would have
    blocked (each measured at confidence_multiplier=0.0 against the real OR).

    If it is past 09:30 IST and an enabled underlying still has no OR, fetch
    today's 09:15–09:30 1m bars from Kite (REST — token only, no WS) and
    restore_range(). Never raises; a fetch failure logs LOUDLY so the operator
    knows the guards are inactive."""
    now_ist = datetime.now(_IST)
    if now_ist.hour * 60 + now_ist.minute < 9 * 60 + 31:
        return  # still within/at the OR window — live ticks will build it normally
    today = now_ist.date().isoformat()
    for _und, _st in _runtime_by_underlying.items():
        if not is_option_target_enabled(_und) or _st.or_engine.ready:
            continue  # not traded here, or already restored from a prior session
        _tok = _SPOT_INDEX_TOKENS.get(_und)
        if _tok is None:
            continue
        try:
            _candles = adapter.kite.historical_data(
                instrument_token=_tok, from_date=f"{today} 09:15:00",
                to_date=f"{today} 09:30:00", interval="minute", continuous=False,
            )
            if not _candles:
                print(f"[OR SEED] {_und}: Kite returned no 09:15–09:30 bars — "
                      f"OR NOT seeded, chop guards INACTIVE for the session!")
                continue
            _hi = max(c["high"] for c in _candles)
            _lo = min(c["low"] for c in _candles)
            _st.or_engine.restore_range(or_high=_hi, or_low=_lo, trading_date=today)
            print(f"[OR SEED] {_und}: late start ({now_ist:%H:%M} IST) — seeded OR "
                  f"from Kite ({_lo:.0f}–{_hi:.0f}, {len(_candles)} bars) — chop guards ACTIVE")
        except Exception as _exc:
            print(f"[OR SEED] {_und}: FAILED to seed OR from Kite ({_exc}) — "
                  f"chop guards INACTIVE, model would trade chop-blind!")


_print_gate_startup()  # print gate profile + per-underlying thresholds at every startup
_load_state()         # restore from disk at process startup
_warm_start_mcb_from_csv()   # pre-seed MCB from historical CSV — eliminates cold-start NEUTRAL lock
_seed_opening_ranges_if_missing()  # late-start fail-safe: rebuild OR from Kite so chop guards stay active

# ── Warm-start readiness guard — NEVER start blind ────────────────────────────
# The 4h + 1d timeframes carry 60% of the MCB bias weight. If they are not seeded
# the model starts BLIND and sits NEUTRAL for hours (observed 2026-07-01). The
# model won't trade blind — bias stays NEUTRAL — but the operator MUST know to
# refresh the warm-start data. Loud, unmissable CRITICAL alert; must be clean
# before arming real funds.
_WARMSTART_SEEDED_OK: bool = False   # set by _verify_warmstart_not_blind()


def _verify_warmstart_not_blind() -> None:
    global _WARMSTART_SEEDED_OK
    _blind: list[tuple[str, list[str]]] = []
    for _k, _st in _runtime_by_underlying.items():
        if not is_option_target_enabled(_k):
            continue
        try:
            _rd = set(_st.market_ctx_builder.build().timeframes_with_history)
        except Exception:
            _rd = set()
        if not {"4h", "1d"}.issubset(_rd):
            _blind.append((_k, sorted(_rd)))
    if _blind:
        print("\n" + "=" * 66)
        print("[MCB WARMSTART CRITICAL] Higher timeframes (4h/1d) NOT seeded —")
        print("  the model would start BLIND (bias NEUTRAL for hours).")
        for _k, _rd in _blind:
            print(f"    {_k}: ready={_rd}  — missing 4h and/or 1d")
        print("  FIX: PYTHONPATH=. python scripts/refresh_mcb_warmstart_data.py, then restart.")
        print("  *** Do NOT arm real funds with a blind start. ***")
        print("=" * 66 + "\n")
        try:
            _db.write_runtime_event(
                "MCB_WARMSTART_BLIND", severity="CRITICAL",
                message=f"higher timeframes not seeded: {_blind}",
                equity=_INITIAL_EQUITY, bar_index=0,
            )
        except Exception:
            pass
    else:
        _WARMSTART_SEEDED_OK = True
        print("[MCB WARMSTART] READINESS OK — all enabled underlyings have 4h+1d "
              "seeded (not starting blind).")


_verify_warmstart_not_blind()

# Start persistence layer — fire-and-forget, never affects trading logic
try:
    _session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _db.start(session_id=f"live_{_session_ts}", run_mode="live")
    _db.write_runtime_event(
        "PIPELINE_START",
        severity="INFO",
        message=f"equity=₹{_equity:,.0f}  halted={_trading_halted}",
        equity=_equity,
    )
except Exception:
    pass

# ── Structural replay-parity fix: snapshot the TRUE MCB warm-start state per session ──
# The replay's CSV warm-start diverges from the live persisted higher-TF state (4h/1d carry
# 60% of MCB bias weight), which inverted historical replay DIRECTION (parity 1/11 vs live).
# Persist a dated snapshot of the exact boot bar_history so future replays can
# SurgicalReplay.warm_start_from_state(...) and stay faithful. Fire-and-forget; never trades.
try:
    from pathlib import Path as _SnapPath
    _snap_dir = _SnapPath("logs/mcb_snapshots")
    _snap_dir.mkdir(parents=True, exist_ok=True)
    _snap = {
        _u: _st.bar_history
        for _u, _st in _runtime_by_underlying.items()
        if is_option_target_enabled(_u) and _st.bar_history
    }
    if _snap:
        _snap_path = _snap_dir / f"{datetime.now(timezone.utc).date().isoformat()}.json"
        _snap_path.write_text(json.dumps(_snap))
        print(f"[MCB SNAPSHOT] wrote warm-start state → {_snap_path.name} "
              + ", ".join(f"{_u}:{sum(len(_v) for _v in _h.values())}bars" for _u, _h in _snap.items()))
except Exception as _snap_exc:
    print(f"[MCB SNAPSHOT] skipped ({_snap_exc})")

# ── Cross-asset shadow (§4.2) — logs-only, daemon thread, NEVER touches the trade path ──
# pre_open risk-state/directional read + intraday constituent flow, all network I/O confined
# to its own thread. Tests whether cross-asset beats the stale HTF bias for the open-drive window.
try:
    from platform_core.data.live.crossasset_shadow import spawn_session_logger as _spawn_xa
    _xa_unds = [_u for _u in _runtime_by_underlying if is_option_target_enabled(_u)]
    if _xa_unds:
        _spawn_xa(adapter, _xa_unds)
        print(f"[CROSSASSET SHADOW] session logger started for {_xa_unds} (logs-only)")
except Exception as _xa_exc:
    print(f"[CROSSASSET SHADOW] not started ({_xa_exc})")

# Execution router — mode is paper by default; real_dry_run and real_armed are
# opt-in via EXECUTION_MODE env var.  Router is always created; paper mode is a no-op.
# Pass the Kite client so real_armed can place orders + query positions/margin.
# Paper/dry-run ignore it.  (adapter is the KiteAdapter created at module load.)
_exec_router = _ExecutionRouter(mode=_EXECUTION_MODE, kite=getattr(adapter, "kite", None))
_exec_router.startup_print()
_session_tag: str = _session_ts if "_session_ts" in dir() else "live"


def _gather_local_positions() -> list[tuple[str, int, float]]:
    """(tradingsymbol, net_quantity, entry_premium) for every open local position."""
    out: list[tuple[str, int, float]] = []
    for _st in _runtime_by_underlying.values():
        _pos = _st.option_trader._position
        if _pos is not None:
            out.append((_pos.contract_symbol, _pos.lots * _pos.lot_size, _pos.entry_premium))
    return out


def _do_position_reconcile(phase: str) -> bool:
    """real_armed only: reconcile broker net positions vs local open positions and
    HALT on a HIGH/CRITICAL break (the pipeline's view has diverged from the
    broker).  Returns True if ok or skipped (paper/dry-run), False if it halted."""
    global _trading_halted, _halt_reason
    if _EXECUTION_MODE != "real_armed":
        return True
    try:
        _broker_pos = _exec_router.get_broker_positions()
        _local_pos  = _gather_local_positions()
        _outcome    = reconcile_positions(
            local_positions=_local_pos, broker_positions=_broker_pos,
        )
    except Exception as _exc:
        print(f"[RECONCILE] {phase} query error: {_exc} (will retry next cycle)")
        return True
    if _outcome.ok:
        print(f"[RECONCILE] {phase} OK  breaks={_outcome.break_count}")
        return True
    _trading_halted = True
    _halt_reason = (
        f"RECONCILE_BREAK {phase} sev={_outcome.highest_severity} {_outcome.summary}"
    )
    print_warning_block(
        "RECONCILIATION BREAK — TRADING HALTED",
        bar_index=_bar_count,
        reason=_halt_reason + " (reconcile broker vs local manually)",
    )
    try:
        _db.write_runtime_event(
            "RECONCILE_BREAK", severity="CRITICAL", message=_halt_reason, equity=_equity,
        )
    except Exception:
        pass
    return False


ENGINES = {}
SWING_DETECTORS = {}
LATEST_HTF_BARS = {}

# Feed-gap structure continuity: per-symbol start_timestamp of the last bar fed
# to the structure engine. When the gap to the next bar exceeds the threshold
# (host suspend, feed stall, reconnect hole), that symbol's structure + swing
# state is reset so no swing/BOS/CHOCH/FVG signal is computed across the
# discontinuity. NSE/BSE run a continuous 09:15–15:30 session (no lunch break),
# so any intraday gap this large is a feed problem, never normal.
_last_struct_bar_ts: dict = {}
_STRUCTURE_GAP_RESET_MIN: float = float(
    os.environ.get("LIVE_STRUCTURE_GAP_RESET_MIN", "10") or 10
)


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


# ── Helpers reused verbatim from BacktestRunner ────────────────────────────────

def _option_exit_signal(emitted_events, trader: OptionPaperTrader):
    """
    Detect CHoCH/BOS structure reversal that invalidates the open position.

    StructureSide enum values are lowercase ("bullish", "bearish") — comparisons
    use .lower() for robustness against enum value changes.
    Proven bug (2026-06-04): uppercase comparisons silently failed; structure
    exits never fired in production.
    """
    pos = trader._position
    if pos is None:
        return None
    # [wide04] Pre-arm berth: an UNARMED position ignores 1m structure exits
    # until premium has been at least MIN_PEAK in its favor. The 30% stop,
    # fast-fail, TP and breakeven ratchet still check every minute — this
    # suppresses only the 1m BOS/CHoCH leg, which on 07-17 shook the book out of
    # right-direction entries near breakeven. THESIS_INVALIDATED still fires
    # downstream (same precedence as surgical_replay). 0 = off.
    #   Evidence: night 2026-07-19 H6 — standalone +23,454 net, and ROBUST
    #   across the 0.02-0.05 plateau (wide02 +21,028 / wide03 +22,478 /
    #   wide04 +23,454 / wide05 +21,416), so the 0.04 choice is not a cliff.
    if (_EXIT_BOS_MIN_PEAK_PCT > 0.0
            and not trader.ratchet_armed()
            and pos.peak_premium < pos.entry_premium * (1.0 + _EXIT_BOS_MIN_PEAK_PCT)):
        return None
    for event in emitted_events:
        evt  = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        side = (event.side.value if hasattr(event.side, "value") else str(event.side)).lower()
        is_reversal = "CHOCH" in evt or evt == "BOS"
        if pos.direction == "LONG"  and is_reversal and side == "bearish":
            return f"STRUCTURE_EXIT ({evt})"
        if pos.direction == "SHORT" and is_reversal and side == "bullish":
            return f"STRUCTURE_EXIT ({evt})"
    return None


def _determine_risk_tier(confidence: float) -> int:
    """
    Deterministic tier selection from account state and confidence.

    Tier 1 (Defensive): current drawdown >= _TIER1_DD_ENTRY.
    Tier 3 (Aggressive): equity > initial*(1+gain_gate) AND low drawdown AND high conf.
    Tier 2 (Normal): all other conditions.
    """
    drawdown = max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0))
    if drawdown >= _TIER1_DD_ENTRY:
        return 1
    # Tier 3 activates only when the session OPENED above the startup threshold —
    # same-day profits cannot promote tier within the current session.
    if (
        _session_opening_equity >= _INITIAL_EQUITY * (1.0 + _TIER3_MIN_GAIN)
        and drawdown < _TIER3_MAX_DD
        and confidence >= _TIER3_CONF_GATE
    ):
        return 3
    return 2


def _check_capital_limits() -> tuple[bool, str]:
    """
    Hard halt: no new trades until the halt clears.

    Triggers (in priority order):
      1. Account drawdown >= _MAX_ACCOUNT_DD           → manual restart required
      2. Daily loss >= _HARD_HALT_DAILY_LOSS_FRAC      → auto-resumes next session
      3. Consecutive losses >= _HARD_HALT_CONSEC_LOSSES → auto-resumes next session
    """
    drawdown = max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0))
    if drawdown >= _MAX_ACCOUNT_DD:
        return True, (
            f"MAX_DRAWDOWN {drawdown:.1%} >= {_MAX_ACCOUNT_DD:.1%} "
            f"— manual restart required"
        )
    daily_loss_frac = max(0.0, -_daily_pnl) / max(_session_opening_equity, 1.0)
    if daily_loss_frac >= _HARD_HALT_DAILY_LOSS_FRAC:
        return True, (
            f"DAILY_LOSS {daily_loss_frac:.1%} >= {_HARD_HALT_DAILY_LOSS_FRAC:.1%} "
            f"— resumes next session"
        )
    if _HARD_HALT_CONSEC_LOSSES > 0 and _consecutive_losses >= _HARD_HALT_CONSEC_LOSSES:
        return True, (
            f"CONSECUTIVE_LOSSES {_consecutive_losses} >= {_HARD_HALT_CONSEC_LOSSES} "
            f"— resumes next session"
        )
    return False, ""


def _check_soft_halt() -> tuple[bool, str]:
    """
    Soft halt: trading continues in defensive mode only.

    Triggered when daily loss >= _SOFT_HALT_DAILY_LOSS_FRAC.
    Forces use of _sizer_soft (0.7% risk, max_lots=1, min_conf=0.65).
    Clears automatically on new session (same as a hard daily-loss halt).
    """
    daily_loss_frac = max(0.0, -_daily_pnl) / max(_session_opening_equity, 1.0)
    if daily_loss_frac >= _SOFT_HALT_DAILY_LOSS_FRAC:
        return True, (
            f"daily_loss={daily_loss_frac:.1%} >= {_SOFT_HALT_DAILY_LOSS_FRAC:.1%}"
        )
    return False, ""


def _try_resume_trading(current_date: date) -> None:
    """
    Detect new trading session; reset daily counters and conditionally resume.

    On each new session:
      - _daily_pnl and _consecutive_losses reset to 0.
      - _soft_halted always clears (soft halt is intra-day only).
      - Hard halts from DAILY_LOSS or CONSECUTIVE_LOSSES auto-resume.
      - Hard halt from MAX_DRAWDOWN never auto-resumes.
    """
    global _session_date, _session_opening_equity, _daily_pnl
    global _trading_halted, _halt_reason
    global _soft_halted, _soft_halt_reason, _consecutive_losses

    if _session_date is not None and current_date == _session_date:
        return  # same session — nothing to reset

    _session_date = current_date
    _session_opening_equity = _equity
    _daily_pnl = 0.0
    _consecutive_losses = 0
    _soft_halted = False
    _soft_halt_reason = ""

    _daily_halt = "DAILY_LOSS" in _halt_reason or "CONSECUTIVE_LOSSES" in _halt_reason
    if _trading_halted and _daily_halt:
        _trading_halted = False
        _halt_reason = ""
        print(
            f"\n[CAPITAL] Session {current_date} — resuming after daily halt.  "
            f"equity=₹{_equity:,.0f}"
        )
    else:
        tier = _determine_risk_tier(0.5)
        print(
            f"\n[CAPITAL] Session {current_date} | "
            f"equity=₹{_equity:,.0f} | peak=₹{_peak_equity:,.0f} | "
            f"tier={tier} | halted={'YES' if _trading_halted else 'NO'}"
        )
    _save_state()


def _validate_entry_contract(
    state: "UnderlyingRuntimeState",
    srec,
    lots: int,
) -> tuple[bool, str]:
    """
    Pre-flight contract sanity check before option_trader.open().

    Returns (ok, rejection_reason).  Checks (all underlyings):
    - underlying is in _OPTION_TARGETS_ENABLED
    - contract_symbol is not a raw index name
    - contract_symbol contains CE or PE
    - lots >= 1
    - srec.lot_size matches state.lot_size
    - srec.underlying_symbol matches state.option_underlying
    - strike_price > 0

    SENSEX/BFO additional check:
    - state.exchange must be "BFO" (fail-closed: if misconfigured, do not trade)
    """
    sym = srec.option_symbol
    if state.option_underlying not in _OPTION_TARGETS_ENABLED:
        return False, f"underlying {state.option_underlying!r} not in ENABLED set"
    if sym in _RAW_INDEX_SYMBOLS:
        return False, f"contract {sym!r} is a raw index symbol — not a valid option contract"
    if "CE" not in sym and "PE" not in sym:
        return False, f"contract {sym!r} missing CE/PE suffix — not a valid option contract"
    if lots < 1:
        return False, f"lots={lots} < 1"
    if srec.lot_size != state.lot_size:
        return False, (
            f"lot_size mismatch: srec={srec.lot_size} != state={state.lot_size} "
            f"for {state.option_underlying}"
        )
    if getattr(srec, "underlying_symbol", None) != state.option_underlying:
        return False, (
            f"underlying mismatch: srec.underlying_symbol={getattr(srec, 'underlying_symbol', None)!r} "
            f"!= state.option_underlying={state.option_underlying!r}"
        )
    strike = getattr(srec, "strike_price", None)
    if strike is None or strike <= 0:
        return False, f"strike_price={strike!r} is invalid (must be > 0)"
    # SENSEX fail-closed: BFO exchange required; any misconfiguration blocks the trade
    if state.option_underlying == "SENSEX" and state.exchange != "BFO":
        return False, (
            f"SENSEX must use exchange='BFO', got {state.exchange!r} — "
            "re-check OPTION_CHAIN_CONFIG"
        )
    return True, ""


def _size_live_position(
    state: UnderlyingRuntimeState,
    rec,
    market_ctx,
    tier: int,
    effective_conf: float,
) -> PositionSizingResult:
    """
    Size position using the tier-appropriate PositionSizer for the given underlying.

    Tier selection determines:
      - which PositionSizer instance (different max_risk_fraction_per_trade)
      - maximum_lot_count ceiling passed to the request
      - max_allowed_drawdown_fraction on the PortfolioRiskSnapshot

    effective_conf is the post-filter confidence (MCB × session × PDL × OR × FVG),
    matching BacktestRunner._size_position which passes effective_confidence to the
    PositionSizingRequest.confidence_score field.
    """
    _tier_maxlots = {0: _SOFT_HALT_MAX_LOTS, 1: _TIER1_MAX_LOTS, 2: _TIER2_MAX_LOTS, 3: _TIER3_MAX_LOTS}

    sizer    = state.sizers[tier]
    max_lots = _tier_maxlots[tier]

    entry_premium = rec.last_traded_price

    # Tier2/3 ceiling 0 = pure %-of-capital sizing: no fixed lot-count ceiling;
    # the risk budget, premium cap, heat, and freeze-qty rails below bind
    # instead. Tier0/1 (defensive) ALWAYS keep their fixed 1-lot ceiling.
    if tier >= 2 and max_lots <= 0:
        max_lots = 10_000  # sentinel: %-rails below are the real constraint
    elif tier >= 2 and _LOT_CAP_SCALE_BASE_CAPITAL > 0.0:
        # Fixed-ceiling mode: scale the ceiling with capital (only grows above
        # the baseline). Session-opening equity so intraday P&L can't move it.
        _cap_scale = max(1.0, _session_opening_equity / _LOT_CAP_SCALE_BASE_CAPITAL)
        max_lots = int(max_lots * _cap_scale)

    # Allocation control: cap the proven net-loser (NIFTY) below the winner so
    # it cannot out-size SENSEX even at the raised ceilings (_PER_SYMBOL_MAX_LOTS).
    _sym_cap = _PER_SYMBOL_MAX_LOTS.get(state.option_underlying)
    if _sym_cap is not None:
        max_lots = min(max_lots, _sym_cap)

    # Exchange freeze-quantity rail (per-order exchange limit).
    _freeze_qty = _FREEZE_QTY_BY_SYM.get(state.option_underlying, 0)
    if _freeze_qty > 0 and rec.lot_size > 0:
        max_lots = min(max_lots, max(_freeze_qty // rec.lot_size, 1))

    # Single-position premium cap (tail defense — the buyer's true worst case
    # is the whole premium). 0 lots is a legitimate outcome: if one lot costs
    # more than the cap, the trade is rejected rather than oversized.
    if _MAX_POSITION_PREMIUM_FRAC > 0.0 and entry_premium > 0.0 and rec.lot_size > 0:
        _prem_cap_lots = int(
            (_MAX_POSITION_PREMIUM_FRAC * max(_session_opening_equity, 1.0))
            // (entry_premium * rec.lot_size)
        )
        max_lots = min(max_lots, _prem_cap_lots)

    # Correlated same-direction heat: budget = frac × equity minus premium
    # already deployed in the SAME direction on OTHER underlyings (armed
    # winners included — gap risk is not zero). Opposite-direction is free.
    if _CORRELATED_HEAT_FRACTION > 0.0 and entry_premium > 0.0 and rec.lot_size > 0:
        _same_dir_prem = sum(
            _p.entry_premium * _p.lot_size * _p.lots
            for _u, _s in _runtime_by_underlying.items()
            if _s is not state
            and (_p := _s.option_trader._position) is not None
            and str(_p.direction) == str(market_ctx.bias)
        )
        if _same_dir_prem > 0.0:
            _corr_budget = (
                _CORRELATED_HEAT_FRACTION * max(_session_opening_equity, 1.0)
                - _same_dir_prem
            )
            max_lots = min(max_lots, max(int(_corr_budget // (entry_premium * rec.lot_size)), 0))

    # ── Per-trade hard MAX-LOSS cap (batch-4 §1a) — bound ex-ante loss, don't trust the stop.
    # Conservative equity basis (min of current and session-opening), matching the sizer.
    _loss_cap_bound = False
    if _PER_TRADE_MAX_LOSS_FRAC > 0.0 and entry_premium > 0.0 and rec.lot_size > 0:
        _loss_cap_equity = max(min(_equity, _session_opening_equity), 1.0)
        _loss_cap_lots = _max_loss_capped_lots(
            equity=_loss_cap_equity, entry_premium=entry_premium, lot_size=rec.lot_size,
            max_loss_frac=_PER_TRADE_MAX_LOSS_FRAC, adverse_frac=_PER_TRADE_ADVERSE_FRAC)
        if _loss_cap_lots is not None and _loss_cap_lots < max_lots:
            _loss_cap_bound = True
            max_lots = _loss_cap_lots

    if max_lots < 1:
        # One lot already exceeds a hard %-rail (premium cap / correlated heat / max-loss cap):
        # REJECT explicitly. Must not fall through to the sizer
        # (maximum_lot_count=0 raises, and the exception fallback below returns
        # a tradable 1-lot result — which would silently defeat the cap).
        return PositionSizingResult(
            underlying_symbol=rec.underlying_symbol,
            timestamp=datetime.now(timezone.utc),
            direction=str(market_ctx.bias),
            recommended_units=0,
            recommended_lots=0,
            notional_exposure=0.0,
            estimated_risk_amount=0.0,
            estimated_round_trip_cost=0.0,
            tradable=False,
            rejection_reason=(
                (f"PER_TRADE_MAX_LOSS_CAP one lot's worst-case loss "
                 f"(₹{entry_premium * rec.lot_size * _PER_TRADE_ADVERSE_FRAC:,.0f}) exceeds "
                 f"{_PER_TRADE_MAX_LOSS_FRAC:.1%} of equity")
                if _loss_cap_bound else
                (f"PREMIUM_RAIL_CAP one lot (₹{entry_premium * rec.lot_size:,.0f}) exceeds "
                 f"the remaining %-rail budget (position cap {_MAX_POSITION_PREMIUM_FRAC:.0%}"
                 f" / correlated same-direction cap {_CORRELATED_HEAT_FRACTION:.0%})")
            ),
            breakdown=PositionSizingBreakdown(
                base_risk_budget=0.0, confidence_scale=1.0,
                volatility_scale=1.0, liquidity_scale=1.0,
                drawdown_scale=1.0, correlation_scale=1.0,
                portfolio_heat_scale=1.0, cash_scale=1.0,
                effective_risk_budget=0.0, per_unit_risk=0.0,
                preliminary_units=0.0, capped_units=0.0,
            ),
            metadata={"premium_cap_frac": _MAX_POSITION_PREMIUM_FRAC},
        )
    stop_premium  = max(entry_premium * _STOP_LOSS_PCT, 0.01)
    drawdown      = max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0))
    heat          = min(_open_premium_cost / max(_equity, 1.0), 1.0)

    # Compute spread_bps from actual bid/ask on the selected contract when
    # available; fall back to a conservative 30 bps for liquid index options.
    # (The previous hardcoded 50 bps exceeded the penalty cap of 40 bps and
    # permanently zeroed the spread component of liquidity_scale.)
    _bid = getattr(rec, "bid_price", None)
    _ask = getattr(rec, "ask_price", None)
    if (
        _bid is not None and _ask is not None
        and _ask > _bid >= 0.0
        and entry_premium > 0.0
    ):
        _spread_bps = (_ask - _bid) / entry_premium * 10_000.0
    else:
        _spread_bps = 30.0  # conservative fallback: liquid Indian index options

    # Use session-opening equity as sizing basis so intraday profits do not
    # inflate the risk budget within the same session.  Losses still reduce
    # available capital correctly since _equity falls below _session_opening_equity.
    _sizing_equity = max(min(_equity, _session_opening_equity), 1.0)
    _open_pos_count = sum(
        1 for _s in _runtime_by_underlying.values()
        if _s.option_trader.has_open_position()
    )
    portfolio = PortfolioRiskSnapshot(
        equity=_sizing_equity,
        available_cash=max(_sizing_equity - _open_premium_cost, 0.0),
        current_portfolio_heat=heat,
        current_drawdown_fraction=drawdown,
        max_allowed_drawdown_fraction=_MAX_ACCOUNT_DD,
        correlation_exposure_fraction=0.0,
        open_position_count=_open_pos_count,
    )

    # Conviction-driven sizing (paper/dry-run): feed the graded conviction into
    # the sizer's confidence_scale instead of the saturated MCB effective_conf,
    # so weak setups size DOWN and strong UP within the existing 1.5× cap.
    _conf_for_sizing = effective_conf
    if _CONV_LIVE:
        _conf_for_sizing = _live_conviction(state, rec)

    try:
        _result = sizer.size_position(
            request=PositionSizingRequest(
                underlying_symbol=rec.underlying_symbol,
                timestamp=datetime.now(timezone.utc),
                direction=str(market_ctx.bias),
                instrument_type="OPTION",
                entry_price=entry_premium,
                stop_price=stop_premium,
                contract_multiplier=1.0,
                lot_size=rec.lot_size,
                minimum_lot_count=1,
                maximum_lot_count=max_lots,
                confidence_score=min(_conf_for_sizing, 1.0),
                actionability_score=min(rec.composite_score, 1.0),
                realized_volatility_ratio=max(state.last_atm_iv / max(_REFERENCE_IV, 0.001), 0.1),
                liquidity_score=min(rec.breakdown.liquidity, 1.0),
                spread_bps=_spread_bps,
                slippage_bps_estimate=10.0,
                expected_correlation_to_book=0.0,
                portfolio=portfolio,
            ),
            valuation_timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:
        return PositionSizingResult(
            underlying_symbol=rec.underlying_symbol,
            timestamp=datetime.now(timezone.utc),
            direction=str(market_ctx.bias),
            recommended_units=rec.lot_size,
            recommended_lots=1,
            notional_exposure=entry_premium * rec.lot_size,
            estimated_risk_amount=entry_premium * rec.lot_size * _STOP_LOSS_PCT,
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

    # ── Min-lot override for small-capital paper accounts ─────────────────────
    # When the position sizer rejects due to INSUFFICIENT_RISK_BUDGET, allow
    # exactly 1 lot if the absolute risk of that single lot is within
    # _PAPER_MAX_SINGLE_TRADE_RISK_FRAC of session-opening equity AND the
    # resulting premium heat fits within the remaining portfolio heat.
    #
    # This override does NOT bypass:
    #   strike quality  |  confluence (OI)  |  context contradiction
    #   portfolio heat  |  daily loss halt  |  warm-start lockout
    #   position limit  |  stop cooldown    |  market session filters
    # Those gates all execute after _size_live_position returns.
    if (
        not _result.tradable
        and _result.rejection_reason == "INSUFFICIENT_RISK_BUDGET"
    ):
        _risk_per_lot  = entry_premium * _MAX_OPTION_PREMIUM_LOSS_FRACTION * rec.lot_size
        _max_risk      = _sizing_equity * _PAPER_MAX_SINGLE_TRADE_RISK_FRAC
        _heat_per_lot  = entry_premium * rec.lot_size / max(_equity, 1.0)
        _remaining_heat = max(0.0, _MAX_HEAT_FRACTION - heat)
        _bd = _result.breakdown
        _tier_fracs    = {0: _SOFT_HALT_RISK_FRAC, 1: _TIER1_RISK_FRAC,
                          2: _TIER2_RISK_FRAC, 3: _TIER3_RISK_FRAC}
        print(
            f"\n[SIZING_BREAKDOWN] {rec.underlying_symbol}"
            f"  contract={getattr(rec, 'option_symbol', '?')}"
            f"  premium=₹{entry_premium:.2f}"
            f"  lot_size={rec.lot_size}"
            f"  min_lots=1"
            f"  risk_per_lot=₹{_risk_per_lot:.0f}"
            f"  premium_cost_per_lot=₹{entry_premium * rec.lot_size:.0f}"
            f"  heat_per_lot={_heat_per_lot:.4f}"
            f"  risk_frac={_tier_fracs.get(tier, _TIER2_RISK_FRAC) * 100:.1f}%"
            f"  max_single_trade={_PAPER_MAX_SINGLE_TRADE_RISK_FRAC * 100:.1f}%"
            f"  base_budget=₹{_bd.base_risk_budget:.0f}"
            f"  eff_budget=₹{_bd.effective_risk_budget:.0f}"
            f"  spread_bps_used={_spread_bps:.1f}"
            f"  vol_scale={_bd.volatility_scale:.3f}"
            f"  liq_scale={_bd.liquidity_scale:.3f}"
            f"  open_positions={_open_pos_count}"
            f"  reason=INSUFFICIENT_RISK_BUDGET"
        )
        if _risk_per_lot <= _max_risk and _heat_per_lot <= _remaining_heat:
            print(
                f"[MIN LOT OVERRIDE] {rec.underlying_symbol}"
                f"  contract={getattr(rec, 'option_symbol', '?')}"
                f"  lots=1"
                f"  reason=MIN_LOT_OVERRIDE_WITHIN_RISK_CAP"
                f"  risk_per_lot=₹{_risk_per_lot:.0f}"
                f"  max_risk_cap=₹{_max_risk:.0f}"
                f"  ({_PAPER_MAX_SINGLE_TRADE_RISK_FRAC * 100:.1f}%)"
                f"  spread_bps_used={_spread_bps:.1f}"
            )
            return PositionSizingResult(
                underlying_symbol=rec.underlying_symbol,
                timestamp=datetime.now(timezone.utc),
                direction=str(market_ctx.bias),
                recommended_units=rec.lot_size,
                recommended_lots=1,
                notional_exposure=entry_premium * rec.lot_size,
                estimated_risk_amount=_risk_per_lot,
                estimated_round_trip_cost=entry_premium * rec.lot_size * (_spread_bps / 10_000.0 + 0.001),
                tradable=True,
                rejection_reason=None,
                breakdown=_result.breakdown,
                metadata={"min_lot_override": "MIN_LOT_OVERRIDE_WITHIN_RISK_CAP",
                          "lot_size": rec.lot_size},
            )

    return _result


def _shadow_counter(underlying: str) -> dict:
    """Return (and lazily initialise) the shadow counter dict for this underlying."""
    if underlying not in _shadow_counters:
        _shadow_counters[underlying] = {
            "attempts":     0,
            "success":      0,
            "no_candidate": 0,
            "failures":     0,
            "last_symbol":  None,
            "last_score":   None,
        }
    return _shadow_counters[underlying]


def _maybe_print_shadow_summary(underlying: str) -> None:
    """Print a one-line [SHADOW SUMMARY] every _SHADOW_SUMMARY_EVERY attempts. Never raises."""
    try:
        c = _shadow_counters.get(underlying)
        if c is None or c["attempts"] == 0:
            return
        if c["attempts"] % _SHADOW_SUMMARY_EVERY != 0:
            return
        last_str = (
            f"  last={c['last_symbol']}  score={c['last_score']:.3f}"
            if c["last_symbol"] else ""
        )
        print(
            f"[SHADOW SUMMARY] {underlying}"
            f"  attempts={c['attempts']}"
            f"  success={c['success']}"
            f"  no_candidate={c['no_candidate']}"
            f"  failures={c['failures']}"
            f"{last_str}"
        )
    except Exception:
        pass


def _classify_shadow_error(exc: Exception) -> str:
    """Map an exception from shadow option intelligence to a failure reason code."""
    msg = str(exc).lower()
    if any(kw in msg for kw in ("token", "forbidden", "access denied", "invalid api", "401", "403")):
        return "KITE_SESSION_INVALID"
    if any(kw in msg for kw in ("bfo", "exchange", "unavailable", "not supported")):
        return "EXCHANGE_UNAVAILABLE"
    if any(kw in msg for kw in ("expiry", "no expiry", "maturity")):
        return "EXPIRY_NOT_FOUND"
    if any(kw in msg for kw in ("empty", "no contracts", "chain")):
        return "OPTION_CHAIN_EMPTY"
    if any(kw in msg for kw in ("metadata", "invalid symbol", "instrument")):
        return "METADATA_ERROR"
    return "UNKNOWN_ERROR"


def _write_shadow_failure_record(
    state: "UnderlyingRuntimeState",
    market_ctx,
    spot: float,
    reason: str,
) -> None:
    """Write a shadow no-candidate record to the shadow audit JSONL. Never raises."""
    try:
        record = {
            "ts":               datetime.now(timezone.utc).isoformat(),
            "mode":             "shadow",
            "feed_symbol":      state.feed_symbol,
            "underlying":       state.option_underlying,
            "exchange":         state.exchange,
            "bias":             str(market_ctx.bias),
            "confidence":       market_ctx.confidence,
            "spot":             spot,
            "selected_symbol":  None,
            "failure_reason":   reason,
        }
        _SHADOW_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _SHADOW_AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"[SHADOW STRIKE] {state.option_underlying} {market_ctx.bias} no_candidate  reason={reason}")
    except Exception:
        pass


def _run_shadow_option_intelligence(
    state: "UnderlyingRuntimeState",
    market_ctx,
    completed_bar: "LiveBar",
) -> None:
    """
    Fetch option chain and run strike selection for a shadow underlying.

    Permitted:  chain fetch, strike scoring, JSONL audit, counter updates.
    Forbidden:  position sizing, open/exit calls, pt_trades DB writes,
                equity/risk state changes, any effect on BANKNIFTY execution.

    Enabling execution for a shadow underlying requires a separate commit that
    moves it from _OPTION_TARGETS_SHADOW into _OPTION_TARGETS_ENABLED.

    Writes to logs/strike_selection_shadow.jsonl.  Never raises.
    """
    c    = _shadow_counter(state.option_underlying)
    c["attempts"] += 1
    spot = completed_bar.close_price
    _outcome = "failure"  # updated in finally block via the local var below
    _outcome_bucket = ["failure"]  # mutable cell so inner scopes can update it
    try:
        bar_date = datetime.now(timezone.utc).date()

        try:
            # Nearest expiry with a DTE floor (best value + runway); no cross-expiry ranking.
            expiry = adapter.select_expiry(
                state.option_underlying, bar_date,
                min_dte=_MIN_EXPIRY_DTE, exchange=state.exchange,
            )
        except Exception:
            _write_shadow_failure_record(state, market_ctx, spot, "EXPIRY_NOT_FOUND")
            return

        _chain_snapshots = []
        try:
            live_chain = adapter.fetch_option_chain(
                underlying=state.option_underlying,
                expiry_date=expiry,
                spot_price=spot,
                lot_size=state.lot_size,
                strike_step=state.strike_step,
                exchange=state.exchange,
            )
            _chain_snapshots.append(live_chain)
        except Exception as exc:
            _write_shadow_failure_record(state, market_ctx, spot, _classify_shadow_error(exc))
            return

        options_ctx = _opt_ctx_builder.build(
            snapshots=_chain_snapshots,
            spot_price=spot,
            lot_size=state.lot_size,
            valuation_timestamp=datetime.now(timezone.utc),
        )

        # Same robust ATM IV feeds BOTH sizing AND strike-selection scoring.
        # Previously selection used the surface options_ctx.atm_iv, which
        # mis-interpolates on the SENSEX/BSE chain (undershoots on high-vol
        # days) → near-ATM strikes scored EXPENSIVE → iv_attractiveness=0.
        _robust_iv = _robust_atm_iv(options_ctx)
        if options_ctx.atm_iv:
            state.last_atm_iv = _robust_iv

        if not options_ctx.strike_candidates:
            _outcome_bucket[0] = "no_candidate"
            _write_shadow_failure_record(state, market_ctx, spot, "CANDIDATES_EMPTY")
            return

        _candidates_list = list(options_ctx.strike_candidates)
        _selection_result = _strike_engine.select(
            bias=market_ctx.bias,
            candidates=_candidates_list,
            valuation_timestamp=datetime.now(timezone.utc),
            oi_snapshot=options_ctx.oi_snapshot,
            gex_snapshot=options_ctx.gex_snapshot,
            atm_iv=_robust_iv,
        )

        if _selection_result.best() is None:
            _outcome_bucket[0] = "no_candidate"
            _write_shadow_failure_record(state, market_ctx, spot, "STRIKE_SELECTION_EMPTY")
            return

        # Terminal [SHADOW STRIKE] line + JSONL record via shadow auditor
        _shadow_auditor.log(
            result=_selection_result,
            candidates=_candidates_list,
            feed_symbol=state.feed_symbol,
            strike_step=state.strike_step,
        )
        _outcome_bucket[0] = "success"
        _best = _selection_result.best()
        c["last_symbol"] = _best.option_symbol
        c["last_score"]  = _best.composite_score

    except Exception as exc:
        try:
            _write_shadow_failure_record(state, market_ctx, spot, _classify_shadow_error(exc))
        except Exception:
            pass
    finally:
        bucket = _outcome_bucket[0]
        if bucket == "success":
            c["success"] += 1
        elif bucket == "no_candidate":
            c["no_candidate"] += 1
        else:
            c["failures"] += 1
        _maybe_print_shadow_summary(state.option_underlying)


def _eod_force_close_all() -> None:
    """
    Force-close every open paper option position at market close.

    For each open position:
      - Requires last_bar_close > 0 AND last_atm_iv > 0.
      - Calls evaluate_and_close(force_reason="EOD_FORCE_CLOSE") with the
        most recent bar close as current_spot and last_atm_iv as current_iv.
        The BSM inside evaluate_and_close computes a fair option price.
      - If either price source is zero (no bar ever seen for this underlying),
        logs EOD_CLOSE_PRICE_UNAVAILABLE and leaves the position open.
      - On success: updates _equity, _daily_pnl, _open_premium_cost,
        _consecutive_losses; writes DB exit row; writes CSV row; saves state.

    Never uses the underlying spot directly as option exit premium.
    Never inserts a fabricated price when the BSM inputs are unavailable.
    """
    global _equity, _peak_equity, _open_premium_cost, _daily_pnl, _consecutive_losses

    for key, und_state in _runtime_by_underlying.items():
        if not und_state.option_trader.has_open_position():
            continue

        pos = und_state.option_trader._position
        spot = und_state.last_bar_close
        iv   = und_state.last_atm_iv

        if spot <= 0 or iv <= 0:
            print(
                f"[EOD CLOSE WARN] {key}: price unavailable "
                f"(spot={spot:.2f}  iv={iv:.4f}) — "
                f"position {pos.contract_symbol} carried, not closed. "
                f"Reason: EOD_CLOSE_PRICE_UNAVAILABLE"
            )
            continue

        # Mark to the REAL option LTP at EOD close; BSM only if no live quote.
        _eod_live_ltp = _held_option_ltp(und_state)
        _closed = und_state.option_trader.evaluate_and_close(
            current_spot=spot,
            current_iv=iv,
            current_date=datetime.now(timezone.utc).date(),
            bar_index=_bar_count,
            stop_loss_pct=_STOP_LOSS_PCT,
            take_profit_pct=_TAKE_PROFIT_PCT,
            min_dte=_MIN_DTE_TO_HOLD,
            force_reason="EOD_FORCE_CLOSE",
            current_premium_override=_eod_live_ltp,
        )

        if _closed is None:
            # Should not happen since force_reason is set, but guard defensively
            print(f"[EOD CLOSE WARN] {key}: evaluate_and_close returned None unexpectedly")
            continue

        # ── Route the EOD exit + confirm the fill before finalizing ──
        # paper/real_dry_run: synthetic FILLED at the BSM exit premium (unchanged).
        # real_armed: place the real SELL and use the ACTUAL fill price; an
        # unconfirmed real EOD exit may leave a position carried overnight →
        # CRITICAL alert (manual close / reconcile).
        _eod_price_source = "BSM_FALLBACK"
        try:
            _eod_exit_result = _exec_router.route_exit(
                underlying    = key,
                tradingsymbol = _closed.contract_symbol,
                lots          = _closed.lots,
                lot_size      = _closed.lot_size,
                exit_premium  = _closed.exit_premium,
                reason        = "EOD_FORCE_CLOSE",
                session_tag   = _session_tag,
                risk_context  = {"pnl": round(_closed.pnl, 2), "equity": _equity},
                db            = _db,
            )
            _eod_exit_fill = _exec_router.confirm_fill(
                _eod_exit_result,
                expected_qty=_closed.lots * _closed.lot_size,
                fallback_premium=_closed.exit_premium,
            )
        except Exception:
            _eod_exit_fill = None

        if (_EXECUTION_MODE == "real_armed"
                and _eod_exit_fill is not None and _eod_exit_fill.is_filled):
            _closed.exit_premium = _eod_exit_fill.average_price
            _closed.pnl = (
                (_closed.exit_premium - _closed.entry_premium)
                * _closed.lot_size * _closed.lots
            )
            # Real fills embed the spread; deduct statutory charges only.
            _closed.pnl, _closed.costs = _apply_txn_costs(
                _closed.pnl, entry_premium=_closed.entry_premium,
                exit_premium=_closed.exit_premium,
                quantity=_closed.lot_size * _closed.lots,
                underlying=_closed.underlying_symbol, spread_frac_per_side=0.0)
            _eod_price_source = "REAL_FILL"
        elif _EXECUTION_MODE == "real_armed":
            _eod_miss = _eod_exit_fill.status if _eod_exit_fill is not None else "ROUTER_ERROR"
            _eod_price_source = f"UNCONFIRMED({_eod_miss})"
            print(
                f"[REAL EOD EXIT NOT CONFIRMED] {key} {_closed.contract_symbol} "
                f"status={_eod_miss} — broker may still hold position; close/reconcile manually"
            )
            try:
                _db.write_runtime_event(
                    "REAL_EOD_EXIT_UNCONFIRMED", severity="CRITICAL",
                    message=f"{_closed.contract_symbol} status={_eod_miss}", equity=_equity,
                )
            except Exception:
                pass

        _equity               += _closed.pnl
        _peak_equity           = max(_peak_equity, _equity)
        _open_premium_cost     = _compute_open_premium_cost()
        _daily_pnl            += _closed.pnl
        # Guards classify on GROSS (market outcome): the cost toll must not turn
        # a breakeven exit into a "loss" for streak/halt purposes — these guards
        # were calibrated pre-cost-model (P&L accounting stays NET).
        if (_closed.pnl + getattr(_closed, "costs", 0.0)) < 0:
            _consecutive_losses += 1
        else:
            _consecutive_losses = 0

        print(
            f"[EOD CLOSE] {key}: {_closed.contract_symbol}  "
            f"exit=₹{_closed.exit_premium:.2f}  "
            f"pnl=₹{_closed.pnl:+,.2f}  lots={_closed.lots}  "
            f"entry=₹{_closed.entry_premium:.2f}  "
            f"EOD_CLOSE_PRICE_SOURCE={_eod_price_source}"
        )

        try:
            _db.write_trade_exit(
                underlying=key,
                exit_premium=_closed.exit_premium,
                pnl=_closed.pnl,
                bars_held=_closed.bars_held,
                exit_reason="EOD_FORCE_CLOSE",
                exit_ts=datetime.now(timezone.utc).isoformat(),
                equity_after=_equity,
            )
        except Exception:
            pass

        if _save_state():
            trade_logger.log_trade(trade=_closed.as_paper_trade())
        else:
            print(
                f"[EOD CLOSE WARN] {key}: state save failed — "
                f"{_closed.contract_symbol} closed in memory but not written to CSV"
            )


def _print_eod_portfolio_summary() -> None:
    """
    Print a compact per-underlying and portfolio-level summary at shutdown.
    """
    print(f"\n{'═'*62}")
    print("  EOD PORTFOLIO SUMMARY")
    print(f"{'═'*62}")
    print(f"  {'Underlying':<12}  {'Opened':>6}  {'Closed':>6}  {'Open':>5}  {'PnL':>12}")
    print(f"  {'─'*12}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*12}")

    total_opened = 0
    total_closed_pnl = 0.0
    for key, und_state in sorted(_runtime_by_underlying.items()):
        # completed_trades includes all this session's closed trades for this underlying
        ct     = und_state.option_trader.completed_trades
        opened = len(ct) + (1 if und_state.option_trader.has_open_position() else 0)
        closed = len(ct)
        open_  = 1 if und_state.option_trader.has_open_position() else 0
        rpnl   = sum(t.pnl for t in ct)
        total_opened     += opened
        total_closed_pnl += rpnl
        print(
            f"  {key:<12}  {opened:>6}  {closed:>6}  {open_:>5}  "
            f"₹{rpnl:>+10,.2f}"
        )

    print(f"  {'─'*52}")
    print(f"  {'TOTAL':<12}  {total_opened:>6}  {'':>6}  {'':>5}  ₹{total_closed_pnl:>+10,.2f}")
    print(f"\n  Equity     : ₹{_equity:>12,.2f}")
    print(f"  Daily PnL  : ₹{_daily_pnl:>+12,.2f}")
    print(f"  Peak Equity: ₹{_peak_equity:>12,.2f}")
    dd = max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0))
    print(f"  Drawdown   :  {dd:>11.2%}")
    print(f"{'═'*62}\n")


def _handle_market_close_shutdown(reason: str) -> None:
    """
    Deterministic market-close shutdown — idempotent, safe to call from any thread.

    Sequence:
      1. Set _shutdown_triggered flag (guards against re-entry).
      2. Force-close all open paper positions (EOD_FORCE_CLOSE).
      3. Save state to disk.
      4. Print EOD portfolio summary.
      5. Write MARKET_CLOSE_SHUTDOWN runtime event to DB.
      6. Disconnect Kite WebSocket.
      7. Return — caller (main loop) calls sys.exit(0).
    """
    global _shutdown_triggered
    # Atomic check-then-set: only the first caller (whichever thread) proceeds to
    # the EOD force-close; any concurrent second caller returns here.
    with _shutdown_lock:
        if _shutdown_triggered:
            return
        _shutdown_triggered = True

    print(f"\n[MARKET CLOSE] {reason} — forcing EOD close and shutting down cleanly.")

    # Force-close all open positions before saving final state
    _eod_force_close_all()

    # Any positions that could not be closed (price unavailable) are still open
    _still_open = [
        s for s in _runtime_by_underlying.values()
        if s.option_trader.has_open_position()
    ]
    if _still_open:
        for _s in _still_open:
            _p = _s.option_trader._position
            print(
                f"[MARKET CLOSE] Position carried (price unavailable): "
                f"{_p.contract_symbol}  entry=₹{_p.entry_premium:.2f}  "
                f"lots={_p.lots}  expiry={_p.expiry_date}"
            )
    else:
        print("[MARKET CLOSE] All positions closed.")

    _save_state()
    _print_eod_portfolio_summary()

    try:
        _pos_syms = [
            s.option_trader._position.contract_symbol
            for s in _still_open
        ]
        _pos_msg = f"carried_positions={_pos_syms}" if _pos_syms else "all_positions_closed"
        _db.write_runtime_event(
            "MARKET_CLOSE_SHUTDOWN",
            severity="INFO",
            message=f"reason={reason}  equity=₹{_equity:,.0f}  daily_pnl=₹{_daily_pnl:+,.0f}  {_pos_msg}",
            equity=_equity,
        )
    except Exception:
        pass

    try:
        adapter.disconnect()
    except Exception:
        pass


def on_ticks(raw_ticks):
    global _equity, _peak_equity, _open_premium_cost, _bar_count
    global _trading_halted, _halt_reason, _daily_pnl, _session_date, _session_opening_equity
    global _soft_halted, _soft_halt_reason, _consecutive_losses
    global _kite_session_valid, _kite_session_error
    # Note: last_atm_iv, bar_history, bars_since_save, fvg_quality, fvg_quality_bar,
    #       captured_market_ctx, captured_strike_rec are now on UnderlyingRuntimeState.

    for raw_tick in raw_ticks:

        # FUTURES ticks (07-14): feed ONLY the order-flow shadow — real volume/CVD/depth —
        # then skip. They must never enter the bar/structure/trade path.
        if _FUT_TOKEN_TO_SYMBOL:
            _ft = (raw_tick.get("instrument_token") if isinstance(raw_tick, dict)
                   else getattr(raw_tick, "instrument_token", None))
            _fsym = _FUT_TOKEN_TO_SYMBOL.get(_ft)
            if _fsym is not None:
                if _orderflow_shadow is not None:
                    _orderflow_shadow.observe(_fsym, raw_tick)
                if _futures_sleeve is not None:
                    _fltp = (raw_tick.get("last_price") if isinstance(raw_tick, dict)
                             else getattr(raw_tick, "last_price", None))
                    if _fltp:
                        _futures_sleeve.on_tick(_fsym, float(_fltp))
                continue

        tick = mapper.map_tick(raw_tick)

        completed_bar = (
            bar_builder.update_tick(
                tick=tick
            )
        )

        if completed_bar is None:
            continue

        # Stop processing the moment a shutdown is triggered — by this thread's
        # own bar-level 15:30 check below OR the main-thread watchdog clock. This
        # WS-thread callback mutates account state (equity / positions / state
        # file); the main thread's EOD force-close mutates the same state, so we
        # must not keep processing bars concurrently with (or after) shutdown.
        if _shutdown_triggered:
            return

        # Market-close guard — stop processing bars after 15:30 IST
        _bar_ist = completed_bar.start_timestamp.astimezone(_IST)
        if _bar_ist.hour * 60 + _bar_ist.minute >= _SESSION_CLOSE_TOD and not _shutdown_triggered:
            _handle_market_close_shutdown("bar-level 15:30 IST detection")
            return   # main loop will see _shutdown_triggered and call sys.exit(0)

        _bar_count += 1

        # Periodic position reconciliation (real_armed only): broker vs local.
        if _RECONCILE_EVERY_BARS > 0 and _bar_count % _RECONCILE_EVERY_BARS == 0:
            _do_position_reconcile("periodic")

        # Reset per-bar captures for all enabled targets
        for _st in _runtime_by_underlying.values():
            if is_option_target_enabled(_st.option_underlying):
                _st.captured_strike_rec    = None
                _st.captured_market_ctx    = None
                _st.captured_score_margin  = None
                _st.captured_gex_snapshot  = None
                _st.captured_tiebreak_pass = False   # never leak a waiver across bars

        # Resolve state for this bar's feed symbol (None = unknown feed)
        _current_state = _state_by_feed.get(completed_bar.symbol)

        # Order-flow shadow (§4.1): flush the FUTURES accumulator for this feed's index on the
        # spot bar close (keeps flow bars minute-aligned with the structure bars).
        if _orderflow_shadow is not None:
            _fut_sym = _FEED_TO_FUT_SYMBOL.get(completed_bar.symbol)
            if _fut_sym:
                _orderflow_shadow.on_bar_close(_fut_sym, completed_bar.start_timestamp)

        # Phase-1 SHADOW: log the volatility regime for this bar (no trade effect).
        if _regime_shadow is not None:
            _regime_shadow.observe(
                underlying=(_current_state.option_underlying if _current_state else completed_bar.symbol),
                timestamp=completed_bar.start_timestamp,
                high=completed_bar.high_price,
                low=completed_bar.low_price,
                close=completed_bar.close_price,
            )
            # Stash latest regime on state for the entry-gate REGIME_DESIZE_MULT.
            if _current_state is not None:
                try:
                    _current_state.last_dir_regime = _regime_shadow.last_regime.get(
                        _current_state.option_underlying, "UNKNOWN"
                    )
                except Exception:
                    pass

        # Phase-1 SHADOW: score the current OR break attempt for trap-ness
        # (direction inferred from the actual break — symmetric, no fixed side).
        if _trap_shadow is not None and _current_state is not None:
            _or_now = (_current_state.or_engine.current_range
                       if _current_state.or_engine.ready else None)
            _trap_shadow.observe(
                underlying=_current_state.option_underlying,
                timestamp=completed_bar.start_timestamp,
                open_=getattr(completed_bar, "open_price", completed_bar.close_price),
                high=completed_bar.high_price,
                low=completed_bar.low_price,
                close=completed_bar.close_price,
                volume=float(getattr(completed_bar, "volume", 0.0) or 0.0),
                or_low=(_or_now.or_low if _or_now else None),
                or_high=(_or_now.or_high if _or_now else None),
            )

        if _TERMINAL_COMPACT:
            print(
                f"[BAR] {completed_bar.symbol} "
                f"{completed_bar.close_price:.2f}"
            )
        else:
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

            if not _TERMINAL_COMPACT:
                print(
                    f"\n[MTF] "
                    f"{completed_bar.symbol} "
                    f"{timeframe}"
                )
                print(mtf_bar)

        # ── Options context update ────────────────────────────────────────────────
        # Inert context (MCB / OR / PDL / FVG / bar_history) runs for ALL prepared
        # underlyings so each builds price history before execution is enabled.
        # Execution-only paths (chain fetch, strike selection, entry/exit) are
        # gated on is_option_target_enabled() inside this block.
        if _current_state is not None:
            # Track latest close price (EOD BSM) and live bar count (warm-start gate)
            _current_state.last_bar_close = completed_bar.close_price
            _current_state.live_bars_seen += 1
            # Session classification — shared stateless engine, safe to call per-bar
            _session_ctx = _session_engine.classify(completed_bar.start_timestamp)
            _current_state.or_engine.update_bar(completed_bar)
            _daily_bar = _current_state.daily_acc.update(completed_bar)
            if _daily_bar is not None:
                _current_state.pdl_engine.update_daily_bar(_daily_bar)
                _current_state.market_ctx_builder.update(bar=_daily_bar, timeframe="1d")
                _h1d = _current_state.bar_history.setdefault("1d", [])
                _h1d.append(_bar_to_dict(_daily_bar))
                if len(_h1d) > _MCB_HISTORY_BARS:
                    del _h1d[:-_MCB_HISTORY_BARS]

            _current_state.market_ctx_builder.update(bar=completed_bar, timeframe="1m")
            _h1m = _current_state.bar_history.setdefault("1m", [])
            _h1m.append(_bar_to_dict(completed_bar))
            if len(_h1m) > _MCB_HISTORY_BARS:
                del _h1m[:-_MCB_HISTORY_BARS]

            for tf_key, tf_bar in mtf_bars.items():
                tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
                _current_state.market_ctx_builder.update(bar=tf_bar, timeframe=tf_name)
                _htf = _current_state.bar_history.setdefault(tf_name, [])
                _htf.append(_bar_to_dict(tf_bar))
                if len(_htf) > _MCB_HISTORY_BARS:
                    del _htf[:-_MCB_HISTORY_BARS]
                # HTF FVG confluence — per-underlying engines
                if tf_key == "MIN_15":
                    _current_state.fvg_15m.process_bar(bar=structure_adapter.convert_bar(live_bar=tf_bar))
                elif tf_key == "HOUR_1":
                    _current_state.fvg_1h.process_bar(bar=structure_adapter.convert_bar(live_bar=tf_bar))

            # ── Periodic state save — triggered by any enabled-target bar, writes all underlyings ──
            # _save_state() includes bar_history_by_underlying for all prepared targets.
            if is_option_target_enabled(_current_state.option_underlying):
                _current_state.bars_since_save += 1
                if _current_state.bars_since_save >= _MCB_SAVE_INTERVAL:
                    _current_state.bars_since_save = 0
                    _save_state()

            market_ctx = _current_state.market_ctx_builder.build(
                valuation_timestamp=datetime.now(timezone.utc)
            )

            # Phase-1 SHADOW note: HTF-bias alignment is recorded AFTER the full HTF
            # market_context is built below. This MCB `market_ctx` lacks per-timeframe
            # biases (daily/h4/h1/m15/is_aligned), so reading it here logged nulls.
            # See the _htf_shadow.observe() call just after the [DIAG HTF] block.

            # ── Chain fetch + strike selection — enabled targets (full execution) ──
            if is_option_target_enabled(_current_state.option_underlying):
                # ── BIAS INVALIDATION (2026-07-15): evidence-weighted override,
                # UPSTREAM of the print/capture/strike-selection so every
                # downstream consumer sees the effective bias. Fail-open.
                if _bias_inval.enabled:
                    try:
                        _bi_raw = str(market_ctx.bias)
                        _bi_opp_side = "bearish" if _bi_raw == "LONG" else "bullish"
                        _bi_opp = _bi_same = 0
                        for _bev in (_LAST_STRUCT_EVENTS.get(completed_bar.symbol) or []):
                            _bet = (_bev.event_type.value
                                    if hasattr(_bev.event_type, "value")
                                    else str(_bev.event_type))
                            if _bet not in ("BOS", "CHOCH"):
                                continue
                            _bes = (_bev.side.value
                                    if hasattr(_bev.side, "value")
                                    else str(_bev.side)).lower()
                            if _bes == _bi_opp_side:
                                _bi_opp += 1
                            elif _bes in ("bullish", "bearish"):
                                _bi_same += 1
                        try:
                            from platform_core.data.live.crossasset_shadow import (
                                get_fresh_flow as _bi_gff,
                            )
                            _bi_flow = _bi_gff(_current_state.option_underlying)
                        except Exception:
                            _bi_flow = None
                        _bi_range = _current_state.or_engine.current_range
                        _bi_eff, _bi_cap, _bi_info = _bias_inval.evaluate(
                            underlying=_current_state.option_underlying,
                            day=datetime.now(_IST).date().isoformat(),
                            bar_index=_bar_count,
                            raw_bias=_bi_raw,
                            close=completed_bar.close_price,
                            bar_open=completed_bar.open_price,
                            or_high=(_bi_range.or_high if _bi_range else None),
                            or_low=(_bi_range.or_low if _bi_range else None),
                            or_ready=bool(_current_state.or_engine.ready),
                            opposite_breaks=_bi_opp,
                            same_breaks=_bi_same,
                            flow=_bi_flow,
                        )
                        if "transition" in _bi_info:
                            _bi_tr = _bi_info["transition"]
                            print(f"[BIAS INVALIDATION] "
                                  f"{_current_state.option_underlying}: "
                                  f"→ {_bi_tr['to']}  ({_bi_tr['reason']})")
                        if _bi_info.get("stage") != "CONFIRM" or "transition" in _bi_info:
                            _bias_inval_log({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "underlying": _current_state.option_underlying,
                                "bar": _bar_count, **_bi_info,
                            })
                        if _bi_eff != _bi_raw:
                            from dataclasses import replace as _dc_replace
                            _bi_conf = (market_ctx.confidence if _bi_cap is None
                                        else min(market_ctx.confidence, _bi_cap))
                            market_ctx = _dc_replace(
                                market_ctx,
                                bias=MarketBias(_bi_eff),
                                confidence=_bi_conf,
                            )
                    except Exception as _bi_exc:
                        print(f"[BIAS INVALIDATION] error (fail-open): {_bi_exc}")

                # trend_score is the raw fused signal; conf saturates at 1.0 for
                # any |trend| >= 0.18, so conf alone cannot show bias decay.
                print(
                    f"\n[OPTIONS] bias={market_ctx.bias}  "
                    f"conf={market_ctx.confidence:.2f}  "
                    f"trend={market_ctx.trend_score:+.3f}"
                )
                _current_state.captured_market_ctx = market_ctx

                if str(market_ctx.bias) != "NEUTRAL" and _kite_session_valid:
                    try:
                        spot = completed_bar.close_price
                        bar_date = datetime.now(timezone.utc).date()
                        # Nearest expiry with a DTE floor (best value + enough runway);
                        # rolls forward only near expiry. No cross-expiry score ranking.
                        expiry = adapter.select_expiry(
                            _current_state.option_underlying, bar_date,
                            min_dte=_MIN_EXPIRY_DTE, exchange=_current_state.exchange,
                        )

                        live_chain = adapter.fetch_option_chain(
                            underlying=_current_state.option_underlying,
                            expiry_date=expiry,
                            spot_price=spot,
                            lot_size=_current_state.lot_size,
                            strike_step=_current_state.strike_step,
                            exchange=_current_state.exchange,
                        )
                        # Approach-A recorder: persist the REAL chain for replay
                        # (no-op unless RECORD_OPTION_CHAIN=1; never raises). Keyed
                        # to the bar's minute so it aligns with the replay's index.
                        try:
                            _record_chain(
                                live_chain,
                                underlying=_current_state.option_underlying,
                                expiry=expiry,
                                bar_ts=completed_bar.start_timestamp,
                            )
                        except Exception:
                            pass
                        _chain_snapshots = [live_chain]
    
                        options_ctx = _opt_ctx_builder.build(
                            snapshots=_chain_snapshots,
                            spot_price=spot,
                            lot_size=_current_state.lot_size,
                            valuation_timestamp=datetime.now(timezone.utc),
                        )
    
                        # Robust ATM IV (near-ATM strike's own raw IV) feeds BOTH
                        # sizing and strike-selection scoring — see companion fix
                        # in the shadow path. Surface atm_iv undershoots on the
                        # SENSEX/BSE chain and spuriously zeroed iv_attractiveness.
                        _robust_iv = _robust_atm_iv(options_ctx)
                        if options_ctx.atm_iv:
                            _current_state.last_atm_iv = _robust_iv

                        # Phase-3 SHADOW: log the 4 options-analytics engine
                        # summaries (GEX/OI/Dealer/Premium) already computed inside
                        # options_ctx. Logs-only, wrapped — never disturbs trades.
                        if _opt_analytics_shadow is not None:
                            try:
                                _opt_analytics_shadow.observe(
                                    underlying=_current_state.option_underlying,
                                    timestamp=completed_bar.start_timestamp,
                                    spot=spot,
                                    exec_bias=getattr(market_ctx, "bias", None),
                                    options_ctx=options_ctx,
                                )
                            except Exception:
                                pass

                        # Phase-3 SHADOW: ContinuationClassifier (logs-only).
                        if _continuation_shadow is not None:
                            try:
                                _or_rng = (_current_state.or_engine.current_range
                                           if _current_state.or_engine.ready else None)
                                _continuation_shadow.observe(
                                    underlying=_current_state.option_underlying,
                                    timestamp=completed_bar.start_timestamp,
                                    exec_bias=getattr(market_ctx, "bias", None),
                                    bars=_current_state.bar_history.get("1m"),
                                    or_high=(_or_rng.or_high if _or_rng else None),
                                    or_low=(_or_rng.or_low if _or_rng else None),
                                    dealer=getattr(options_ctx, "dealer_hedging_snapshot", None),
                                    premium=getattr(options_ctx, "premium_behavior_snapshot", None),
                                    oi=getattr(options_ctx, "oi_snapshot", None),
                                    atm_iv=_robust_iv,
                                )
                            except Exception:
                                pass

                        # Phase-3 SHADOW: ReversalClassifier (logs-only).
                        if _reversal_shadow is not None:
                            try:
                                _or_rng2 = (_current_state.or_engine.current_range
                                            if _current_state.or_engine.ready else None)
                                _reversal_shadow.observe(
                                    underlying=_current_state.option_underlying,
                                    timestamp=completed_bar.start_timestamp,
                                    exec_bias=getattr(market_ctx, "bias", None),
                                    bars=_current_state.bar_history.get("1m"),
                                    or_high=(_or_rng2.or_high if _or_rng2 else None),
                                    or_low=(_or_rng2.or_low if _or_rng2 else None),
                                    dealer=getattr(options_ctx, "dealer_hedging_snapshot", None),
                                    premium=getattr(options_ctx, "premium_behavior_snapshot", None),
                                    oi=getattr(options_ctx, "oi_snapshot", None),
                                    atm_iv=_robust_iv,
                                )
                            except Exception:
                                pass

                        if options_ctx.strike_candidates:
                            _candidates_list = list(options_ctx.strike_candidates)
                            _selection_result = _strike_engine.select(
                                bias=market_ctx.bias,
                                candidates=_candidates_list,
                                valuation_timestamp=datetime.now(timezone.utc),
                                oi_snapshot=options_ctx.oi_snapshot,
                                gex_snapshot=options_ctx.gex_snapshot,
                                atm_iv=_robust_iv,
                            )
                            best = _selection_result.best()
                            if best is not None:
                                _current_state.captured_strike_rec = best
                                _current_state.captured_gex_snapshot = options_ctx.gex_snapshot
                                _recs = _selection_result.recommendations
                                if len(_recs) >= 2:
                                    _current_state.captured_score_margin = (
                                        _recs[0].breakdown.weighted_raw
                                        - _recs[1].breakdown.weighted_raw
                                        if _MARGIN_PRE_DTE else
                                        _recs[0].composite_score
                                        - _recs[1].composite_score
                                    )
                                else:
                                    _current_state.captured_score_margin = None
                                # ── [P1] delta tie-break (see knob decl). Only
                                # fires when the margin would have BLOCKED, and
                                # only at symbols whose floor >= MIN_FLOOR — the
                                # guard that keeps delta away from SENSEX.
                                _current_state.captured_tiebreak_pass = False
                                _tb_floor = _GATE_SCORE_MARGIN_BY_SYM.get(
                                    _current_state.option_underlying,
                                    _MIN_SCORE_MARGIN_TIER23,
                                )
                                _tb_margin = _current_state.captured_score_margin
                                _tb_pick, _tb_fired = _delta_tiebreak_pick(
                                    _recs, _tb_floor, _STRIKE_TIEBREAK,
                                    _STRIKE_TIEBREAK_MIN_FLOOR, _tb_margin,
                                    underlying=_current_state.option_underlying,
                                )
                                if _tb_fired:
                                    _changed = _tb_pick.option_symbol != best.option_symbol
                                    best = _tb_pick
                                    _current_state.captured_strike_rec = best
                                    _current_state.captured_tiebreak_pass = True
                                    print(
                                        f"[TIEBREAK] {_current_state.option_underlying}"
                                        f" margin={_tb_margin:.4f}<{_tb_floor} "
                                        f"→ delta pick {best.option_symbol} "
                                        f"(δ={best.delta:.3f}, "
                                        f"{'RE-RANKED' if _changed else 'unchanged'}) "
                                        f"— margin gate allowed"
                                    )
                                print(
                                    f"[STRIKE] {best.option_symbol}  "
                                    f"δ={best.delta:.2f}  "
                                    f"score={best.composite_score:.2f}  "
                                    f"iv={best.implied_volatility:.1%}"
                                )
                                # Observability only — never raises, never affects behavior
                                _audit_selection(
                                    result=_selection_result,
                                    candidates=_candidates_list,
                                    feed_symbol=completed_bar.symbol,
                                    strike_step=_current_state.strike_step,
                                )
                    except Exception as exc:
                        _err_str = str(exc)
                        _is_auth = any(
                            kw in _err_str.lower()
                            for kw in ("token", "forbidden", "access denied", "invalid api", "401", "403")
                        )
                        if _is_auth and _kite_session_valid:
                            _kite_session_valid = False
                            _kite_session_error = _err_str
                            print(
                                f"\n{'='*60}\n"
                                f"[KITE SESSION EXPIRED] Token rejected by Kite API.\n"
                                f"  Details: {_err_str}\n"
                                f"  Action: run `python scripts/generate_kite_session.py` and restart.\n"
                                f"  Options pipeline DISABLED — structure analysis continues.\n"
                                f"{'='*60}"
                            )
                        elif _kite_session_valid:
                            print(f"[OPTIONS] Pipeline error: {exc}")

            # ── Shadow option intelligence — observe chain/strike, no execution ──
            # Runs for NIFTY/SENSEX only: chain fetch, strike scoring, JSONL audit,
            # and per-underlying attempt/success/no_candidate/failure counters.
            #
            # SAFETY BOUNDARY — this elif branch may NEVER:
            #   • call _size_live_position() or any sizing helper
            #   • call option_trader.open_position() / close_position()
            #   • call _db.write_trade_entry() / write_trade_exit()
            #   • modify _equity, _peak_equity, _consecutive_losses, or _soft_halted
            #
            # To enable execution for a shadow underlying, move it from
            # _OPTION_TARGETS_SHADOW to _OPTION_TARGETS_ENABLED in a separate commit.
            elif (is_option_target_shadow(_current_state.option_underlying)
                    and str(market_ctx.bias) != "NEUTRAL"
                    and _kite_session_valid
                    and _shadow_last_bar.get(_current_state.option_underlying, -1) != _bar_count):
                _shadow_last_bar[_current_state.option_underlying] = _bar_count
                _run_shadow_option_intelligence(
                    state=_current_state,
                    market_ctx=market_ctx,
                    completed_bar=completed_bar,
                )
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
                "\n[DIAG HTF — not used for option execution]\n"
            )

            print(market_context)

        # Stash HTF alignment + OPPOSITION on state for the entry gates
        # (independent of whether the shadow logger is enabled).
        if _current_state is not None:
            try:
                _current_state.last_is_aligned = bool(getattr(market_context, "is_aligned", True))
                # Opposition (07-14 redesign): count only ACTIVE disagreement across the
                # real intraday TFs (daily is hardcoded NEUTRAL upstream, so exclude it).
                _tf_b = [str(getattr(market_context, k, "NEUTRAL"))
                         for k in ("h4_bias", "h1_bias", "m15_bias")]
                _current_state.last_htf_opposed_long  = sum(b == "BEARISH" for b in _tf_b) >= 2
                _current_state.last_htf_opposed_short = sum(b == "BULLISH" for b in _tf_b) >= 2
            except Exception:
                pass

        # Phase-1 SHADOW: record HTF-bias alignment now that the FULL HTF context
        # (daily/h4/h1/m15/is_aligned/confidence_score) exists. exec_bias/exec_conf
        # come from the MCB execution context; the per-timeframe biases from
        # market_context. Guarded on _current_state so market_ctx is always defined.
        if _htf_shadow is not None and _current_state is not None:
            try:
                _htf_shadow.observe(
                    underlying=_current_state.option_underlying,
                    timestamp=completed_bar.start_timestamp,
                    exec_bias=str(getattr(market_ctx, "bias", "")),
                    exec_conf=getattr(market_ctx, "confidence", None),
                    daily_bias=getattr(market_context, "daily_bias", None),
                    h4_bias=getattr(market_context, "h4_bias", None),
                    h1_bias=getattr(market_context, "h1_bias", None),
                    m15_bias=getattr(market_context, "m15_bias", None),
                    is_aligned=getattr(market_context, "is_aligned", None),
                    htf_confidence=getattr(market_context, "confidence_score", None),
                )
            except Exception:
                # SHADOW path must NEVER disturb the live trade handler.
                pass

        

        # ── Feed-gap structure continuity guard ──────────────────────────────
        # A discontinuity in the 1-minute bar stream (host suspend, feed stall,
        # reconnect) must NOT be analysed across: swings / BOS / CHOCH / FVG
        # computed over a multi-hour hole are spurious. If the gap since this
        # symbol's last bar exceeds the threshold, reset its structure + swing
        # state so the post-gap bar starts a fresh, clean structure context.
        _sym         = completed_bar.symbol
        _prev_bar_ts = _last_struct_bar_ts.get(_sym)
        _cur_bar_ts  = completed_bar.start_timestamp
        if _prev_bar_ts is not None:
            _gap_min = (_cur_bar_ts - _prev_bar_ts).total_seconds() / 60.0
            if _gap_min > _STRUCTURE_GAP_RESET_MIN:
                ENGINES[_sym].reset()
                SWING_DETECTORS[_sym].reset()
                print(
                    f"\n{'='*60}\n"
                    f"[STRUCTURE GAP RESET] {_sym}: {_gap_min:.0f}-min gap in bar "
                    f"stream (> {_STRUCTURE_GAP_RESET_MIN:.0f}m) — reset structure + "
                    f"swing state so no signal spans the discontinuity.\n"
                    f"  last_bar={_prev_bar_ts:%Y-%m-%d %H:%M} "
                    f"new_bar={_cur_bar_ts:%Y-%m-%d %H:%M} (UTC)\n"
                    f"{'='*60}"
                )
                try:
                    _db.write_runtime_event(
                        "STRUCTURE_GAP_RESET",
                        severity="WARNING",
                        message=(f"{_sym}: {_gap_min:.0f}-min bar-stream gap; "
                                 f"structure+swing state reset"),
                        equity=_equity,
                        bar_index=_bar_count,
                    )
                except Exception:
                    pass
        _last_struct_bar_ts[_sym] = _cur_bar_ts

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
        # Stash for next bar's bias-invalidation read (see _LAST_STRUCT_EVENTS).
        _LAST_STRUCT_EVENTS[completed_bar.symbol] = list(result.emitted_events or [])

        # ── Persist structure events (fire-and-forget) ───────────────────────
        if result.emitted_events:
            try:
                _db.write_structure_events(
                    result.emitted_events,
                    bar_index=_bar_count,
                    bar_ts=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                pass

        # ── OptionPaperTrader: exit evaluation + entry (enabled targets only) ───
        if (_current_state is not None
                and is_option_target_enabled(_current_state.option_underlying)
                and _current_state.captured_market_ctx is not None):
            _bar_date_live = datetime.now(timezone.utc).date()

            # Exit evaluation runs on every bar while a position is open
            if _current_state.option_trader.has_open_position():
                _exit_sig = _option_exit_signal(result.emitted_events, _current_state.option_trader)
                # THESIS_INVALIDATED (2026-07-15, roadmap #4): exit on thesis
                # death, not only on structure/%-levels. When the invalidation
                # engine stands NEUTRALIZED or FLIPPED against this position's
                # direction, its reason to exist is gone — the mining data says
                # our visible %-exits sit where liquidity gets taken; this one
                # is invisible (evidence-level). Fail-open: any error → no
                # forced exit, the normal paths still run.
                if _exit_sig is None and _bias_inval.enabled:
                    try:
                        _ti_pos = getattr(_current_state.option_trader, "_position", None)
                        _ti_dir = str(getattr(_ti_pos, "direction", "") or "").upper()
                        if _ti_dir and _bias_inval.position_direction_invalidated(
                                _current_state.option_underlying, _ti_dir):
                            _exit_sig = "THESIS_INVALIDATED"
                            print(f"[BIAS INVALIDATION] "
                                  f"{_current_state.option_underlying}: forcing exit "
                                  f"— thesis behind {_ti_dir} position is dead")
                    except Exception:
                        pass
                # Mark to the REAL option LTP (live paper/dry-run); BSM only if the
                # quote is unavailable. Fixes fictional model-priced exits.
                _live_ltp = _held_option_ltp(_current_state)
                _closed = _current_state.option_trader.evaluate_and_close(
                    current_spot=completed_bar.close_price,
                    current_iv=_current_state.last_atm_iv,
                    current_date=_bar_date_live,
                    bar_index=_bar_count,
                    stop_loss_pct=_STOP_LOSS_PCT,
                    take_profit_pct=_TAKE_PROFIT_PCT,
                    min_dte=_MIN_DTE_TO_HOLD,
                    force_reason=_exit_sig,
                    bar_low_spot=completed_bar.low_price,
                    bar_high_spot=completed_bar.high_price,
                    current_premium_override=_live_ltp,
                )
                if _closed is not None:
                    # ── Route the exit + confirm the fill BEFORE finalizing ──
                    # paper/real_dry_run: confirm_fill synthesizes a FILLED at the BSM
                    # exit premium, so accounting is unchanged.  real_armed: place the
                    # real SELL order and confirm the ACTUAL exit price.  An unconfirmed
                    # real exit means the broker may STILL hold the position → HALT
                    # (fail-closed; reconciliation + manual intervention resolve).
                    try:
                        _exit_result = _exec_router.route_exit(
                            underlying    = _current_state.option_underlying,
                            tradingsymbol = _closed.contract_symbol,
                            lots          = _closed.lots,
                            lot_size      = _closed.lot_size,
                            exit_premium  = _closed.exit_premium,
                            reason        = str(_closed.exit_reason),
                            session_tag   = _session_tag,
                            risk_context  = {"pnl": round(_closed.pnl, 2), "equity": _equity},
                            db            = _db,
                        )
                        _exit_fill = _exec_router.confirm_fill(
                            _exit_result,
                            expected_qty=_closed.lots * _closed.lot_size,
                            fallback_premium=_closed.exit_premium,
                        )
                    except Exception:
                        _exit_fill = None

                    # Final exit price/pnl: actual fill in real_armed, else BSM estimate.
                    if (_EXECUTION_MODE == "real_armed"
                            and _exit_fill is not None and _exit_fill.is_filled):
                        _final_exit_premium = _exit_fill.average_price
                        _final_pnl = (
                            (_final_exit_premium - _closed.entry_premium)
                            * _closed.lot_size * _closed.lots
                        )
                        # Real fills embed the spread; statutory charges only.
                        _final_pnl, _closed.costs = _apply_txn_costs(
                            _final_pnl, entry_premium=_closed.entry_premium,
                            exit_premium=_final_exit_premium,
                            quantity=_closed.lot_size * _closed.lots,
                            underlying=_closed.underlying_symbol,
                            spread_frac_per_side=0.0)
                        print(
                            f"[REAL FILL] EXIT {_closed.contract_symbol} filled "
                            f"@₹{_final_exit_premium:.2f} qty={_exit_fill.filled_quantity} "
                            f"actual_pnl=₹{_final_pnl:,.0f} (est ₹{_closed.pnl:,.0f})"
                        )
                    else:
                        _final_exit_premium = _closed.exit_premium
                        _final_pnl = _closed.pnl

                    # Keep _closed consistent with the executed fill so the JSON trade
                    # log (as_paper_trade) matches pt_trades and the accounting below.
                    _closed.exit_premium = _final_exit_premium
                    _closed.pnl          = _final_pnl

                    # real_armed exit that did not confirm — HALT (broker may still hold).
                    if (_EXECUTION_MODE == "real_armed"
                            and (_exit_fill is None or not _exit_fill.is_filled)):
                        _miss = _exit_fill.status if _exit_fill is not None else "ROUTER_ERROR"
                        _trading_halted = True
                        _halt_reason = (
                            f"REAL_EXIT_UNCONFIRMED {_closed.contract_symbol} status={_miss}"
                        )
                        print_warning_block(
                            "REAL EXIT NOT CONFIRMED — TRADING HALTED",
                            bar_index=_bar_count,
                            underlying=_current_state.option_underlying,
                            contract=_closed.contract_symbol,
                            reason=_halt_reason + " (reconcile broker position manually)",
                        )
                        try:
                            _db.write_runtime_event(
                                "REAL_EXIT_UNCONFIRMED", severity="CRITICAL",
                                message=_halt_reason, equity=_equity,
                            )
                        except Exception:
                            pass

                    # ── Finalize accounting at the final exit price ──
                    _equity += _final_pnl
                    _peak_equity = max(_peak_equity, _equity)
                    _open_premium_cost = _compute_open_premium_cost()
                    _daily_pnl += _final_pnl
                    # Guards classify on GROSS — see EOD-close site (breakeven
                    # exits must not feed loss streaks because of the toll).
                    if (_final_pnl + getattr(_closed, "costs", 0.0)) < 0:
                        _consecutive_losses += 1
                    else:
                        _consecutive_losses = 0
                    # Re-entry cooldown state (keyed on exit_reason, not pnl)
                    if "STOP_LOSS" in str(_closed.exit_reason):
                        _current_state.last_stop_bar      = _bar_count
                        _current_state.last_stop_contract = _closed.contract_symbol
                    # Structure-exit cooldown state (chop filter): BOS/CHOCH exits
                    # previously bypassed the cooldown, causing same-strike churn.
                    if "STRUCTURE_EXIT" in str(_closed.exit_reason):
                        _current_state.last_struct_exit_bar      = _bar_count
                        _current_state.last_struct_exit_contract = _closed.contract_symbol
                    # Same-contract session loss limit: count losing exits per
                    # (contract, direction); at the limit that pair is blocked
                    # for the rest of the session (same-strike churn killer).
                    _current_state.contract_loss_tracker.record_exit(
                        contract=_closed.contract_symbol,
                        direction=str(_closed.direction),
                        # GROSS: a toll-only negative is not a losing exit.
                        pnl=_final_pnl + getattr(_closed, "costs", 0.0),
                    )
                    if _current_state.contract_loss_tracker.blocked(
                        contract=_closed.contract_symbol,
                        direction=str(_closed.direction),
                    ):
                        print(
                            f"[SAME-CONTRACT SESSION BLOCK] {_closed.contract_symbol} "
                            f"{_closed.direction}: "
                            f"{_current_state.contract_loss_tracker.losses(contract=_closed.contract_symbol, direction=str(_closed.direction))} "
                            f"losing exits — blocked for the rest of the session"
                        )
                    # Chop re-entry brake: consecutive losing exits lock the
                    # underlying briefly; a win resets immediately (trend continues).
                    # GROSS classification (identical to pre-cost-model behaviour).
                    if (_closed.pnl + getattr(_closed, "costs", 0.0)) > 0:
                        _current_state.reentry_loss_streak = 0
                    else:
                        _current_state.reentry_loss_streak += 1
                        if _current_state.reentry_loss_streak >= _MAX_REENTRY_LOSSES:
                            _current_state.reentry_lockout_until_bar = _bar_count + _REENTRY_LOCKOUT_BARS
                            _current_state.reentry_loss_streak = 0  # fresh start after the lockout
                            print(
                                f"[CHOP RE-ENTRY LOCKOUT] {_current_state.option_underlying}: "
                                f"{_MAX_REENTRY_LOSSES} consecutive losing exits — new entries "
                                f"blocked for {_REENTRY_LOCKOUT_BARS} bars (until bar "
                                f"{_current_state.reentry_lockout_until_bar})"
                            )
                    print_exit_block(
                        underlying=_closed.underlying_symbol,
                        strike=_closed.strike,
                        option_type=_closed.option_type,
                        direction=_closed.direction,
                        lots=_closed.lots,
                        lot_size=_closed.lot_size,
                        entry_premium=_closed.entry_premium,
                        exit_premium=_final_exit_premium,
                        pnl=_final_pnl,
                        bars_held=_closed.bars_held,
                        exit_reason=str(_closed.exit_reason),
                        equity=_equity,
                    )
                    try:
                        _db.write_trade_exit(
                            underlying=_current_state.option_underlying,
                            exit_premium=_final_exit_premium,
                            pnl=_final_pnl,
                            bars_held=_closed.bars_held,
                            exit_reason=str(_closed.exit_reason),
                            exit_ts=datetime.now(timezone.utc).isoformat(),
                            equity_after=_equity,
                        )
                    except Exception:
                        pass
                    if _save_state():
                        trade_logger.log_trade(trade=_closed.as_paper_trade())
                    else:
                        print_warning_block(
                            "STATE SAVE FAILED",
                            reason=(
                                "trade not logged to CSV — "
                                "position closed in memory, will re-evaluate after restart"
                            ),
                        )
                    _all_completed = [
                        t.as_paper_trade()
                        for _s in _runtime_by_underlying.values()
                        for t in _s.option_trader.completed_trades
                    ]
                    _summary = performance_tracker.calculate_summary(trades=_all_completed)
                    print_performance_summary(
                        total_trades=_summary.total_trades,
                        wins=_summary.winning_trades,
                        losses=_summary.losing_trades,
                        total_pnl=_summary.total_pnl,
                        avg_pnl=_summary.average_pnl,
                        equity=_equity,
                        initial_equity=_INITIAL_EQUITY,
                    )

            # Session reset + capital protection checks run every BANKNIFTY bar
            _try_resume_trading(_bar_date_live)

            # ── Hard halt check ──────────────────────────────────────────────
            _should_halt, _halt_msg = _check_capital_limits()
            if _should_halt and not _trading_halted:
                _trading_halted = True
                _halt_reason = _halt_msg
                print_warning_block(
                    "CAPITAL HALT",
                    equity=f"₹{_equity:,.0f}",
                    reason=_halt_reason,
                )
                try:
                    _db.write_runtime_event(
                        "CAPITAL_HALT",
                        severity="CRITICAL",
                        message=_halt_reason,
                        equity=_equity,
                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                        bar_index=_bar_count,
                    )
                except Exception:
                    pass
                _save_state()

            # ── Soft halt check ──────────────────────────────────────────────
            _is_soft, _soft_msg = _check_soft_halt()
            if _is_soft and not _soft_halted and not _trading_halted:
                _soft_halted = True
                _soft_halt_reason = _soft_msg
                print_warning_block(
                    "SOFT HALT",
                    equity=f"₹{_equity:,.0f}",
                    reason=f"{_soft_halt_reason} — defensive-only mode",
                )
                try:
                    _db.write_runtime_event(
                        "SOFT_HALT",
                        severity="WARNING",
                        message=f"{_soft_halt_reason} — defensive-only mode",
                        equity=_equity,
                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                        bar_index=_bar_count,
                    )
                except Exception:
                    pass
                _save_state()

            # ── FVG quality + effective confidence (mirrors BacktestRunner step 8) ─
            _fvg_events = [
                e for e in result.emitted_events
                if e.event_type == StructureEventType.FVG_CREATED
            ]
            _recent_sweeps_live = tuple(
                ENGINES[_current_state.feed_symbol]._liquidity_sweep_detector.recent_sweeps
            )
            _htf_fvgs_live = {
                "MIN_15": _current_state.fvg_15m.active_gaps,
                "HOUR_1": _current_state.fvg_1h.active_gaps,
            }

            _mctx = _current_state.captured_market_ctx
            _effective_conf = (
                _mctx.confidence * _session_ctx.confidence_multiplier
            )

            _pdl_mult, _pdl_reason = 1.0, ""
            if _current_state.pdl_engine.ready and str(_mctx.bias) != "NEUTRAL":
                _pdl_mult, _pdl_reason = _current_state.pdl_engine.levels.confidence_multiplier(
                    spot=completed_bar.close_price,
                    bias=str(_mctx.bias),
                )
            _effective_conf *= _pdl_mult

            _or_mult, _or_reason = 1.0, ""
            if _current_state.or_engine.ready and str(_mctx.bias) != "NEUTRAL":
                _or_mult, _or_reason = _current_state.or_engine.current_range.confidence_multiplier(
                    spot=completed_bar.close_price,
                    bias=str(_mctx.bias),
                )
            elif (not _current_state.or_engine.ready and str(_mctx.bias) != "NEUTRAL"
                    and (datetime.now(_IST).hour * 60 + datetime.now(_IST).minute) >= 9 * 60 + 31):
                # FAIL-CLOSED: past 09:30 IST with no opening range means every OR
                # guard is inactive and the model would trade chop blind (the
                # 2026-07-08 −₹2,893 leak). The startup Kite seed
                # (_seed_opening_ranges_if_missing) normally prevents this; if it
                # ALSO failed, refuse entries rather than trade unprotected.
                _or_mult, _or_reason = 0.0, (
                    "OR_UNAVAILABLE — fail-closed (no opening range past 09:30; "
                    "Kite seed failed — refusing to trade chop-blind)"
                )
            _effective_conf *= _or_mult

            _fvg_quality_for_entry = 1.0
            _fvg_quality_reason = ""
            if _fvg_events and str(_mctx.bias) != "NEUTRAL":
                _rescored = [
                    _fvg_scorer.score_event(
                        event=e,
                        market_bias=str(_mctx.bias),
                        recent_sweeps=_recent_sweeps_live,
                        active_htf_fvgs=_htf_fvgs_live,
                        mitigation_ratio=0.0,
                    )
                    for e in _fvg_events
                ]
                _best_q = max(_rescored, key=lambda s: s.total)
                _current_state.fvg_quality = _best_q.total
                _current_state.fvg_quality_bar = _bar_count
                _fvg_quality_for_entry = _best_q.confidence_multiplier()
                if _best_q.blocks_entry:
                    _fvg_quality_reason = (
                        f"low FVG quality={_best_q.total:.2f} "
                        f"(disp={_best_q.displacement:.2f} "
                        f"sess={_best_q.session:.2f} "
                        f"sweep={_best_q.sweep_origin:.2f} "
                        f"htf={_best_q.htf_alignment:.2f})"
                    )
            elif _bar_count - _current_state.fvg_quality_bar <= FVG_QUALITY_STALENESS_BARS:
                _stored_q = FVGQualityScore(
                    total=_current_state.fvg_quality,
                    displacement=0.0, session=0.0, sweep_origin=0.0,
                    htf_alignment=0.0, freshness=0.0, volume=0.0,
                )
                _fvg_quality_for_entry = _stored_q.confidence_multiplier()
                if _current_state.fvg_quality < FVG_QUALITY_BLOCK_THRESHOLD:
                    _fvg_quality_reason = f"stored low FVG quality={_current_state.fvg_quality:.2f}"
            _effective_conf *= _fvg_quality_for_entry

            # HTF OPPOSITION gate (2026-07-14 redesign — replaces the misalignment block).
            # OLD: block LONG when is_aligned=False. With daily hardcoded NEUTRAL, alignment
            # needed h4+h1+m15 ALL agreeing → a de-facto blanket long ban (blocked the entire
            # +0.8% 07-13 recovery the MCB had RIGHT). NEW: evaluate, don't ban —
            #   block only when the HTF ACTIVELY OPPOSES the direction (≥2 of h4/h1/m15
            #   against). NEUTRAL never blocks. SYMMETRIC (shorts evaluated the same way).
            # ADAPTIVE OVERRIDE: live constituent flow (crossasset LAST_FLOW, <5 min fresh)
            # agreeing with the trade direction overrides an opposed HTF — flow flipped LONG
            # at the 07-13 low while structure lagged; the market's own turn outranks a
            # stale structure read. Lock-free dict read; any failure = no override.
            _align_reason = ""
            _dir = str(_mctx.bias)
            _opposed = (getattr(_current_state, "last_htf_opposed_long", False) if _dir == "LONG"
                        else getattr(_current_state, "last_htf_opposed_short", False) if _dir == "SHORT"
                        else False)
            # Invalidation-flip exemption (2026-07-15): a FLIPPED effective bias
            # already carried sustained contradiction + a structural/flow
            # confirmer against the stale HTF view — the opposition gate and the
            # NIFTY flow-evidence gate encode exactly that stale view, so they
            # must not re-block the flip (they'd neuter it every time).
            _bi_flip = _bias_inval.flip_active(_current_state.option_underlying, _dir)
            if _bi_flip and _opposed:
                print(f"[BIAS INVALIDATION] {_current_state.option_underlying} {_dir}: "
                      f"HTF-opposition gate waived — flip outranks the stale prior")
            if _LONG_ALIGNMENT_GATE and _opposed and not _bi_flip:
                _flow = None
                try:
                    from platform_core.data.live.crossasset_shadow import get_fresh_flow as _gff
                    _flow = _gff(_current_state.option_underlying)
                except Exception:
                    _flow = None
                # agree = level agrees AND a FRESH push (momentum) in the trade direction —
                # the from-open level alone is sticky and made the override quasi-permanent
                # (323 fires on 07-14). Early session (mom None) falls back to level-only.
                _mom = (_flow or {}).get("mom")
                _mom_ok = (_mom is None or (_mom >= 0.02 if _dir == "LONG" else _mom <= -0.02))
                _flow_agrees = (_flow is not None and _flow.get("flow_bias") == _dir
                                and abs(_flow.get("avg_flow_pct") or 0.0) >= 0.10 and _mom_ok)
                if _flow_agrees:
                    if not _override_printed.get((_current_state.option_underlying, _dir)):
                        _override_printed[(_current_state.option_underlying, _dir)] = True
                        print(f"[ADAPTIVE OVERRIDE] {_current_state.option_underlying} {_dir}: "
                              f"HTF opposed but constituent flow {_dir} "
                              f"({_flow['avg_flow_pct']:+.2f}%, mom={_mom}) — fresh turn outranks stale structure")
                else:
                    _override_printed[(_current_state.option_underlying, _dir)] = False
                    _effective_conf *= 0.0
                    _align_reason = (f"HTF_OPPOSED — ≥2 of h4/h1/m15 actively against {_dir}"
                                     f" (flow={'none' if _flow is None else _flow.get('flow_bias')})")

            # NIFTY-long gate (2026-07-14 redesign): the blanket ban (proven −₹21.8k bleed
            # bucket) becomes an EVIDENCE requirement — a NIFTY long now needs live
            # constituent-flow support; with fresh agreeing flow it trades, without it stays
            # blocked (conservative default preserves the measured protection).
            _nifty_reason = ""
            if (_NIFTY_LONG_GATE and _dir == "LONG" and not _bi_flip
                    and _current_state.option_underlying in ("NIFTY", "NIFTY50")):
                _nflow = None
                try:
                    from platform_core.data.live.crossasset_shadow import get_fresh_flow as _gff2
                    _nflow = _gff2("NIFTY")
                except Exception:
                    _nflow = None
                if (_nflow is not None and _nflow.get("flow_bias") == "LONG"
                        and (_nflow.get("avg_flow_pct") or 0.0) >= 0.10):
                    print(f"[ADAPTIVE OVERRIDE] NIFTY LONG: flow support "
                          f"{_nflow['avg_flow_pct']:+.2f}% — evidence requirement met")
                else:
                    _effective_conf *= 0.0
                    _nifty_reason = ("NIFTY_LONG_GATE — needs live flow support "
                                     f"(flow={'none' if _nflow is None else _nflow.get('flow_bias')})")

            # Open-drive window: session-phase (and warm-up) blocks are lifted
            # during the seeded 09:15–09:35 IST window — see _open_drive_pass_now.
            _session_entry_ok = _session_ctx.allow_entry or _open_drive_pass_now()
            if (_pdl_reason or _or_reason or _fvg_quality_reason or _align_reason
                    or _nifty_reason or not _session_entry_ok):
                _filter_parts = []
                if not _session_entry_ok:
                    _filter_parts.append(f"session={_session_ctx.phase}")
                if _pdl_reason:
                    _filter_parts.append(_pdl_reason)
                if _or_reason:
                    _filter_parts.append(_or_reason)
                if _fvg_quality_reason:
                    _filter_parts.append(_fvg_quality_reason)
                if _align_reason:
                    _filter_parts.append(_align_reason)
                if _nifty_reason:
                    _filter_parts.append(_nifty_reason)
                _filter_reason_str = "  |  ".join(_filter_parts)
                print_warning_block(
                    "ENTRY FILTER",
                    bar_index=_bar_count,
                    conf=f"{_mctx.confidence:.2f}→{_effective_conf:.2f}",
                    reason=_filter_reason_str,
                )
                # Audit trail: since the 07-14 gate redesign these filters zero
                # confidence UPSTREAM of every write_decision site, so pt_decisions
                # recorded NOTHING for 3 sessions. Record on state-change per
                # underlying (not per bar) to keep the table readable.
                if _last_filter_reason.get(_current_state.option_underlying) != _filter_reason_str:
                    _last_filter_reason[_current_state.option_underlying] = _filter_reason_str
                    try:
                        _db.write_decision(
                            bar_index=_bar_count,
                            bar_ts=datetime.now(timezone.utc).isoformat(),
                            underlying=_current_state.option_underlying,
                            bias=str(_mctx.bias),
                            conf_raw=_mctx.confidence,
                            conf_eff=_effective_conf,
                            outcome="FILTERED",
                            filter_reason=_filter_reason_str,
                            equity=_equity,
                            drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                        )
                    except Exception:
                        pass

            # FUTURES SLEEVE direction signal (07-14 fix): futures need DIRECTION, not a
            # strike. Hooked UPSTREAM of strike selection so options-only gates
            # (SCORE_MARGIN_LOW / premium rails) cannot veto a futures trade — that veto
            # left the sleeve starved on 07-14 while every directional gate was green.
            # All DIRECTIONAL gates still bind (OR/session/opposition/flow/halts/conf).
            if (_futures_sleeve is not None
                    and str(_mctx.bias) in ("LONG", "SHORT")
                    and not (_pdl_reason or _or_reason or _fvg_quality_reason
                             or _align_reason or _nifty_reason)
                    and _session_entry_ok and not _trading_halted and not _soft_halted
                    and _effective_conf >= 0.55):
                _futures_sleeve.on_direction_signal(
                    _current_state.option_underlying, str(_mctx.bias))

            # ── Entry gate: fresh strike, no open position for THIS underlying, not hard-halted ─
            _srec = _current_state.captured_strike_rec
            if (
                _srec is not None
                and not _current_state.option_trader.has_open_position()
                and not _trading_halted
                and _session_entry_ok
                and not _fvg_quality_reason
            ):
                # Soft halt overrides tier selection: forces tier 0 (sizer_soft)
                if _soft_halted:
                    _effective_tier    = 0
                    _effective_min_conf = _SOFT_HALT_MIN_CONF
                else:
                    _effective_tier     = _determine_risk_tier(_mctx.confidence)
                    _tier_min_confs     = {1: _TIER1_MIN_CONF, 2: _TIER2_MIN_CONF, 3: _TIER3_MIN_CONF}
                    _effective_min_conf = _tier_min_confs[_effective_tier]

                if _effective_conf >= _effective_min_conf:
                    # Print MCB execution context explicitly so the terminal is not misleading
                    print(
                        f"[EXECUTION_MCB_CONTEXT] {_current_state.option_underlying}  "
                        f"bias={_mctx.bias}  conf={_mctx.confidence:.2f}  "
                        f"live_bars={_current_state.live_bars_seen}"
                    )

                    # Phase-2 SHADOW: log a transparent graded conviction for this
                    # decision (real inputs; cannot saturate). No trade effect.
                    if _conviction_shadow is not None:
                        _conviction_shadow.observe(
                            underlying=_current_state.option_underlying,
                            timestamp=completed_bar.start_timestamp,
                            composite=getattr(_srec, "composite_score", 0.0),
                            score_margin=(_current_state.captured_score_margin or 0.0),
                            liquidity=getattr(getattr(_srec, "breakdown", None), "liquidity", 0.0),
                            vol_ratio=max(_current_state.last_atm_iv / max(_REFERENCE_IV, 0.001), 0.1),
                            fvg_quality=_current_state.fvg_quality,
                            mcb_conf=_mctx.confidence,
                            effective_conf=_effective_conf,
                            bias=str(_mctx.bias),
                        )

                    # ── Pre-entry safety checks [A] warmup  [B] cooldown ─────
                    _pre_entry_block: str | None = None
                    _bars_since_stop = _bar_count - _current_state.last_stop_bar

                    # [A-time] Time-of-day entry cutoff (afternoon-chop guard,
                    # measured in replay + confirmed OOS live 07-02). New entries
                    # only — open positions manage and exit normally.
                    if _time_cutoff_blocked(
                        completed_bar.start_timestamp, _ENTRY_CUTOFF_IST_HOUR
                    ):
                        _pre_entry_block = (
                            f"ENTRY_TIME_CUTOFF at/after {_ENTRY_CUTOFF_IST_HOUR}:00 IST "
                            f"(afternoon-chop guard, LIVE_ENTRY_CUTOFF_IST=0 disables)"
                        )

                    # [A-contract] Same-contract session loss limit: this exact
                    # contract+direction already took the session's loss budget.
                    if (
                        _pre_entry_block is None
                        and _current_state.contract_loss_tracker.blocked(
                            contract=_srec.option_symbol, direction=str(_mctx.bias)
                        )
                    ):
                        _pre_entry_block = (
                            f"SAME_CONTRACT_SESSION_BLOCK {_srec.option_symbol} "
                            f"{_mctx.bias} — "
                            f"{_current_state.contract_loss_tracker.losses(contract=_srec.option_symbol, direction=str(_mctx.bias))} "
                            f"losing exits this session"
                        )

                    # [A-direction] Same-direction session entry cap: after N
                    # entries in one direction the day's move has been harvested —
                    # later same-direction re-entries chase the tail (measured:
                    # every 4th+ same-direction entry lost). A reversal is free.
                    if (
                        _pre_entry_block is None
                        and _current_state.direction_entry_counter.blocked(
                            direction=str(_mctx.bias)
                        )
                    ):
                        _pre_entry_block = (
                            f"DIRECTION_ENTRY_CAP {_mctx.bias} — "
                            f"{_current_state.direction_entry_counter.entries(direction=str(_mctx.bias))} "
                            f"entries this session (max "
                            f"{_current_state.direction_entry_counter.max_entries})"
                        )

                    # [A-chop] Chop re-entry lockout: after consecutive losing exits
                    # the underlying is locked out briefly (a win resets the streak).
                    if _pre_entry_block is None and _bar_count < _current_state.reentry_lockout_until_bar:
                        _pre_entry_block = (
                            f"CHOP_REENTRY_LOCKOUT until bar "
                            f"{_current_state.reentry_lockout_until_bar} "
                            f"(bar={_bar_count})"
                        )

                    # [A-conv] Conviction floor (paper/dry-run): skip genuinely weak
                    # setups (pure risk reduction — never sizes UP, only skips).
                    if _pre_entry_block is None and _CONV_LIVE:
                        _entry_conv = _live_conviction(_current_state, _srec)
                        if _entry_conv < _CONV_FLOOR:
                            _pre_entry_block = (
                                f"LOW_CONVICTION conv={_entry_conv:.2f}<{_CONV_FLOOR:.2f}"
                            )

                    # [A0] Capital-tier simultaneous position limit — counts
                    # AT-RISK positions only: a ratchet-armed (breakeven-protected)
                    # winner no longer consumes a risk slot, so the next
                    # opportunity can be taken while the winner trails. The heat
                    # cap still counts ALL open premium (gap risk is not zero).
                    _cur_open = sum(
                        1 for _s in _runtime_by_underlying.values()
                        if _s.option_trader.position_at_risk()
                    )
                    if _pre_entry_block is None and _cur_open >= _MAX_OPEN_POSITIONS:
                        _pre_entry_block = (
                            f"CAPITAL_TIER_POSITION_LIMIT "
                            f"open_positions={_cur_open}/{_MAX_OPEN_POSITIONS} "
                            f"capital=₹{_session_opening_equity:,.0f}"
                        )

                    # [A] Live bar warmup gate (lifted during the seeded
                    # open-drive window — bias comes from the warm-start).
                    if (_pre_entry_block is None
                            and _current_state.live_bars_seen < _LIVE_BARS_BEFORE_ENTRY
                            and not _open_drive_pass_now()):
                        _pre_entry_block = (
                            f"WARM_START_LOCKOUT "
                            f"live_bars={_current_state.live_bars_seen}/{_LIVE_BARS_BEFORE_ENTRY}"
                        )
                    # [A2] Context contradiction: BOS/CHOCH at this bar opposes MCB bias.
                    # Proven defect (2026-06-04): BANKNIFTY BOS BULLISH fired at bar 59
                    # while MCB said SHORT — entry was made on a bullish structure break.
                    # Only blocks when HTF structure actively contradicts; NEUTRAL is not blocked.
                    elif _pre_entry_block is None:
                        _bias_str = str(_mctx.bias).upper()
                        if _bias_str in ("SHORT", "LONG"):
                            for _ev in result.emitted_events:
                                _ev_type = (
                                    _ev.event_type.value
                                    if hasattr(_ev.event_type, "value") else str(_ev.event_type)
                                )
                                _ev_side = (
                                    _ev.side.value
                                    if hasattr(_ev.side, "value") else str(_ev.side)
                                ).lower()
                                _is_structural = "CHOCH" in _ev_type or _ev_type == "BOS"
                                if (_bias_str == "SHORT" and _is_structural and _ev_side == "bullish"):
                                    _pre_entry_block = (
                                        f"CONTEXT_CONTRADICTION_REJECTED "
                                        f"mcb=SHORT structure={_ev_type}_BULLISH at bar={_bar_count}"
                                    )
                                    _current_state.last_contradiction_bar = _bar_count
                                    _current_state.last_contradiction_meta = {
                                        "ts":        datetime.now(timezone.utc).isoformat(),
                                        "bar":       _bar_count,
                                        "reason":    _pre_entry_block,
                                        "structure": f"{_ev_type}_BULLISH",
                                        "mcb_bias":  _bias_str,
                                    }
                                    _current_state.contra_bos_count += 1
                                    if _current_state.contra_bos_count >= _MCB_CONTRA_BOS_WARN_THRESHOLD:
                                        print(
                                            f"[MCB ADAPTATION WATCH] underlying={_current_state.option_underlying}"
                                            f" bias=SHORT conf={_mctx.confidence:.2f}"
                                            f" opposite_bos_count={_current_state.contra_bos_count}"
                                            f" bar={_bar_count}"
                                            f" — diagnostic only, no execution change"
                                        )
                                    break
                                if (_bias_str == "LONG" and _is_structural and _ev_side == "bearish"):
                                    _pre_entry_block = (
                                        f"CONTEXT_CONTRADICTION_REJECTED "
                                        f"mcb=LONG structure={_ev_type}_BEARISH at bar={_bar_count}"
                                    )
                                    _current_state.last_contradiction_bar = _bar_count
                                    _current_state.last_contradiction_meta = {
                                        "ts":        datetime.now(timezone.utc).isoformat(),
                                        "bar":       _bar_count,
                                        "reason":    _pre_entry_block,
                                        "structure": f"{_ev_type}_BEARISH",
                                        "mcb_bias":  _bias_str,
                                    }
                                    _current_state.contra_bos_count += 1
                                    if _current_state.contra_bos_count >= _MCB_CONTRA_BOS_WARN_THRESHOLD:
                                        print(
                                            f"[MCB ADAPTATION WATCH] underlying={_current_state.option_underlying}"
                                            f" bias=LONG conf={_mctx.confidence:.2f}"
                                            f" opposite_bos_count={_current_state.contra_bos_count}"
                                            f" bar={_bar_count}"
                                            f" — diagnostic only, no execution change"
                                        )
                                    break

                    # [A3] Context-contradiction cooldown — HARD gate, applies to
                    # ALL profiles and ALL execution modes (paper / real_dry_run /
                    # real_armed).  Rejects BEFORE sizing and order-intent creation.
                    if _pre_entry_block is None:
                        _cd_blocked, _cd_bars_since = _contradiction_cooldown_active(
                            _bar_count,
                            _current_state.last_contradiction_bar,
                            _CONTEXT_CONTRADICTION_COOLDOWN_BARS,
                        )
                        if _cd_blocked:
                            print(
                                f"[CONTEXT COOLDOWN BLOCK] "
                                f"underlying={_current_state.option_underlying} "
                                f"bars_since={_cd_bars_since} "
                                f"cooldown={_CONTEXT_CONTRADICTION_COOLDOWN_BARS} "
                                f"reason=recent_context_contradiction"
                            )
                            _pre_entry_block = (
                                f"CONTEXT_COOLDOWN_BLOCK "
                                f"bars_since={_cd_bars_since}/{_CONTEXT_CONTRADICTION_COOLDOWN_BARS} "
                                f"last={_current_state.last_contradiction_meta.get('reason', '?')}"
                            )

                    # [B1] Underlying-level cooldown after stop
                    if _pre_entry_block is None and _bars_since_stop < _SAME_UNDERLYING_STOP_COOLDOWN_BARS:
                        _pre_entry_block = (
                            f"STOP_COOLDOWN_ACTIVE underlying_cooldown "
                            f"bars_since_stop={_bars_since_stop}/{_SAME_UNDERLYING_STOP_COOLDOWN_BARS}"
                        )
                    # [B2] Same-contract cooldown after stop
                    elif (_srec.option_symbol == _current_state.last_stop_contract
                            and _bars_since_stop < _SAME_CONTRACT_STOP_COOLDOWN_BARS):
                        _pre_entry_block = (
                            f"STOP_COOLDOWN_ACTIVE same_contract_cooldown "
                            f"bars_since_stop={_bars_since_stop}/{_SAME_CONTRACT_STOP_COOLDOWN_BARS}"
                        )

                    # [B1s]/[B2s] Structure-exit re-entry cooldown (chop filter):
                    # prevents re-buying the same strike (or churning the same
                    # underlying) immediately after a BOS/CHOCH structure exit.
                    _bars_since_struct = _bar_count - _current_state.last_struct_exit_bar
                    if (_pre_entry_block is None
                            and _bars_since_struct < _SAME_UNDERLYING_STRUCT_COOLDOWN_BARS):
                        _pre_entry_block = (
                            f"STRUCT_EXIT_COOLDOWN underlying_cooldown "
                            f"bars_since={_bars_since_struct}/{_SAME_UNDERLYING_STRUCT_COOLDOWN_BARS}"
                        )
                    elif (_pre_entry_block is None
                            and _srec.option_symbol == _current_state.last_struct_exit_contract
                            and _bars_since_struct < _SAME_CONTRACT_STRUCT_COOLDOWN_BARS):
                        _pre_entry_block = (
                            f"STRUCT_EXIT_COOLDOWN same_contract_cooldown "
                            f"bars_since={_bars_since_struct}/{_SAME_CONTRACT_STRUCT_COOLDOWN_BARS}"
                        )

                    # [B3] Confluence floor (Tier2/3): the per-underlying
                    # oi_support floor (profile-resolved) rejects BEFORE sizing /
                    # order-intent creation. NOTE: this RELATIVE-OI floor is a
                    # weak proxy — the real liquidity protection is the [B3b]
                    # ABSOLUTE-liquidity gate below (min_abs_oi + min_liquidity),
                    # which applies to every profile/mode and is never relaxed.
                    # (SENSEX floor is 0.005 by default — its relative ratio is
                    # structurally tiny; the abs gate is what guards liquidity.)
                    _pre_oi_supp = getattr(
                        getattr(_srec, "breakdown", None), "oi_support", 1.0
                    )
                    _pre_oi_min = _GATE_OI_SUPPORT_BY_SYM.get(
                        _current_state.option_underlying, _MIN_OI_SUPPORT_TIER23
                    )
                    if (_pre_entry_block is None
                            and _effective_tier >= 2
                            and _pre_oi_supp < _pre_oi_min):
                        _pre_entry_block = (
                            f"INSUFFICIENT_CONFLUENCE oi_support={_pre_oi_supp:.3f} "
                            f"< min={_pre_oi_min} for tier={_effective_tier}"
                        )

                    # [B3b] Absolute liquidity hard gate (Tier2/3): real OI +
                    # tradeable spread/volume.  Unlike the relative oi_support
                    # ratio above, this never rejects a liquid non-peak strike;
                    # it rejects strikes that are absolutely too thin to exit at
                    # a fair price.  Strengthening gate — every profile/mode.
                    if (_pre_entry_block is None
                            and _effective_tier >= 2):
                        _pre_abs_oi = getattr(_srec, "open_interest", 0) or 0
                        _pre_liq = getattr(
                            getattr(_srec, "breakdown", None), "liquidity", 1.0
                        )
                        _pre_min_abs_oi = _MIN_ABS_OI_BY_SYM.get(
                            _current_state.option_underlying, 3000.0
                        )
                        if _pre_abs_oi < _pre_min_abs_oi or _pre_liq < _MIN_LIQUIDITY_TIER23:
                            _pre_entry_block = (
                                f"INSUFFICIENT_LIQUIDITY abs_oi={_pre_abs_oi:,} "
                                f"(min={_pre_min_abs_oi:,.0f})  liquidity={_pre_liq:.3f} "
                                f"(min={_MIN_LIQUIDITY_TIER23:.2f}) for tier={_effective_tier}"
                            )

                    # [A6] Symbol routing.
                    #  - Conviction-live (paper/dry-run): NO symbol is forced. Both
                    #    SENSEX and NIFTY trade purely on their own conviction — the
                    #    symmetric [A-conv] floor + conviction sizing + the 2-position
                    #    limit decide. Removes the old ONE-WAY SENSEX force, which
                    #    misfired 2026-06-30 (a weak SENSEX traded at full size and lost
                    #    while a higher-conviction NIFTY was blocked). "Higher
                    #    probability wins," no thumb on the scale.
                    #  - Otherwise (real_armed / conviction off): keep the original
                    #    SENSEX-primary directive (NIFTY defers to an open SENSEX and
                    #    needs a very-strong MCB conf).
                    if (_pre_entry_block is None
                            and not _CONV_LIVE
                            and _SENSEX_PRIMARY_ROUTING
                            and _current_state.option_underlying == "NIFTY"):
                        _sensex_rt = _runtime_by_underlying.get(_SENSEX_PRIORITY_UNDERLYING)
                        # Defer only to an AT-RISK SENSEX position: once SENSEX is
                        # ratchet-armed (risk-free winner trailing), NIFTY may take
                        # the next opportunity instead of standing down all day.
                        if _sensex_rt is not None and _sensex_rt.option_trader.position_at_risk():
                            _pre_entry_block = (
                                "SENSEX_PRIMARY_DEFER "
                                "(SENSEX position at risk — NIFTY stands down)"
                            )
                        elif _effective_conf < _NIFTY_VERY_STRONG_CONF:
                            _pre_entry_block = (
                                f"NIFTY_NOT_VERY_STRONG conf={_effective_conf:.2f}"
                                f"<{_NIFTY_VERY_STRONG_CONF:.2f} (SENSEX-primary routing)"
                            )

                    # [A7] GEX wall-proximity gate. MEASURED HARMFUL as a veto
                    # (see _GEX_GATE_SHADOW note) — GEX_REGIME_GATE stays opt-in
                    # and should NOT be enabled on current evidence. Shadow mode
                    # logs the verdict per entry evaluation without blocking.
                    if (_GEX_REGIME_GATE or _GEX_GATE_SHADOW) and _pre_entry_block is None:
                        _gx = _current_state.captured_gex_snapshot
                        if _gx is not None:
                            _gex_blk = _gex_wall_block(
                                bias=_mctx.bias,
                                spot=completed_bar.close_price,
                                gex=_gx,
                                proximity_pct=_GEX_WALL_PROXIMITY_PCT,
                                block_long_gamma=_GEX_REGIME_VETO,
                            )
                            print(
                                f"[GEX{'' if _GEX_REGIME_GATE else ' SHADOW'}] "
                                f"{_current_state.option_underlying} "
                                f"regime={getattr(_gx, 'net_gamma_regime', '?')} "
                                f"call_wall={getattr(_gx, 'call_wall_strike', None)} "
                                f"put_wall={getattr(_gx, 'put_wall_strike', None)} "
                                f"spot={completed_bar.close_price:.0f} "
                                f"verdict={('BLOCK' if _gex_blk else 'ok') if _GEX_REGIME_GATE else ('would-block' if _gex_blk else 'ok')}"
                            )
                            if _GEX_REGIME_GATE and _gex_blk:
                                _pre_entry_block = _gex_blk

                    if _pre_entry_block is not None:
                        print_warning_block(
                            "ENTRY CHECK BLOCKED",
                            bar_index=_bar_count,
                            underlying=_current_state.option_underlying,
                            reason=_pre_entry_block,
                        )
                        try:
                            _db.write_decision(
                                bar_index=_bar_count,
                                bar_ts=datetime.now(timezone.utc).isoformat(),
                                underlying=_current_state.option_underlying,
                                bias=str(_mctx.bias),
                                conf_raw=_mctx.confidence,
                                conf_eff=_effective_conf,
                                outcome=_pre_entry_block.split()[0],
                                filter_reason=_pre_entry_block,
                                equity=_equity,
                                drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                            )
                        except Exception:
                            pass
                    else:
                        # Pre-entry checks passed — proceed to sizing
                        _sizing = _size_live_position(
                            _current_state, _srec, _mctx, _effective_tier, _effective_conf,
                        )
                        if _sizing.tradable and _sizing.recommended_lots >= 1:
                            _lots = _sizing.recommended_lots
                            # Re-entry risk haircut (measured 2026-07-06, replay
                            # batch 14 day-samples: −1,354 → +317 net, same trade
                            # set): the 2nd+ same-direction entry of the session
                            # sizes at a fraction of normal lots — setup quality
                            # decays with each re-entry, size must too (07-06
                            # live: the day's LARGEST size hit the worst entry).
                            # PAPER_REENTRY_RISK_HAIRCUT=0 disables.
                            if (_REENTRY_RISK_HAIRCUT > 0.0
                                    and _current_state.direction_entry_counter.entries(
                                        direction=str(_mctx.bias)) >= 1):
                                _lots = max(int(_lots * _REENTRY_RISK_HAIRCUT), 1)
                            # Regime de-size (2026-07-10, env REGIME_DESIZE_MULT,
                            # dry-run test): the BALANCED directional regime lost
                            # 5 of 7 days this week — shrink size there. Real lot
                            # reduction (not a conf haircut). =1.0 disables.
                            if (_REGIME_DESIZE_MULT < 1.0
                                    and getattr(_current_state, "last_dir_regime", "UNKNOWN") == "BALANCED"):
                                _lots = max(int(_lots * _REGIME_DESIZE_MULT), 1)
                            _cost = (
                                _srec.last_traded_price
                                * _srec.lot_size
                                * _lots
                            )
                            _avail = max(_equity - _open_premium_cost, 0.0)
                            if _cost <= _avail:
                                # [C] Confluence gate: Tier2/3 requires OI support
                                _oi_supp = getattr(
                                    getattr(_srec, "breakdown", None), "oi_support", 1.0
                                )
                                _eff_oi_min = _GATE_OI_SUPPORT_BY_SYM.get(
                                    _current_state.option_underlying, _MIN_OI_SUPPORT_TIER23
                                )
                                if (_effective_tier >= 2
                                        and _oi_supp < _eff_oi_min):
                                    _oi_reason = (
                                        f"INSUFFICIENT_CONFLUENCE oi_support={_oi_supp:.3f} "
                                        f"< min={_eff_oi_min} for tier={_effective_tier}"
                                    )
                                    print_warning_block(
                                        "ENTRY CHECK BLOCKED",
                                        bar_index=_bar_count,
                                        underlying=_current_state.option_underlying,
                                        reason=_oi_reason,
                                    )
                                    try:
                                        _db.write_decision(
                                            bar_index=_bar_count,
                                            bar_ts=datetime.now(timezone.utc).isoformat(),
                                            underlying=_current_state.option_underlying,
                                            bias=str(_mctx.bias),
                                            conf_raw=_mctx.confidence,
                                            conf_eff=_effective_conf,
                                            outcome="INSUFFICIENT_CONFLUENCE",
                                            filter_reason=_oi_reason,
                                            equity=_equity,
                                            drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                        )
                                    except Exception:
                                        pass
                                else:
                                    # [D] Strike-quality gate: Tier2/3 must have
                                    # meaningful score separation and IV attractiveness.
                                    # Proven defect (2026-06-04): SENSEX iv_attr=0.000
                                    # and SCORE_MARGIN_LOW let bad strikes through OI filter.
                                    _sq_reason: str | None = None
                                    if _effective_tier >= 2:
                                        _bd       = getattr(_srec, "breakdown", None)
                                        _iv_attr  = getattr(_bd, "iv_attractiveness", None)
                                        _oi_supp2 = getattr(_bd, "oi_support", None)
                                        _sm_val   = _current_state.captured_score_margin
                                        _eff_sm_min = _GATE_SCORE_MARGIN_BY_SYM.get(
                                            _current_state.option_underlying, _MIN_SCORE_MARGIN_TIER23
                                        )
                                        _eff_sq_oi_min = _GATE_OI_SUPPORT_BY_SYM.get(
                                            _current_state.option_underlying, _MIN_OI_SUPPORT_TIER23
                                        )
                                        _sq_parts: list[str] = []
                                        # [P1] a resolved delta tie-break waives ONLY the
                                        # margin leg; iv_attr / oi_support still bind.
                                        if (_sm_val is not None and _sm_val < _eff_sm_min
                                                and not _current_state.captured_tiebreak_pass):
                                            _sq_parts.append(
                                                f"score_margin={_sm_val:.3f}<{_eff_sm_min}"
                                            )
                                        if _iv_attr is not None and _iv_attr < _MIN_IV_ATTR_TIER23:
                                            _sq_parts.append(
                                                f"iv_attr={_iv_attr:.3f}<{_MIN_IV_ATTR_TIER23}"
                                            )
                                        if _oi_supp2 is not None and _oi_supp2 < _eff_sq_oi_min:
                                            _sq_parts.append(
                                                f"oi_support={_oi_supp2:.3f}<{_eff_sq_oi_min}"
                                            )
                                        if _sq_parts:
                                            _sq_reason = (
                                                f"INSUFFICIENT_STRIKE_QUALITY "
                                                f"{' '.join(_sq_parts)} "
                                                f"tier={_effective_tier}"
                                            )
                                    if _sq_reason is not None:
                                        print_warning_block(
                                            "ENTRY CHECK BLOCKED",
                                            bar_index=_bar_count,
                                            underlying=_current_state.option_underlying,
                                            reason=_sq_reason,
                                        )
                                        try:
                                            _db.write_decision(
                                                bar_index=_bar_count,
                                                bar_ts=datetime.now(timezone.utc).isoformat(),
                                                underlying=_current_state.option_underlying,
                                                bias=str(_mctx.bias),
                                                conf_raw=_mctx.confidence,
                                                conf_eff=_effective_conf,
                                                outcome="INSUFFICIENT_STRIKE_QUALITY",
                                                filter_reason=_sq_reason,
                                                equity=_equity,
                                                drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                            )
                                        except Exception:
                                            pass
                                    else:
                                        # ── Entry contract guard ──────────────
                                        _entry_ok, _entry_rejection = _validate_entry_contract(
                                            _current_state, _srec, _lots
                                        )
                                        if not _entry_ok:
                                            print_warning_block(
                                                "ENTRY GUARD REJECTED",
                                                bar_index=_bar_count,
                                                underlying=_current_state.option_underlying,
                                                contract=_srec.option_symbol,
                                                reason=_entry_rejection,
                                            )
                                            try:
                                                _db.write_decision(
                                                    bar_index=_bar_count,
                                                    bar_ts=datetime.now(timezone.utc).isoformat(),
                                                    underlying=_srec.underlying_symbol,
                                                    bias=str(_mctx.bias),
                                                    conf_raw=_mctx.confidence,
                                                    conf_eff=_effective_conf,
                                                    outcome="ENTRY_GUARD_REJECTED",
                                                    filter_reason=_entry_rejection,
                                                    equity=_equity,
                                                    drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                )
                                            except Exception:
                                                pass
                                        else:
                                            # ── Hard/soft gate snapshot ───────
                                            # Auditable record that every hard
                                            # gate passed before this entry.
                                            # Profiles relax SOFT gates only.
                                            _sm_snap = _current_state.captured_score_margin
                                            _cd_snap_since = (
                                                _bar_count - _current_state.last_contradiction_bar
                                                if _current_state.last_contradiction_bar is not None
                                                else None
                                            )
                                            print(
                                                f"[ENTRY HARD GATES] symbol={_srec.option_symbol}"
                                                f" contradiction=False"
                                                f" contradiction_cooldown=False"
                                                f"(bars_since="
                                                + (str(_cd_snap_since) if _cd_snap_since is not None else "none")
                                                + f"/{_CONTEXT_CONTRADICTION_COOLDOWN_BARS})"
                                                f" warm_start=passed({_current_state.live_bars_seen}/{_LIVE_BARS_BEFORE_ENTRY})"
                                                f" oi_ok=True({_pre_oi_supp:.3f}>={_pre_oi_min})"
                                                f" risk_ok=True(lots={_lots})"
                                                f" heat_ok=True(cost=₹{_cost:,.0f})"
                                                f" contract_ok=True"
                                                f" cooldown_ok=True"
                                                f" position_limit_ok=True({_cur_open}/{_MAX_OPEN_POSITIONS})"
                                                f" halt_ok=True"
                                            )
                                            print(
                                                f"[ENTRY SOFT GATES] profile={_PAPER_GATE_PROFILE}"
                                                f" score_margin="
                                                + (f"{_sm_snap:.4f}" if _sm_snap is not None else "n/a")
                                                + f"(min={_GATE_SCORE_MARGIN_BY_SYM.get(_current_state.option_underlying, _MIN_SCORE_MARGIN_TIER23)})"
                                                f" confluence={_pre_oi_supp:.3f}"
                                                f"(min={_pre_oi_min})"
                                                f" tier={_effective_tier}"
                                            )
                                            # ── BANKNIFTY confirmation (double-check, non-blocking) ──
                                            _bnk_bias_str, _bnk_verdict = _banknifty_confirmation(
                                                _current_state.option_underlying, _mctx.bias
                                            )
                                            print(
                                                f"[CONFIRMATION] BANKNIFTY bias={_bnk_bias_str}"
                                                f" vs {_current_state.option_underlying} entry={str(_mctx.bias)}"
                                                f" -> {_bnk_verdict}  (double-check only, non-blocking)"
                                            )
                                            # Position is opened in the _entry_ok block
                                            # below — AFTER the order is routed and the
                                            # fill confirmed (real modes open at the
                                            # actual fill price; paper/dry-run at LTP).
                                        _tier_label = "SOFT" if _soft_halted else str(_effective_tier)
                                        # ── Pre-trade margin gate (real_armed only) ──
                                        # Block a real entry if broker margin can't cover the premium cost.
                                        # paper/dry-run: get_available_margin() returns +inf → never blocks.
                                        _margin_ok = True
                                        if _entry_ok and _EXECUTION_MODE == "real_armed":
                                            _avail_margin = _exec_router.get_available_margin()
                                            _need_margin  = _srec.last_traded_price * _srec.lot_size * _lots
                                            if _avail_margin < _need_margin:
                                                _margin_ok = False
                                                print_warning_block(
                                                    "REAL ENTRY BLOCKED — INSUFFICIENT MARGIN",
                                                    bar_index=_bar_count,
                                                    underlying=_srec.underlying_symbol,
                                                    reason=f"available=₹{_avail_margin:,.0f} < need=₹{_need_margin:,.0f}",
                                                )
                                                try:
                                                    _db.write_decision(
                                                        bar_index=_bar_count,
                                                        bar_ts=datetime.now(timezone.utc).isoformat(),
                                                        underlying=_srec.underlying_symbol,
                                                        bias=str(_mctx.bias),
                                                        conf_raw=_mctx.confidence,
                                                        conf_eff=_effective_conf,
                                                        outcome="REAL_MARGIN_BLOCK",
                                                        filter_reason=f"avail={_avail_margin:.0f}<need={_need_margin:.0f}",
                                                        equity=_equity,
                                                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                    )
                                                except Exception:
                                                    pass
                                            # ── Hard rupee caps: MAX_REAL_DAILY_LOSS / MAX_REAL_CAPITAL.
                                            # These interlocks were required-but-UNREAD before 2026-07-05
                                            # (decorative). Enforced at ENTRY only — exits are never
                                            # blocked, or a live position could be trapped. Unparsable
                                            # values fail closed.
                                            if _margin_ok:
                                                try:
                                                    _max_dl  = float(os.environ.get("MAX_REAL_DAILY_LOSS", "0") or 0)
                                                    _max_cap = float(os.environ.get("MAX_REAL_CAPITAL", "0") or 0)
                                                except (TypeError, ValueError):
                                                    _max_dl = _max_cap = -1.0
                                                _cap_block = None
                                                if _max_dl < 0 or _max_cap < 0:
                                                    _cap_block = "unparsable MAX_REAL_DAILY_LOSS/MAX_REAL_CAPITAL (fail-closed)"
                                                elif _max_dl > 0 and -_daily_pnl >= _max_dl:
                                                    _cap_block = (f"daily loss ₹{-_daily_pnl:,.0f} >= "
                                                                  f"MAX_REAL_DAILY_LOSS ₹{_max_dl:,.0f}")
                                                elif _max_cap > 0 and _open_premium_cost + _need_margin > _max_cap:
                                                    _cap_block = (f"premium outlay ₹{_open_premium_cost + _need_margin:,.0f} "
                                                                  f"would exceed MAX_REAL_CAPITAL ₹{_max_cap:,.0f}")
                                                if _cap_block is not None:
                                                    _margin_ok = False
                                                    print_warning_block(
                                                        "REAL ENTRY BLOCKED — RUPEE CAP",
                                                        bar_index=_bar_count,
                                                        underlying=_srec.underlying_symbol,
                                                        reason=_cap_block,
                                                    )
                                                    try:
                                                        _db.write_decision(
                                                            bar_index=_bar_count,
                                                            bar_ts=datetime.now(timezone.utc).isoformat(),
                                                            underlying=_srec.underlying_symbol,
                                                            bias=str(_mctx.bias),
                                                            conf_raw=_mctx.confidence,
                                                            conf_eff=_effective_conf,
                                                            outcome="REAL_CAP_BLOCK",
                                                            filter_reason=_cap_block[:120],
                                                            equity=_equity,
                                                            drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                        )
                                                    except Exception:
                                                        pass
                                        if _entry_ok and _margin_ok:
                                            # ── Execute: route order → confirm fill → open at ACTUAL fill ──
                                            # paper/real_dry_run: route is a no-op/log and confirm_fill
                                            # synthesizes a FILLED at the strike LTP (behaviour unchanged).
                                            # real_armed: the real order is placed and confirmed at the real
                                            # average fill price; if it does not fully fill, NO local position
                                            # is opened and nothing is written to pt_trades (fail-closed).
                                            try:
                                                _entry_result = _exec_router.route_entry(
                                                    underlying    = _srec.underlying_symbol,
                                                    tradingsymbol = _srec.option_symbol,
                                                    lots          = _lots,
                                                    lot_size      = _srec.lot_size,
                                                    premium       = _srec.last_traded_price,
                                                    direction     = str(_mctx.bias),
                                                    session_tag   = _session_tag,
                                                    strategy_context = {
                                                        "bias": str(_mctx.bias),
                                                        "conf_raw": _mctx.confidence,
                                                        "conf_eff": _effective_conf,
                                                        "tier": _tier_label,
                                                    },
                                                    risk_context  = {
                                                        "equity": _equity,
                                                        "drawdown": round(
                                                            max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)), 4
                                                        ),
                                                    },
                                                    strike_context = {
                                                        "strike": _srec.strike_price,
                                                        "expiry": _srec.expiry_date.isoformat(),
                                                        "option_type": _srec.option_type,
                                                        "premium": _srec.last_traded_price,
                                                    },
                                                    db = _db,
                                                )
                                                _entry_fill = _exec_router.confirm_fill(
                                                    _entry_result,
                                                    expected_qty=_lots * _srec.lot_size,
                                                    fallback_premium=_srec.last_traded_price,
                                                )
                                            except Exception:
                                                _entry_fill = None   # unexpected router error → fail-closed

                                            # PARTIAL entry fill (real_armed): confirm_fill has
                                            # already cancelled the remainder, but the broker
                                            # HOLDS the filled lots — refusing to open locally
                                            # would leave an unmanaged orphan bleeding premium
                                            # until the periodic reconciler halts. ADOPT the
                                            # filled lots as the position instead (options fill
                                            # in lot multiples) and manage them normally.
                                            _adopt_partial = False
                                            if (_entry_fill is not None
                                                    and _entry_fill.status == "PARTIAL"
                                                    and _entry_fill.average_price > 0.0
                                                    and _entry_fill.filled_quantity >= _srec.lot_size):
                                                _part_lots = int(_entry_fill.filled_quantity // _srec.lot_size)
                                                print_warning_block(
                                                    "PARTIAL ENTRY FILL ADOPTED",
                                                    bar_index=_bar_count,
                                                    underlying=_srec.underlying_symbol,
                                                    reason=(f"filled {_entry_fill.filled_quantity}"
                                                            f"/{_lots * _srec.lot_size} qty → managing "
                                                            f"{_part_lots} lots (remainder cancelled)"),
                                                )
                                                try:
                                                    _db.write_runtime_event(
                                                        "REAL_PARTIAL_ENTRY_ADOPTED", severity="WARNING",
                                                        message=(f"{_srec.option_symbol} filled "
                                                                 f"{_entry_fill.filled_quantity}"
                                                                 f"/{_lots * _srec.lot_size}"),
                                                        equity=_equity,
                                                    )
                                                except Exception:
                                                    pass
                                                _lots = _part_lots
                                                _adopt_partial = True

                                            if _entry_fill is not None and (_entry_fill.is_filled or _adopt_partial):
                                                _exec_premium = _entry_fill.average_price
                                                _exec_cost    = _exec_premium * _srec.lot_size * _lots
                                                _open_premium_cost += _exec_cost
                                                _current_state.option_trader.open(
                                                    recommendation=_srec,
                                                    direction=str(_mctx.bias),
                                                    bar_index=_bar_count,
                                                    lots=_lots,
                                                    entry_premium_override=_exec_premium,
                                                )
                                                _current_state.direction_entry_counter.record_entry(
                                                    direction=str(_mctx.bias),
                                                )
                                                # FUTURES SLEEVE mirror (paper): same signal,
                                                # linear expression — never affects this path.
                                                if _futures_sleeve is not None:
                                                    _futures_sleeve.on_option_entry(
                                                        _srec.underlying_symbol, str(_mctx.bias))
                                                if _EXECUTION_MODE == "real_armed":
                                                    print(
                                                        f"[REAL FILL] ENTRY {_srec.option_symbol} "
                                                        f"filled @₹{_exec_premium:.2f} qty={_entry_fill.filled_quantity}"
                                                    )
                                                # ── Entry print + DB write (at the executed fill price) ──
                                                print_entry_block(
                                                    direction=str(_mctx.bias),
                                                    underlying=_srec.underlying_symbol,
                                                    strike=_srec.strike_price,
                                                    option_type=_srec.option_type,
                                                    expiry=_srec.expiry_date,
                                                    lots=_lots,
                                                    lot_size=_srec.lot_size,
                                                    premium=_exec_premium,
                                                    notional=_exec_cost,
                                                    bar_index=_bar_count,
                                                    session=str(_session_ctx.phase),
                                                    conf_raw=_mctx.confidence,
                                                    conf_eff=_effective_conf,
                                                    fvg_quality=_current_state.fvg_quality,
                                                    # Pass the REAL sizing inputs so the operator sees true
                                                    # setup/vol/liquidity (previously defaulted to 0/1/0 —
                                                    # misleading display; the sizer always used the real values).
                                                    setup_score=min(_srec.composite_score, 1.0),
                                                    vol_ratio=max(_current_state.last_atm_iv / max(_REFERENCE_IV, 0.001), 0.1),
                                                    liquidity=min(_srec.breakdown.liquidity, 1.0),
                                                    drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                    extra_label=f"TIER{_tier_label}",
                                                )
                                                try:
                                                    _db.write_trade_entry(
                                                        underlying=_srec.underlying_symbol,
                                                        contract_symbol=_srec.option_symbol,
                                                        strike=_srec.strike_price,
                                                        option_type=_srec.option_type,
                                                        expiry_date=_srec.expiry_date.isoformat(),
                                                        direction=str(_mctx.bias),
                                                        lots=_lots,
                                                        lot_size=_srec.lot_size,
                                                        entry_premium=_exec_premium,
                                                        entry_spot=completed_bar.close_price,
                                                        entry_iv=_current_state.last_atm_iv,
                                                        entry_bar_index=_bar_count,
                                                        entry_ts=datetime.now(timezone.utc).isoformat(),
                                                        conf_raw=_mctx.confidence,
                                                        conf_eff=_effective_conf,
                                                        fvg_quality=_current_state.fvg_quality,
                                                        drawdown_at_entry=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                        equity_before=_equity,
                                                    )
                                                    _db.write_decision(
                                                        bar_index=_bar_count,
                                                        bar_ts=datetime.now(timezone.utc).isoformat(),
                                                        underlying=_srec.underlying_symbol,
                                                        bias=str(_mctx.bias),
                                                        conf_raw=_mctx.confidence,
                                                        conf_eff=_effective_conf,
                                                        outcome=f"ENTRY_TIER{_tier_label}",
                                                        session_phase=str(_session_ctx.phase),
                                                        fvg_quality=_current_state.fvg_quality,
                                                        equity=_equity,
                                                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                    )
                                                except Exception:
                                                    pass
                                            else:
                                                # real_armed order did not fully fill — stay flat, open
                                                # nothing, record the miss (fail-closed).  Unreachable in
                                                # paper/real_dry_run (confirm_fill always synthesizes FILLED).
                                                _miss = _entry_fill.status if _entry_fill is not None else "ROUTER_ERROR"
                                                print_warning_block(
                                                    "REAL ENTRY NOT FILLED",
                                                    bar_index=_bar_count,
                                                    underlying=_srec.underlying_symbol,
                                                    contract=_srec.option_symbol,
                                                    reason=f"{_miss} — no local position opened",
                                                )
                                                try:
                                                    _db.write_decision(
                                                        bar_index=_bar_count,
                                                        bar_ts=datetime.now(timezone.utc).isoformat(),
                                                        underlying=_srec.underlying_symbol,
                                                        bias=str(_mctx.bias),
                                                        conf_raw=_mctx.confidence,
                                                        conf_eff=_effective_conf,
                                                        outcome="REAL_ENTRY_NOT_FILLED",
                                                        filter_reason=_miss,
                                                        equity=_equity,
                                                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                                    )
                                                    _db.write_runtime_event(
                                                        "REAL_ENTRY_NOT_FILLED",
                                                        severity="WARN",
                                                        message=f"{_srec.option_symbol} status={_miss}",
                                                        equity=_equity,
                                                    )
                                                except Exception:
                                                    pass
                            else:
                                print_warning_block(
                                    "CASH BLOCK",
                                    bar_index=_bar_count,
                                    need=f"₹{_cost:,.2f}",
                                    available=f"₹{_avail:,.2f}",
                                    equity=f"₹{_equity:,.0f}",
                                )
                                try:
                                    _db.write_decision(
                                        bar_index=_bar_count,
                                        bar_ts=datetime.now(timezone.utc).isoformat(),
                                        underlying=_srec.underlying_symbol,
                                        bias=str(_mctx.bias),
                                        conf_raw=_mctx.confidence,
                                        conf_eff=_effective_conf,
                                        outcome="CASH_BLOCK",
                                        filter_reason=f"need=₹{_cost:,.0f} available=₹{_avail:,.0f}",
                                        equity=_equity,
                                        drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                    )
                                except Exception:
                                    pass
                        elif not _sizing.tradable:
                            print_warning_block(
                                "SIZING REJECTED",
                                bar_index=_bar_count,
                                tier=f"{'SOFT' if _soft_halted else _effective_tier}",
                                equity=f"₹{_equity:,.0f}",
                                reason=_sizing.rejection_reason or "",
                            )
                            try:
                                _db.write_decision(
                                    bar_index=_bar_count,
                                    bar_ts=datetime.now(timezone.utc).isoformat(),
                                    underlying=_srec.underlying_symbol,
                                    bias=str(_mctx.bias),
                                    conf_raw=_mctx.confidence,
                                    conf_eff=_effective_conf,
                                    outcome="SIZING_REJECTED",
                                    filter_reason=_sizing.rejection_reason,
                                    equity=_equity,
                                    drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                )
                            except Exception:
                                pass

        if result.emitted_events:
            if _TERMINAL_COMPACT:
                _strongest = max(
                    result.emitted_events,
                    key=lambda e: getattr(e, "strength", 0.0),
                    default=None,
                )
                _s_type = (
                    (_strongest.event_type.value
                     if hasattr(_strongest.event_type, "value")
                     else str(_strongest.event_type))
                    if _strongest is not None else "?"
                )
                print(
                    f"[STRUCTURE] {completed_bar.symbol} "
                    f"count={len(result.emitted_events)} "
                    f"strongest={_s_type}"
                )
            else:
                print(
                    "\nSTRUCTURE EVENTS:\n"
                )
                for event in (
                    result.emitted_events
                ):
                    print(event)
        else:
            if not _TERMINAL_COMPACT:
                print(
                    "\nNo structure events."
                )


# ── Validate Kite session before opening WebSocket ────────────────────────────
_kite_session_valid, _kite_session_error = adapter.check_session()
if not _kite_session_valid:
    print(
        f"\n{'='*60}\n"
        f"[KITE SESSION ERROR] Access token is invalid or expired.\n"
        f"  Details: {_kite_session_error}\n"
        f"  Action: run `python scripts/generate_kite_session.py` and restart.\n"
        f"  Options pipeline DISABLED — tick stream and structure analysis active.\n"
        f"{'='*60}\n"
    )
else:
    print("[KITE SESSION] Token valid — options pipeline enabled.")

# Startup position reconciliation (real_armed only) — confirm the broker's state
# matches local/restored state before the first live bar is processed.
_do_position_reconcile("startup")

# ── on_ticks crash guard ─────────────────────────────────────────────────────
# The WS tick callback runs the ENTIRE decision pipeline. Wrap it so any uncaught
# code exception (bad tick/bar, or a defect anywhere in structure/options/sizing/
# shadow paths) is logged loudly and the bar batch is skipped — the WS thread and
# pipeline keep running instead of dying mid-session. This is a try/except ONLY
# (never a lock — locks across on_ticks deadlocked before), so it cannot block or
# deadlock. Internet/feed failures are handled separately by the reconnect logic;
# this covers CODE/technical failures so a single bad bar can't stop the model.
_on_ticks_guard_errors = 0


def _on_ticks_guarded(raw_ticks):
    global _on_ticks_guard_errors
    try:
        on_ticks(raw_ticks)
    except Exception as _exc:
        _on_ticks_guard_errors += 1
        import traceback as _tb
        print(
            f"\n[ON_TICKS GUARD] caught {type(_exc).__name__}: {_exc} "
            f"(#{_on_ticks_guard_errors}) — bar batch skipped, pipeline continues."
        )
        _tb.print_exc()
        try:
            _db.write_runtime_event(
                "ON_TICKS_EXCEPTION",
                severity="ERROR",
                message=f"{type(_exc).__name__}: {_exc} (#{_on_ticks_guard_errors})",
                equity=_equity,
                bar_index=_bar_count,
            )
        except Exception:
            pass


adapter.add_tick_callback(_on_ticks_guarded)

adapter.connect(
    instrument_tokens=(
        list(INSTRUMENTS.values())
        # index FUTURES for order flow (07-14): real volume/depth; ticks are intercepted
        # in on_ticks and never reach the bar/structure path.
        + list(_FUT_TOKEN_TO_SYMBOL.keys())
    )
)

print(
    "\nLIVE STRUCTURE PIPELINE ACTIVE\n"
)

import time

# ── Tick-staleness watchdog config ────────────────────────────────────────────
# A silent WebSocket stall (socket open, no data) does not fire on_close, so the
# adapter's reconnect machinery never triggers.  Proven defect 2026-06-15: the
# feed went quiet at 13:53 IST and the pipeline sat idle for the rest of the
# session.  This watchdog runs on the main thread (independent of ticks), and
# during market hours forces a reconnect when no tick has arrived for
# LIVE_TICK_STALE_SECONDS.  Transparent: every action is logged loudly.
_SESSION_OPEN_TOD:    int   = 9 * 60 + 15   # 09:15 IST
_TICK_STALE_SECONDS:  float = float(_os.environ.get("LIVE_TICK_STALE_SECONDS", "120") or 120)
_STALL_ACTION_COOLDOWN: float = 60.0        # min seconds between forced reconnects
_last_stall_action_ts: float = 0.0
# Host-suspend detection: this loop sleeps 1 s per tick, so a wall-clock jump
# far larger than that means the process did not run — the host was suspended
# (laptop sleep / WSL pause). Proven 2026-06-23: a ~2.8 h suspend froze the feed
# mid-session with no warning. We flag the gap loudly, record it, and force a
# clean reconnect on resume so a silent data gap can never pass unnoticed.
_SUSPEND_GAP_SECONDS: float = 90.0
_last_loop_wall:      float = time.time()

while True:
    # Main-loop crash guard: this is the wall-clock SAFETY thread (15:30 EOD
    # force-close + suspend/feed watchdogs) and must survive any transient code
    # fault — if it died, the guaranteed EOD close would be stranded (real-money
    # position carried overnight). try/except ONLY (no lock). SystemExit (the
    # intentional shutdown via sys.exit) is re-raised so shutdown still works.
    try:
        time.sleep(1)
        _loop_gap = time.time() - _last_loop_wall
        _last_loop_wall = time.time()
        if _loop_gap > _SUSPEND_GAP_SECONDS:
            _resumed_ist = datetime.now(_IST)
            print(
                f"\n{'='*60}\n"
                f"[HOST SUSPEND DETECTED] Main loop stalled {_loop_gap:.0f}s "
                f"(probable machine sleep) — feed + bar stream have a DATA GAP.\n"
                f"  resumed_at={_resumed_ist:%Y-%m-%d %H:%M:%S} IST — forcing reconnect.\n"
                f"{'='*60}"
            )
            try:
                _db.write_runtime_event(
                    "HOST_SUSPEND_GAP",
                    severity="CRITICAL",
                    message=(f"main loop stalled {_loop_gap:.0f}s (probable host sleep); "
                             f"data gap; forcing reconnect"),
                    equity=_equity,
                    bar_index=_bar_count,
                )
            except Exception:
                pass
            try:
                adapter.force_reconnect(f"host suspend gap {_loop_gap:.0f}s")
            except Exception as _susp_exc:
                print(f"[HOST SUSPEND] force_reconnect failed: {_susp_exc}")
            _last_stall_action_ts = time.time()
        if _shutdown_triggered:
            sys.exit(0)
        _now_ist = datetime.now(_IST)
        _tod_min = _now_ist.hour * 60 + _now_ist.minute
        if _tod_min >= _SESSION_CLOSE_TOD:
            # Guard the EOD close: if it errors, log and retry next tick rather
            # than letting the main loop die (which would strand the close).
            try:
                _handle_market_close_shutdown("main-loop 15:30 IST clock")
            except Exception as _eod_exc:
                print(f"[MARKET CLOSE] shutdown error: {_eod_exc} — will retry next tick")
            continue

        # Tick-staleness watchdog — market hours only (avoids pre-open/post-close
        # false positives when the feed is legitimately quiet).
        if _SESSION_OPEN_TOD <= _tod_min < _SESSION_CLOSE_TOD:
            _tick_gap = adapter.seconds_since_last_tick()
            if (_tick_gap > _TICK_STALE_SECONDS
                    and (time.time() - _last_stall_action_ts) > _STALL_ACTION_COOLDOWN):
                _open_now = sum(
                    1 for _s in _runtime_by_underlying.values()
                    if _s.option_trader.has_open_position()
                )
                _risk_note = (
                    "  *** OPEN POSITION UNMANAGED WHILE FEED STALLED ***"
                    if _open_now else ""
                )
                print(
                    f"\n{'='*60}\n"
                    f"[FEED STALL] No ticks for {_tick_gap:.0f}s "
                    f"(threshold {_TICK_STALE_SECONDS:.0f}s) during market hours — "
                    f"forcing WebSocket reconnect.\n"
                    f"  open_positions={_open_now}{_risk_note}\n"
                    f"{'='*60}"
                )
                try:
                    adapter.force_reconnect(f"no ticks for {_tick_gap:.0f}s")
                except Exception as _stall_exc:
                    print(f"[FEED STALL] force_reconnect failed: {_stall_exc}")
                try:
                    _db.write_runtime_event(
                        "FEED_STALL",
                        severity="WARNING",
                        message=f"no ticks for {_tick_gap:.0f}s; forced reconnect; open_positions={_open_now}",
                        equity=_equity,
                        bar_index=_bar_count,
                    )
                except Exception:
                    pass
                _last_stall_action_ts = time.time()
    except SystemExit:
        raise
    except Exception as _loop_exc:
        print(f"[MAIN LOOP GUARD] caught {type(_loop_exc).__name__}: {_loop_exc} — continuing")
        try:
            _db.write_runtime_event(
                "MAIN_LOOP_EXCEPTION",
                severity="ERROR",
                message=f"{type(_loop_exc).__name__}: {_loop_exc}",
                equity=_equity,
                bar_index=_bar_count,
            )
        except Exception:
            pass
        time.sleep(1)

