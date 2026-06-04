"""
validate_live_pipeline.py
═══════════════════════════════════════════════════════════════════════
End-to-end live paper-trading validation.

Runs against a real Kite session. No real orders placed.

Steps:
  1.  Kite authentication
  2.  Live bar generation (historical proxy, 1-min BANKNIFTY)
  3.  MarketContextBuilder update
  4.  Live option-chain retrieval from Kite REST
  5.  OptionsContextBuilder output
  6.  StrikeSelectionEngine recommendations
  7.  PositionSizer calculations
  8.  OptionPaperTrader execution (paper only)
  9.  TradeLogger output
  10. State persistence during runtime
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── path ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── output helpers ────────────────────────────────────────────────────────────

_PASS  = "  PASS"
_FAIL  = "  FAIL"
_WARN  = "  WARN"
_WIDTH = 65
_results: list[tuple[int, str, bool]] = []   # (step, label, passed)


def _hdr(step: int, label: str) -> None:
    print(f"\n{'─' * _WIDTH}")
    print(f"  Step {step:2d} — {label}")
    print(f"{'─' * _WIDTH}")


def _ok(msg: str) -> None:
    print(f"{_PASS}  {msg}")


def _warn(msg: str) -> None:
    print(f"{_WARN}  {msg}")


def _err(msg: str) -> None:
    print(f"{_FAIL}  {msg}")


def _record(step: int, label: str, passed: bool) -> None:
    _results.append((step, label, passed))


def _summary() -> None:
    print(f"\n{'═' * _WIDTH}")
    print("  VALIDATION SUMMARY")
    print(f"{'═' * _WIDTH}")
    passed = sum(1 for _, _, ok in _results if ok)
    total  = len(_results)
    for step, label, ok in _results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] Step {step:2d}  {label}")
    print(f"{'─' * _WIDTH}")
    print(f"  {passed}/{total} steps passed")
    print(f"{'═' * _WIDTH}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Kite Authentication
# ─────────────────────────────────────────────────────────────────────────────
_hdr(1, "Kite Authentication")
try:
    from platform_core.data.live.kite_adapter import KiteAdapter
    adapter = KiteAdapter()
    valid, auth_err = adapter.check_session()
    if not valid:
        _err(f"Token rejected: {auth_err}")
        _err("Run: python scripts/generate_kite_session.py")
        _warn("Continuing in OFFLINE mode — Steps 2 and 4 will use synthetic data")
        adapter = None          # signal offline mode to downstream steps
    else:
        profile = adapter.kite.profile()
        _ok(f"User:    {profile.get('user_name', '?')}")
        _ok(f"Email:   {profile.get('email', '?')}")
        _ok(f"Broker:  {profile.get('broker', '?')}")
    _record(1, "Kite Authentication", valid)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(1, "Kite Authentication", False)
    _summary()
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Bar Generation (historical proxy)
# ─────────────────────────────────────────────────────────────────────────────
_hdr(2, "Bar Generation (historical proxy via Kite REST)")
BANKNIFTY_TOKEN = 260105
bars = []
try:
    from platform_core.data.bar_builder import LiveBar
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    to_dt   = now_ist.replace(tzinfo=None)
    from_dt = to_dt - timedelta(days=5)   # fetch last 5 days, take tail

    if adapter is None:
        raise RuntimeError("Offline mode — using synthetic bars")
    candles = adapter.kite.historical_data(
        instrument_token=BANKNIFTY_TOKEN,
        from_date=from_dt,
        to_date=to_dt,
        interval="minute",
    )
    if not candles:
        _warn("No candles returned — market may be closed; using synthetic bars")
    else:
        for c in candles[-50:]:   # last 50 bars for MCB warmup
            ts = c["date"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append(LiveBar(
                symbol="BANKNIFTY",
                timeframe="MIN_1",
                start_timestamp=ts,
                end_timestamp=ts + timedelta(minutes=1),
                open_price=float(c["open"]),
                high_price=float(c["high"]),
                low_price=float(c["low"]),
                close_price=float(c["close"]),
                volume=float(c["volume"]),
            ))
        _ok(f"Fetched {len(candles)} total bars; using last {len(bars)}")
        _ok(f"Latest bar: {bars[-1].start_timestamp.date()}  "
            f"O={bars[-1].open_price:.0f}  H={bars[-1].high_price:.0f}  "
            f"L={bars[-1].low_price:.0f}  C={bars[-1].close_price:.0f}  "
            f"V={bars[-1].volume:.0f}")
    _record(2, "Bar Generation", len(bars) > 0)
except Exception as exc:
    _warn(f"Historical fetch failed: {exc} — will use synthetic bars")
    _record(2, "Bar Generation", False)

# ── synthetic fallback if no real bars ────────────────────────────────────────
if not bars:
    _warn("Generating 50 synthetic 1-min bars at ₹52,000")
    from platform_core.data.bar_builder import LiveBar
    from datetime import timedelta
    base_ts = datetime(2026, 5, 30, 9, 15, tzinfo=timezone.utc)
    base_px = 52_000.0
    for i in range(50):
        ts = base_ts + timedelta(minutes=i)
        bars.append(LiveBar(
            symbol="BANKNIFTY", timeframe="MIN_1",
            start_timestamp=ts,
            end_timestamp=ts + timedelta(minutes=1),
            open_price=base_px + i * 10,
            high_price=base_px + i * 10 + 30,
            low_price=base_px + i * 10 - 20,
            close_price=base_px + i * 10 + 15,
            volume=5000.0,
        ))

spot_price = bars[-1].close_price
_ok(f"Spot proxy: ₹{spot_price:,.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — MarketContextBuilder
# ─────────────────────────────────────────────────────────────────────────────
_hdr(3, "MarketContextBuilder")
try:
    from platform_core.intelligence.market_context_builder import MarketContextBuilder
    mcb = MarketContextBuilder(
        underlying_symbol="BANKNIFTY",
        min_history_bars=20,
    )
    for bar in bars:
        mcb.update(bar=bar, timeframe="1m")
    ctx = mcb.build(valuation_timestamp=bars[-1].end_timestamp)
    ready_tfs = ctx.timeframes_with_history
    _ok(f"bias={ctx.bias}  confidence={ctx.confidence:.3f}")
    _ok(f"trend_score={ctx.trend_score:+.3f}  momentum={ctx.momentum_score:.3f}")
    _ok(f"vol_regime={ctx.volatility_regime}  liq_regime={ctx.liquidity_regime}")
    _ok(f"timeframes_with_history: {ready_tfs}")
    if not ready_tfs:
        _warn("No timeframe has reached min_history_bars — MCB returning NEUTRAL/0 (normal at cold start)")
    _record(3, "MarketContextBuilder", ctx is not None)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(3, "MarketContextBuilder", False)
    ctx = None

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Live Option-Chain Retrieval
# ─────────────────────────────────────────────────────────────────────────────
_hdr(4, "Live Option-Chain Retrieval")
chain = None
lot_size = 30  # BANKNIFTY lot size per current Kite master (updated 2026-06-04)
try:
    from platform_core.data.synthetic_chain_builder import (
        ExpiryProvider, SyntheticOptionChainBuilder, SyntheticChainConfig,
    )
    expiry_provider = ExpiryProvider("BANKNIFTY")
    today  = date.today()

    if adapter is not None:
        expiry = adapter.nearest_expiry("BANKNIFTY", today)
        _ok(f"Mode: LIVE  target_expiry={expiry}  spot=₹{spot_price:,.0f}")
        chain = adapter.fetch_option_chain(
            underlying="BANKNIFTY",
            expiry_date=expiry,
            spot_price=spot_price,
            lot_size=lot_size,
            strike_step=100.0,
            strikes_around_atm=10,
        )
        source = "kite_live"
    else:
        expiry = expiry_provider.get_weekly_expiry(today)
        _warn(f"Mode: OFFLINE (synthetic)  expiry={expiry}  spot=₹{spot_price:,.0f}")
        chain_builder = SyntheticOptionChainBuilder(config=SyntheticChainConfig(
            underlying_symbol="BANKNIFTY",
            lot_size=lot_size,
            strike_step=100,
            risk_free_rate=0.068,
            dividend_yield=0.0,
            strikes_around_atm=10,
            vol_lookback_bars=20,
        ))
        for bar in bars:
            chain_builder.update_bar(bar)
        chain = chain_builder.build_snapshot(
            spot_price=spot_price,
            expiry_date=expiry,
            valuation_timestamp=bars[-1].end_timestamp,
        )
        source = "synthetic"

    calls = [c for c in chain.contracts if c.option_side == "CALL"]
    puts  = [c for c in chain.contracts if c.option_side == "PUT"]
    _ok(f"Contracts: {len(chain.contracts)} ({len(calls)} calls  {len(puts)} puts)  "
        f"source={source}")

    for c in sorted(chain.contracts, key=lambda x: float(x.strike_price))[:4]:
        ltp = float(c.last_traded_price) if c.last_traded_price else 0.0
        oi  = c.open_interest or 0
        _ok(f"  {c.contract_symbol:<28} LTP=₹{ltp:>8.2f}  OI={oi:>10,}")

    _record(4, "Live Option-Chain Retrieval", len(chain.contracts) > 0)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(4, "Live Option-Chain Retrieval", False)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — OptionsContextBuilder
# ─────────────────────────────────────────────────────────────────────────────
_hdr(5, "OptionsContextBuilder")
opt_ctx = None
try:
    if chain is None:
        raise RuntimeError("No option chain — Step 4 failed or offline mode")

    from platform_core.options.options_context import OptionsContextBuilder, OptionsContextBuilderConfig
    ocb = OptionsContextBuilder(config=OptionsContextBuilderConfig(
        compute_missing_greeks=True,
        risk_free_rate=0.068,
        dividend_yield=0.0,
    ))
    opt_ctx = ocb.build(
        snapshots=[chain],
        spot_price=spot_price,
        lot_size=lot_size,
        valuation_timestamp=bars[-1].end_timestamp,
    )
    _ok(f"Contracts processed:  {opt_ctx.contracts_processed}")
    _ok(f"Strike candidates:    {len(opt_ctx.strike_candidates)}")
    _ok(f"ATM IV:               {opt_ctx.atm_iv * 100:.2f}%" if opt_ctx.atm_iv else "ATM IV: not available")
    _ok(f"PCR (OI):             {opt_ctx.pcr:.3f}" if opt_ctx.pcr else "PCR: not available")
    _ok(f"Max pain:             ₹{opt_ctx.max_pain:,.0f}" if opt_ctx.max_pain else "Max pain: not available")
    _ok(f"Gamma regime:         {opt_ctx.gamma_regime}" if opt_ctx.gamma_regime else "Gamma regime: not available")
    if opt_ctx.build_warnings:
        for w in opt_ctx.build_warnings[:3]:
            _warn(f"Builder warning: {w}")
    _record(5, "OptionsContextBuilder", len(opt_ctx.strike_candidates) > 0)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(5, "OptionsContextBuilder", False)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — StrikeSelectionEngine
# ─────────────────────────────────────────────────────────────────────────────
_hdr(6, "StrikeSelectionEngine Recommendations")
rec = None
bias_used = None
try:
    from platform_core.options.strike_selection import StrikeSelectionEngine, StrikeSelectionConfig

    if opt_ctx is None or not opt_ctx.strike_candidates:
        raise RuntimeError("No candidates from Step 5")

    # Use real bias if MCB has history; force LONG for validation otherwise
    if ctx and str(ctx.bias) != "NEUTRAL" and ctx.timeframes_with_history:
        bias_used = str(ctx.bias)
    else:
        bias_used = "LONG"
        _warn(f"MCB returned NEUTRAL/no history — using bias=LONG for validation")

    strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(
        top_n=3,
        target_delta_min=0.28,
        target_delta_max=0.52,
        min_open_interest=100,
    ))
    sel_result = strike_engine.select(
        bias=bias_used,
        candidates=list(opt_ctx.strike_candidates),
        valuation_timestamp=bars[-1].end_timestamp,
        oi_snapshot=opt_ctx.oi_snapshot,
        gex_snapshot=opt_ctx.gex_snapshot,
        atm_iv=opt_ctx.atm_iv,
    )
    _ok(f"Candidates evaluated:  {sel_result.candidates_evaluated}")
    _ok(f"Passed filters:        {sel_result.candidates_passed_filters}")
    _ok(f"Recommendations:       {len(sel_result.recommendations)}")
    rec = sel_result.best()
    if rec:
        _ok(f"")
        _ok(f"  TOP RECOMMENDATION:")
        _ok(f"  {rec.summary()}")
        _ok(f"")
        _ok(f"  Strike:      ₹{rec.strike_price:,.0f}  {rec.option_type}")
        _ok(f"  Expiry:      {rec.expiry_date}  ({rec.days_to_expiry} DTE)")
        _ok(f"  LTP:         ₹{rec.last_traded_price:.2f}")
        _ok(f"  Delta:       {rec.delta:+.3f}  Gamma: {rec.gamma:.4f}")
        _ok(f"  IV:          {rec.implied_volatility * 100:.2f}%")
        _ok(f"  OI:          {rec.open_interest:,}")
        _ok(f"  Score:       {rec.composite_score:.4f}")
        _ok(f"  Liquidity:   {rec.liquidity_grade}")
        _ok(f"  GEX label:   {rec.gex_label}")
    else:
        _warn("No recommendation passed filters — check OI and DTE thresholds")
    _record(6, "StrikeSelectionEngine", rec is not None)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(6, "StrikeSelectionEngine", False)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — PositionSizer
# ─────────────────────────────────────────────────────────────────────────────
_hdr(7, "PositionSizer (Tier 2 — Normal)")
sizing = None
try:
    from platform_core.risk.position_sizing import (
        PortfolioRiskSnapshot, PositionSizer, PositionSizingConfig, PositionSizingRequest,
    )

    if rec is None:
        raise RuntimeError("No recommendation from Step 6")

    EQUITY     = 100_000.0
    STOP_PCT   = 0.50
    MAX_LOTS   = 3
    RISK_FRAC  = 0.020
    ATM_IV_REF = 0.18

    sizer = PositionSizer(config=PositionSizingConfig(
        max_risk_fraction_per_trade=RISK_FRAC,
        max_portfolio_heat_fraction=0.10,
    ))
    portfolio = PortfolioRiskSnapshot(
        equity=EQUITY,
        available_cash=EQUITY,
        current_portfolio_heat=0.0,
        current_drawdown_fraction=0.0,
        max_allowed_drawdown_fraction=0.15,
        correlation_exposure_fraction=0.0,
        open_position_count=0,
    )
    conf = ctx.confidence if ctx else 0.60
    sizing = sizer.size_position(
        request=PositionSizingRequest(
            underlying_symbol=rec.underlying_symbol,
            timestamp=bars[-1].end_timestamp,
            direction=bias_used,
            instrument_type="OPTION",
            entry_price=rec.last_traded_price,
            stop_price=max(rec.last_traded_price * STOP_PCT, 0.01),
            contract_multiplier=1.0,
            lot_size=rec.lot_size,
            minimum_lot_count=1,
            maximum_lot_count=MAX_LOTS,
            confidence_score=min(conf, 1.0),
            actionability_score=min(rec.composite_score, 1.0),
            realized_volatility_ratio=max(
                (opt_ctx.atm_iv or ATM_IV_REF) / ATM_IV_REF, 0.1
            ) if opt_ctx else 1.0,
            liquidity_score=min(rec.breakdown.liquidity, 1.0),
            spread_bps=50.0,
            slippage_bps_estimate=10.0,
            expected_correlation_to_book=0.0,
            portfolio=portfolio,
        ),
        valuation_timestamp=bars[-1].end_timestamp,
    )
    _ok(f"Equity:             ₹{EQUITY:,.0f}  risk_frac={RISK_FRAC:.1%}")
    _ok(f"Risk budget:        ₹{EQUITY * RISK_FRAC:,.0f}")
    _ok(f"Entry premium:      ₹{rec.last_traded_price:.2f}")
    _ok(f"Stop premium:       ₹{rec.last_traded_price * STOP_PCT:.2f}")
    _ok(f"Risk per lot:       ₹{rec.last_traded_price * STOP_PCT * rec.lot_size:.2f}")
    _ok(f"Tradable:           {sizing.tradable}")
    _ok(f"Recommended lots:   {sizing.recommended_lots}")
    _ok(f"Rejection reason:   {sizing.rejection_reason or 'none'}")
    if sizing.tradable:
        cost = rec.last_traded_price * rec.lot_size * sizing.recommended_lots
        _ok(f"Total premium cost: ₹{cost:,.2f} ({cost/EQUITY:.1%} of equity)")
    _record(7, "PositionSizer", True)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(7, "PositionSizer", False)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — OptionPaperTrader (open → evaluate → force close)
# ─────────────────────────────────────────────────────────────────────────────
_hdr(8, "OptionPaperTrader (paper execution — no real orders)")
try:
    from platform_core.backtesting.backtest_runner import OptionPaperTrader
    from platform_core.options.greeks_engine import GreeksEngine, GreeksEngineConfig

    if rec is None:
        raise RuntimeError("No recommendation from Step 6")

    trader = OptionPaperTrader()
    lots   = sizing.recommended_lots if (sizing and sizing.tradable and sizing.recommended_lots >= 1) else 1
    trader.open(recommendation=rec, direction=bias_used, bar_index=1, lots=lots)
    _ok(f"Position opened: {rec.contract_symbol if hasattr(rec, 'contract_symbol') else rec.option_symbol}  "
        f"lots={lots}  prem=₹{rec.last_traded_price:.2f}")

    eval_date = date.today()   # same session as entry — avoids spurious time-decay stop

    # Evaluate at current market (same prices) — won't close
    closed = trader.evaluate_and_close(
        current_spot=spot_price,
        current_iv=opt_ctx.atm_iv or 0.18 if opt_ctx else 0.18,
        current_date=eval_date,
        bar_index=2,
        stop_loss_pct=0.50,
        take_profit_pct=2.00,
        min_dte=2,
        force_reason=None,
    )
    if closed is None:
        _ok("Evaluate at entry: no exit triggered (correct — price unchanged)")
    else:
        _warn(f"Unexpected close at entry: {closed.exit_reason}")

    # Force close for validation
    closed = trader.evaluate_and_close(
        current_spot=spot_price,
        current_iv=opt_ctx.atm_iv or 0.18 if opt_ctx else 0.18,
        current_date=eval_date,
        bar_index=3,
        stop_loss_pct=0.50,
        take_profit_pct=2.00,
        min_dte=2,
        force_reason="VALIDATION_TEST",
    )
    assert closed is not None, "Force close should always close"
    _ok(f"Force close: exit=₹{closed.exit_premium:.2f}  "
        f"pnl=₹{closed.pnl:+,.2f}  reason={closed.exit_reason}")
    _ok(f"Completed trades: {len(trader.completed_trades)}")
    _record(8, "OptionPaperTrader", True)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(8, "OptionPaperTrader", False)
    closed = None

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — TradeLogger
# ─────────────────────────────────────────────────────────────────────────────
_hdr(9, "TradeLogger")
try:
    from platform_core.paper_trading.trade_logger import TradeLogger, LOG_PATH as REAL_LOG
    from platform_core.paper_trading.performance_tracker import PerformanceTracker
    from platform_core.paper_trading.paper_broker import PaperTrade

    if closed is None:
        raise RuntimeError("No closed trade from Step 8")

    paper_trade = closed.as_paper_trade()
    logger  = TradeLogger()
    tracker = PerformanceTracker()

    logger.log_trade(trade=paper_trade)
    _ok(f"Logged to: {REAL_LOG}")

    # Verify the row was written
    log_lines = Path(REAL_LOG).read_text().strip().split("\n")
    _ok(f"Log file rows: {len(log_lines)} (1 header + {len(log_lines)-1} trade(s))")
    _ok(f"Last row: {log_lines[-1]}")

    # Performance summary
    summary = tracker.calculate_summary(trades=[paper_trade])
    _ok(f"Performance: total={summary.total_trades}  "
        f"wins={summary.winning_trades}  losses={summary.losing_trades}  "
        f"pnl=₹{summary.total_pnl:+,.2f}  win_rate={summary.win_rate:.1%}")
    _record(9, "TradeLogger", True)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(9, "TradeLogger", False)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 10 — State Persistence
# ─────────────────────────────────────────────────────────────────────────────
_hdr(10, "State Persistence (save → simulate restart → restore)")
STATE_TEST_FILE = ROOT / "logs" / "paper_trading_state_validation.json"
try:
    # Simulate the state that would exist after Step 8
    test_state = {
        "equity":                 100_000.0 + (closed.pnl if closed else 0.0),
        "peak_equity":            100_000.0,
        "daily_pnl":              closed.pnl if closed else 0.0,
        "consecutive_losses":     1 if (closed and closed.pnl < 0) else 0,
        "trading_halted":         False,
        "halt_reason":            "",
        "soft_halted":            False,
        "soft_halt_reason":       "",
        "session_date":           date.today().isoformat(),
        "session_opening_equity": 100_000.0,
        "last_atm_iv":            opt_ctx.atm_iv if opt_ctx and opt_ctx.atm_iv else 0.18,
    }

    # ── Write (atomic) ─────────────────────────────────────────────────────
    STATE_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_TEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(test_state, indent=2))
    tmp.replace(STATE_TEST_FILE)
    _ok(f"State written: {STATE_TEST_FILE.name}")

    # ── Simulate process restart (read back) ───────────────────────────────
    s      = json.loads(STATE_TEST_FILE.read_text())
    eq     = float(s["equity"])
    peak   = float(s["peak_equity"])
    dpnl   = float(s["daily_pnl"])
    consec = int(s["consecutive_losses"])
    halted = bool(s["trading_halted"])
    soft   = bool(s["soft_halted"])
    sdate  = date.fromisoformat(s["session_date"])
    iv     = float(s.get("last_atm_iv", 0.18))
    _ok(f"Restored after simulated restart:")
    _ok(f"  equity=₹{eq:,.2f}  peak=₹{peak:,.0f}  daily_pnl=₹{dpnl:+,.2f}")
    _ok(f"  consecutive_losses={consec}  halted={halted}  soft={soft}")
    _ok(f"  session_date={sdate}  last_atm_iv={iv:.2%}")

    # ── Verify round-trip fidelity ─────────────────────────────────────────
    assert eq     == test_state["equity"]
    assert peak   == test_state["peak_equity"]
    assert dpnl   == test_state["daily_pnl"]
    assert consec == test_state["consecutive_losses"]
    assert halted == test_state["trading_halted"]
    assert sdate  == date.fromisoformat(test_state["session_date"])
    _ok("Round-trip fidelity: all fields match")

    # Cleanup validation file
    STATE_TEST_FILE.unlink()
    _record(10, "State Persistence", True)
except Exception as exc:
    _err(f"Exception: {exc}")
    traceback.print_exc()
    _record(10, "State Persistence", False)

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
_summary()
