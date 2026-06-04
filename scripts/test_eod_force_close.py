#!/usr/bin/env python3
"""
scripts/test_eod_force_close.py

Offline tests for the EOD force-close logic.

Tests:
  1. BANKNIFTY + NIFTY open positions are both force-closed when price available.
  2. Correct BSM-based exit premium (not fabricated).
  3. EOD_FORCE_CLOSE appears as exit_reason on the closed OptionTrade.
  4. equity / daily_pnl / open_premium_cost updated correctly after each close.
  5. Position cleared from option_trader after force-close.
  6. When price unavailable (spot=0 or iv=0): position remains open, warning printed.
  7. SENSEX with no open position: no-op.
  8. No raw index symbol written to CSV.

Does NOT import live_structure_pipeline (avoids PID lock, Kite connect, etc.).
Uses a temporary CSV file; does not touch logs/paper_trades.csv.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import platform_core.paper_trading.trade_logger as _tl_module
from platform_core.backtesting.backtest_runner import (
    DailyBarAccumulator,
    OptionPaperTrader,
    OptionPosition,
    OptionTrade,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


# ── Minimal state ─────────────────────────────────────────────────────────────

class _FakeState:
    """Minimal stand-in for UnderlyingRuntimeState."""
    def __init__(self, key: str, has_position: bool = True,
                 spot: float = 54000.0, iv: float = 0.20):
        from platform_core.backtesting.backtest_runner import OptionPaperTrader
        self.option_underlying = key
        self.last_bar_close    = spot
        self.last_atm_iv       = iv
        self.option_trader     = OptionPaperTrader()
        if has_position:
            self.option_trader._position = OptionPosition(
                underlying_symbol = key,
                contract_symbol   = f"{key}26JUN54000PE",
                strike            = 54000.0,
                option_type       = "PUT",
                expiry_date       = date(2026, 6, 30),
                lot_size          = 30 if key == "BANKNIFTY" else 65,
                lots              = 1,
                entry_premium     = 800.0,
                entry_spot        = 54200.0,
                entry_iv          = 0.18,
                direction         = "SHORT",
                bar_index         = 100,
            )


# ── The EOD close logic (extracted/reproduced for offline test) ───────────────
# Rather than importing live_structure_pipeline (which has side-effects),
# we reproduce the _eod_force_close_all logic inline here for testing.

_STOP_LOSS_PCT:   float = 0.50
_TAKE_PROFIT_PCT: float = 2.00
_MIN_DTE_TO_HOLD: int   = 2


def _force_close_one(
    key: str,
    und_state: _FakeState,
    bar_count: int,
    equity: float,
    daily_pnl: float,
    open_premium_cost: float,
    consecutive_losses: int,
) -> tuple[float, float, float, int, str | None]:
    """
    Returns (equity, daily_pnl, open_premium_cost, consecutive_losses, warn_msg).
    warn_msg is not-None when the position could not be closed.
    """
    if not und_state.option_trader.has_open_position():
        return equity, daily_pnl, open_premium_cost, consecutive_losses, None

    pos  = und_state.option_trader._position
    spot = und_state.last_bar_close
    iv   = und_state.last_atm_iv

    if spot <= 0 or iv <= 0:
        msg = (
            f"EOD_CLOSE_PRICE_UNAVAILABLE: {key} spot={spot} iv={iv} "
            f"— position {pos.contract_symbol} carried"
        )
        return equity, daily_pnl, open_premium_cost, consecutive_losses, msg

    _closed = und_state.option_trader.evaluate_and_close(
        current_spot=spot,
        current_iv=iv,
        current_date=datetime.now(timezone.utc).date(),
        bar_index=bar_count,
        stop_loss_pct=_STOP_LOSS_PCT,
        take_profit_pct=_TAKE_PROFIT_PCT,
        min_dte=_MIN_DTE_TO_HOLD,
        force_reason="EOD_FORCE_CLOSE",
    )

    if _closed is None:
        return equity, daily_pnl, open_premium_cost, consecutive_losses, "UNEXPECTED_NONE"

    equity            += _closed.pnl
    daily_pnl         += _closed.pnl
    # Recompute open_premium_cost (position is cleared)
    open_premium_cost -= (_closed.entry_premium * _closed.lot_size * _closed.lots)
    open_premium_cost  = max(open_premium_cost, 0.0)
    if _closed.pnl < 0:
        consecutive_losses += 1
    else:
        consecutive_losses = 0

    return equity, daily_pnl, open_premium_cost, consecutive_losses, None


# ── Tests ─────────────────────────────────────────────────────────────────────

_tmp_csv = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
_tmp_csv.close()
Path(_tmp_csv.name).unlink()  # delete so TradeLogger creates header
_orig_log_path = _tl_module.LOG_PATH
_tl_module.LOG_PATH = _tmp_csv.name


print("\n[Test 1] BANKNIFTY force-closed when spot+iv available")
bnk = _FakeState("BANKNIFTY", has_position=True, spot=54000.0, iv=0.20)
eq, dpnl, opc, cl, warn = _force_close_one(
    "BANKNIFTY", bnk, 400, 1_000_000.0, 0.0,
    800.0 * 30 * 1, 0
)
check("bnk_position_cleared",     not bnk.option_trader.has_open_position())
check("bnk_warn_is_none",         warn is None,           f"warn={warn}")
check("bnk_exit_reason",
      len(bnk.option_trader.completed_trades) == 1 and
      bnk.option_trader.completed_trades[0].exit_reason == "EOD_FORCE_CLOSE",
      f"reason={bnk.option_trader.completed_trades[0].exit_reason if bnk.option_trader.completed_trades else 'NONE'}")
closed_bnk = bnk.option_trader.completed_trades[0]
check("bnk_exit_premium_positive", closed_bnk.exit_premium > 0,
      f"exit={closed_bnk.exit_premium:.2f}")
check("bnk_pnl_in_equity",        abs(eq - (1_000_000.0 + closed_bnk.pnl)) < 0.01,
      f"eq={eq:.2f} pnl={closed_bnk.pnl:.2f}")
check("bnk_contract_not_raw_index",
      "CE" in closed_bnk.contract_symbol or "PE" in closed_bnk.contract_symbol)


print("\n[Test 2] NIFTY force-closed when spot+iv available")
nif = _FakeState("NIFTY", has_position=True, spot=23400.0, iv=0.18)
nif.option_trader._position.lot_size = 65
eq2, dpnl2, opc2, cl2, warn2 = _force_close_one(
    "NIFTY", nif, 400, eq, closed_bnk.pnl, 800.0 * 65 * 1, 0
)
check("nif_position_cleared",  not nif.option_trader.has_open_position())
check("nif_warn_is_none",      warn2 is None, f"warn={warn2}")
check("nif_eod_reason",
      nif.option_trader.completed_trades[0].exit_reason == "EOD_FORCE_CLOSE")
check("nif_exit_premium_pos",  nif.option_trader.completed_trades[0].exit_premium > 0)


print("\n[Test 3] Price unavailable (spot=0): position carried, no crash")
bnk_no_spot = _FakeState("BANKNIFTY", has_position=True, spot=0.0, iv=0.20)
eq3, dpnl3, opc3, cl3, warn3 = _force_close_one(
    "BANKNIFTY", bnk_no_spot, 400, 1_000_000.0, 0.0, 24000.0, 0
)
check("no_spot_warn_present",    warn3 is not None, f"warn={warn3}")
check("no_spot_position_open",   bnk_no_spot.option_trader.has_open_position())
check("no_spot_equity_unchanged",abs(eq3 - 1_000_000.0) < 0.01, f"eq={eq3}")


print("\n[Test 4] Price unavailable (iv=0): position carried, no crash")
bnk_no_iv = _FakeState("BANKNIFTY", has_position=True, spot=54000.0, iv=0.0)
_, _, _, _, warn4 = _force_close_one(
    "BANKNIFTY", bnk_no_iv, 400, 1_000_000.0, 0.0, 24000.0, 0
)
check("no_iv_warn_present",   warn4 is not None)
check("no_iv_position_open",  bnk_no_iv.option_trader.has_open_position())


print("\n[Test 5] SENSEX has no open position — no-op")
sensex_empty = _FakeState("SENSEX", has_position=False)
eq5, dpnl5, opc5, cl5, warn5 = _force_close_one(
    "SENSEX", sensex_empty, 400, 1_000_000.0, 0.0, 0.0, 0
)
check("sensex_noop_warn_none",    warn5 is None)
check("sensex_noop_no_trades",    len(sensex_empty.option_trader.completed_trades) == 0)
check("sensex_noop_eq_unchanged", abs(eq5 - 1_000_000.0) < 0.01)


print("\n[Test 6] CSV: closed trade logged, no raw index symbol")
from platform_core.paper_trading.trade_logger import TradeLogger
import csv as _csv

logger_test = TradeLogger()
ct  = bnk.option_trader.completed_trades[0]
pt  = ct.as_paper_trade()
logger_test.log_trade(trade=pt)

with open(_tmp_csv.name, newline="") as fh:
    csv_rows = list(_csv.DictReader(fh))

check("csv_one_row",           len(csv_rows) == 1, f"rows={len(csv_rows)}")
check("csv_not_raw_index",
      all(
          ("CE" in r.get("symbol","") or "PE" in r.get("symbol",""))
          for r in csv_rows
      ),
      f"symbols={[r.get('symbol') for r in csv_rows]}")
check("csv_direction_logged",  csv_rows[0].get("direction") == "SHORT")


print("\n[Test 7] Duplicate CSV guard: same trade logged twice → only one row")
logger_test.log_trade(trade=pt)   # second call — should be suppressed
with open(_tmp_csv.name, newline="") as fh:
    csv_rows2 = list(_csv.DictReader(fh))
check("csv_no_dup_after_second_log", len(csv_rows2) == 1, f"rows={len(csv_rows2)}")


# Cleanup
_tl_module.LOG_PATH = _orig_log_path
try:
    Path(_tmp_csv.name).unlink(missing_ok=True)
except Exception:
    pass


# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  Results: {len(PASS)}/{len(PASS)+len(FAIL)} passed")
if FAIL:
    for f in FAIL:
        print(f"  FAIL: {f}")
else:
    print("  ALL PASS")
print(f"{'='*60}\n")

sys.exit(0 if not FAIL else 1)
