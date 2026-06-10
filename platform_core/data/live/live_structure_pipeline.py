import atexit
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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
# _OPTION_TARGETS_ENABLED   — all underlyings currently executing paper trades.
#                             BANKNIFTY and NIFTY trade on NFO; SENSEX on BFO.
# _OPTION_TARGETS_SHADOW    — empty; shadow infrastructure retained for future use.
# _OPTION_TARGETS_AVAILABLE — all underlyings with complete option-chain config.

_PRIMARY_OPTION_TARGET:    str       = "BANKNIFTY"
_OPTION_TARGETS_ENABLED:   frozenset = frozenset({"BANKNIFTY", "NIFTY", "SENSEX"})
_OPTION_TARGETS_SHADOW:    frozenset = frozenset()  # no active shadow targets
_OPTION_TARGETS_AVAILABLE: frozenset = frozenset({"BANKNIFTY", "NIFTY", "SENSEX"})
# Raw index/feed names that must never appear as a contract symbol
_RAW_INDEX_SYMBOLS:        frozenset = frozenset({"BANKNIFTY", "NIFTY", "NIFTY50", "SENSEX", "FINNIFTY"})
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
    live_bars_seen:   int   = 0                 # live 1m bars received this session
    bar_history:      dict = field(default_factory=dict)  # {tf: [bar_dict, ...]}
    bars_since_save:  int  = 0                  # bars since last state-file write
    fvg_quality:      float = 1.0
    fvg_quality_bar:  int   = 0                 # set to -FVG_QUALITY_STALENESS_BARS-1
    sizers:           dict  = field(default_factory=dict)  # {0..3: PositionSizer}

    # ── Per-bar captures — reset to None each bar ──────────────────────────
    captured_market_ctx:   object | None = None
    captured_strike_rec:   object | None = None
    captured_score_margin: float  | None = None  # rank1 - rank2 composite score

    # ── MCB adaptation watch ───────────────────────────────────────────────
    contra_bos_count:  int = 0   # BOS events opposing current MCB bias (session total)


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
# Configurable via PAPER_STARTING_CAPITAL env var (default ₹10,00,000).
# For real account paper runs: PAPER_STARTING_CAPITAL=300000
# At < ₹3,00,000 the system sizes to 1 lot per trade and caps simultaneous
# open positions to 2 to keep portfolio heat under 10%.
# At < ₹5,00,000 open positions are capped at 2; otherwise the default is 3.
_INITIAL_EQUITY: float = float(os.environ.get("PAPER_STARTING_CAPITAL", "1000000"))

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
_TIER1_MAX_LOTS:  int   = 1
_TIER2_MAX_LOTS:  int   = 3
_TIER3_MAX_LOTS:  int   = 5
_TIER1_MIN_CONF:  float = 0.55    # higher conviction bar in defensive mode
_TIER2_MIN_CONF:  float = 0.40
_TIER3_MIN_CONF:  float = 0.35    # tier 3 eligibility enforced at tier selection

# Tier transition thresholds (deterministic, evaluated per bar)
_TIER1_DD_ENTRY:  float = 0.05    # enter Tier 1 when drawdown exceeds 5%
_TIER3_MIN_GAIN:  float = 0.03    # Tier 3 requires equity > initial × 1.03
_TIER3_MAX_DD:    float = 0.025   # Tier 3 ineligible when drawdown ≥ 2.5%
_TIER3_CONF_GATE: float = 0.65    # Tier 3 requires confidence ≥ 65%

# Portfolio heat and trade management
_MAX_HEAT_FRACTION: float = 0.10   # max open-premium / equity
# Stop-loss: max 30% loss from entry premium (premium must remain >= 70% of entry).
# Tighter than the previous 50%-remaining threshold; still subject to bar-close gap risk.
_MAX_OPTION_PREMIUM_LOSS_FRACTION: float = 0.30   # stop at 30% loss from entry
_STOP_LOSS_PCT:     float = 1.0 - _MAX_OPTION_PREMIUM_LOSS_FRACTION   # 0.70 remaining
_TAKE_PROFIT_PCT:   float = 2.00
_MIN_DTE_TO_HOLD:   int   = 2

# Stop-loss cooldown: prevent immediate re-entry after a loss exit.
# Proven defect: SENSEX re-entered the same contract at the exact bar of stop-loss exit.
_SAME_UNDERLYING_STOP_COOLDOWN_BARS: int = 5    # any contract on this underlying
_SAME_CONTRACT_STOP_COOLDOWN_BARS:   int = 15   # exact same contract symbol

# Confluence gate for Tier2/Tier3: require minimum OI support on the selected strike.
# Proven defect: SENSEX entry had oi_support=0.034 (near-zero OI = liquidity risk).
_MIN_OI_SUPPORT_TIER23: float = 0.10

# Live bar warmup gate: require at least this many live bars before allowing entry.
# Prevents entries driven purely by warm-start historical MCB state (no live confirmation).
_LIVE_BARS_BEFORE_ENTRY: int = 20

# Strike quality gate for Tier2/3: reject low-quality strikes that passed OI filter.
# Proven defect (2026-06-04): SENSEX iv_attr=0.000 and all entries SCORE_MARGIN_LOW.
_MIN_SCORE_MARGIN_TIER23: float = 0.05   # min rank-1 minus rank-2 score gap
_MIN_IV_ATTR_TIER23:      float = 0.05   # min IV attractiveness factor score

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
# Soft halt — defensive mode, not a full stop
_SOFT_HALT_DAILY_LOSS_FRAC: float = 0.020   # soft-halt trigger: daily loss >= 2%
_SOFT_HALT_RISK_FRAC:       float = 0.007   # 0.7% risk — below Tier 1's 1%
_SOFT_HALT_MAX_LOTS:        int   = 1
_SOFT_HALT_MIN_CONF:        float = 0.65    # higher bar than Tier 1's 0.55
# Hard halt — no new trades
_HARD_HALT_DAILY_LOSS_FRAC: float = 0.040   # hard-halt trigger: daily loss >= 4%
_HARD_HALT_CONSEC_LOSSES:   int   = 3       # hard-halt trigger: 3 consecutive losses
_MAX_ACCOUNT_DD:            float = 0.150   # hard-halt trigger: account drawdown >= 15%

# ── Per-underlying PositionSizer sets ────────────────────────────────────────
# Each underlying gets independent instances so sizer internal tracking
# (_last_underlying_symbol, _processed_requests) never bleeds across symbols.
for _sizer_init_state in _runtime_by_underlying.values():
    _sizer_init_state.sizers = {
        0: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_SOFT_HALT_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
        )),
        1: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER1_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
        )),
        2: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER2_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
        )),
        3: PositionSizer(config=PositionSizingConfig(
            max_risk_fraction_per_trade=_TIER3_RISK_FRAC,
            max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
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
_shadow_last_bar:        dict        = {}     # {underlying: _bar_count} — shadow rate-limit
_shadow_counters:        dict        = {}     # {underlying: {attempts,success,no_candidate,failures,last_symbol,last_score}}
_SHADOW_SUMMARY_EVERY:   int         = 10     # print [SHADOW SUMMARY] every N attempts per underlying

# Operational state
_STATE_FILE:         Path = Path("logs/paper_trading_state.json")
_PID_FILE:           Path = Path("logs/live_pipeline.pid")
_SHADOW_AUDIT_PATH:  Path = Path("logs/strike_selection_shadow.jsonl")
_shadow_auditor      = StrikeAuditLogger(audit_path=_SHADOW_AUDIT_PATH, mode="shadow")
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
    }
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(_STATE_FILE)   # atomic on POSIX
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
    if abs(_state_capital - _INITIAL_EQUITY) > 0.5:
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
        if existing_15m >= 20:
            try:
                _ctx = und_state.market_ctx_builder.build()
                print(
                    f"[MCB WARMSTART] {key}: state has {existing_15m} 15m bars — "
                    f"skipping CSV  bias={_ctx.bias}  conf={_ctx.confidence:.2f}"
                )
            except Exception:
                pass
            continue

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

        # Re-feed intraday state bars (newer timestamps accepted on top of CSV).
        _refed = 0
        for _stf, _sbar in _state_bars_to_replay:
            try:
                und_state.market_ctx_builder.update(bar=_sbar, timeframe=_stf)
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
if _TIER2_RISK_FRAC > 0.025:
    print(
        f"[WARNING] Paper risk profile '{_PAPER_RISK_PROFILE}' uses "
        f"{_TIER2_RISK_FRAC * 100:.1f}% Tier2 risk — not approved for real funds."
    )
_print_gate_startup()  # print gate profile + per-underlying thresholds at every startup
_load_state()         # restore from disk at process startup
_warm_start_mcb_from_csv()   # pre-seed MCB from historical CSV — eliminates cold-start NEUTRAL lock

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

# Execution router — mode is paper by default; real_dry_run and real_armed are
# opt-in via EXECUTION_MODE env var.  Router is always created; paper mode is a no-op.
_exec_router = _ExecutionRouter(mode=_EXECUTION_MODE)
_exec_router.startup_print()
_session_tag: str = _session_ts if "_session_ts" in dir() else "live"


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
    if _consecutive_losses >= _HARD_HALT_CONSEC_LOSSES:
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
                confidence_score=min(effective_conf, 1.0),
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
            expiry = adapter.nearest_expiry(
                state.option_underlying, bar_date, exchange=state.exchange,
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

        # Best-effort: add next-nearest expiry for DTE ranking (mirrors BANKNIFTY path)
        try:
            expiry_next = adapter.nearest_expiry(
                state.option_underlying, expiry + timedelta(days=1),
                exchange=state.exchange,
            )
            _chain_snapshots.append(adapter.fetch_option_chain(
                underlying=state.option_underlying,
                expiry_date=expiry_next,
                spot_price=spot,
                lot_size=state.lot_size,
                strike_step=state.strike_step,
                exchange=state.exchange,
            ))
        except Exception:
            pass  # next expiry unavailable — proceed with nearest only

        options_ctx = _opt_ctx_builder.build(
            snapshots=_chain_snapshots,
            spot_price=spot,
            lot_size=state.lot_size,
            valuation_timestamp=datetime.now(timezone.utc),
        )

        if options_ctx.atm_iv:
            state.last_atm_iv = options_ctx.atm_iv

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
            atm_iv=options_ctx.atm_iv,
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

        _closed = und_state.option_trader.evaluate_and_close(
            current_spot=spot,
            current_iv=iv,
            current_date=datetime.now(timezone.utc).date(),
            bar_index=_bar_count,
            stop_loss_pct=_STOP_LOSS_PCT,
            take_profit_pct=_TAKE_PROFIT_PCT,
            min_dte=_MIN_DTE_TO_HOLD,
            force_reason="EOD_FORCE_CLOSE",
        )

        if _closed is None:
            # Should not happen since force_reason is set, but guard defensively
            print(f"[EOD CLOSE WARN] {key}: evaluate_and_close returned None unexpectedly")
            continue

        _equity               += _closed.pnl
        _peak_equity           = max(_peak_equity, _equity)
        _open_premium_cost     = _compute_open_premium_cost()
        _daily_pnl            += _closed.pnl
        if _closed.pnl < 0:
            _consecutive_losses += 1
        else:
            _consecutive_losses = 0

        print(
            f"[EOD CLOSE] {key}: {_closed.contract_symbol}  "
            f"exit=₹{_closed.exit_premium:.2f}  "
            f"pnl=₹{_closed.pnl:+,.2f}  lots={_closed.lots}  "
            f"entry=₹{_closed.entry_premium:.2f}  "
            f"EOD_CLOSE_PRICE_SOURCE=BSM_FALLBACK"
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
        # Execution router exit for EOD force-close
        try:
            _exec_router.route_exit(
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

        tick = mapper.map_tick(raw_tick)

        completed_bar = (
            bar_builder.update_tick(
                tick=tick
            )
        )

        if completed_bar is None:
            continue

        # Market-close guard — stop processing bars after 15:30 IST
        _bar_ist = completed_bar.start_timestamp.astimezone(_IST)
        if _bar_ist.hour * 60 + _bar_ist.minute >= _SESSION_CLOSE_TOD and not _shutdown_triggered:
            _handle_market_close_shutdown("bar-level 15:30 IST detection")
            return   # main loop will see _shutdown_triggered and call sys.exit(0)

        _bar_count += 1

        # Reset per-bar captures for all enabled targets
        for _st in _runtime_by_underlying.values():
            if is_option_target_enabled(_st.option_underlying):
                _st.captured_strike_rec    = None
                _st.captured_market_ctx    = None
                _st.captured_score_margin  = None

        # Resolve state for this bar's feed symbol (None = unknown feed)
        _current_state = _state_by_feed.get(completed_bar.symbol)

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

            # ── Chain fetch + strike selection — enabled targets (full execution) ──
            if is_option_target_enabled(_current_state.option_underlying):
                print(
                    f"\n[OPTIONS] bias={market_ctx.bias}  "
                    f"conf={market_ctx.confidence:.2f}"
                )
                _current_state.captured_market_ctx = market_ctx

                if str(market_ctx.bias) != "NEUTRAL" and _kite_session_valid:
                    try:
                        spot = completed_bar.close_price
                        bar_date = datetime.now(timezone.utc).date()
                        expiry = adapter.nearest_expiry(
                            _current_state.option_underlying, bar_date,
                            exchange=_current_state.exchange,
                        )
    
                        live_chain = adapter.fetch_option_chain(
                            underlying=_current_state.option_underlying,
                            expiry_date=expiry,
                            spot_price=spot,
                            lot_size=_current_state.lot_size,
                            strike_step=_current_state.strike_step,
                            exchange=_current_state.exchange,
                        )
    
                        # Fetch next-nearest expiry (mirrors BacktestRunner weekly+biweekly).
                        # Enables cross-expiry DTE ranking and prevents expiry-day blackout
                        # when the nearest contract has DTE < dte_min_viable.
                        _chain_snapshots = [live_chain]
                        try:
                            expiry_next = adapter.nearest_expiry(
                                _current_state.option_underlying,
                                expiry + timedelta(days=1),
                                exchange=_current_state.exchange,
                            )
                            live_chain_next = adapter.fetch_option_chain(
                                underlying=_current_state.option_underlying,
                                expiry_date=expiry_next,
                                spot_price=spot,
                                lot_size=_current_state.lot_size,
                                strike_step=_current_state.strike_step,
                                exchange=_current_state.exchange,
                            )
                            _chain_snapshots.append(live_chain_next)
                        except Exception:
                            pass  # next expiry unavailable — proceed with nearest only
    
                        options_ctx = _opt_ctx_builder.build(
                            snapshots=_chain_snapshots,
                            spot_price=spot,
                            lot_size=_current_state.lot_size,
                            valuation_timestamp=datetime.now(timezone.utc),
                        )
    
                        if options_ctx.atm_iv:
                            _current_state.last_atm_iv = options_ctx.atm_iv
    
                        if options_ctx.strike_candidates:
                            _candidates_list = list(options_ctx.strike_candidates)
                            _selection_result = _strike_engine.select(
                                bias=market_ctx.bias,
                                candidates=_candidates_list,
                                valuation_timestamp=datetime.now(timezone.utc),
                                oi_snapshot=options_ctx.oi_snapshot,
                                gex_snapshot=options_ctx.gex_snapshot,
                                atm_iv=options_ctx.atm_iv,
                            )
                            best = _selection_result.best()
                            if best is not None:
                                _current_state.captured_strike_rec = best
                                _recs = _selection_result.recommendations
                                _current_state.captured_score_margin = (
                                    _recs[0].composite_score - _recs[1].composite_score
                                    if len(_recs) >= 2 else None
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
                _closed = _current_state.option_trader.evaluate_and_close(
                    current_spot=completed_bar.close_price,
                    current_iv=_current_state.last_atm_iv,
                    current_date=_bar_date_live,
                    bar_index=_bar_count,
                    stop_loss_pct=_STOP_LOSS_PCT,
                    take_profit_pct=_TAKE_PROFIT_PCT,
                    min_dte=_MIN_DTE_TO_HOLD,
                    force_reason=_exit_sig,
                )
                if _closed is not None:
                    _equity += _closed.pnl
                    _peak_equity = max(_peak_equity, _equity)
                    _open_premium_cost = _compute_open_premium_cost()
                    _daily_pnl += _closed.pnl
                    if _closed.pnl < 0:
                        _consecutive_losses += 1
                    else:
                        _consecutive_losses = 0
                    # Record stop-loss cooldown state (prevents immediate re-entry)
                    if "STOP_LOSS" in str(_closed.exit_reason):
                        _current_state.last_stop_bar      = _bar_count
                        _current_state.last_stop_contract = _closed.contract_symbol
                    print_exit_block(
                        underlying=_closed.underlying_symbol,
                        strike=_closed.strike,
                        option_type=_closed.option_type,
                        direction=_closed.direction,
                        lots=_closed.lots,
                        lot_size=_closed.lot_size,
                        entry_premium=_closed.entry_premium,
                        exit_premium=_closed.exit_premium,
                        pnl=_closed.pnl,
                        bars_held=_closed.bars_held,
                        exit_reason=str(_closed.exit_reason),
                        equity=_equity,
                    )
                    try:
                        _db.write_trade_exit(
                            underlying=_current_state.option_underlying,
                            exit_premium=_closed.exit_premium,
                            pnl=_closed.pnl,
                            bars_held=_closed.bars_held,
                            exit_reason=str(_closed.exit_reason),
                            exit_ts=datetime.now(timezone.utc).isoformat(),
                            equity_after=_equity,
                        )
                    except Exception:
                        pass
                    # Execution router exit — paper=no-op, dry-run/real=log exit intent
                    try:
                        _exec_router.route_exit(
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

            if _pdl_reason or _or_reason or _fvg_quality_reason or not _session_ctx.allow_entry:
                _filter_parts = []
                if not _session_ctx.allow_entry:
                    _filter_parts.append(f"session={_session_ctx.phase}")
                if _pdl_reason:
                    _filter_parts.append(_pdl_reason)
                if _or_reason:
                    _filter_parts.append(_or_reason)
                if _fvg_quality_reason:
                    _filter_parts.append(_fvg_quality_reason)
                print_warning_block(
                    "ENTRY FILTER",
                    bar_index=_bar_count,
                    conf=f"{_mctx.confidence:.2f}→{_effective_conf:.2f}",
                    reason="  |  ".join(_filter_parts),
                )

            # ── Entry gate: fresh strike, no open position for THIS underlying, not hard-halted ─
            _srec = _current_state.captured_strike_rec
            if (
                _srec is not None
                and not _current_state.option_trader.has_open_position()
                and not _trading_halted
                and _session_ctx.allow_entry
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

                    # ── Pre-entry safety checks [A] warmup  [B] cooldown ─────
                    _pre_entry_block: str | None = None
                    _bars_since_stop = _bar_count - _current_state.last_stop_bar

                    # [A0] Capital-tier simultaneous position limit.
                    # Prevents portfolio heat from breaching 10% at small capital.
                    _cur_open = sum(
                        1 for _s in _runtime_by_underlying.values()
                        if _s.option_trader.has_open_position()
                    )
                    if _cur_open >= _MAX_OPEN_POSITIONS:
                        _pre_entry_block = (
                            f"CAPITAL_TIER_POSITION_LIMIT "
                            f"open_positions={_cur_open}/{_MAX_OPEN_POSITIONS} "
                            f"capital=₹{_session_opening_equity:,.0f}"
                        )

                    # [A] Live bar warmup gate
                    if _current_state.live_bars_seen < _LIVE_BARS_BEFORE_ENTRY:
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

                    # [B3] Confluence hard floor (Tier2/3): OI support is a HARD
                    # gate and must reject BEFORE sizing / order-intent creation.
                    # No gate profile may relax it below the 0.10 floor.
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
                                        if _sm_val is not None and _sm_val < _eff_sm_min:
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
                                            print(
                                                f"[ENTRY HARD GATES] symbol={_srec.option_symbol}"
                                                f" contradiction=False"
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
                                            # ── Open position ─────────────────
                                            _open_premium_cost += _cost
                                            _current_state.option_trader.open(
                                                recommendation=_srec,
                                                direction=str(_mctx.bias),
                                                bar_index=_bar_count,
                                                lots=_lots,
                                            )
                                        _tier_label = "SOFT" if _soft_halted else str(_effective_tier)
                                        if _entry_ok:
                                            # ── Entry print + DB write ─────────
                                            print_entry_block(
                                                direction=str(_mctx.bias),
                                                underlying=_srec.underlying_symbol,
                                                strike=_srec.strike_price,
                                                option_type=_srec.option_type,
                                                expiry=_srec.expiry_date,
                                                lots=_lots,
                                                lot_size=_srec.lot_size,
                                                premium=_srec.last_traded_price,
                                                notional=_cost,
                                                bar_index=_bar_count,
                                                session=str(_session_ctx.phase),
                                                conf_raw=_mctx.confidence,
                                                conf_eff=_effective_conf,
                                                fvg_quality=_current_state.fvg_quality,
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
                                                    entry_premium=_srec.last_traded_price,
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
                                            # Execution router — paper=no-op, dry-run/real=log order intent
                                            try:
                                                _exec_router.route_entry(
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
    if _shutdown_triggered:
        sys.exit(0)
    _now_ist = datetime.now(_IST)
    if _now_ist.hour * 60 + _now_ist.minute >= _SESSION_CLOSE_TOD:
        _handle_market_close_shutdown("main-loop 15:30 IST clock")

