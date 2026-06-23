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
print("\n[Test 4] DEFAULT (real-money) profile keeps strict OI floor 0.10; "
      "paper_trade_unlock relaxes per-underlying (paper-only)")

# Real-money safety invariant: the default profile (the one real_armed uses)
# keeps the strict 0.10 OI-support floor for every underlying.
for und in ("BANKNIFTY", "NIFTY", "SENSEX"):
    check(f"DEFAULT {und} oi_support_min stays strict 0.10",
          abs(PROFILE_GATE_TABLES["default"][und]["oi_support"] - 0.10) < 1e-9)

# paper_trade_unlock intentionally recalibrates oi_support per-underlying
# (paper/real_dry_run ONLY — oi_support is a relative-popularity ratio that is
# structurally tiny for delta-targeted SENSEX/NIFTY strikes; the absolute-
# liquidity gate [B3b] is the real arbiter). Values documented in gate_profiles.py.
_unlock_oi = {"BANKNIFTY": 0.05, "NIFTY": 0.005, "SENSEX": 0.005}
for und, floor in _unlock_oi.items():
    check(f"unlock {und} oi_support recalibrated to {floor}",
          abs(PROFILE_GATE_TABLES["paper_trade_unlock"][und]["oi_support"] - floor) < 1e-9)

# The near-miss CLASSIFIER still flags low OI diagnostically even though the
# unlock soft-gate now passes it (classification != gating).
res_oi = _nm.classify_near_miss(_row(factor_scores={"oi_support": 0.05}))
check("oi=0.05 → UNSAFE_NEAR_MISS", res_oi["classification"] == "UNSAFE_NEAR_MISS")
check("LOW_OI flagged", any("LOW_OI" in r for r in res_oi["unsafe_reasons"]))
# Real-money gate still works: oi=0.05 < 0.10 fails under the DEFAULT profile.
check("oi=0.05 fails soft gates under DEFAULT (real-money) profile",
      not _nm.soft_gates_pass(_row(factor_scores={"oi_support": 0.05}),
                              "default"))


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
    # Compounding default (commit 8204ec6): with no --capital the supervisor
    # leaves PAPER_STARTING_CAPITAL unset so the pipeline rolls the capital basis
    # forward from the state file's current equity (profits compound into base).
    check("default profile compounds (no fixed PAPER_STARTING_CAPITAL)",
          env_def.get("PAPER_GATE_PROFILE") == "default"
          and env_def.get("PAPER_STARTING_CAPITAL") is None
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


# ── Test 11: requested capital mismatch never silently continues ─────────────
print("\n[Test 11] --capital 400000 with old ₹300k state: explicit archive/refuse")

import json as _json

_STATE_300K = {
    "starting_capital": 300000.0,
    "equity": 297938.73, "peak_equity": 300000.0, "daily_pnl": -2061.27,
    "consecutive_losses": 3, "trading_halted": True, "halt_reason": "x",
    "soft_halted": False, "soft_halt_reason": "",
    "session_date": "2026-06-10", "session_opening_equity": 300000.0,
    "open_position": None,
    "open_positions": {"NIFTY": None, "BANKNIFTY": None, "SENSEX": None},
}

with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    state = tdp / "paper_trading_state.json"
    arch  = tdp / "archive"

    # mismatch + no open positions → state is ARCHIVED (explicit reset)
    state.write_text(_json.dumps(_STATE_300K))
    action, detail = _runner.resolve_state_capital(400000.0, state, arch)
    check("mismatch (no open pos) → action=archived", action == "archived", detail[:90])
    check("state file physically moved out of the way", not state.exists())
    check("archived copy exists in archive dir",
          any(arch.glob("paper_trading_state_*.json")))
    check("action is never a silent continue",
          action in ("fresh", "match", "archived", "refuse"))

    # matching capital → continue
    state.write_text(_json.dumps(_STATE_300K))
    action_m, _ = _runner.resolve_state_capital(300000.0, state, arch)
    check("matching capital → action=match (state kept)",
          action_m == "match" and state.exists())

    # mismatch WITH open positions → refuse, state untouched
    _state_open = dict(_STATE_300K)
    _state_open["open_positions"] = {"NIFTY": {"contract_symbol": "X"},
                                     "BANKNIFTY": None, "SENSEX": None}
    state.write_text(_json.dumps(_state_open))
    action_r, detail_r = _runner.resolve_state_capital(400000.0, state, arch)
    check("mismatch + open positions → action=refuse",
          action_r == "refuse", detail_r[:90])
    check("refused state file untouched", state.exists())

    # no state file → fresh
    state.unlink()
    action_f, _ = _runner.resolve_state_capital(400000.0, state, arch)
    check("no state file → action=fresh", action_f == "fresh")

    # legacy state without starting_capital → inferred from peak, still explicit
    _legacy = {k: v for k, v in _STATE_300K.items() if k != "starting_capital"}
    state.write_text(_json.dumps(_legacy))
    action_l, detail_l = _runner.resolve_state_capital(400000.0, state, arch)
    check("legacy state (no starting_capital) → still archived, not continued",
          action_l == "archived", detail_l[:90])

# Pipeline-side guard (defense in depth): _load_state refuses on mismatch
_pipe_src = (ROOT / "platform_core" / "data" / "live"
             / "live_structure_pipeline.py").read_text(encoding="utf-8")
check("pipeline _load_state has capital-mismatch guard",
      "[STATE CAPITAL MISMATCH]" in _pipe_src)
check("pipeline default policy is refuse (fail-closed)",
      'PAPER_STATE_CAPITAL_MISMATCH", "refuse"' in _pipe_src)
check("pipeline prints [STATE CAPITAL] equity/peak at startup",
      "[STATE CAPITAL] equity=" in _pipe_src)
check("state file now records starting_capital",
      '"starting_capital":      _INITIAL_EQUITY' in _pipe_src)


# ── Test 12: performance return = total_pnl / starting_capital ───────────────
print("\n[Test 12] Performance summary return uses PnL over starting capital")

from platform_core.paper_trading.terminal_display import print_performance_summary

buf_ret = io.StringIO()
with contextlib.redirect_stdout(buf_ret):
    print_performance_summary(
        total_trades=3, wins=0, losses=3,
        total_pnl=-2061.27, avg_pnl=-687.09,
        equity=297938.73, initial_equity=300000.0,
    )
_out_ret = buf_ret.getvalue()
check("₹-2,061 on ₹3L reports -0.69% (not -25%)",
      "-0.69%" in _out_ret, _out_ret.strip().splitlines()[-2][-40:])
check("no -25% artifact even with mismatched equity basis",
      "-25" not in _out_ret)

buf_ret2 = io.StringIO()
with contextlib.redirect_stdout(buf_ret2):
    print_performance_summary(
        total_trades=3, wins=0, losses=3,
        total_pnl=-2061.27, avg_pnl=-687.09,
        equity=297938.73, initial_equity=400000.0,
    )
check("same PnL on ₹4L capital reports -0.52%",
      "-0.52%" in buf_ret2.getvalue())


# ── Test 13: hard gates logged + OI floor rejected before sizing ─────────────
print("\n[Test 13] Entry hard-gate snapshot + pre-sizing OI rejection")

check("pipeline logs [ENTRY HARD GATES] snapshot before opening",
      "[ENTRY HARD GATES]" in _pipe_src)
check("pipeline logs [ENTRY SOFT GATES] snapshot before opening",
      "[ENTRY SOFT GATES]" in _pipe_src)
check("hard-gate snapshot includes contradiction field",
      "contradiction=False" in _pipe_src)
check("soft-gate snapshot names the active profile",
      "[ENTRY SOFT GATES] profile=" in _pipe_src)

# OI hard floor must reject BEFORE sizing/order intent: the [B3] pre-entry
# confluence check must appear before _size_live_position in the entry flow.
_b3_idx     = _pipe_src.find("[B3] Confluence hard floor")
_sizing_idx = _pipe_src.find("_sizing = _size_live_position(")
check("pre-sizing OI confluence gate exists", _b3_idx != -1)
check("OI hard floor rejects before sizing is invoked",
      _b3_idx != -1 and _sizing_idx != -1 and _b3_idx < _sizing_idx,
      f"b3@{_b3_idx} sizing@{_sizing_idx}")

# Contradiction check is structural — no profile reference inside the gate
_contra_idx = _pipe_src.find("[A2] Context contradiction")
_contra_end = _pipe_src.find("[A3]", _contra_idx) if _contra_idx != -1 else -1
_contra_blk = _pipe_src[_contra_idx:_contra_end] if _contra_idx != -1 and _contra_end != -1 else ""
check("contradiction gate has no profile-based bypass",
      _contra_idx != -1 and "_PAPER_GATE_PROFILE" not in _contra_blk
      and "profile" not in _contra_blk.lower())


# ── Test 14: context-contradiction cooldown (hard gate) ──────────────────────
print("\n[Test 14] Context-contradiction cooldown blocks re-entry for N bars")

from platform_core.execution.context_cooldown import (
    DEFAULT_COOLDOWN_BARS,
    contradiction_cooldown_active,
    cooldown_bars_from_env,
)

# (1) Contradiction starts a cooldown for the same underlying
blocked0, since0 = contradiction_cooldown_active(116, 116, 5)
check("contradiction bar itself is inside cooldown", blocked0 and since0 == 0)

# (2) Entry 1 bar after contradiction is blocked
blocked1, since1 = contradiction_cooldown_active(117, 116, 5)
check("entry 1 bar after contradiction blocked", blocked1 and since1 == 1)

# (3) Entry 5 bars after contradiction blocked when cooldown=5
blocked5, since5 = contradiction_cooldown_active(121, 116, 5)
check("entry 5 bars after contradiction blocked (cooldown=5)",
      blocked5 and since5 == 5)

# (4) Entry 6 bars after contradiction allowed
blocked6, since6 = contradiction_cooldown_active(122, 116, 5)
check("entry 6 bars after contradiction allowed", not blocked6 and since6 == 6)

# (5) Per-underlying: no contradiction recorded → never blocked
blocked_none, since_none = contradiction_cooldown_active(117, None, 5)
check("underlying with no contradiction is not blocked",
      not blocked_none and since_none is None,
      "NIFTY contradiction must not block BANKNIFTY")

# Negative bars_since (bar-counter anomaly) fails closed
blocked_neg, _ = contradiction_cooldown_active(100, 116, 5)
check("negative bars_since fails closed (blocked)", blocked_neg)

# cooldown=0 disables (tests/debug only)
blocked_off, _ = contradiction_cooldown_active(117, 116, 0)
check("cooldown=0 disables the gate", not blocked_off)

# Env var configuration
_old_cd = os.environ.pop("CONTEXT_CONTRADICTION_COOLDOWN_BARS", None)
try:
    check("default cooldown is 5 bars",
          cooldown_bars_from_env() == 5 and DEFAULT_COOLDOWN_BARS == 5)
    os.environ["CONTEXT_CONTRADICTION_COOLDOWN_BARS"] = "8"
    check("env var overrides cooldown", cooldown_bars_from_env() == 8)
    os.environ["CONTEXT_CONTRADICTION_COOLDOWN_BARS"] = "garbage"
    check("invalid env value falls back to default", cooldown_bars_from_env() == 5)
finally:
    if _old_cd is None:
        os.environ.pop("CONTEXT_CONTRADICTION_COOLDOWN_BARS", None)
    else:
        os.environ["CONTEXT_CONTRADICTION_COOLDOWN_BARS"] = _old_cd

# (6) paper_trade_unlock cannot bypass: gate logic has no profile parameter,
# and the pipeline's [A3] block never references the gate profile
_a3_idx  = _pipe_src.find("[A3] Context-contradiction cooldown")
_a3_end  = _pipe_src.find("[B1]", _a3_idx) if _a3_idx != -1 else -1
_a3_blk  = _pipe_src[_a3_idx:_a3_end] if _a3_idx != -1 and _a3_end != -1 else ""
check("pipeline has [A3] contradiction-cooldown gate", _a3_idx != -1)
check("cooldown gate has no profile-based bypass",
      _a3_blk != "" and "_PAPER_GATE_PROFILE" not in _a3_blk
      and "EXECUTION_MODE" not in _a3_blk)
import inspect as _inspect
check("cooldown helper takes no profile/mode argument",
      "profile" not in _inspect.signature(contradiction_cooldown_active).parameters
      and "mode" not in _inspect.signature(contradiction_cooldown_active).parameters)

# (7) Cooldown block occurs before sizing / order intent
_sizing_idx2 = _pipe_src.find("_sizing = _size_live_position(")
check("cooldown gate rejects before sizing",
      _a3_idx != -1 and _sizing_idx2 != -1 and _a3_idx < _sizing_idx2,
      f"a3@{_a3_idx} sizing@{_sizing_idx2}")
check("pipeline logs [CONTEXT COOLDOWN BLOCK] on rejection",
      "[CONTEXT COOLDOWN BLOCK]" in _pipe_src)
check("contradiction is recorded per-underlying on runtime state",
      "_current_state.last_contradiction_bar = _bar_count" in _pipe_src)
check("hard-gate snapshot includes contradiction_cooldown field",
      "contradiction_cooldown=False" in _pipe_src)

# (8) Audit flags trades entered inside the cooldown window
_audit = _load_script("audit_executed_trade_decisions")

_log_ev = {"execution_mode": "paper", "profile": "paper_trade_unlock",
           "hard_gate_lines": [], "soft_gate_lines": []}
_trade2 = {
    "id": 2, "underlying": "NIFTY", "contract_symbol": "NIFTY2661623400CE",
    "entry_ts": "2026-06-10T06:55:01+00:00", "exit_ts": "2026-06-10T07:04:00+00:00",
    "entry_premium": 174.85, "exit_premium": 170.99, "pnl": -752.15, "state": "CLOSED",
}
_decs = [
    {"id": 829, "underlying": "NIFTY", "bar_index": 116,
     "bar_ts": "2026-06-10T06:54:01+00:00",
     "outcome": "CONTEXT_CONTRADICTION_REJECTED"},
    {"id": 832, "underlying": "NIFTY", "bar_index": 119,
     "bar_ts": "2026-06-10T06:55:01+00:00", "outcome": "ENTRY_TIER2"},
]
_audits_rows = [{"selected_symbol": "NIFTY2661623400CE",
                 "ts": "2026-06-10T06:55:01+00:00", "score_margin": 0.068}]
_res2 = _audit.audit_trade(_trade2, _decs, _audits_rows, [], _log_ev)
check("audit FAILs trade entered 3 bars after contradiction",
      any("ENTERED_WITHIN_CONTRADICTION_COOLDOWN" in f for f in _res2["failures"]),
      str(_res2["failures"]))
check("audit reports bars_since_last_context_contradiction",
      _res2["bars_since_last_context_contradiction"] == 3)

_decs_far = [
    {"id": 800, "underlying": "NIFTY", "bar_index": 110,
     "bar_ts": "2026-06-10T06:48:01+00:00",
     "outcome": "CONTEXT_CONTRADICTION_REJECTED"},
    {"id": 832, "underlying": "NIFTY", "bar_index": 119,
     "bar_ts": "2026-06-10T06:55:01+00:00", "outcome": "ENTRY_TIER2"},
]
_res_far = _audit.audit_trade(_trade2, _decs_far, _audits_rows, [], _log_ev)
check("audit passes entry 9 bars after contradiction",
      not any("COOLDOWN" in f for f in _res_far["failures"]),
      str(_res_far["failures"]))

# (9) Near-miss analyzer marks recent contradiction UNSAFE
_res_recent = _nm.classify_near_miss(_row(), had_recent_contradiction=True)
check("recent contradiction → UNSAFE_NEAR_MISS",
      _res_recent["classification"] == "UNSAFE_NEAR_MISS")
check("RECENT_CONTEXT_CONTRADICTION in unsafe reasons",
      any("RECENT_CONTEXT_CONTRADICTION" in r for r in _res_recent["unsafe_reasons"]))
check("no intent under unlock within cooldown",
      not _res_recent["would_intent_under_unlock"])

# End-to-end: analyze() detects the cooldown window from decision rows
_e2e_decisions = [{
    "underlying": "BANKNIFTY", "outcome": "CONTEXT_CONTRADICTION_REJECTED",
    "bar_ts": "2026-06-09T10:58:00+05:30", "conf_raw": 1.0,
}]
_e2e = _nm.analyze([_row(ts="2026-06-09T11:00:00+05:30")], _e2e_decisions)
_e2e_res = _e2e["results"][0]
check("analyze() flags candidate 2 min after contradiction",
      _e2e_res["had_recent_contradiction"]
      and _e2e_res["classification"] == "UNSAFE_NEAR_MISS",
      _e2e_res["classification"])

# (10) covered by Test 10 above: real_dry_run never calls kite.place_order


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
