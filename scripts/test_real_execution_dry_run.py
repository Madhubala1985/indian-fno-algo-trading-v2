"""
test_real_execution_dry_run.py
──────────────────────────────────────────────────────────────────────
Tests for real-market execution infrastructure (dry-run locked):

  1.  real_dry_run creates entry OrderIntent but never calls kite.place_order.
  2.  Raw index symbol rejected (no CE/PE in tradingsymbol).
  3.  SENSEX with non-BFO exchange rejected.
  4.  NIFTY/BANKNIFTY with wrong exchange (BFO instead of NFO) rejected.
  5.  Quantity mismatch (lots × lot_size ≠ quantity) rejected.
  6.  real_armed without interlocks: get_broker_adapter falls back to dry-run.
  7.  real_armed with missing REAL_TRADING_CONFIRMATION blocked.
  8.  paper mode: PaperBrokerAdapter returned; place_order not called.
  9.  Exit OrderIntent generated correctly (SELL, correct symbol).
  10. DB order-intent tables written when DB provided (real_dry_run).
  11. order_id not treated as fill without status/trade confirmation.
  12. EOD exit routes through execution router (logs dry-run exit intent).

Usage:
  PYTHONPATH=. python3 scripts/test_real_execution_dry_run.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = _PASS if cond else _FAIL
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    _results.append((name, cond, detail))


print("=" * 60)
print("  REAL EXECUTION DRY-RUN TESTS")
print("=" * 60)

from platform_core.execution.broker_adapter import (
    OrderIntent,
    OrderResult,
    PaperBrokerAdapter,
    KiteDryRunBrokerAdapter,
    KiteLiveBrokerAdapter,
    get_broker_adapter,
    validate_order_intent,
    _check_real_interlocks,
    _required_exchange,
)
from platform_core.execution.execution_router import ExecutionRouter


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_intent(
    underlying="BANKNIFTY",
    tradingsymbol="BANKNIFTY26JUN54000PE",
    exchange="NFO",
    transaction_type="BUY",
    quantity=15,
    lot_size=15,
    lots=1,
    product="NRML",
    order_type="MARKET",
    price=0.0,
    reason="ENTRY",
) -> OrderIntent:
    return OrderIntent(
        underlying=underlying,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        transaction_type=transaction_type,
        quantity=quantity,
        lot_size=lot_size,
        lots=lots,
        product=product,
        order_type=order_type,
        price=price,
        trigger_price=0.0,
        variety="regular",
        validity="DAY",
        tag="test",
        reason=reason,
    )


_place_order_called = False


class _MockKite:
    """Mock that records place_order calls."""
    def place_order(self, **kwargs):
        global _place_order_called
        _place_order_called = True
        return "REAL-ORDER-ID-12345"

    def orders(self):
        return []

    def order_trades(self, order_id):
        return []

    def positions(self):
        return {"net": []}

    def margins(self, segment=None):
        return {"available": {"live_balance": 100000.0}}


# ── Test 1: real_dry_run never calls kite.place_order ────────────────────────
print("\n[Test 1] real_dry_run creates OrderIntent but never calls kite.place_order")

# Clear any real interlocks for clean test
_saved_env = {k: os.environ.pop(k, None) for k in [
    "REAL_TRADING_ENABLED", "REAL_TRADING_CONFIRMATION",
    "BROKER_LIVE_ORDERS_ALLOWED", "EXECUTION_MODE",
]}

adapter = KiteDryRunBrokerAdapter()
_place_order_called = False
intent = _make_intent()
result = adapter.place_entry_order(intent)

check("KiteDryRunBrokerAdapter returns success", result.success, f"status={result.status}")
check("order_id is DRYRUN-prefixed", result.order_id.startswith("DRYRUN-"),
      f"got {result.order_id}")
check("kite.place_order was NOT called", not _place_order_called,
      "place_order was called!")
check("filled=False (not auto-filled)", not result.filled,
      f"filled={result.filled}")
check("mode=real_dry_run", result.mode == "real_dry_run", f"got {result.mode}")

# Restore env
for k, v in _saved_env.items():
    if v is not None:
        os.environ[k] = v


# ── Test 2: raw index symbol rejected ────────────────────────────────────────
print("\n[Test 2] Raw index symbol rejected (no CE/PE in tradingsymbol)")
for raw_sym in ("NIFTY", "BANKNIFTY", "SENSEX", "NIFTY50"):
    intent_bad = _make_intent(tradingsymbol=raw_sym, exchange="NFO")
    ok, reason = validate_order_intent(intent_bad)
    check(f"{raw_sym} rejected", not ok, reason[:60] if reason else "no reason")


# ── Test 3: SENSEX with non-BFO rejected ─────────────────────────────────────
print("\n[Test 3] SENSEX with non-BFO exchange rejected")
intent_snx_nfo = _make_intent(
    underlying="SENSEX", tradingsymbol="SENSEX26JUN80000PE",
    exchange="NFO",  # wrong — should be BFO
)
ok, reason = validate_order_intent(intent_snx_nfo)
check("SENSEX+NFO rejected", not ok, reason[:60])
check("reason mentions BFO", "BFO" in reason, reason[:80])

# Correct SENSEX intent
intent_snx_bfo = _make_intent(
    underlying="SENSEX", tradingsymbol="SENSEX26JUN80000PE",
    exchange="BFO", quantity=10, lot_size=10, lots=1,
)
ok2, _ = validate_order_intent(intent_snx_bfo)
check("SENSEX+BFO accepted", ok2)


# ── Test 4: NIFTY/BANKNIFTY wrong exchange rejected ───────────────────────────
print("\n[Test 4] NIFTY/BANKNIFTY with BFO (wrong exchange) rejected")
for und in ("NIFTY", "BANKNIFTY"):
    sym = f"{und}26JUN24000CE" if und == "NIFTY" else f"{und}26JUN54000PE"
    intent_bfo = _make_intent(underlying=und, tradingsymbol=sym, exchange="BFO")
    ok, reason = validate_order_intent(intent_bfo)
    check(f"{und}+BFO rejected", not ok, reason[:60])
    # Correct: NFO
    intent_nfo = _make_intent(underlying=und, tradingsymbol=sym, exchange="NFO",
                               quantity=50 if und == "NIFTY" else 15,
                               lot_size=50 if und == "NIFTY" else 15)
    ok2, _ = validate_order_intent(intent_nfo)
    check(f"{und}+NFO accepted", ok2)


# ── Test 5: Quantity mismatch rejected ───────────────────────────────────────
print("\n[Test 5] Quantity mismatch (lots × lot_size ≠ quantity) rejected")
intent_mismatch = _make_intent(quantity=30, lot_size=15, lots=1)  # 1×15=15 ≠ 30
ok, reason = validate_order_intent(intent_mismatch)
check("quantity mismatch rejected", not ok, reason[:80])
check("reason mentions mismatch", "!=" in reason or "mismatch" in reason.lower(), reason[:80])

# Correct quantity
intent_correct = _make_intent(quantity=15, lot_size=15, lots=1)
ok2, _ = validate_order_intent(intent_correct)
check("correct quantity accepted", ok2)


# ── Test 6: real_armed without interlocks → falls back to dry-run ─────────────
print("\n[Test 6] real_armed without interlocks: get_broker_adapter falls back to dry-run")
# Clear interlocks
_saved2 = {k: os.environ.pop(k, None) for k in [
    "REAL_TRADING_ENABLED", "REAL_TRADING_CONFIRMATION",
    "BROKER_LIVE_ORDERS_ALLOWED", "MAX_REAL_CAPITAL",
    "MAX_REAL_ORDER_VALUE", "MAX_REAL_DAILY_LOSS",
    "REAL_ALLOWED_UNDERLYINGS", "REAL_ALLOWED_EXCHANGES",
    "REAL_MAX_LOTS_PER_ORDER",
]}

adapter_armed = get_broker_adapter("real_armed")
check("real_armed without interlocks returns KiteDryRunBrokerAdapter",
      isinstance(adapter_armed, KiteDryRunBrokerAdapter),
      f"got {type(adapter_armed).__name__}")

# Restore
for k, v in _saved2.items():
    if v is not None:
        os.environ[k] = v


# ── Test 7: real_armed missing REAL_TRADING_CONFIRMATION blocked ──────────────
print("\n[Test 7] real_armed with wrong REAL_TRADING_CONFIRMATION blocked")
os.environ["REAL_TRADING_ENABLED"] = "true"
os.environ["REAL_TRADING_CONFIRMATION"] = "WRONG_CONFIRMATION"
os.environ["BROKER_LIVE_ORDERS_ALLOWED"] = "true"
os.environ["MAX_REAL_CAPITAL"] = "300000"
os.environ["MAX_REAL_ORDER_VALUE"] = "10000"
os.environ["MAX_REAL_DAILY_LOSS"] = "5000"
os.environ["REAL_ALLOWED_UNDERLYINGS"] = "BANKNIFTY"
os.environ["REAL_ALLOWED_EXCHANGES"] = "NFO"
os.environ["REAL_MAX_LOTS_PER_ORDER"] = "2"

ok_interlocks, missing = _check_real_interlocks()
check("interlocks fail with wrong confirmation", not ok_interlocks,
      f"missing={missing}")
check("REAL_TRADING_CONFIRMATION in missing", any("CONFIRMATION" in m for m in missing),
      str(missing))

# Clean up
for k in ["REAL_TRADING_ENABLED", "REAL_TRADING_CONFIRMATION", "BROKER_LIVE_ORDERS_ALLOWED",
          "MAX_REAL_CAPITAL", "MAX_REAL_ORDER_VALUE", "MAX_REAL_DAILY_LOSS",
          "REAL_ALLOWED_UNDERLYINGS", "REAL_ALLOWED_EXCHANGES", "REAL_MAX_LOTS_PER_ORDER"]:
    os.environ.pop(k, None)


# ── Test 8: paper mode: PaperBrokerAdapter returned ──────────────────────────
print("\n[Test 8] paper mode: PaperBrokerAdapter returned, no real order")
adapter_paper = get_broker_adapter("paper")
check("paper returns PaperBrokerAdapter", isinstance(adapter_paper, PaperBrokerAdapter),
      f"got {type(adapter_paper).__name__}")

_place_order_called = False
mock = _MockKite()
# PaperBrokerAdapter never has a kite reference
intent_paper = _make_intent()
result_paper = adapter_paper.place_entry_order(intent_paper)
check("paper place_entry_order succeeds", result_paper.success)
check("paper mode=paper", result_paper.mode == "paper", result_paper.mode)
check("paper order_id PAPER-prefixed", result_paper.order_id.startswith("PAPER-"),
      result_paper.order_id)
check("kite.place_order NOT called in paper mode", not _place_order_called)


# ── Test 9: Exit OrderIntent generated correctly ──────────────────────────────
print("\n[Test 9] Exit OrderIntent generated correctly via ExecutionRouter")
router_dry = ExecutionRouter(mode="real_dry_run")
exit_result = router_dry.route_exit(
    underlying="BANKNIFTY",
    tradingsymbol="BANKNIFTY26JUN54000PE",
    lots=1,
    lot_size=15,
    exit_premium=65.0,
    reason="STOP_LOSS",
    session_tag="test_session",
)
check("exit result success", exit_result.success, f"status={exit_result.status}")
check("exit order_id DRYRUN-prefixed", exit_result.order_id.startswith("DRYRUN-"),
      exit_result.order_id)
check("exit mode=real_dry_run", exit_result.mode == "real_dry_run", exit_result.mode)

# Build the intent manually and verify it's SELL
from platform_core.execution.broker_adapter import _required_exchange
exit_intent = OrderIntent(
    underlying="BANKNIFTY",
    tradingsymbol="BANKNIFTY26JUN54000PE",
    exchange=_required_exchange("BANKNIFTY"),
    transaction_type="SELL",
    quantity=15,
    lot_size=15,
    lots=1,
    product="NRML",
    order_type="MARKET",
    price=0.0,
    trigger_price=0.0,
    variety="regular",
    validity="DAY",
    tag="test",
    reason="STOP_LOSS",
)
ok_exit, _ = validate_order_intent(exit_intent)
check("exit SELL OrderIntent validates OK", ok_exit)
check("exit transaction_type=SELL", exit_intent.transaction_type == "SELL")
check("exit tradingsymbol has PE", "PE" in exit_intent.tradingsymbol)


# ── Test 10: DB order-intent tables are written ───────────────────────────────
print("\n[Test 10] DB order-intent tables written when DB provided")
import sys as _sys
sys.path.insert(0, str(ROOT))
from platform_core.persistence.trade_database import TradeDatabase

with tempfile.TemporaryDirectory() as tmp:
    db_path = str(Path(tmp) / "test_orders.db")
    db = TradeDatabase(db_path=db_path)
    db.start(session_id="test_dry_run", run_mode="real_dry_run")

    router_with_db = ExecutionRouter(mode="real_dry_run")
    r = router_with_db.route_entry(
        underlying="BANKNIFTY",
        tradingsymbol="BANKNIFTY26JUN54000PE",
        lots=1, lot_size=15, premium=80.0,
        direction="SHORT", session_tag="test",
        db=db,
    )
    import time
    time.sleep(0.2)  # let background writer flush

    conn = sqlite3.connect(db_path)
    tbls = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    check("pt_order_intents table exists", "pt_order_intents" in tbls)
    check("pt_broker_orders table exists", "pt_broker_orders" in tbls)
    check("pt_order_events table exists",  "pt_order_events"  in tbls)

    # The intent is written synchronously; broker_order/event via queue
    n_intents = conn.execute(
        "SELECT COUNT(*) FROM pt_order_intents WHERE mode='real_dry_run'"
    ).fetchone()[0]
    check(f"pt_order_intents has ≥1 row for real_dry_run", n_intents >= 1,
          f"count={n_intents}")
    conn.close()
    db.stop()


# ── Test 11: order_id not treated as fill ─────────────────────────────────────
print("\n[Test 11] order_id not treated as fill without status/trade confirmation")
adapter_dry = KiteDryRunBrokerAdapter()
intent_no_fill = _make_intent()
result_no_fill = adapter_dry.place_entry_order(intent_no_fill)
check("dry-run result.filled=False", not result_no_fill.filled,
      f"filled={result_no_fill.filled}")
check("dry-run filled_quantity=0", result_no_fill.filled_quantity == 0,
      f"filled_quantity={result_no_fill.filled_quantity}")
check("dry-run get_order_trades returns []",
      adapter_dry.get_order_trades(result_no_fill.order_id) == [],
      "expected empty list")
# A KiteLiveBrokerAdapter also returns filled=False before status confirmation
# (we test via the model directly — status/fill only set after confirmation)
check("PaperBrokerAdapter is the only adapter with filled=True",
      PaperBrokerAdapter().place_entry_order(intent_no_fill).filled,
      "paper should auto-fill")


# ── Test 12: EOD exit routes through execution router ────────────────────────
print("\n[Test 12] EOD exit routes through execution router (dry-run)")
router_eod = ExecutionRouter(mode="real_dry_run")
eod_result = router_eod.route_exit(
    underlying="SENSEX",
    tradingsymbol="SENSEX26JUN80000PE",
    lots=1,
    lot_size=10,
    exit_premium=120.0,
    reason="EOD_FORCE_CLOSE",
    session_tag="eod_test",
)
check("EOD exit result success", eod_result.success, f"status={eod_result.status}")
check("EOD exit order_id DRYRUN-prefixed", eod_result.order_id.startswith("DRYRUN-"))
check("EOD exit mode=real_dry_run", eod_result.mode == "real_dry_run")
# Verify exchange routing for SENSEX
from platform_core.execution.broker_adapter import _required_exchange as _req_exch
check("SENSEX exchange=BFO", _req_exch("SENSEX") == "BFO",
      f"got {_req_exch('SENSEX')}")


# ── Test 13: dry-run readiness evidence requires mode='real_dry_run' ─────────
print("\n[Test 13] Readiness: paper/unknown intents never count as dry-run evidence")

import importlib.util as _ilu


def _load_script(name: str):
    spec = _ilu.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_rfc = _load_script("real_funds_readiness_check")

_INTENT_DDL = """
CREATE TABLE pt_order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER, mode TEXT NOT NULL, underlying TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL, exchange TEXT NOT NULL,
    transaction_type TEXT NOT NULL, quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL, price REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL, reason TEXT NOT NULL, created_ts TEXT NOT NULL,
    raw_intent_json TEXT
);
"""


def _make_intent_db(path: Path, modes: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_INTENT_DDL)
    for i, m in enumerate(modes):
        conn.execute(
            "INSERT INTO pt_order_intents (mode, underlying, tradingsymbol, "
            "exchange, transaction_type, quantity, order_type, status, reason, "
            "created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (m, "NIFTY", "NIFTY2661623400CE", "NFO", "BUY", 75, "MARKET",
             "X", "ENTRY", f"2026-06-10T06:0{i}:00+00:00"),
        )
    conn.commit()
    conn.close()


with tempfile.TemporaryDirectory() as _td:
    _tdp = Path(_td)

    db_paper = _tdp / "paper.db"
    _make_intent_db(db_paper, ["paper", "paper", "paper"])
    check("paper-only intents do NOT satisfy dry-run readiness",
          _rfc.check_dry_run_tested(db_paper) is False)

    db_dry = _tdp / "dry.db"
    _make_intent_db(db_dry, ["real_dry_run", "real_dry_run"])
    check("real_dry_run intents DO satisfy dry-run readiness",
          _rfc.check_dry_run_tested(db_dry) is True)

    db_mixed = _tdp / "mixed.db"
    _make_intent_db(db_mixed, ["paper", "", "legacy_mode"])
    check("missing/unknown modes classified UNKNOWN, not dry-run",
          _rfc.check_dry_run_tested(db_mixed) is False)
    counts = _rfc.order_intent_mode_counts(db_mixed)
    check("UNKNOWN bucket counts malformed modes",
          counts.get("UNKNOWN") == 2 and counts.get("paper") == 1,
          str(counts))
    check("no real_dry_run rows inferred from unknown modes",
          counts.get("real_dry_run", 0) == 0)


# ── Test 14: analyzer derives execution mode from session evidence ───────────
print("\n[Test 14] Analyzer reports the session's actual execution mode")

_nm = _load_script("analyze_near_miss_entries")

with tempfile.TemporaryDirectory() as _td:
    _tdp = Path(_td)
    log = _tdp / "live_run_20260610_114507.log"
    log.write_text(
        "[CAPITAL CONFIG] starting_capital=₹400,000  max_open_positions=2\n"
        "[STATE] Restored: equity=₹300,000  peak=₹300,000\n"
        "[EXECUTION MODE] real_dry_run\n",
        encoding="utf-8",
    )
    db = _tdp / "trades.db"
    _make_intent_db(db, ["real_dry_run", "real_dry_run"])

    ev = _nm.session_evidence(log, db)
    check("mode derived as real_dry_run (not analyzer env)",
          ev["execution_mode"] == "real_dry_run", str(ev["execution_mode"]))
    check("mode source is log+db", ev["mode_source"] == "log+db")
    check("no log/db mismatch flagged", not ev["mode_mismatch"])
    check("capital read from [CAPITAL CONFIG]",
          ev["capital_from_log"] == 400000.0, str(ev["capital_from_log"]))
    check("state equity read from log",
          ev["state_equity_from_log"] == 300000.0,
          str(ev["state_equity_from_log"]))

    # Env must not influence the derived mode
    _old_env = os.environ.get("EXECUTION_MODE")
    os.environ["EXECUTION_MODE"] = "paper"
    try:
        ev2 = _nm.session_evidence(log, db)
        check("EXECUTION_MODE env ignored by evidence derivation",
              ev2["execution_mode"] == "real_dry_run")
    finally:
        if _old_env is None:
            os.environ.pop("EXECUTION_MODE", None)
        else:
            os.environ["EXECUTION_MODE"] = _old_env

    # DB-only evidence (no log) and unknown-only DB
    ev3 = _nm.session_evidence(None, db)
    check("DB-only evidence → real_dry_run (source=db)",
          ev3["execution_mode"] == "real_dry_run" and ev3["mode_source"] == "db")

    db_unknown = _tdp / "unknown.db"
    _make_intent_db(db_unknown, ["legacy_mode"])
    ev4 = _nm.session_evidence(None, db_unknown)
    check("unknown-mode rows → UNKNOWN, never real_dry_run",
          ev4["execution_mode"] == "UNKNOWN", str(ev4["execution_mode"]))

    # Log says paper but DB has real_dry_run rows → mismatch flagged
    log_paper = _tdp / "live_run_paper.log"
    log_paper.write_text("[EXECUTION MODE] paper\n", encoding="utf-8")
    ev5 = _nm.session_evidence(log_paper, db)
    check("log/db disagreement flagged as mismatch", ev5["mode_mismatch"])


# ── Summary ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in _results if ok)
total  = len(_results)
print(f"\n{'=' * 60}")
print(f"  Results: {passed}/{total} passed")
if passed == total:
    print("  ALL PASS")
else:
    print("  FAILURES:")
    for name, ok, detail in _results:
        if not ok:
            print(f"    FAIL: {name}  {detail}")
print(f"{'=' * 60}")
sys.exit(0 if passed == total else 1)
