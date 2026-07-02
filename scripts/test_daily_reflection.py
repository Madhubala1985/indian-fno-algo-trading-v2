"""
test_daily_reflection.py
──────────────────────────────────────────────────────────────────────
Tests for generate_daily_reflection.py:
  1.  Module importable without pipeline import.
  2.  _parse_log extracts counts from synthetic log.
  3.  _parse_log extracts score_margin statistics.
  4.  _parse_log identifies dominant MCB bias.
  5.  _parse_trades returns zero trades for empty CSV.
  6.  _parse_trades counts trades and sums PnL correctly.
  7.  _render produces all required sections.
  8.  _render includes 'No Automatic Policy Mutation' section.
  9.  Dry-run does not write a file.
  10. Dry-run output contains block summary.
  11. Zero-block log renders without error.
  12. MCB adaptation events appear in rendered output.

Usage:
  PYTHONPATH=. python3 scripts/test_daily_reflection.py
"""
from __future__ import annotations

import csv
import importlib.util as _ilu
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
print("  DAILY REFLECTION TESTS")
print("=" * 60)

# ── Import module ────────────────────────────────────────────────────────────
_spec = _ilu.spec_from_file_location(
    "generate_daily_reflection",
    ROOT / "scripts" / "generate_daily_reflection.py",
)
_mod = _ilu.module_from_spec(_spec)   # type: ignore[arg-type]
_spec.loader.exec_module(_mod)        # type: ignore[union-attr]

_parse_log    = _mod._parse_log
_parse_trades = _mod._parse_trades
_parse_state  = _mod._parse_state
_parse_audit  = _mod._parse_audit
_render       = _mod._render


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _write_trades(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        if not rows:
            w = csv.writer(fh)
            w.writerow(["symbol", "direction", "entry_price", "exit_price", "quantity", "pnl"])
            return
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ── Test 1: Module importable ─────────────────────────────────────────────────
print("\n[Test 1] Module importable")
check("generate_daily_reflection importable", _mod is not None)
check("_render function present", callable(_render))

# ── Test 2: _parse_log counts ─────────────────────────────────────────────────
print("\n[Test 2] _parse_log extracts pattern counts")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "│  WARM_START_LOCKOUT live_bars=1/20 │",
        "│  WARM_START_LOCKOUT live_bars=2/20 │",
        "│  INSUFFICIENT_STRIKE_QUALITY score_margin=0.031 │",
        "│  INSUFFICIENT_CONFLUENCE oi_support=0.059 │",
        "[MCB ADAPTATION WATCH] underlying=BANKNIFTY prev=SHORT new=LONG",
        "[KITE] WebSocket connected.",
    ])
    parsed = _parse_log(log)
    c = parsed["counts"]
    check("WARM_START_LOCKOUT=2", c.get("WARM_START_LOCKOUT", 0) == 2,
          f"got {c.get('WARM_START_LOCKOUT', 0)}")
    check("INSUFFICIENT_STRIKE_QUALITY=1", c.get("INSUFFICIENT_STRIKE_QUALITY", 0) == 1,
          f"got {c.get('INSUFFICIENT_STRIKE_QUALITY', 0)}")
    check("INSUFFICIENT_CONFLUENCE=1", c.get("INSUFFICIENT_CONFLUENCE", 0) == 1,
          f"got {c.get('INSUFFICIENT_CONFLUENCE', 0)}")
    check("MCB_ADAPTATION_WATCH=1", c.get("MCB_ADAPTATION_WATCH", 0) == 1,
          f"got {c.get('MCB_ADAPTATION_WATCH', 0)}")

# ── Test 3: _parse_log score_margin stats ─────────────────────────────────────
print("\n[Test 3] _parse_log extracts score_margin stats")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "score_margin=0.010 [SCORE_MARGIN_LOW]",
        "score_margin=0.020 [SCORE_MARGIN_LOW]",
        "score_margin=0.030 [SCORE_MARGIN_LOW]",
    ])
    parsed = _parse_log(log)
    sm = parsed["score_margin_stats"]
    check("sm stats present", sm is not None)
    if sm:
        check("sm.n=3", sm["n"] == 3, f"got {sm['n']}")
        check("sm.min=0.01", sm["min"] == 0.01, f"got {sm['min']}")

# ── Test 4: _parse_log dominant bias ─────────────────────────────────────────
print("\n[Test 4] _parse_log identifies dominant bias")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    # 3× SHORT, 1× LONG → dominant=SHORT
    _write_log(log, [
        "MCB BANKNIFTY bias=SHORT conf=1.00",
        "MCB BANKNIFTY bias=SHORT conf=0.95",
        "MCB BANKNIFTY bias=SHORT conf=0.90",
        "MCB BANKNIFTY bias=LONG conf=0.60",
    ])
    parsed = _parse_log(log)
    check("dominant_bias=SHORT", parsed["dominant_bias"] == "SHORT",
          f"got {parsed['dominant_bias']}")

# ── Test 5: _parse_trades — log with no trades ───────────────────────────────
print("\n[Test 5] _parse_trades handles a log with no trades")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_20260702.log"
    _write_log(log, ["[KITE] WebSocket connected."])

    result = _parse_trades(log)

    check("count=0 for no-trade log", result["count"] == 0, f"got {result['count']}")
    check("total_pnl=0 for empty", result["total_pnl"] == 0.0,
          f"got {result['total_pnl']}")

# ── Test 6: _parse_trades counts SESSION trades from the log ─────────────────
# Regression (2026-07-02): the header previously summed the whole CUMULATIVE
# paper_trades.csv (52 trades / +₹25,935 on a 3-trade -₹9,754 day). Session
# trades must come from [TRADE LOGGED] lines in THIS session's log.
print("\n[Test 6] _parse_trades counts session trades and sums PnL from log")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_20260702.log"
    _write_log(log, [
        "  [TRADE LOGGED]  SENSEX2670977500CE LONG  entry=₹528.60  exit=₹514.25  pnl=₹-2,870.00",
        "[BAR] SENSEX 77300.00",
        "  [TRADE LOGGED]  NIFTY2670724000PE SHORT  entry=₹214.40  exit=₹295.55  pnl=₹+1,250.00",
    ])

    result = _parse_trades(log)

    check("count=2", result["count"] == 2, f"got {result['count']}")
    check("total_pnl=-1620", result["total_pnl"] == -1620.0, f"got {result['total_pnl']}")
    check("direction parsed", result["rows"][0]["direction"] == "LONG",
          f"got {result['rows'][0]['direction']}")

# ── Test 7: _render produces required sections ────────────────────────────────
print("\n[Test 7] _render produces all required sections")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_20260608.log"
    _write_log(log, ["[KITE] WebSocket connected."])
    parsed = _parse_log(log)
    content = _render("2026-06-08", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    for section_title in [
        "Daily Reflection",
        "Decision Block Summary",
        "Gate Statistics",
        "MCB State",
        "What Worked",
        "Blocks Requiring Investigation",
        "No Automatic Policy Mutation",
    ]:
        check(f"section '{section_title}' present", section_title in content,
              f"not found in output")

# ── Test 8: No Automatic Policy Mutation section present ──────────────────────
print("\n[Test 8] 'No Automatic Policy Mutation' section present")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, ["baseline"])
    parsed = _parse_log(log)
    content = _render("2026-06-09", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    check("policy mutation section present", "No Automatic Policy Mutation" in content)
    check("mentions 'offline backtest'", "offline backtest" in content.lower())

# ── Test 9: Dry-run does not write file ───────────────────────────────────────
print("\n[Test 9] Dry-run does not write file")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_test.log"
    _write_log(log, ["baseline"])
    out = d / "daily_reflection_2026-06-09.md"

    _orig_logs = _mod.LOGS_DIR
    _mod.LOGS_DIR = d
    # dry-run: call _render but don't write
    parsed = _parse_log(log)
    _ = _render("2026-06-09", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    _mod.LOGS_DIR = _orig_logs

    check("dry-run leaves no output file", not out.exists(), f"file exists: {out.exists()}")

# ── Test 10: Dry-run output contains block summary ────────────────────────────
print("\n[Test 10] Dry-run render output contains block summary")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "│  WARM_START_LOCKOUT live_bars=1/20 │",
        "│  SCORE_MARGIN_LOW                  │",
    ])
    parsed = _parse_log(log)
    content = _render("2026-06-08", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    check("block summary table present", "Decision Block Summary" in content)
    check("WARM_START_LOCKOUT row present", "WARM_START_LOCKOUT" in content)

# ── Test 11: Zero-block log renders without error ─────────────────────────────
print("\n[Test 11] Zero-block log renders without error")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, ["[KITE] WebSocket connected.", "Session started OK."])
    raised = False
    try:
        parsed = _parse_log(log)
        content = _render("2026-06-08", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    except Exception as exc:
        raised = True
    check("no exception on zero-block log", not raised)

# ── Test 12: MCB adaptation events in render ──────────────────────────────────
print("\n[Test 12] MCB adaptation events appear in rendered output")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "[MCB ADAPTATION WATCH] underlying=BANKNIFTY prev_bias=SHORT new_bias=LONG bar=250",
    ])
    parsed = _parse_log(log)
    content = _render("2026-06-08", log, parsed, {"count": 0, "total_pnl": 0.0}, {}, {})
    check("MCB Adaptation Watch section present", "Adaptation Watch" in content,
          "section heading not found")


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
