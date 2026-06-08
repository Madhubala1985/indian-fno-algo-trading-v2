"""
test_live_issue_register.py
──────────────────────────────────────────────────────────────────────
Tests for update_live_issue_register.py:
  1.  Detects SCORE_MARGIN_LOW count from a synthetic log.
  2.  Detects INSUFFICIENT_CONFLUENCE count from a synthetic log.
  3.  Detects CONTEXT_CONTRADICTION_REJECTED count.
  4.  Detects WARM_START_LOCKOUT count.
  5.  Detects MCB ADAPTATION WATCH events.
  6.  Extracts score_margin numeric statistics (min/max/avg).
  7.  Extracts oi_support numeric statistics.
  8.  Dry-run produces non-empty output without writing a file.
  9.  Creates new register file when one does not exist.
  10. Appends to existing register file.
  11. Zero-event log produces no spurious counts.
  12. Audit JSONL: low_margin_rows counted correctly.

Usage:
  PYTHONPATH=. python3 scripts/test_live_issue_register.py
"""
from __future__ import annotations

import importlib.util as _ilu
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
print("  LIVE ISSUE REGISTER TESTS")
print("=" * 60)

# ── Import module ────────────────────────────────────────────────────────────
_spec = _ilu.spec_from_file_location(
    "update_live_issue_register",
    ROOT / "scripts" / "update_live_issue_register.py",
)
_mod = _ilu.module_from_spec(_spec)   # type: ignore[arg-type]
_spec.loader.exec_module(_mod)        # type: ignore[union-attr]

_parse_log = _mod._parse_log
_parse_audit_jsonl = _mod._parse_audit_jsonl
_render_issue_section = _mod._render_issue_section


# ── Synthetic log factory ────────────────────────────────────────────────────

def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ── Tests ────────────────────────────────────────────────────────────────────

# Test 1-5: pattern counting
print("\n[Test 1] Counts SCORE_MARGIN_LOW")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "  [STRIKE AUDIT] BANKNIFTY SHORT score=0.580 margin=0.027 [SCORE_MARGIN_LOW]",
        "  [STRIKE AUDIT] BANKNIFTY SHORT score=0.581 margin=0.032 [SCORE_MARGIN_LOW]",
        "  [STRIKE AUDIT] BANKNIFTY SHORT score=0.582 margin=0.034 [SCORE_MARGIN_LOW]",
    ])
    parsed = _parse_log(log)
    check("SCORE_MARGIN_LOW=3", parsed["counts"].get("SCORE_MARGIN_LOW", 0) == 3,
          f"got {parsed['counts'].get('SCORE_MARGIN_LOW', 0)}")

print("\n[Test 2] Counts INSUFFICIENT_CONFLUENCE")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "│  INSUFFICIENT_CONFLUENCE oi_support=0.059 < min=0.1 for tier=2            │",
        "│  INSUFFICIENT_CONFLUENCE oi_support=0.041 < min=0.1 for tier=2            │",
    ])
    parsed = _parse_log(log)
    check("INSUFFICIENT_CONFLUENCE=2", parsed["counts"].get("INSUFFICIENT_CONFLUENCE", 0) == 2,
          f"got {parsed['counts'].get('INSUFFICIENT_CONFLUENCE', 0)}")

print("\n[Test 3] Counts CONTEXT_CONTRADICTION_REJECTED")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "│  CONTEXT_CONTRADICTION_REJECTED mcb=SHORT structure=BOS_BULLISH at bar=85 │",
        "│  CONTEXT_CONTRADICTION_REJECTED mcb=SHORT structure=BOS_BULLISH at bar=95 │",
        "│  CONTEXT_CONTRADICTION_REJECTED mcb=SHORT structure=BOS_BULLISH at bar=100 │",
    ])
    parsed = _parse_log(log)
    check("CONTEXT_CONTRADICTION_REJECTED=3",
          parsed["counts"].get("CONTEXT_CONTRADICTION_REJECTED", 0) == 3,
          f"got {parsed['counts'].get('CONTEXT_CONTRADICTION_REJECTED', 0)}")

print("\n[Test 4] Counts WARM_START_LOCKOUT")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "│  WARM_START_LOCKOUT live_bars=1/20                                        │",
        "│  WARM_START_LOCKOUT live_bars=2/20                                        │",
        "│  WARM_START_LOCKOUT live_bars=3/20                                        │",
        "│  WARM_START_LOCKOUT live_bars=4/20                                        │",
        "│  WARM_START_LOCKOUT live_bars=5/20                                        │",
    ])
    parsed = _parse_log(log)
    check("WARM_START_LOCKOUT=5", parsed["counts"].get("WARM_START_LOCKOUT", 0) == 5,
          f"got {parsed['counts'].get('WARM_START_LOCKOUT', 0)}")

print("\n[Test 5] Captures MCB ADAPTATION WATCH events")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "[MCB ADAPTATION WATCH] underlying=BANKNIFTY prev_bias=SHORT new_bias=LONG bar=250",
        "[MCB ADAPTATION WATCH] underlying=NIFTY prev_bias=NEUTRAL new_bias=SHORT bar=300",
    ])
    parsed = _parse_log(log)
    check("MCB_ADAPTATION_WATCH count=2",
          parsed["counts"].get("MCB_ADAPTATION_WATCH", 0) == 2,
          f"got {parsed['counts'].get('MCB_ADAPTATION_WATCH', 0)}")
    check("adaptation events list non-empty",
          len(parsed["mcb_adaptation_events"]) > 0,
          f"got {parsed['mcb_adaptation_events']}")

# Test 6: score_margin stats
print("\n[Test 6] Extracts score_margin statistics")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "score_margin=0.020 [SCORE_MARGIN_LOW]",
        "score_margin=0.030 [SCORE_MARGIN_LOW]",
        "score_margin=0.040 [SCORE_MARGIN_LOW]",
    ])
    parsed = _parse_log(log)
    sm = parsed["score_margin_stats"]
    check("score_margin_stats not None", sm is not None, f"got {sm}")
    if sm:
        check("score_margin min=0.02", sm["min"] == 0.02, f"got {sm['min']}")
        check("score_margin max=0.04", sm["max"] == 0.04, f"got {sm['max']}")
        check("score_margin avg=0.03", abs(sm["avg"] - 0.03) < 1e-9, f"got {sm['avg']}")

# Test 7: oi_support stats
print("\n[Test 7] Extracts oi_support statistics")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, [
        "INSUFFICIENT_CONFLUENCE oi_support=0.050",
        "INSUFFICIENT_CONFLUENCE oi_support=0.060",
        "INSUFFICIENT_CONFLUENCE oi_support=0.070",
    ])
    parsed = _parse_log(log)
    oi = parsed["oi_support_stats"]
    check("oi_support_stats not None", oi is not None, f"got {oi}")
    if oi:
        check("oi_support min=0.05", oi["min"] == 0.05, f"got {oi['min']}")
        check("oi_support max=0.07", oi["max"] == 0.07, f"got {oi['max']}")

# Test 8: dry-run produces output
print("\n[Test 8] Dry-run produces non-empty output")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "test.log"
    _write_log(log, ["│  SCORE_MARGIN_LOW                                                        │"])
    parsed = _parse_log(log)
    section = _render_issue_section(log, parsed, {}, "2026-06-08")
    check("render output non-empty", len(section.strip()) > 0,
          f"len={len(section)}")
    check("render contains 'Block event counts'", "Block event counts" in section)

# Test 9: creates new register file
print("\n[Test 9] Creates new register file when absent")
import json as _json
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_test.log"
    _write_log(log, ["│  WARM_START_LOCKOUT live_bars=1/20 │"])
    register = d / "live_issue_register_2026-06-08.md"
    check("register does not exist yet", not register.exists())

    _orig_logs = _mod.LOGS_DIR
    _mod.LOGS_DIR = d
    # Temporarily set audit path to nonexistent
    import os
    _orig_env = os.environ.get("PYTHONPATH", "")

    parsed = _parse_log(log)
    section = _render_issue_section(log, parsed, {}, "2026-06-08")
    register.write_text(f"# Live Issue Register\n{section}", encoding="utf-8")
    _mod.LOGS_DIR = _orig_logs

    check("register file created", register.exists())
    content = register.read_text()
    check("register contains auto-extracted section", "Auto-extracted issues" in content)

# Test 10: append to existing
print("\n[Test 10] Appends to existing register file")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "live_run_test.log"
    _write_log(log, ["│  WARM_START_LOCKOUT live_bars=1/20 │"])
    register = d / "live_issue_register_2026-06-08.md"
    register.write_text("# Existing header\n\nExisting content\n", encoding="utf-8")

    parsed = _parse_log(log)
    section = _render_issue_section(log, parsed, {}, "2026-06-08")
    with register.open("a", encoding="utf-8") as fh:
        fh.write(section)

    content = register.read_text()
    check("original header preserved", "Existing header" in content)
    check("auto-extracted section appended", "Auto-extracted issues" in content)

# Test 11: zero-event log
print("\n[Test 11] Zero-event log produces no spurious counts")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = d / "empty.log"
    _write_log(log, ["WebSocket connected.", "Session started."])
    parsed = _parse_log(log)
    c = parsed["counts"]
    spurious = {k: v for k, v in c.items()
                if k not in ("WEBSOCKET_CONNECTED",) and v > 0}
    check("no spurious block counts", len(spurious) == 0, f"spurious={spurious}")

# Test 12: audit JSONL low_margin_rows
print("\n[Test 12] Audit JSONL: low_margin_rows counted")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    audit_path = d / "strike_selection_audit.jsonl"
    rows = [
        {"underlying": "BANKNIFTY", "flags": ["SCORE_MARGIN_LOW", "SAME_STRIKE_AS_PREVIOUS"]},
        {"underlying": "BANKNIFTY", "flags": ["SCORE_MARGIN_LOW"]},
        {"underlying": "NIFTY",     "flags": []},
        {"underlying": "SENSEX",    "flags": ["SCORE_MARGIN_LOW"]},
    ]
    with audit_path.open("w") as fh:
        for r in rows:
            fh.write(_json.dumps(r) + "\n")

    # Temporarily patch LOGS_DIR so _parse_audit_jsonl reads our file
    _orig_logs = _mod.LOGS_DIR
    _mod.LOGS_DIR = d
    result = _parse_audit_jsonl()
    _mod.LOGS_DIR = _orig_logs

    check("total=4", result.get("total_audit_rows", 0) == 4,
          f"got {result.get('total_audit_rows', 0)}")
    check("low_margin=3", result.get("low_margin_rows", 0) == 3,
          f"got {result.get('low_margin_rows', 0)}")


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
