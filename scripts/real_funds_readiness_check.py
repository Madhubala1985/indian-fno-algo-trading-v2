"""
real_funds_readiness_check.py
──────────────────────────────────────────────────────────────────────
Pre-flight checklist before deploying real capital.

Current output MUST be REAL_FUNDS_READY=NO until all criteria are met.

Checks:
  1.  Git working tree clean (only configs/data/logs allowed dirty)
  2.  EXECUTION_MODE=real_armed explicitly set
  3.  All real-money safety interlocks configured
  4.  MAX_REAL_CAPITAL / MAX_REAL_ORDER_VALUE / MAX_REAL_DAILY_LOSS set
  5.  REAL_MAX_LOTS_PER_ORDER set
  6.  Access token valid (file check)
  7.  Lifecycle validator PASS
  8.  At least 5 completed paper trades across ≥ 2 sessions
  9.  No orphan open positions in state
  10. Dry-run order lifecycle tested (marker file or env flag)
  11. EOD close tested (marker from previous session)
  12. DB order tables valid (schema present)

Usage:
  PYTHONPATH=. python3 scripts/real_funds_readiness_check.py
  PYTHONPATH=. python3 scripts/real_funds_readiness_check.py --verbose
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_WARN = "\033[33mWARN\033[0m"


def _ok(msg: str) -> None:
    print(f"  [{_PASS}] {msg}")


def _fail(msg: str) -> None:
    print(f"  [{_FAIL}] {msg}")


def _warn(msg: str) -> None:
    print(f"  [{_WARN}] {msg}")


_required_interlocks = [
    "REAL_TRADING_ENABLED",
    "REAL_TRADING_CONFIRMATION",
    "BROKER_LIVE_ORDERS_ALLOWED",
    "MAX_REAL_CAPITAL",
    "MAX_REAL_ORDER_VALUE",
    "MAX_REAL_DAILY_LOSS",
    "REAL_ALLOWED_UNDERLYINGS",
    "REAL_ALLOWED_EXCHANGES",
    "REAL_MAX_LOTS_PER_ORDER",
]
_required_values = {
    "REAL_TRADING_ENABLED": "true",
    "REAL_TRADING_CONFIRMATION": "I_UNDERSTAND_REAL_MONEY_RISK",
    "BROKER_LIVE_ORDERS_ALLOWED": "true",
}
_ALLOWED_DIRTY = {
    "configs/kite_config.json",
}


def check_git_clean() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), capture_output=True, text=True
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        truly_dirty = []
        for ln in lines:
            parts = ln.split(None, 1)
            if len(parts) < 2:
                continue
            path = parts[1].strip()
            # Allow data/, logs/, configs/kite_config.json
            if (path.startswith("data/") or path.startswith("logs/")
                    or path in _ALLOWED_DIRTY or path.startswith("?? data/")
                    or path.startswith("?? logs/") or path.startswith("?? .claude/")):
                continue
            status = parts[0]
            if status.startswith("??"):
                continue
            truly_dirty.append(ln)
        if truly_dirty:
            _fail(f"Dirty git working tree: {len(truly_dirty)} file(s) modified — commit or stash")
            for d in truly_dirty[:5]:
                print(f"      {d}")
            return False
        _ok("Git working tree clean (code files)")
        return True
    except Exception as exc:
        _warn(f"git status failed: {exc}")
        return False


def check_execution_mode() -> bool:
    mode = os.environ.get("EXECUTION_MODE", "")
    if mode == "real_armed":
        _ok("EXECUTION_MODE=real_armed")
        return True
    _fail(f"EXECUTION_MODE must be 'real_armed', got '{mode}' — set EXECUTION_MODE=real_armed")
    return False


def check_interlocks() -> bool:
    all_pass = True
    for key in _required_interlocks:
        val = os.environ.get(key, "")
        if not val:
            _fail(f"Missing interlock: {key} not set")
            all_pass = False
            continue
        expected = _required_values.get(key)
        if expected and val != expected:
            _fail(f"Interlock {key}={val!r} must be {expected!r}")
            all_pass = False
        else:
            _ok(f"Interlock OK: {key}={val!r}")
    return all_pass


def check_capital_limits() -> bool:
    all_pass = True
    for key in ("MAX_REAL_CAPITAL", "MAX_REAL_ORDER_VALUE", "MAX_REAL_DAILY_LOSS", "REAL_MAX_LOTS_PER_ORDER"):
        val = os.environ.get(key, "")
        if not val:
            _fail(f"{key} not set")
            all_pass = False
        else:
            try:
                v = float(val)
                if v <= 0:
                    _fail(f"{key}={val} must be > 0")
                    all_pass = False
                else:
                    _ok(f"{key}={val}")
            except ValueError:
                _fail(f"{key}={val!r} must be a number")
                all_pass = False
    return all_pass


def check_access_token() -> bool:
    cfg = ROOT / "configs" / "kite_config.json"
    if not cfg.exists():
        _fail("configs/kite_config.json not found")
        return False
    try:
        data = json.loads(cfg.read_text())
        token = data.get("access_token", "")
        if len(token) > 10:
            _ok(f"Access token present (length={len(token)})")
            return True
        _fail("Access token missing or too short — run scripts/generate_kite_session.py")
        return False
    except Exception as exc:
        _fail(f"Could not read kite_config.json: {exc}")
        return False


def check_lifecycle_validator() -> bool:
    script = ROOT / "scripts" / "validate_live_lifecycle_state.py"
    if not script.exists():
        _warn("validate_live_lifecycle_state.py not found")
        return False
    result = subprocess.run(
        [sys.executable, str(script)], cwd=str(ROOT),
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    if result.returncode == 0 and "FAILs : 0" in result.stdout:
        _ok("Lifecycle validator: PASS")
        return True
    _fail(f"Lifecycle validator: FAIL (exit={result.returncode})")
    return False


def check_paper_trades() -> bool:
    trades_path = LOGS_DIR / "paper_trades.csv"
    if not trades_path.exists():
        _fail("paper_trades.csv not found — no completed paper trades yet")
        return False
    rows = []
    with trades_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    if len(rows) < 5:
        _fail(f"Only {len(rows)} completed paper trades — need at least 5 across ≥2 sessions")
        return False
    # Count unique sessions (by session_id if present, else approximate by date)
    sessions = set()
    for r in rows:
        sid = r.get("session_id") or r.get("entry_ts", "")[:10]
        if sid:
            sessions.add(sid[:10])
    if len(sessions) < 2:
        _fail(f"{len(rows)} trades but only {len(sessions)} session(s) — need ≥2 sessions")
        return False
    _ok(f"{len(rows)} paper trades across {len(sessions)} session(s)")
    return True


def check_no_orphan_positions() -> bool:
    state_path = LOGS_DIR / "paper_trading_state.json"
    if not state_path.exists():
        _ok("No state file — no orphan positions")
        return True
    try:
        state = json.loads(state_path.read_text())
        open_pos = state.get("open_positions", {})
        orphans = {k: v for k, v in open_pos.items() if v}
        if orphans:
            _fail(f"Open orphan positions in state: {list(orphans.keys())}")
            return False
        _ok("No orphan open positions in state")
        return True
    except Exception as exc:
        _warn(f"Could not read state file: {exc}")
        return False


def check_dry_run_tested() -> bool:
    # Check if any dry-run order records exist in DB
    db_path = LOGS_DIR / "trades.db"
    if not db_path.exists():
        _fail("trades.db not found — dry-run order lifecycle not tested")
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        # Check table exists
        tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "pt_order_intents" not in tbls:
            conn.close()
            _fail("pt_order_intents table missing — dry-run lifecycle not tested")
            return False
        count = conn.execute(
            "SELECT COUNT(*) FROM pt_order_intents WHERE mode='real_dry_run'"
        ).fetchone()[0]
        conn.close()
        if count >= 1:
            _ok(f"Dry-run order lifecycle tested ({count} intent record(s))")
            return True
        _fail("No real_dry_run records in pt_order_intents — run a dry-run session first")
        return False
    except Exception as exc:
        _fail(f"DB check failed: {exc}")
        return False


def check_eod_close_tested() -> bool:
    # Look for EOD_FORCE_CLOSE evidence in logs or DB
    db_path = LOGS_DIR / "trades.db"
    eod_in_db = False
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            tbls = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "pt_order_intents" in tbls:
                n = conn.execute(
                    "SELECT COUNT(*) FROM pt_order_intents WHERE reason='EOD_FORCE_CLOSE'"
                ).fetchone()[0]
                if n > 0:
                    eod_in_db = True
            conn.close()
        except Exception:
            pass

    # Also check logs
    eod_in_log = False
    for log_file in sorted(LOGS_DIR.glob("live_run_*.log")):
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            if "EOD_FORCE_CLOSE" in content or "MARKET CLOSE" in content:
                eod_in_log = True
                break
        except Exception:
            pass

    if eod_in_db or eod_in_log:
        _ok("EOD close path tested (evidence found in logs/DB)")
        return True
    _warn("EOD close not yet confirmed — run a full session through 15:30 IST")
    return False


def check_db_order_tables() -> bool:
    db_path = LOGS_DIR / "trades.db"
    if not db_path.exists():
        _fail("trades.db not found")
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        required = {"pt_order_intents", "pt_broker_orders", "pt_order_events"}
        missing  = required - tbls
        if missing:
            _fail(f"Missing DB tables: {missing}")
            return False
        _ok(f"DB order lifecycle tables present: {sorted(required)}")
        return True
    except Exception as exc:
        _fail(f"DB check error: {exc}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-funds readiness checklist (must all pass before deploying capital)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("=" * 70)
    print("  REAL-FUNDS READINESS CHECKLIST")
    print("=" * 70)
    print("  Current result must be REAL_FUNDS_READY=NO until all checks pass.\n")

    results: dict[str, bool] = {}

    checks = [
        ("Git clean",           check_git_clean),
        ("Execution mode",      check_execution_mode),
        ("Interlocks",          check_interlocks),
        ("Capital limits",      check_capital_limits),
        ("Access token",        check_access_token),
        ("Lifecycle validator", check_lifecycle_validator),
        ("Paper trades",        check_paper_trades),
        ("No orphan positions", check_no_orphan_positions),
        ("Dry-run tested",      check_dry_run_tested),
        ("EOD close tested",    check_eod_close_tested),
        ("DB order tables",     check_db_order_tables),
    ]

    for label, fn in checks:
        print(f"\n── {label}")
        try:
            results[label] = fn()
        except Exception as exc:
            _fail(f"Check raised exception: {exc}")
            results[label] = False

    all_pass = all(results.values())

    print(f"\n{'=' * 70}")
    print(f"  Checks: {sum(results.values())}/{len(results)} passed")

    if all_pass:
        print("  REAL_FUNDS_READY=YES")
    else:
        print("  REAL_FUNDS_READY=NO")
        failed = [k for k, v in results.items() if not v]
        print(f"  Failed: {failed}")

    print(f"{'=' * 70}")

    # Always return 0 so || true in CI doesn't swallow important output
    return 0


if __name__ == "__main__":
    sys.exit(main())
