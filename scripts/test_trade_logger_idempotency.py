#!/usr/bin/env python3
"""
scripts/test_trade_logger_idempotency.py

Tests that TradeLogger suppresses duplicate trades both within a process
run and across restarts (via CSV-seeded deduplication).

Never touches logs/paper_trades.csv — all writes go to a temp file.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import platform_core.paper_trading.trade_logger as _tl_module

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def _make_trade(symbol: str, direction: str = "LONG",
                entry: float = 100.0, exit_: float = 120.0,
                qty: int = 15, pnl: float = 300.0):
    from platform_core.paper_trading.paper_broker import PaperTrade
    t = PaperTrade.__new__(PaperTrade)
    t.symbol      = symbol
    t.direction   = direction
    t.entry_price = entry
    t.exit_price  = exit_
    t.quantity    = qty
    t.pnl         = pnl
    return t


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _with_tmp_log(fn):
    """Run fn(tmp_path) with a fresh temp CSV; restore LOG_PATH afterwards."""
    orig = _tl_module.LOG_PATH
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    Path(tmp_path).unlink()  # delete so TradeLogger creates it fresh with header
    _tl_module.LOG_PATH = tmp_path
    try:
        fn(tmp_path)
    finally:
        _tl_module.LOG_PATH = orig
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


# ── Test 1: same trade twice → only one CSV row ───────────────────────────────
def test_same_trade_twice(tmp: str) -> None:
    from platform_core.paper_trading.trade_logger import TradeLogger
    logger = TradeLogger()
    t = _make_trade("BNK26JUN54000CE")
    logger.log_trade(trade=t)
    logger.log_trade(trade=t)   # should be suppressed
    rows = _read_csv(tmp)
    check("same_trade_twice_count", len(rows) == 1,
          f"expected 1 row, got {len(rows)}")


print("\n[Test 1] Same trade logged twice → only one CSV row")
_with_tmp_log(test_same_trade_twice)


# ── Test 2: two distinct trades → two CSV rows ────────────────────────────────
def test_two_distinct_trades(tmp: str) -> None:
    from platform_core.paper_trading.trade_logger import TradeLogger
    logger = TradeLogger()
    logger.log_trade(trade=_make_trade("BNK26JUN54000CE"))
    logger.log_trade(trade=_make_trade("NIFTY2660923500CE", entry=200.0, exit_=210.0))
    rows = _read_csv(tmp)
    check("two_distinct_count", len(rows) == 2,
          f"expected 2 rows, got {len(rows)}")
    syms = {r["symbol"] for r in rows}
    check("two_distinct_symbols", syms == {"BNK26JUN54000CE", "NIFTY2660923500CE"})


print("\n[Test 2] Two distinct trades → two CSV rows")
_with_tmp_log(test_two_distinct_trades)


# ── Test 3: different PnL same key → suppressed (key ignores pnl) ─────────────
def test_same_key_different_pnl(tmp: str) -> None:
    from platform_core.paper_trading.trade_logger import TradeLogger
    logger = TradeLogger()
    logger.log_trade(trade=_make_trade("BNK26JUN54000CE", pnl=300.0))
    logger.log_trade(trade=_make_trade("BNK26JUN54000CE", pnl=999.0))  # key same; suppressed
    rows = _read_csv(tmp)
    check("same_key_diff_pnl_count", len(rows) == 1,
          f"expected 1 row, got {len(rows)}")


print("\n[Test 3] Same trade key with different pnl → only first row kept")
_with_tmp_log(test_same_key_different_pnl)


# ── Test 4: interleaved duplicate → still suppressed ─────────────────────────
def test_interleaved_duplicate(tmp: str) -> None:
    from platform_core.paper_trading.trade_logger import TradeLogger
    logger = TradeLogger()
    logger.log_trade(trade=_make_trade("T1", entry=100.0, exit_=110.0))
    logger.log_trade(trade=_make_trade("T2", entry=200.0, exit_=220.0))
    logger.log_trade(trade=_make_trade("T1", entry=100.0, exit_=110.0))  # dup of first
    rows = _read_csv(tmp)
    check("interleaved_dup_count", len(rows) == 2,
          f"expected 2, got {len(rows)}")
    check("interleaved_t1_once", sum(1 for r in rows if r["symbol"] == "T1") == 1)
    check("interleaved_t2_once", sum(1 for r in rows if r["symbol"] == "T2") == 1)


print("\n[Test 4] Interleaved duplicate (T1, T2, T1 again) → 2 rows not 3")
_with_tmp_log(test_interleaved_duplicate)


# ── Test 5: cross-restart deduplication via CSV seed ─────────────────────────
def test_cross_restart_dedup(tmp: str) -> None:
    from platform_core.paper_trading.trade_logger import TradeLogger

    # First "process run": write one trade
    logger1 = TradeLogger()
    logger1.log_trade(trade=_make_trade("BNK26JUN54000CE", entry=500.0, exit_=550.0))
    rows_after_first = _read_csv(tmp)
    check("cross_restart_first_run", len(rows_after_first) == 1)

    # Second "process run": new TradeLogger reads existing CSV, seeds dedup set
    logger2 = TradeLogger()
    check("cross_restart_seeded", len(logger2._logged_keys) >= 1,
          f"expected ≥1 seeded key, got {len(logger2._logged_keys)}")

    # Attempt to log the same trade again — should be suppressed
    logger2.log_trade(trade=_make_trade("BNK26JUN54000CE", entry=500.0, exit_=550.0))
    rows_after_second = _read_csv(tmp)
    check("cross_restart_no_dup", len(rows_after_second) == 1,
          f"expected 1 row, got {len(rows_after_second)}")

    # A genuinely new trade goes through
    logger2.log_trade(trade=_make_trade("BNK26JUN54000CE", entry=600.0, exit_=660.0))
    rows_after_new = _read_csv(tmp)
    check("cross_restart_new_trade_written", len(rows_after_new) == 2,
          f"expected 2 rows, got {len(rows_after_new)}")


print("\n[Test 5] Cross-restart dedup: new TradeLogger seeds from CSV, blocks old duplicate")
_with_tmp_log(test_cross_restart_dedup)


# ── Test 6: unreadable CSV → graceful fallback, in-process guard still works ──
def test_unreadable_csv_fallback(tmp: str) -> None:
    # Write a mismatched-header CSV that DictReader can open but yields no valid keys
    Path(tmp).write_text("bad_col1,bad_col2\n")  # columns mismatch, but readable
    from platform_core.paper_trading.trade_logger import TradeLogger
    logger = TradeLogger()
    # _seed_from_csv must not crash; it will seed 0 or 1 key (the malformed row)
    check("fallback_no_crash", True)
    # In-process guard: after writing the first trade, the key enters _logged_keys
    logger.log_trade(trade=_make_trade("BNK26JUN54000CE"))
    t_key_count_after_first = sum(
        1 for k in logger._logged_keys if k[0] == "BNK26JUN54000CE"
    )
    check("fallback_key_in_set", t_key_count_after_first == 1,
          f"expected 1 matching key, got {t_key_count_after_first}")
    # Second call: same trade suppressed in-process
    logger.log_trade(trade=_make_trade("BNK26JUN54000CE"))
    t_key_count_after_second = sum(
        1 for k in logger._logged_keys if k[0] == "BNK26JUN54000CE"
    )
    check("fallback_in_process_guard",
          t_key_count_after_second == 1,  # set size unchanged
          f"expected 1 key in set, got {t_key_count_after_second}")


print("\n[Test 6] Malformed/mismatched CSV → no crash, in-process guard still works")
_with_tmp_log(test_unreadable_csv_fallback)


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
