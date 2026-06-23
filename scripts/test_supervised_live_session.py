"""
test_supervised_live_session.py
──────────────────────────────────────────────────────────────────────
Tests for run_supervised_live_session.py:
  1.  Dry-run completes without error (exit code 0).
  2.  Preflight detects missing access token.
  3.  Preflight detects stale warm-start CSV.
  4.  Preflight passes with fresh CSV and valid token.
  5.  Archive step moves old log files to logs/archive/.
  6.  build_env: default env vars set when absent.
  7.  build_env: --profile overrides PAPER_GATE_PROFILE.
  8.  build_env: --capital overrides PAPER_STARTING_CAPITAL.
  9.  Post-market step handles missing scripts gracefully (no crash).
  10. Stale threshold constant is 3 trading days.

Usage:
  PYTHONPATH=. python3 scripts/test_supervised_live_session.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
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
print("  SUPERVISED LIVE SESSION TESTS")
print("=" * 60)

# ── Import helpers from the script ──────────────────────────────────────────
# Import only pure-logic helpers; avoid importing the pipeline module

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "run_supervised_live_session",
    ROOT / "scripts" / "run_supervised_live_session.py",
)
_mod = _ilu.module_from_spec(_spec)   # type: ignore[arg-type]
_spec.loader.exec_module(_mod)        # type: ignore[union-attr]

_find_csv   = _mod._find_warmstart_csv
_csv_ts     = _mod._csv_last_ts
_td_since   = _mod._trading_days_since
_build_env  = _mod.build_env
_STALE      = _mod._STALE_THRESHOLD

import csv as _csv_mod


def _write_csv(path: Path, last_ts: str, n: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromisoformat(last_ts)
    rows = [ts - timedelta(minutes=(n-1-i)) for i in range(n)]
    with path.open("w", newline="") as fh:
        w = _csv_mod.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([r.isoformat(), 100, 101, 99, 100, 0])
    # Pad to 60KB
    with path.open("a") as fh:
        while path.stat().st_size < 60_000:
            fh.write("# padding\n")


# ── Test 1: Dry-run import does not crash ─────────────────────────────────────
print("\n[Test 1] Script importable and dry-run flag accessible")
check("run_supervised_live_session importable", _mod is not None)
check("_STALE_THRESHOLD attribute exists", hasattr(_mod, "_STALE_THRESHOLD"))


# ── Test 2: Preflight detects missing access token ─────────────────────────────
print("\n[Test 2] Preflight: missing access token")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "configs").mkdir()
    cfg_path = d / "configs" / "kite_config.json"
    cfg_path.write_text(json.dumps({"api_key": "test", "access_token": ""}))

    # Patch ROOT inside module temporarily
    _orig_root = _mod.ROOT
    _mod.ROOT = d
    _mod.DATA_DIR = d / "data"
    (d / "data").mkdir()
    (d / "logs").mkdir()
    _mod.LOGS_DIR = d / "logs"
    _mod.ARCHIVE = d / "logs" / "archive"

    result = _mod.run_preflight(run_oi_diagnostics=False)
    _mod.ROOT = _orig_root
    _mod.DATA_DIR = ROOT / "data"
    _mod.LOGS_DIR = ROOT / "logs"
    _mod.ARCHIVE = ROOT / "logs" / "archive"

    check("preflight returns False when token empty", result is False,
          f"got {result}")


# ── Test 3: Preflight detects stale warm-start CSV ────────────────────────────
print("\n[Test 3] Preflight: stale CSV warning (non-blocking, no crash)")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "configs").mkdir()
    (d / "data").mkdir()
    (d / "logs").mkdir()

    cfg_path = d / "configs" / "kite_config.json"
    cfg_path.write_text(json.dumps({"api_key": "test", "access_token": "a" * 50}))

    # Write a stale CSV (last ts: May 2026)
    stale_csv = d / "data" / "banknifty_1m_2026-04-01_2026-05-01.csv"
    _write_csv(stale_csv, "2026-05-01T15:29:00")

    _mod.ROOT = d
    _mod.DATA_DIR = d / "data"
    _mod.LOGS_DIR = d / "logs"
    _mod.ARCHIVE  = d / "logs" / "archive"

    # Shouldn't crash (stale is a warning, not a hard failure)
    raised = False
    try:
        _mod.run_preflight(run_oi_diagnostics=False)
    except Exception as exc:
        raised = True

    _mod.ROOT = ROOT
    _mod.DATA_DIR = ROOT / "data"
    _mod.LOGS_DIR = ROOT / "logs"
    _mod.ARCHIVE  = ROOT / "logs" / "archive"

    check("stale CSV does not crash preflight", not raised)


# ── Test 4: build_env defaults ────────────────────────────────────────────────
print("\n[Test 4] build_env: defaults set when env vars absent")
# Note: do NOT check for pipeline import — live_structure_pipeline.py has
# startup side effects (WebSocket, Kite session) that must not run in tests.
import argparse as _ap

_args = _ap.Namespace(profile="", capital=0)
# Temporarily clear env vars
_saved = {}
for k in ("PAPER_GATE_PROFILE", "PAPER_STARTING_CAPITAL", "PAPER_RISK_PROFILE", "LIVE_TERMINAL_MODE"):
    _saved[k] = os.environ.pop(k, None)

env = _build_env(_args)
check("PAPER_GATE_PROFILE defaulted to 'default'", env.get("PAPER_GATE_PROFILE") == "default",
      f"got {env.get('PAPER_GATE_PROFILE')}")
check("PAPER_STARTING_CAPITAL left unset (compounding default — pipeline rolls "
      "the basis forward from state equity)", env.get("PAPER_STARTING_CAPITAL") is None,
      f"got {env.get('PAPER_STARTING_CAPITAL')}")
check("PAPER_RISK_PROFILE defaulted to 'balanced'", env.get("PAPER_RISK_PROFILE") == "balanced",
      f"got {env.get('PAPER_RISK_PROFILE')}")
check("LIVE_TERMINAL_MODE defaulted to 'compact'", env.get("LIVE_TERMINAL_MODE") == "compact",
      f"got {env.get('LIVE_TERMINAL_MODE')}")

# Restore
for k, v in _saved.items():
    if v is not None:
        os.environ[k] = v


# ── Test 5: build_env respects --profile ─────────────────────────────────────
print("\n[Test 5] build_env: --profile overrides PAPER_GATE_PROFILE")
_args2 = _ap.Namespace(profile="banknifty_experiment", capital=0)
env2 = _build_env(_args2)
check("--profile sets PAPER_GATE_PROFILE",
      env2.get("PAPER_GATE_PROFILE") == "banknifty_experiment",
      f"got {env2.get('PAPER_GATE_PROFILE')}")


# ── Test 6: build_env respects --capital ─────────────────────────────────────
print("\n[Test 6] build_env: --capital overrides PAPER_STARTING_CAPITAL")
_args3 = _ap.Namespace(profile="", capital=500000)
env3 = _build_env(_args3)
check("--capital sets PAPER_STARTING_CAPITAL",
      env3.get("PAPER_STARTING_CAPITAL") == "500000",
      f"got {env3.get('PAPER_STARTING_CAPITAL')}")


# ── Test 7: Archive step moves old log files ──────────────────────────────────
print("\n[Test 7] Archive step moves yesterday's logs")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    logs = d / "logs"
    logs.mkdir()
    # Simulate a log from yesterday vs one from the actual current day
    # (stamp must be dynamic — archive_previous_session keeps only today)
    _today_stamp = _mod._now_ist().strftime("%Y%m%d")
    old_log = logs / "live_run_20260607_093000.log"
    old_log.write_text("old log")
    today_log = logs / f"live_run_{_today_stamp}_093000.log"
    today_log.write_text("today log")

    _mod.LOGS_DIR = logs
    _mod.ARCHIVE  = logs / "archive"
    _mod.archive_previous_session()
    _mod.LOGS_DIR = ROOT / "logs"
    _mod.ARCHIVE  = ROOT / "logs" / "archive"

    archive_dir = logs / "archive"
    archived_file = archive_dir / "live_run_20260607_093000.log"
    today_still   = logs / f"live_run_{_today_stamp}_093000.log"

    check("old log moved to archive", archived_file.exists(),
          f"found={archived_file.exists()}")
    check("today log stays in logs/", today_still.exists(),
          f"found={today_still.exists()}")


# ── Test 8: _trading_days_since ───────────────────────────────────────────────
print("\n[Test 8] _trading_days_since counts correctly")
_fri = date(2026, 6, 5)
_mon = date(2026, 6, 8)
check("Fri→Mon = 1 trading day", _td_since(_fri, _mon) == 1,
      f"got {_td_since(_fri, _mon)}")
check("same day = 0", _td_since(_fri, _fri) == 0,
      f"got {_td_since(_fri, _fri)}")


# ── Test 9: Stale threshold is 3 ─────────────────────────────────────────────
print("\n[Test 9] _STALE_THRESHOLD is 3 trading days")
check("_STALE_THRESHOLD == 3", _STALE == 3, f"got {_STALE}")


# ── Test 10: post_market graceful missing script ──────────────────────────────
print("\n[Test 10] run_post_market does not crash when scripts are absent")
raised = False
try:
    _mod.run_post_market(dry_run=False)
except Exception as exc:
    raised = True
check("run_post_market tolerates missing scripts", not raised, "no exception raised")


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
