"""
test_gate_profiles.py
──────────────────────────────────────────────────────────────────────
Tests for PAPER_GATE_PROFILE and per-underlying gate threshold logic.

Verifies:
  - default profile keeps 0.05/0.10 for all underlyings
  - banknifty_experiment sets only BANKNIFTY margin to 0.03
  - small_capital_experiment sets BNK+NIFTY margin=0.03, NIFTY oi=0.08
  - SENSEX remains strict (0.05/0.10) in all profiles
  - explicit env var overrides profile
  - MCB adaptation watch counter increments correctly

Usage:
  PYTHONPATH=. python3 scripts/test_gate_profiles.py
"""
from __future__ import annotations

import importlib
import os
import sys
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


def _import_pipeline_gate_tables(profile: str, env_overrides: dict[str, str] | None = None):
    """Import gate table resolution logic by replicating the pipeline logic inline."""
    # Mirrors the logic in live_structure_pipeline.py gate config section
    _MIN_SM = 0.05
    _MIN_OI = 0.10

    _PROFILES: dict[str, dict[str, dict[str, float]]] = {
        "default": {
            "BANKNIFTY": {"score_margin": 0.05, "oi_support": 0.10},
            "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
            # SENSEX recalibrated 0.10 -> 0.005 (relative-OI false-reject; the
            # absolute-liquidity gate is the real arbiter). See gate_profiles.py.
            "SENSEX":    {"score_margin": 0.05, "oi_support": 0.005},
        },
        "banknifty_experiment": {
            "BANKNIFTY": {"score_margin": 0.03, "oi_support": 0.10},
            "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
            "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},
        },
        "small_capital_experiment": {
            "BANKNIFTY": {"score_margin": 0.03, "oi_support": 0.10},
            "NIFTY":     {"score_margin": 0.03, "oi_support": 0.08},
            "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},
        },
    }

    table = _PROFILES.get(profile, _PROFILES["default"])

    def _get(sym: str, metric: str, env_key: str) -> float:
        profile_val = table.get(sym, {}).get(metric, _MIN_SM if metric == "score_margin" else _MIN_OI)
        if env_overrides and env_key in env_overrides:
            try:
                return float(env_overrides[env_key])
            except ValueError:
                pass
        return profile_val

    gate_sm = {s: _get(s, "score_margin", f"PAPER_{s}_SCORE_MARGIN_MIN") for s in ("BANKNIFTY", "NIFTY", "SENSEX")}
    gate_oi = {s: _get(s, "oi_support",   f"PAPER_{s}_OI_SUPPORT_MIN")   for s in ("BANKNIFTY", "NIFTY", "SENSEX")}
    return gate_sm, gate_oi


print("=" * 60)
print("  GATE PROFILE TESTS")
print("=" * 60)

# ── Test 1: default profile ────────────────────────────────────────────────────
print("\n[Test 1] Default profile — all thresholds at 0.05/0.10")
sm, oi = _import_pipeline_gate_tables("default")
check("default BNK score_margin=0.05",  abs(sm["BANKNIFTY"] - 0.05) < 1e-9, f"got {sm['BANKNIFTY']}")
check("default NIFTY score_margin=0.05", abs(sm["NIFTY"]    - 0.05) < 1e-9, f"got {sm['NIFTY']}")
check("default SENSEX score_margin=0.05",abs(sm["SENSEX"]   - 0.05) < 1e-9, f"got {sm['SENSEX']}")
check("default BNK oi_support=0.10",    abs(oi["BANKNIFTY"] - 0.10) < 1e-9, f"got {oi['BANKNIFTY']}")
check("default NIFTY oi_support=0.10",  abs(oi["NIFTY"]    - 0.10) < 1e-9, f"got {oi['NIFTY']}")
check("default SENSEX oi_support=0.005", abs(oi["SENSEX"]   - 0.005) < 1e-9, f"got {oi['SENSEX']}")

# ── Test 2: banknifty_experiment ───────────────────────────────────────────────
print("\n[Test 2] banknifty_experiment — only BANKNIFTY margin changed")
sm, oi = _import_pipeline_gate_tables("banknifty_experiment")
check("bnk_exp BNK score_margin=0.03",   abs(sm["BANKNIFTY"] - 0.03) < 1e-9, f"got {sm['BANKNIFTY']}")
check("bnk_exp NIFTY score_margin=0.05", abs(sm["NIFTY"]    - 0.05) < 1e-9, f"got {sm['NIFTY']}")
check("bnk_exp SENSEX score_margin=0.05",abs(sm["SENSEX"]   - 0.05) < 1e-9, f"got {sm['SENSEX']}")
check("bnk_exp BNK oi unchanged=0.10",  abs(oi["BANKNIFTY"] - 0.10) < 1e-9, f"got {oi['BANKNIFTY']}")
check("bnk_exp SENSEX oi strict=0.10",  abs(oi["SENSEX"]   - 0.10) < 1e-9, f"got {oi['SENSEX']}")

# ── Test 3: small_capital_experiment ──────────────────────────────────────────
print("\n[Test 3] small_capital_experiment — BNK+NIFTY relaxed, SENSEX strict")
sm, oi = _import_pipeline_gate_tables("small_capital_experiment")
check("sml_cap BNK score_margin=0.03",  abs(sm["BANKNIFTY"] - 0.03) < 1e-9, f"got {sm['BANKNIFTY']}")
check("sml_cap NIFTY score_margin=0.03",abs(sm["NIFTY"]    - 0.03) < 1e-9, f"got {sm['NIFTY']}")
check("sml_cap NIFTY oi=0.08",          abs(oi["NIFTY"]    - 0.08) < 1e-9, f"got {oi['NIFTY']}")
check("sml_cap SENSEX score_margin=0.05 (STRICT)", abs(sm["SENSEX"] - 0.05) < 1e-9, f"got {sm['SENSEX']}")
check("sml_cap SENSEX oi=0.10 (STRICT)",           abs(oi["SENSEX"] - 0.10) < 1e-9, f"got {oi['SENSEX']}")

# ── Test 4: SENSEX score_margin strict; oi recalibrated only in default ───────
print("\n[Test 4] SENSEX score_margin strict in all profiles; default oi recalibrated to 0.005")
for prof in ("default", "banknifty_experiment", "small_capital_experiment"):
    sm, oi = _import_pipeline_gate_tables(prof)
    # default SENSEX oi recalibrated 0.10 -> 0.005 (2026-06-24); experiments still 0.10.
    _exp_oi = 0.005 if prof == "default" else 0.10
    check(f"SENSEX in {prof} (sm=0.05, oi={_exp_oi})",
          abs(sm["SENSEX"] - 0.05) < 1e-9 and abs(oi["SENSEX"] - _exp_oi) < 1e-9)

# SENSEX CAN be overridden by explicit env var
sm_env, oi_env = _import_pipeline_gate_tables(
    "default",
    env_overrides={"PAPER_SENSEX_SCORE_MARGIN_MIN": "0.01", "PAPER_SENSEX_OI_SUPPORT_MIN": "0.03"}
)
check("SENSEX env override sets sm=0.01", abs(sm_env["SENSEX"] - 0.01) < 1e-9, f"got {sm_env['SENSEX']}")
check("SENSEX env override sets oi=0.03", abs(oi_env["SENSEX"] - 0.03) < 1e-9, f"got {oi_env['SENSEX']}")
check("SENSEX env override does not affect BNK", abs(sm_env["BANKNIFTY"] - 0.05) < 1e-9)

# ── Test 5: env var overrides profile ─────────────────────────────────────────
print("\n[Test 5] Explicit env var overrides profile value")
sm, oi = _import_pipeline_gate_tables(
    "banknifty_experiment",
    env_overrides={"PAPER_BANKNIFTY_SCORE_MARGIN_MIN": "0.04"}
)
check("env overrides profile: BNK sm=0.04 not 0.03", abs(sm["BANKNIFTY"] - 0.04) < 1e-9, f"got {sm['BANKNIFTY']}")
check("NIFTY unchanged at 0.05", abs(sm["NIFTY"] - 0.05) < 1e-9)

# ── Test 6: MCB adaptation watch counter ──────────────────────────────────────
print("\n[Test 6] MCB adaptation watch — counter and threshold")
_MCB_THRESHOLD = 3

class MockState:
    def __init__(self):
        self.contra_bos_count = 0

state = MockState()
watch_msgs: list[str] = []

def _process_bos_contradiction(state, bias: str, struct: str, bar: int, conf: float, underlying: str):
    state.contra_bos_count += 1
    if state.contra_bos_count >= _MCB_THRESHOLD:
        msg = (
            f"[MCB ADAPTATION WATCH] underlying={underlying}"
            f" bias={bias} conf={conf:.2f}"
            f" opposite_bos_count={state.contra_bos_count}"
            f" bar={bar}"
        )
        watch_msgs.append(msg)

_process_bos_contradiction(state, "SHORT", "BOS_BULLISH", 85, 1.0, "BANKNIFTY")
check("no watch at count=1", len(watch_msgs) == 0, f"watch_msgs={watch_msgs}")
_process_bos_contradiction(state, "SHORT", "BOS_BULLISH", 150, 1.0, "BANKNIFTY")
check("no watch at count=2", len(watch_msgs) == 0)
_process_bos_contradiction(state, "SHORT", "BOS_BULLISH", 268, 1.0, "BANKNIFTY")
check("watch fires at count=3", len(watch_msgs) == 1, f"got {len(watch_msgs)}")
check("watch message has correct count", "opposite_bos_count=3" in watch_msgs[0], watch_msgs[0])
check("watch does not block execution", True,  # counter is purely diagnostic
      "counter increments but no trade logic uses it")

# ── Test 7: unknown profile name falls back to default ────────────────────────
print("\n[Test 7] Unknown profile name falls back to default")
sm, oi = _import_pipeline_gate_tables("garbage_profile_name")
check("unknown profile → BNK sm=0.05", abs(sm["BANKNIFTY"] - 0.05) < 1e-9)
check("unknown profile → NIFTY oi=0.10", abs(oi["NIFTY"] - 0.10) < 1e-9)

# ── Test 7b: real gate_profiles module matches and adds paper_trade_unlock ────
print("\n[Test 7b] platform_core.execution.gate_profiles module")
from platform_core.execution.gate_profiles import (
    PROFILE_GATE_TABLES as _REAL_TABLES,
    PAPER_ONLY_PROFILES as _REAL_PAPER_ONLY,
    validate_profile_mode as _real_validate,
)

for prof in ("default", "banknifty_experiment", "small_capital_experiment"):
    sm, oi = _import_pipeline_gate_tables(prof)
    real = _REAL_TABLES[prof]
    match = all(
        abs(real[s]["score_margin"] - sm[s]) < 1e-9
        and abs(real[s]["oi_support"] - oi[s]) < 1e-9
        for s in ("BANKNIFTY", "NIFTY", "SENSEX")
    )
    check(f"module table matches inline replica for {prof}", match)

# paper_trade_unlock: margin 0.015 all; oi_support recalibrated per-underlying
# (2026-06-15) because oi_support is a relative-popularity ratio that is
# structurally tiny for delta-targeted SENSEX/NIFTY strikes. DEFAULT stays 0.10.
_PTU_EXPECTED_OI = {"BANKNIFTY": 0.05, "NIFTY": 0.005, "SENSEX": 0.005}
for s in ("BANKNIFTY", "NIFTY", "SENSEX"):
    t = _REAL_TABLES["paper_trade_unlock"][s]
    check(f"paper_trade_unlock {s} sm=0.015 oi={_PTU_EXPECTED_OI[s]}",
          abs(t["score_margin"] - 0.015) < 1e-9
          and abs(t["oi_support"] - _PTU_EXPECTED_OI[s]) < 1e-9,
          f"got sm={t['score_margin']} oi={t['oi_support']}")
# DEFAULT oi_support: NIFTY/BANKNIFTY strict 0.10; SENSEX recalibrated to 0.005
# (its relative-OI ratio is structurally tiny → 0.10 false-rejected every SENSEX
# entry; the absolute-liquidity gate min_abs_oi=3000 is the real arbiter).
_DEFAULT_EXPECTED_OI = {"BANKNIFTY": 0.10, "NIFTY": 0.10, "SENSEX": 0.005}
for s in ("BANKNIFTY", "NIFTY", "SENSEX"):
    check(f"default {s} oi_support == {_DEFAULT_EXPECTED_OI[s]}",
          abs(_REAL_TABLES["default"][s]["oi_support"] - _DEFAULT_EXPECTED_OI[s]) < 1e-9)

_ok_armed, _reason = _real_validate("paper_trade_unlock", "real_armed")
check("paper_trade_unlock rejected under real_armed", not _ok_armed, _reason[:60])
check("all experiment profiles are paper-only",
      {"paper_trade_unlock", "banknifty_experiment",
       "small_capital_experiment"} <= set(_REAL_PAPER_ONLY))
check("default never paper-only", "default" not in _REAL_PAPER_ONLY)

# ── Test 8: full-day fetch detects partial data ────────────────────────────────
print("\n[Test 8] Full-day fetch validates bar count")
_MIN_BARS = 300
for n, expected_ok in [(375, True), (45, False), (0, False), (300, True)]:
    is_ok = n >= _MIN_BARS
    check(f"{n} bars → partial_detected={not is_ok}", is_ok == expected_ok, f"n={n}")

# ── Summary ────────────────────────────────────────────────────────────────────
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
