"""
test_trade_starvation_unlock.py
──────────────────────────────────────────────────────────────────────
Tests for the trade-starvation fixes:

  1.  stale current_live_log fallback (live_log_resolver)
  2.  paper_trade_unlock rejected under EXECUTION_MODE=real_armed
  3.  paper_trade_unlock does NOT disable context-contradiction rejection
  4.  paper_trade_unlock does NOT lower the OI hard floor (0.10)
  5.  paper_trade_unlock does NOT disable risk / portfolio-heat gates
  6.  score_margin 0.016 passes soft gates under paper_trade_unlock
  7.  score_margin 0.016 fails soft gates under default profile
  8.  supervised runner --profile paper_trade_unlock sets the 4L/5% env
  9.  near-miss analyzer classifies SAFE_NEAR_MISS vs UNSAFE_NEAR_MISS
  10. real_dry_run adapter never calls kite.place_order

Usage:
  PYTHONPATH=. python3 scripts/test_trade_starvation_unlock.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = _PASS if cond else _FAIL
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    _results.append((name, cond, detail))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


print("=" * 60)
print("  TRADE STARVATION UNLOCK TESTS")
print("=" * 60)

from platform_core.execution.gate_profiles import (
    PAPER_ONLY_PROFILES,
    PROFILE_GATE_TABLES,
    validate_profile_mode,
)
from platform_core.utils.live_log_resolver import (
    POINTER_NAME,
    resolve_current_live_log,
)

_nm = _load_script("analyze_near_miss_entries")
_runner = _load_script("run_supervised_live_session")


# ── Test 1: stale current_live_log fallback ──────────────────────────────────
print("\n[Test 1] Stale current_live_log pointer falls back + repairs")

with tempfile.TemporaryDirectory() as td:
    logs = Path(td) / "logs"
    logs.mkdir()
    old = logs / "live_run_20260608_093000.log"
    new = logs / "live_run_20260609_091500.log"
    old.write_text("old\n")
    time.sleep(0.05)
    new.write_text("new\n")
    # Pointer references an archived (missing) log
    (logs / POINTER_NAME).write_text("logs/archive/live_run_20260608_093000.log\n")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resolved = resolve_current_live_log(logs)
    out = buf.getvalue()

    check("stale pointer resolves to newest live_run_*.log",
          resolved == new, f"got {resolved}")
    check("[CURRENT LOG FIX] printed", "[CURRENT LOG FIX]" in out, out.strip()[:80])
    check("FIX line names stale path", "live_run_20260608_093000.log" in out)
    check("pointer rewritten to valid log",
          new.name in (logs / POINTER_NAME).read_text())

    # Valid pointer → no FIX message, same file returned
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        resolved2 = resolve_current_live_log(logs)
    check("valid pointer returned as-is", resolved2 == new)
    check("no FIX message when pointer valid", "[CURRENT LOG FIX]" not in buf2.getvalue())

with tempfile.TemporaryDirectory() as td:
    logs = Path(td) / "logs"
    logs.mkdir()
    (logs / POINTER_NAME).write_text("logs/live_run_gone.log\n")
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        resolved3 = resolve_current_live_log(logs)
    check("no live_run_*.log at all → None (clear error, no crash)",
          resolved3 is None, buf3.getvalue().strip()[:80])


# ── Test 2: paper_trade_unlock rejected in real_armed ────────────────────────
print("\n[Test 2] paper_trade_unlock rejected under real_armed (fail-closed)")

ok, reason = validate_profile_mode("paper_trade_unlock", "real_armed")
check("validate_profile_mode rejects real_armed", not ok, reason[:80])
check("rejection reason names the profile", "paper_trade_unlock" in reason)
ok_p, _ = validate_profile_mode("paper_trade_unlock", "paper")
ok_d, _ = validate_profile_mode("paper_trade_unlock", "real_dry_run")
check("allowed in paper", ok_p)
check("allowed in real_dry_run", ok_d)
ok_def, _ = validate_profile_mode("default", "real_armed")
check("default profile still allowed in real_armed", ok_def)
check("paper_trade_unlock registered paper-only",
      "paper_trade_unlock" in PAPER_ONLY_PROFILES)

# Supervised runner build_env must also fail closed
_armed_args = SimpleNamespace(profile="paper_trade_unlock", capital=0,
                              execution_mode="real_armed")
_raised = False
try:
    _runner.build_env(_armed_args)
except SystemExit as exc:
    _raised = "paper_trade_unlock" in str(exc)
check("build_env raises SystemExit for unlock+real_armed", _raised)


# ── Shared near-miss fixture rows ─────────────────────────────────────────────
def _row(**kw) -> dict:
    base = {
        "underlying": "BANKNIFTY",
        "selected_symbol": "BANKNIFTY26JUN54000PE",
        "runner_up_contract": "BANKNIFTY26JUN53900PE",
        "score_margin": 0.03,
        "spread_pct": 0.4,
        "premium": 300.0,
        "lot_size": 15,
        "bias": "SHORT",
        "ts": "2026-06-09T11:00:00+05:30",
        "factor_scores": {
            "composite": 0.62, "oi_support": 0.20,
            "iv_attractiveness": 0.20, "liquidity": 0.80,
        },
    }
    fs = kw.pop("factor_scores", None)
    base.update(kw)
    if fs:
        base["factor_scores"] = {**base["factor_scores"], **fs}
    return base


# ── Test 3: unlock does not disable context contradiction ────────────────────
print("\n[Test 3] paper_trade_unlock keeps context-contradiction rejection")

res = _nm.classify_near_miss(_row(), had_contradiction=True)
check("contradiction → UNSAFE_NEAR_MISS", res["classification"] == "UNSAFE_NEAR_MISS")
check("CONTEXT_CONTRADICTION in unsafe reasons",
      any("CONTEXT_CONTRADICTION" in r for r in res["unsafe_reasons"]))
check("no order intent under unlock with contradiction",
      not res["would_intent_under_unlock"])
check("profile table carries only soft thresholds (no contradiction switch)",
      set(PROFILE_GATE_TABLES["paper_trade_unlock"]["BANKNIFTY"]) ==
      {"score_margin", "oi_support"})


# ── Test 4: unlock does not lower OI hard floor ───────────────────────────────
print("\n[Test 4] paper_trade_unlock keeps OI floor at 0.10")

for und in ("BANKNIFTY", "NIFTY", "SENSEX"):
    check(f"{und} oi_support_min stays 0.10",
          abs(PROFILE_GATE_TABLES["paper_trade_unlock"][und]["oi_support"] - 0.10) < 1e-9)

res_oi = _nm.classify_near_miss(_row(factor_scores={"oi_support": 0.05}))
check("oi=0.05 → UNSAFE_NEAR_MISS", res_oi["classification"] == "UNSAFE_NEAR_MISS")
check("LOW_OI flagged", any("LOW_OI" in r for r in res_oi["unsafe_reasons"]))
check("oi=0.05 fails soft gates even under unlock",
      not _nm.soft_gates_pass(_row(factor_scores={"oi_support": 0.05}),
                              "paper_trade_unlock"))


# ── Test 5: unlock does not disable risk / heat gates ────────────────────────
print("\n[Test 5] paper_trade_unlock keeps risk / portfolio-heat gates")

# 1 lot costing ₹60,000 breaches the 10% heat budget on ₹4L capital
res_risk = _nm.classify_near_miss(_row(premium=4000.0, lot_size=15),
                                  capital=400_000.0)
check("heat breach → risk_ok=False", not res_risk["risk_ok"],
      f"cost=₹{res_risk['cost_one_lot']:,.0f}")
check("heat breach → UNSAFE_NEAR_MISS", res_risk["classification"] == "UNSAFE_NEAR_MISS")
check("RISK_BLOCKED flagged", any("RISK_BLOCKED" in r for r in res_risk["unsafe_reasons"]))
check("no order intent when risk blocked", not res_risk["would_intent_under_unlock"])

# Restricted session (pre-open / closing) also stays blocked
res_close = _nm.classify_near_miss(_row(ts="2026-06-09T15:10:00+05:30"))
check("closing-window entry → UNSAFE_NEAR_MISS",
      res_close["classification"] == "UNSAFE_NEAR_MISS")
check("PREOPEN_OR_CLOSING flagged",
      any("PREOPEN_OR_CLOSING" in r for r in res_close["unsafe_reasons"]))


# ── Tests 6 + 7: score_margin 0.016 across profiles ──────────────────────────
print("\n[Test 6/7] score_margin=0.016 passes unlock, fails default")

row_016 = _row(score_margin=0.016)
check("0.016 passes paper_trade_unlock soft gates",
      _nm.soft_gates_pass(row_016, "paper_trade_unlock"))
check("0.016 fails default soft gates",
      not _nm.soft_gates_pass(row_016, "default"))
check("0.016 fails banknifty_experiment soft gates",
      not _nm.soft_gates_pass(row_016, "banknifty_experiment"))
check("0.014 fails even paper_trade_unlock",
      not _nm.soft_gates_pass(_row(score_margin=0.014), "paper_trade_unlock"))


# ── Test 8: supervised runner sets 4L/5% env for the profile ─────────────────
print("\n[Test 8] --profile paper_trade_unlock sets 4L/5% env block")

_POPPED = [
    "PAPER_GATE_PROFILE", "PAPER_STARTING_CAPITAL", "PAPER_RISK_PROFILE",
    "PAPER_TIER2_RISK_FRAC", "PAPER_TIER3_RISK_FRAC",
    "PAPER_MAX_SINGLE_TRADE_RISK_FRAC", "MAX_OPEN_POSITIONS",
    "LIVE_TERMINAL_MODE", "EXECUTION_MODE",
]
_saved = {k: os.environ.pop(k, None) for k in _POPPED}
try:
    env = _runner.build_env(SimpleNamespace(
        profile="paper_trade_unlock", capital=400000,
        execution_mode="real_dry_run",
    ))
    check("PAPER_GATE_PROFILE=paper_trade_unlock",
          env.get("PAPER_GATE_PROFILE") == "paper_trade_unlock")
    check("EXECUTION_MODE=real_dry_run", env.get("EXECUTION_MODE") == "real_dry_run")
    check("PAPER_RISK_PROFILE=balanced", env.get("PAPER_RISK_PROFILE") == "balanced")
    check("PAPER_TIER2_RISK_FRAC=0.05", env.get("PAPER_TIER2_RISK_FRAC") == "0.05")
    check("PAPER_TIER3_RISK_FRAC=0.05", env.get("PAPER_TIER3_RISK_FRAC") == "0.05")
    check("PAPER_MAX_SINGLE_TRADE_RISK_FRAC=0.05",
          env.get("PAPER_MAX_SINGLE_TRADE_RISK_FRAC") == "0.05")
    check("MAX_OPEN_POSITIONS=2", env.get("MAX_OPEN_POSITIONS") == "2")
    check("LIVE_TERMINAL_MODE=compact", env.get("LIVE_TERMINAL_MODE") == "compact")
    check("PAPER_STARTING_CAPITAL=400000 from --capital",
          env.get("PAPER_STARTING_CAPITAL") == "400000")

    env_nocap = _runner.build_env(SimpleNamespace(
        profile="paper_trade_unlock", capital=0, execution_mode="real_dry_run",
    ))
    check("capital defaults to 400000 without --capital",
          env_nocap.get("PAPER_STARTING_CAPITAL") == "400000")

    env_def = _runner.build_env(SimpleNamespace(
        profile="", capital=0, execution_mode="paper",
    ))
    check("default profile unaffected (capital stays 300000)",
          env_def.get("PAPER_GATE_PROFILE") == "default"
          and env_def.get("PAPER_STARTING_CAPITAL") == "300000"
          and "PAPER_TIER2_RISK_FRAC" not in env_def)
finally:
    for k, v in _saved.items():
        if v is not None:
            os.environ[k] = v


# ── Test 9: near-miss analyzer SAFE vs UNSAFE classification ─────────────────
print("\n[Test 9] Near-miss analyzer classification + shadow probe")

res_safe = _nm.classify_near_miss(_row())
check("clean barely-missed margin → SAFE_NEAR_MISS",
      res_safe["classification"] == "SAFE_NEAR_MISS")
check("SAFE near miss would produce intent under unlock",
      res_safe["would_intent_under_unlock"])
check("blocked gate identified",
      res_safe["blocked_gate"] == "INSUFFICIENT_STRIKE_QUALITY")

res_deep = _nm.classify_near_miss(_row(score_margin=0.005))
check("deep miss (sm=0.005) → NOT_NEAR_MISS",
      res_deep["classification"] == "NOT_NEAR_MISS")

analysis = _nm.analyze(
    [_row(), _row(factor_scores={"oi_support": 0.05}), _row(score_margin=0.016)],
    decisions=[],
)
counts = {r["classification"] for r in analysis["results"]}
check("analyze() yields both SAFE and UNSAFE",
      {"SAFE_NEAR_MISS", "UNSAFE_NEAR_MISS"} <= counts, str(counts))
check("what-if counts: unlock >= default",
      analysis["pass_counts"]["paper_trade_unlock"]
      >= analysis["pass_counts"]["default"])

probe = _nm.shadow_intent_probe(analysis["results"],
                                execution_mode="real_dry_run", intents_count=0)
check("shadow probe reports a would-be intent",
      any("DRY_RUN_NEAR_MISS_SHADOW_INTENT" in ln for ln in probe))

unsafe_only = [_nm.classify_near_miss(_row(factor_scores={"oi_support": 0.05}))]
probe_skip = _nm.shadow_intent_probe(unsafe_only,
                                     execution_mode="real_dry_run", intents_count=0)
check("probe emits DRY_RUN_NEAR_MISS_ORDER_INTENT_SKIPPED for unsafe-only",
      any("DRY_RUN_NEAR_MISS_ORDER_INTENT_SKIPPED reason=" in ln for ln in probe_skip))

probe_has = _nm.shadow_intent_probe(analysis["results"],
                                    execution_mode="real_dry_run", intents_count=3)
check("probe skips when intents already recorded",
      any("not needed" in ln for ln in probe_has))


# ── Test 10: no kite.place_order in real_dry_run ─────────────────────────────
print("\n[Test 10] real_dry_run adapter never calls kite.place_order")

from platform_core.execution.broker_adapter import (
    KiteDryRunBrokerAdapter,
    OrderIntent,
)

_place_order_called = False


class _MockKite:
    def place_order(self, **kwargs):
        global _place_order_called
        _place_order_called = True
        return "REAL-ORDER-ID"


adapter = KiteDryRunBrokerAdapter()
# Even if a kite handle is attached, the dry-run adapter must never use it
adapter._kite = _MockKite()  # type: ignore[attr-defined]
intent = OrderIntent(
    underlying="BANKNIFTY", tradingsymbol="BANKNIFTY26JUN54000PE",
    exchange="NFO", transaction_type="BUY", quantity=15, lot_size=15,
    lots=1, product="NRML", order_type="MARKET", price=0.0,
    trigger_price=0.0, variety="regular", validity="DAY",
    tag="starvation_test", reason="ENTRY",
)
result = adapter.place_entry_order(intent)
check("dry-run entry succeeds", result.success, f"status={result.status}")
check("order_id DRYRUN-prefixed", result.order_id.startswith("DRYRUN-"))
check("kite.place_order NOT called", not _place_order_called)
check("dry-run never auto-fills", not result.filled)

exit_intent = OrderIntent(
    underlying="BANKNIFTY", tradingsymbol="BANKNIFTY26JUN54000PE",
    exchange="NFO", transaction_type="SELL", quantity=15, lot_size=15,
    lots=1, product="NRML", order_type="MARKET", price=0.0,
    trigger_price=0.0, variety="regular", validity="DAY",
    tag="starvation_test", reason="EXIT",
)
result_exit = adapter.place_exit_order(exit_intent)
check("dry-run exit succeeds without place_order",
      result_exit.success and not _place_order_called)


# ── Summary ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
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
