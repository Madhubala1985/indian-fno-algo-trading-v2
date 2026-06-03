import atexit
import json
import os
import sys
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
from platform_core.options.strike_selection_audit import log_selection as _audit_selection
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
# _PRIMARY_OPTION_TARGET   — the single active BANKNIFTY execution target (unchanged).
# _OPTION_TARGETS_ENABLED  — set of underlyings currently allowed to enter positions.
#                            Contains only BANKNIFTY until Phase B2 validation passes.
# _OPTION_TARGETS_AVAILABLE — all underlyings with complete option-chain config.
#
# NIFTY and SENSEX are PREPARED but DISABLED.
# Before enabling either, the following must pass:
#   1. validate_option_chain_support.py PASS for the target
#   2. BacktestRunner(symbol=target).run() produces valid trades on historical data
#   3. Per-underlying state containers (MCB, OptionPaperTrader, DailyBarAccumulator,
#      PDL, OR, FVG engines, last_atm_iv) must be instantiated and wired
#   4. DB/state persistence extended to open_positions dict (not single open_position)
#   5. KiteAdapter BFO cache proven live for SENSEX (market-hours test required)

_PRIMARY_OPTION_TARGET:    str       = "BANKNIFTY"
_OPTION_TARGETS_ENABLED:   frozenset = frozenset({"BANKNIFTY"})
_OPTION_TARGETS_AVAILABLE: frozenset = frozenset({"BANKNIFTY", "NIFTY", "SENSEX"})
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


# MTF key normalisation (matches BacktestRunner convention)
_MTF_KEY_MAP: dict[str, str] = {
    "MIN_5":  "5m",
    "MIN_15": "15m",
    "HOUR_1": "1h",
    "HOUR_4": "4h",
}

_market_ctx_builder = MarketContextBuilder(underlying_symbol=_PRIMARY_OPTION_TARGET, min_history_bars=20)
_opt_ctx_builder = OptionsContextBuilder(
    config=OptionsContextBuilderConfig(compute_missing_greeks=True)
)
# top_n=5 so the audit logger has rank-2..5 margin and top-candidate analysis
_strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))
# ── Session / PDH-PDL / Opening-Range / FVG-quality filters ──────────────────
# Identical instances and configuration to BacktestRunner.
_session_engine   = SessionContextEngine()
_pdl_engine       = PreviousDayLevelsEngine()
_or_engine        = OpeningRangeEngine()
_fvg_scorer       = FVGQualityScorer()
_fvg_15m = FVGEngine(config=FVGEngineConfig(instrument_id=_PRIMARY_OPTION_TARGET, timeframe="MIN_15"))
_fvg_1h  = FVGEngine(config=FVGEngineConfig(instrument_id=_PRIMARY_OPTION_TARGET, timeframe="HOUR_1"))
_fvg_quality:     float = 1.0
_fvg_quality_bar: int   = -FVG_QUALITY_STALENESS_BARS - 1
_daily_acc = DailyBarAccumulator(symbol=_PRIMARY_OPTION_TARGET)

# ── Virtual account — ₹10,00,000 capital model ───────────────────────────────
# Set to match BacktestConfig.initial_equity (₹10,00,000) so live paper trading
# uses the same sizing calibration as the validated 440-trade baseline.
# At ₹1,00,000 the effective risk budget (₹1,006 at Tier 2) is below the
# BANKNIFTY 15-unit lot threshold (≥₹3,000 needed) → INSUFFICIENT_RISK_BUDGET
# on every bar.  See PROJECT_STATUS §34 for full mathematical proof.
_INITIAL_EQUITY: float = 1_000_000.0  # ₹10,00,000 — aligns with backtest baseline
_REFERENCE_IV:   float = 0.18       # baseline IV for vol-ratio scaling

# Risk tier parameters — tier is selected at runtime from account state
# Tier 1 (Defensive): entered when drawdown is elevated
# Tier 2 (Normal):    default operating mode
# Tier 3 (Aggressive): only when equity is above initial, low drawdown, high confidence
_TIER1_RISK_FRAC: float = 0.010   # 1.0% of equity at risk
_TIER2_RISK_FRAC: float = 0.020   # 2.0% of equity at risk
_TIER3_RISK_FRAC: float = 0.035   # 3.5% of equity at risk
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
_MAX_HEAT_FRACTION: float = 0.10  # max open-premium / equity
_STOP_LOSS_PCT:     float = 0.50
_TAKE_PROFIT_PCT:   float = 2.00
_MIN_DTE_TO_HOLD:   int   = 2

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

# ── Per-tier PositionSizer instances (same class, different risk configs) ──────
_option_trader = OptionPaperTrader()
_sizer_t1 = PositionSizer(config=PositionSizingConfig(
    max_risk_fraction_per_trade=_TIER1_RISK_FRAC,
    max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
))
_sizer_t2 = PositionSizer(config=PositionSizingConfig(
    max_risk_fraction_per_trade=_TIER2_RISK_FRAC,
    max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
))
_sizer_t3 = PositionSizer(config=PositionSizingConfig(
    max_risk_fraction_per_trade=_TIER3_RISK_FRAC,
    max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
))
_sizer_soft = PositionSizer(config=PositionSizingConfig(
    max_risk_fraction_per_trade=_SOFT_HALT_RISK_FRAC,
    max_portfolio_heat_fraction=_MAX_HEAT_FRACTION,
))

# ── Mutable account state ──────────────────────────────────────────────────────
_equity:            float = _INITIAL_EQUITY
_peak_equity:       float = _INITIAL_EQUITY
_open_premium_cost: float = 0.0
_bar_count:         int   = 0
_last_atm_iv:       float = _REFERENCE_IV

# Capital protection state
_session_date:           date | None = None
_session_opening_equity: float       = _INITIAL_EQUITY
_daily_pnl:              float       = 0.0
_trading_halted:         bool        = False
_halt_reason:            str         = ""
_soft_halted:            bool        = False
_soft_halt_reason:       str         = ""
_consecutive_losses:     int         = 0

# Operational state
_STATE_FILE:         Path = Path("logs/paper_trading_state.json")
_PID_FILE:           Path = Path("logs/live_pipeline.pid")
_kite_session_valid: bool = True   # set False when token expiry is detected
_kite_session_error: str  = ""

# Persistence layer — fire-and-forget, never affects trading logic
_DB_PATH: Path = Path("logs/trades.db")
_db: TradeDatabase = TradeDatabase(str(_DB_PATH))

# MCB warm-start: recent bar history persisted for post-restart rehydration
_MCB_HISTORY_BARS:   int  = 30   # bars per timeframe kept (> min_history_bars=20)
_MCB_SAVE_INTERVAL:  int  = 15   # write state every N BANKNIFTY 1-min bars

_bar_history: dict          = {}  # {tf: [bar_dict, ...]} — in-place mutated, no global needed
_bars_since_history_save: int = 0


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
    pos = _option_trader._position
    state = {
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
        "last_atm_iv":           _last_atm_iv,
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
        "bar_history": _bar_history,
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
    global _session_date, _session_opening_equity, _last_atm_iv
    global _open_premium_cost

    if not _STATE_FILE.exists():
        print(f"[STATE] No state file — starting fresh.  equity=₹{_INITIAL_EQUITY:,.0f}")
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
        iv     = float(s.get("last_atm_iv", _REFERENCE_IV))
        sd     = s.get("session_date")
        sdate  = date.fromisoformat(sd) if sd else None
        # Parse open position if present (optional key — absent in older state files)
        pos_raw  = s.get("open_position")
        pos_data = None
        if pos_raw is not None:
            pos_data = dict(
                underlying_symbol = str(pos_raw["underlying_symbol"]),
                contract_symbol   = str(pos_raw["contract_symbol"]),
                strike            = float(pos_raw["strike"]),
                option_type       = str(pos_raw["option_type"]),
                expiry_date       = date.fromisoformat(str(pos_raw["expiry_date"])),
                lot_size          = int(pos_raw["lot_size"]),
                lots              = int(pos_raw["lots"]),
                entry_premium     = float(pos_raw["entry_premium"]),
                entry_spot        = float(pos_raw["entry_spot"]),
                entry_iv          = float(pos_raw["entry_iv"]),
                direction         = str(pos_raw["direction"]),
                bar_index         = int(pos_raw["bar_index"]),
            )
    except Exception as exc:
        print(f"[STATE] Restore failed ({exc}) — starting fresh.  equity=₹{_INITIAL_EQUITY:,.0f}")
        return
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
    _last_atm_iv            = iv
    _session_date           = sdate
    print(
        f"[STATE] Restored: equity=₹{_equity:,.0f}  peak=₹{_peak_equity:,.0f}  "
        f"daily_pnl=₹{_daily_pnl:+,.0f}  consec={_consecutive_losses}  "
        f"halted={_trading_halted}  soft={_soft_halted}  session={_session_date}"
    )
    # Restore open position — re-derive _open_premium_cost from position data
    if pos_data is not None:
        _option_trader._position = OptionPosition(**pos_data)
        _open_premium_cost = (
            pos_data["entry_premium"] * pos_data["lot_size"] * pos_data["lots"]
        )
        print(
            f"[STATE] Open position restored: {pos_data['contract_symbol']}  "
            f"lots={pos_data['lots']}  entry=₹{pos_data['entry_premium']:.2f}  "
            f"direction={pos_data['direction']}  expiry={pos_data['expiry_date']}"
        )
        # Re-link the DB rowid so write_trade_exit() can update the correct row.
        # Called before _db.start() — recover_open_trade_row() connects directly.
        _db.recover_open_trade_row(
            contract_symbol=pos_data["contract_symbol"],
            direction=pos_data["direction"],
            entry_premium=pos_data["entry_premium"],
            entry_bar_index=pos_data["bar_index"],
        )
    # Rehydrate MarketContextBuilder from persisted bar history.
    # Parsed and fed separately so a corrupt bar_history never blocks account state restore.
    try:
        hist_raw = s.get("bar_history", {})
        if hist_raw:
            bars_by_tf: dict[str, list[LiveBar]] = {}
            for tf, raw_bars in hist_raw.items():
                bars_by_tf[tf] = [_dict_to_bar(b) for b in raw_bars]
            # Restore in-memory history dict (as dicts, ready for next save)
            _bar_history.clear()
            for tf, raw_bars in hist_raw.items():
                _bar_history[tf] = list(raw_bars)
            # Replay into MCB — each timeframe's classifier is independent
            total = sum(len(v) for v in bars_by_tf.values())
            for tf in sorted(bars_by_tf.keys()):
                for bar in bars_by_tf[tf]:
                    _market_ctx_builder.update(bar=bar, timeframe=tf)
            ctx = _market_ctx_builder.build()
            print(
                f"[STATE] MCB rehydrated: {total} bars across {len(bars_by_tf)} timeframe(s)  "
                f"bias={ctx.bias}  conf={ctx.confidence:.2f}  "
                f"ready={list(ctx.timeframes_with_history)}"
            )
    except Exception as exc:
        print(f"[STATE] MCB warm-start skipped ({exc}) — cold start required.")


_acquire_pid_lock()   # must run before _load_state so state is never shared by two instances
_load_state()         # restore from disk at process startup

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
    """Detect CHoCH/BOS structure reversal that invalidates the open position."""
    pos = trader._position
    if pos is None:
        return None
    for event in emitted_events:
        evt  = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
        side = event.side.value if hasattr(event.side, "value") else str(event.side)
        is_reversal = "CHOCH" in evt or evt == "BOS"
        if pos.direction == "LONG"  and is_reversal and side == "BEARISH":
            return f"STRUCTURE_EXIT ({evt})"
        if pos.direction == "SHORT" and is_reversal and side == "BULLISH":
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
    if (
        _equity >= _INITIAL_EQUITY * (1.0 + _TIER3_MIN_GAIN)
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


def _size_live_position(rec, market_ctx, tier: int, effective_conf: float) -> PositionSizingResult:
    """
    Size position using the tier-appropriate PositionSizer.

    Tier selection determines:
      - which PositionSizer instance (different max_risk_fraction_per_trade)
      - maximum_lot_count ceiling passed to the request
      - max_allowed_drawdown_fraction on the PortfolioRiskSnapshot

    effective_conf is the post-filter confidence (MCB × session × PDL × OR × FVG),
    matching BacktestRunner._size_position which passes effective_confidence to the
    PositionSizingRequest.confidence_score field.
    """
    _tier_sizers  = {0: _sizer_soft, 1: _sizer_t1, 2: _sizer_t2, 3: _sizer_t3}
    _tier_maxlots = {0: _SOFT_HALT_MAX_LOTS, 1: _TIER1_MAX_LOTS, 2: _TIER2_MAX_LOTS, 3: _TIER3_MAX_LOTS}

    sizer    = _tier_sizers[tier]
    max_lots = _tier_maxlots[tier]

    entry_premium = rec.last_traded_price
    stop_premium  = max(entry_premium * _STOP_LOSS_PCT, 0.01)
    drawdown      = max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0))
    available     = max(_equity - _open_premium_cost, 0.0)
    heat          = min(_open_premium_cost / max(_equity, 1.0), 1.0)

    portfolio = PortfolioRiskSnapshot(
        equity=max(_equity, 1.0),
        available_cash=available,
        current_portfolio_heat=heat,
        current_drawdown_fraction=drawdown,
        max_allowed_drawdown_fraction=_MAX_ACCOUNT_DD,
        correlation_exposure_fraction=0.0,
        open_position_count=1 if _option_trader.has_open_position() else 0,
    )

    try:
        return sizer.size_position(
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
                realized_volatility_ratio=max(_last_atm_iv / max(_REFERENCE_IV, 0.001), 0.1),
                liquidity_score=min(rec.breakdown.liquidity, 1.0),
                spread_bps=50.0,
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


def on_ticks(raw_ticks):
    global _equity, _peak_equity, _open_premium_cost, _bar_count, _last_atm_iv
    global _trading_halted, _halt_reason, _daily_pnl, _session_date, _session_opening_equity
    global _soft_halted, _soft_halt_reason, _consecutive_losses
    global _kite_session_valid, _kite_session_error
    global _bars_since_history_save
    global _fvg_quality, _fvg_quality_bar

    for raw_tick in raw_ticks:

        tick = mapper.map_tick(raw_tick)

        completed_bar = (
            bar_builder.update_tick(
                tick=tick
            )
        )

        if completed_bar is None:
            continue

        _bar_count += 1
        _captured_strike_rec = None
        _captured_market_ctx = None

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
        if completed_bar.symbol == _PRIMARY_OPTION_TARGET:
            # Session / Opening-Range / PDH-PDL — same update sequence as BacktestRunner
            _session_ctx = _session_engine.classify(completed_bar.start_timestamp)
            _or_engine.update_bar(completed_bar)
            _daily_bar = _daily_acc.update(completed_bar)
            if _daily_bar is not None:
                _pdl_engine.update_daily_bar(_daily_bar)
                _market_ctx_builder.update(bar=_daily_bar, timeframe="1d")
                _h1d = _bar_history.setdefault("1d", [])
                _h1d.append(_bar_to_dict(_daily_bar))
                if len(_h1d) > _MCB_HISTORY_BARS:
                    del _h1d[:-_MCB_HISTORY_BARS]

            _market_ctx_builder.update(bar=completed_bar, timeframe="1m")
            _h1m = _bar_history.setdefault("1m", [])
            _h1m.append(_bar_to_dict(completed_bar))
            if len(_h1m) > _MCB_HISTORY_BARS:
                del _h1m[:-_MCB_HISTORY_BARS]

            for tf_key, tf_bar in mtf_bars.items():
                tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
                _market_ctx_builder.update(bar=tf_bar, timeframe=tf_name)
                _htf = _bar_history.setdefault(tf_name, [])
                _htf.append(_bar_to_dict(tf_bar))
                if len(_htf) > _MCB_HISTORY_BARS:
                    del _htf[:-_MCB_HISTORY_BARS]
                # Feed completed MTF bars to dedicated FVGEngine instances (HTF confluence)
                if tf_key == "MIN_15":
                    _fvg_15m.process_bar(bar=structure_adapter.convert_bar(live_bar=tf_bar))
                elif tf_key == "HOUR_1":
                    _fvg_1h.process_bar(bar=structure_adapter.convert_bar(live_bar=tf_bar))

            _bars_since_history_save += 1
            if _bars_since_history_save >= _MCB_SAVE_INTERVAL:
                _bars_since_history_save = 0
                _save_state()

            market_ctx = _market_ctx_builder.build(
                valuation_timestamp=datetime.now(timezone.utc)
            )
            print(
                f"\n[OPTIONS] bias={market_ctx.bias}  "
                f"conf={market_ctx.confidence:.2f}"
            )

            _captured_market_ctx = market_ctx

            if str(market_ctx.bias) != "NEUTRAL" and _kite_session_valid:
                try:
                    spot = completed_bar.close_price
                    bar_date = datetime.now(timezone.utc).date()
                    expiry = adapter.nearest_expiry(_PRIMARY_OPTION_TARGET, bar_date)

                    live_chain = adapter.fetch_option_chain(
                        underlying=_PRIMARY_OPTION_TARGET,
                        expiry_date=expiry,
                        spot_price=spot,
                        lot_size=_LOT_SIZE,
                        strike_step=_STRIKE_STEP,
                    )

                    # Fetch next-nearest expiry (mirrors BacktestRunner weekly+biweekly).
                    # Enables cross-expiry DTE ranking and prevents expiry-day blackout
                    # when the nearest contract has DTE < dte_min_viable.
                    _chain_snapshots = [live_chain]
                    try:
                        expiry_next = adapter.nearest_expiry(
                            _PRIMARY_OPTION_TARGET, expiry + timedelta(days=1)
                        )
                        live_chain_next = adapter.fetch_option_chain(
                            underlying=_PRIMARY_OPTION_TARGET,
                            expiry_date=expiry_next,
                            spot_price=spot,
                            lot_size=_LOT_SIZE,
                            strike_step=_STRIKE_STEP,
                        )
                        _chain_snapshots.append(live_chain_next)
                    except Exception:
                        pass  # next expiry unavailable — proceed with nearest only

                    options_ctx = _opt_ctx_builder.build(
                        snapshots=_chain_snapshots,
                        spot_price=spot,
                        lot_size=_LOT_SIZE,
                        valuation_timestamp=datetime.now(timezone.utc),
                    )

                    if options_ctx.atm_iv:
                        _last_atm_iv = options_ctx.atm_iv

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
                            _captured_strike_rec = best
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
                                strike_step=_STRIKE_STEP,
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

        # ── OptionPaperTrader: exit evaluation + entry (BANKNIFTY only) ──────
        if completed_bar.symbol == _PRIMARY_OPTION_TARGET and _captured_market_ctx is not None:
            _bar_date_live = datetime.now(timezone.utc).date()

            # Exit evaluation runs on every bar while a position is open
            if _option_trader.has_open_position():
                _exit_sig = _option_exit_signal(result.emitted_events, _option_trader)
                _closed = _option_trader.evaluate_and_close(
                    current_spot=completed_bar.close_price,
                    current_iv=_last_atm_iv,
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
                    _open_premium_cost = 0.0
                    _daily_pnl += _closed.pnl
                    if _closed.pnl < 0:
                        _consecutive_losses += 1
                    else:
                        _consecutive_losses = 0
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
                            exit_premium=_closed.exit_premium,
                            pnl=_closed.pnl,
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
                    _summary = performance_tracker.calculate_summary(
                        trades=[t.as_paper_trade() for t in _option_trader.completed_trades]
                    )
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
                ENGINES[_PRIMARY_OPTION_TARGET]._liquidity_sweep_detector.recent_sweeps
            )
            _htf_fvgs_live = {
                "MIN_15": _fvg_15m.active_gaps,
                "HOUR_1": _fvg_1h.active_gaps,
            }

            _effective_conf = (
                _captured_market_ctx.confidence * _session_ctx.confidence_multiplier
            )

            _pdl_mult, _pdl_reason = 1.0, ""
            if _pdl_engine.ready and str(_captured_market_ctx.bias) != "NEUTRAL":
                _pdl_mult, _pdl_reason = _pdl_engine.levels.confidence_multiplier(
                    spot=completed_bar.close_price,
                    bias=str(_captured_market_ctx.bias),
                )
            _effective_conf *= _pdl_mult

            _or_mult, _or_reason = 1.0, ""
            if _or_engine.ready and str(_captured_market_ctx.bias) != "NEUTRAL":
                _or_mult, _or_reason = _or_engine.current_range.confidence_multiplier(
                    spot=completed_bar.close_price,
                    bias=str(_captured_market_ctx.bias),
                )
            _effective_conf *= _or_mult

            _fvg_quality_for_entry = 1.0
            _fvg_quality_reason = ""
            if _fvg_events and str(_captured_market_ctx.bias) != "NEUTRAL":
                _rescored = [
                    _fvg_scorer.score_event(
                        event=e,
                        market_bias=str(_captured_market_ctx.bias),
                        recent_sweeps=_recent_sweeps_live,
                        active_htf_fvgs=_htf_fvgs_live,
                        mitigation_ratio=0.0,
                    )
                    for e in _fvg_events
                ]
                _best_q = max(_rescored, key=lambda s: s.total)
                _fvg_quality = _best_q.total
                _fvg_quality_bar = _bar_count
                _fvg_quality_for_entry = _best_q.confidence_multiplier()
                if _best_q.blocks_entry:
                    _fvg_quality_reason = (
                        f"low FVG quality={_best_q.total:.2f} "
                        f"(disp={_best_q.displacement:.2f} "
                        f"sess={_best_q.session:.2f} "
                        f"sweep={_best_q.sweep_origin:.2f} "
                        f"htf={_best_q.htf_alignment:.2f})"
                    )
            elif _bar_count - _fvg_quality_bar <= FVG_QUALITY_STALENESS_BARS:
                _stored_q = FVGQualityScore(
                    total=_fvg_quality,
                    displacement=0.0, session=0.0, sweep_origin=0.0,
                    htf_alignment=0.0, freshness=0.0, volume=0.0,
                )
                _fvg_quality_for_entry = _stored_q.confidence_multiplier()
                if _fvg_quality < FVG_QUALITY_BLOCK_THRESHOLD:
                    _fvg_quality_reason = f"stored low FVG quality={_fvg_quality:.2f}"
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
                    conf=f"{_captured_market_ctx.confidence:.2f}→{_effective_conf:.2f}",
                    reason="  |  ".join(_filter_parts),
                )

            # ── Entry gate: fresh strike, no open position, not hard-halted ─
            if (
                _captured_strike_rec is not None
                and not _option_trader.has_open_position()
                and not _trading_halted
                and _session_ctx.allow_entry
                and not _fvg_quality_reason
            ):
                # Soft halt overrides tier selection: forces tier 0 (sizer_soft)
                if _soft_halted:
                    _effective_tier    = 0
                    _effective_min_conf = _SOFT_HALT_MIN_CONF
                else:
                    _effective_tier     = _determine_risk_tier(_captured_market_ctx.confidence)
                    _tier_min_confs     = {1: _TIER1_MIN_CONF, 2: _TIER2_MIN_CONF, 3: _TIER3_MIN_CONF}
                    _effective_min_conf = _tier_min_confs[_effective_tier]

                if _effective_conf >= _effective_min_conf:
                    _sizing = _size_live_position(
                        _captured_strike_rec, _captured_market_ctx, _effective_tier,
                        _effective_conf,
                    )
                    if _sizing.tradable and _sizing.recommended_lots >= 1:
                        _lots = _sizing.recommended_lots
                        _cost = (
                            _captured_strike_rec.last_traded_price
                            * _captured_strike_rec.lot_size
                            * _lots
                        )
                        _avail = max(_equity - _open_premium_cost, 0.0)
                        if _cost <= _avail:
                            _open_premium_cost = _cost
                            _option_trader.open(
                                recommendation=_captured_strike_rec,
                                direction=str(_captured_market_ctx.bias),
                                bar_index=_bar_count,
                                lots=_lots,
                            )
                            _tier_label = "SOFT" if _soft_halted else str(_effective_tier)
                            print_entry_block(
                                direction=str(_captured_market_ctx.bias),
                                underlying=_captured_strike_rec.underlying_symbol,
                                strike=_captured_strike_rec.strike_price,
                                option_type=_captured_strike_rec.option_type,
                                expiry=_captured_strike_rec.expiry_date,
                                lots=_lots,
                                lot_size=_captured_strike_rec.lot_size,
                                premium=_captured_strike_rec.last_traded_price,
                                notional=_cost,
                                bar_index=_bar_count,
                                session=str(_session_ctx.phase),
                                conf_raw=_captured_market_ctx.confidence,
                                conf_eff=_effective_conf,
                                fvg_quality=_fvg_quality,
                                drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                extra_label=f"TIER{_tier_label}",
                            )
                            try:
                                _db.write_trade_entry(
                                    underlying=_captured_strike_rec.underlying_symbol,
                                    contract_symbol=_captured_strike_rec.option_symbol,
                                    strike=_captured_strike_rec.strike_price,
                                    option_type=_captured_strike_rec.option_type,
                                    expiry_date=_captured_strike_rec.expiry_date.isoformat(),
                                    direction=str(_captured_market_ctx.bias),
                                    lots=_lots,
                                    lot_size=_captured_strike_rec.lot_size,
                                    entry_premium=_captured_strike_rec.last_traded_price,
                                    entry_spot=completed_bar.close_price,
                                    entry_iv=_last_atm_iv,
                                    entry_bar_index=_bar_count,
                                    entry_ts=datetime.now(timezone.utc).isoformat(),
                                    conf_raw=_captured_market_ctx.confidence,
                                    conf_eff=_effective_conf,
                                    fvg_quality=_fvg_quality,
                                    drawdown_at_entry=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                                    equity_before=_equity,
                                )
                                _db.write_decision(
                                    bar_index=_bar_count,
                                    bar_ts=datetime.now(timezone.utc).isoformat(),
                                    underlying=_captured_strike_rec.underlying_symbol,
                                    bias=str(_captured_market_ctx.bias),
                                    conf_raw=_captured_market_ctx.confidence,
                                    conf_eff=_effective_conf,
                                    outcome=f"ENTRY_TIER{_tier_label}",
                                    session_phase=str(_session_ctx.phase),
                                    fvg_quality=_fvg_quality,
                                    equity=_equity,
                                    drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
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
                                    underlying=_captured_strike_rec.underlying_symbol,
                                    bias=str(_captured_market_ctx.bias),
                                    conf_raw=_captured_market_ctx.confidence,
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
                                underlying=_captured_strike_rec.underlying_symbol,
                                bias=str(_captured_market_ctx.bias),
                                conf_raw=_captured_market_ctx.confidence,
                                conf_eff=_effective_conf,
                                outcome="SIZING_REJECTED",
                                filter_reason=_sizing.rejection_reason,
                                equity=_equity,
                                drawdown=max(0.0, (_peak_equity - _equity) / max(_peak_equity, 1.0)),
                            )
                        except Exception:
                            pass

        if result.emitted_events:

            print(
                "\nSTRUCTURE EVENTS:\n"
            )

            for event in (
                result.emitted_events
            ):

                print(event)

        else:

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

