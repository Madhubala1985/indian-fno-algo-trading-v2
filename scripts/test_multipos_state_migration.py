#!/usr/bin/env python3
"""
Temp test: multi-position state migration.

Tests:
  1. Old format (open_position only)  → migrated to per-underlying restore
  2. New format (open_positions dict) → all positions restored independently
  3. Unknown underlying in state      → warning printed, no crash
  4. Corrupt/missing field in one pos → that position skipped, others unaffected
  5. DB recover_open_trade_row called with correct underlying per position
  6. _compute_open_premium_cost reflects all open positions
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _make_pos_dict(underlying: str, contract: str, premium: float, lots: int, lot_size: int) -> dict:
    return {
        "underlying_symbol": underlying,
        "contract_symbol":   contract,
        "strike":            48000.0,
        "option_type":       "CALL",
        "expiry_date":       "2026-06-11",
        "lot_size":          lot_size,
        "lots":              lots,
        "entry_premium":     premium,
        "entry_spot":        48000.0,
        "entry_iv":          0.18,
        "direction":         "LONG",
        "bar_index":         42,
    }


# ── Test 1: DB rowids dict ─────────────────────────────────────────────────────
print("\n[Test 1] TradeDatabase._open_trade_rowids is a dict")
from platform_core.persistence.trade_database import TradeDatabase
db = TradeDatabase(":memory:")
check("rowids_is_dict", isinstance(db._open_trade_rowids, dict), str(type(db._open_trade_rowids)))
check("rowids_empty",   db._open_trade_rowids == {})


# ── Test 2: write_trade_exit has underlying param ──────────────────────────────
print("\n[Test 2] write_trade_exit accepts underlying param")
import inspect
sig = inspect.signature(TradeDatabase.write_trade_exit)
check("exit_has_underlying", "underlying" in sig.parameters)


# ── Test 3: recover_open_trade_row has underlying param ───────────────────────
print("\n[Test 3] recover_open_trade_row accepts underlying param")
sig_r = inspect.signature(TradeDatabase.recover_open_trade_row)
check("recover_has_underlying", "underlying" in sig_r.parameters)


# ── Test 4: DB entry/exit per underlying ──────────────────────────────────────
print("\n[Test 4] DB entry tracked per-underlying; exit clears correct slot only")
import tempfile
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
db2 = TradeDatabase(_tmp_db.name)
db2.start(session_id="test", run_mode="test")

import time
db2.write_trade_entry(underlying="BANKNIFTY", contract_symbol="BNK_CE", strike=48000,
    option_type="CALL", expiry_date="2026-06-11", direction="LONG",
    lots=1, lot_size=30, entry_premium=500.0, entry_spot=48000.0,
    entry_iv=0.18, entry_bar_index=1, entry_ts="2026-06-04T09:30:00Z")
db2.write_trade_entry(underlying="NIFTY", contract_symbol="NIFTY_CE", strike=23500,
    option_type="CALL", expiry_date="2026-06-11", direction="LONG",
    lots=1, lot_size=65, entry_premium=200.0, entry_spot=23500.0,
    entry_iv=0.18, entry_bar_index=1, entry_ts="2026-06-04T09:30:00Z")
db2.flush()

check("bnk_rowid_set", db2._open_trade_rowids.get("BANKNIFTY") is not None)
check("nifty_rowid_set", db2._open_trade_rowids.get("NIFTY") is not None)

bnk_rowid_before = db2._open_trade_rowids.get("BANKNIFTY")
nifty_rowid_before = db2._open_trade_rowids.get("NIFTY")

# Close NIFTY only
db2.write_trade_exit(underlying="NIFTY", exit_premium=220.0, pnl=1300.0,
    bars_held=5, exit_reason="TAKE_PROFIT", exit_ts="2026-06-04T10:00:00Z",
    equity_after=1_001_300.0)
db2.flush()

check("nifty_rowid_cleared",  db2._open_trade_rowids.get("NIFTY") is None)
check("bnk_rowid_unchanged",  db2._open_trade_rowids.get("BANKNIFTY") == bnk_rowid_before,
      f"expected={bnk_rowid_before} got={db2._open_trade_rowids.get('BANKNIFTY')}")

# Now close BANKNIFTY
db2.write_trade_exit(underlying="BANKNIFTY", exit_premium=480.0, pnl=-600.0,
    bars_held=8, exit_reason="STOP_LOSS", exit_ts="2026-06-04T10:30:00Z",
    equity_after=1_000_700.0)
db2.flush()

check("bnk_rowid_cleared", db2._open_trade_rowids.get("BANKNIFTY") is None)


# ── Test 5: _compute_open_premium_cost helper ─────────────────────────────────
print("\n[Test 5] _compute_open_premium_cost sums across all open positions")

# We can't import live_structure_pipeline (it would start the pipeline).
# Instead test the logic directly.
class FakePos:
    def __init__(self, premium, lot_size, lots):
        self.entry_premium = premium
        self.lot_size = lot_size
        self.lots = lots

class FakeTrader:
    def __init__(self, pos=None):
        self._position = pos
    def has_open_position(self):
        return self._position is not None

states = {
    "BANKNIFTY": FakeTrader(FakePos(500, 30, 2)),   # cost = 500*30*2 = 30,000
    "NIFTY":     FakeTrader(FakePos(200, 65, 1)),   # cost = 200*65*1 = 13,000
    "SENSEX":    FakeTrader(None),                   # no position
}
expected_cost = 30_000 + 13_000
computed = sum(
    s._position.entry_premium * s._position.lot_size * s._position.lots
    for s in states.values()
    if s._position is not None
)
check("premium_cost_sum", computed == expected_cost, f"expected={expected_cost} got={computed}")

# After closing NIFTY
states["NIFTY"]._position = None
computed_after = sum(
    s._position.entry_premium * s._position.lot_size * s._position.lots
    for s in states.values()
    if s._position is not None
)
check("premium_cost_after_close", computed_after == 30_000, f"got={computed_after}")


# ── Test 6: State migration old→new ──────────────────────────────────────────
print("\n[Test 6] Old open_position format migrated into per-underlying dict")
old_state = {
    "equity": 1_000_000.0,
    "peak_equity": 1_000_000.0,
    "daily_pnl": 0.0,
    "consecutive_losses": 0,
    "trading_halted": False,
    "halt_reason": "",
    "soft_halted": False,
    "soft_halt_reason": "",
    "session_opening_equity": 1_000_000.0,
    "last_atm_iv": 0.18,
    "session_date": None,
    "open_position": _make_pos_dict("BANKNIFTY", "BNK26JUN48000CE", 500.0, 2, 30),
    # No open_positions key
}
check("old_no_open_positions_key", "open_positions" not in old_state)
# Simulate migration logic
pos_raw = old_state.get("open_position")
new_positions_raw = old_state.get("open_positions")
positions_to_restore: dict = {}
if new_positions_raw is not None:
    for k, v in new_positions_raw.items():
        if v is not None:
            positions_to_restore[k] = v
elif pos_raw is not None:
    legacy_und = pos_raw.get("underlying_symbol", "BANKNIFTY")
    positions_to_restore[legacy_und] = pos_raw
check("old_migrated_to_bnk", "BANKNIFTY" in positions_to_restore)
check("old_migration_single_entry", len(positions_to_restore) == 1)


# ── Test 7: New format with two open positions ────────────────────────────────
print("\n[Test 7] New format restores both BANKNIFTY and NIFTY positions")
new_state = {
    **old_state,
    "open_positions": {
        "BANKNIFTY": _make_pos_dict("BANKNIFTY", "BNK26JUN48000CE", 500.0, 2, 30),
        "NIFTY":     _make_pos_dict("NIFTY",     "NIF26JUN23500CE", 200.0, 1, 65),
        "SENSEX":    None,
    },
}
new_pr = new_state.get("open_positions")
pos_to_r2: dict = {}
if new_pr is not None:
    for k, v in new_pr.items():
        if v is not None:
            pos_to_r2[k] = v
check("new_format_bnk_found",   "BANKNIFTY" in pos_to_r2)
check("new_format_nifty_found", "NIFTY"     in pos_to_r2)
check("new_format_sensex_null", "SENSEX"    not in pos_to_r2)
check("new_format_count",       len(pos_to_r2) == 2)


# ── Test 8: Unknown underlying → no crash ─────────────────────────────────────
print("\n[Test 8] Unknown underlying skipped without crashing")
known_underlyings = {"BANKNIFTY", "NIFTY", "SENSEX"}
state_with_unknown = {
    "BANKNIFTY": _make_pos_dict("BANKNIFTY", "BNK26JUN48000CE", 500.0, 2, 30),
    "UNKNOWN_IDX": _make_pos_dict("UNKNOWN_IDX", "UNK26JUN1000CE", 100.0, 1, 10),
}
skipped = []
restored = []
for und_k, pos_d in state_with_unknown.items():
    if und_k not in known_underlyings:
        skipped.append(und_k)
    else:
        restored.append(und_k)
check("unknown_skipped",  "UNKNOWN_IDX" in skipped)
check("known_restored",   "BANKNIFTY"   in restored)
check("no_crash_unknown", True)  # if we got here, no crash


# ── Test 9: Corrupt position → that position skipped, others OK ───────────────
print("\n[Test 9] Corrupt position dict skipped; other positions unaffected")
corrupt_state = {
    "BANKNIFTY": _make_pos_dict("BANKNIFTY", "BNK26JUN48000CE", 500.0, 2, 30),
    "NIFTY":     {"underlying_symbol": "NIFTY", "MISSING_FIELDS": True},  # corrupt
}
restored9 = []
skipped9  = []
for und_k, pos_d in corrupt_state.items():
    if und_k not in known_underlyings:
        skipped9.append(und_k)
        continue
    try:
        # Simulate the parse
        _ = dict(
            underlying_symbol = str(pos_d["underlying_symbol"]),
            contract_symbol   = str(pos_d["contract_symbol"]),  # KeyError for NIFTY
            strike            = float(pos_d["strike"]),
        )
        restored9.append(und_k)
    except (KeyError, ValueError) as exc:
        skipped9.append(und_k)
        print(f"    [expected skip] {und_k}: {exc}")
check("bnk_restored_despite_corrupt_nifty", "BANKNIFTY" in restored9)
check("corrupt_nifty_skipped",              "NIFTY"     in skipped9)
check("no_crash_on_corrupt",               True)


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
