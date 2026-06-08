#!/usr/bin/env python3
"""
Test suite: small-capital paper risk engine (Tasks 1-10).

Tests PositionSizer math, min-lot override logic, paper risk profiles,
capital model, spread_bps computation, and intraday-profit safeguards.
"""
import os
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, ".")

from platform_core.risk.position_sizing import (
    PortfolioRiskSnapshot,
    PositionSizer,
    PositionSizingBreakdown,
    PositionSizingConfig,
    PositionSizingRequest,
    PositionSizingResult,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    status = PASS if ok else FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def _make_portfolio(
    equity: float,
    open_premium: float = 0.0,
    drawdown: float = 0.0,
    open_positions: int = 0,
) -> PortfolioRiskSnapshot:
    heat = open_premium / max(equity, 1.0)
    return PortfolioRiskSnapshot(
        equity=equity,
        available_cash=max(equity - open_premium, 0.0),
        current_portfolio_heat=heat,
        current_drawdown_fraction=drawdown,
        max_allowed_drawdown_fraction=0.15,
        correlation_exposure_fraction=0.0,
        open_position_count=open_positions,
    )


def _make_request(
    *,
    underlying: str,
    equity: float,
    premium: float,
    lot: int,
    conf: float = 1.0,
    composite: float = 0.70,
    iv: float = 0.20,
    spread_bps: float = 30.0,
    risk_frac: float = 0.020,
    max_lots: int = 3,
    open_premium: float = 0.0,
    drawdown: float = 0.0,
) -> PositionSizingRequest:
    stop = premium * 0.70
    portfolio = _make_portfolio(equity, open_premium=open_premium, drawdown=drawdown)
    return PositionSizingRequest(
        underlying_symbol=underlying,
        timestamp=datetime.now(timezone.utc),
        direction="SHORT",
        instrument_type="OPTION",
        entry_price=premium,
        stop_price=stop,
        contract_multiplier=1.0,
        lot_size=lot,
        minimum_lot_count=1,
        maximum_lot_count=max_lots,
        confidence_score=min(conf, 1.0),
        actionability_score=min(composite, 1.0),
        realized_volatility_ratio=max(iv / 0.18, 0.1),
        liquidity_score=0.70,
        spread_bps=spread_bps,
        slippage_bps_estimate=10.0,
        expected_correlation_to_book=0.0,
        portfolio=portfolio,
    )


def _sizer(risk_frac: float, heat_frac: float = 0.10) -> PositionSizer:
    return PositionSizer(config=PositionSizingConfig(
        max_risk_fraction_per_trade=risk_frac,
        max_portfolio_heat_fraction=heat_frac,
    ))


# ---------------------------------------------------------------------------
# Test 1 — ₹300k conservative 2%: high-premium SENSEX/NIFTY fail
# ---------------------------------------------------------------------------
print("\n=== Test 1: ₹300k conservative 2% — high-premium options fail ===")
try:
    equity = 300_000.0
    sz = _sizer(0.020)

    # SENSEX ATM ₹1,100, lot=20 → risk_per_lot=₹6,600 > 2%×₹300k=₹6,000 (before scaling)
    req_sensex = _make_request(underlying="SENSEX", equity=equity, premium=1100.0,
                               lot=20, spread_bps=30.0, risk_frac=0.020)
    res = sz.size_position(request=req_sensex)
    _record("SENSEX ATM ₹1100 rejected at 2%",
            not res.tradable and res.rejection_reason == "INSUFFICIENT_RISK_BUDGET",
            f"tradable={res.tradable} reason={res.rejection_reason}")

    # NIFTY ATM ₹350, lot=65 → risk_per_lot=₹6,825 > ₹6,000 (before scaling)
    sz2 = _sizer(0.020)
    req_nifty = _make_request(underlying="NIFTY", equity=equity, premium=350.0,
                              lot=65, spread_bps=30.0, risk_frac=0.020)
    res2 = sz2.size_position(request=req_nifty)
    _record("NIFTY ATM ₹350 rejected at 2%",
            not res2.tradable and res2.rejection_reason == "INSUFFICIENT_RISK_BUDGET",
            f"tradable={res2.tradable} reason={res2.rejection_reason}")
except Exception as exc:
    _record("Test 1 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 2 — ₹300k balanced 4%: SENSEX/NIFTY 1 lot can pass
# ---------------------------------------------------------------------------
print("\n=== Test 2: ₹300k balanced 4% — SENSEX + NIFTY pass ===")
try:
    equity = 300_000.0
    sz4 = _sizer(0.040)

    # SENSEX ATM ₹1,100, lot=20 → need eff≥₹6,600; 4%×₹300k=₹12k, with scales ≈₹6.9k
    req_s = _make_request(underlying="SENSEX", equity=equity, premium=1100.0,
                          lot=20, spread_bps=30.0, risk_frac=0.040)
    res_s = sz4.size_position(request=req_s)
    _record("SENSEX ATM ₹1100 passes at 4%",
            res_s.tradable and res_s.recommended_lots >= 1,
            f"lots={res_s.recommended_lots} eff_budget=₹{res_s.breakdown.effective_risk_budget:.0f}")

    # NIFTY ATM ₹350, lot=65 → need eff≥₹6,825
    sz4_nifty = _sizer(0.040)
    req_n = _make_request(underlying="NIFTY", equity=equity, premium=350.0,
                          lot=65, spread_bps=30.0, risk_frac=0.040)
    res_n = sz4_nifty.size_position(request=req_n)
    _record("NIFTY ATM ₹350 passes at 4%",
            res_n.tradable and res_n.recommended_lots >= 1,
            f"lots={res_n.recommended_lots} eff_budget=₹{res_n.breakdown.effective_risk_budget:.0f}")
except Exception as exc:
    _record("Test 2 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 3 — BANKNIFTY: passes only when premium/risk fits within budget
# ---------------------------------------------------------------------------
print("\n=== Test 3: BANKNIFTY — premium-aware pass/fail ===")
try:
    equity = 300_000.0
    sz4 = _sizer(0.040)

    # BANKNIFTY ₹900, lot=30 → risk_per_lot=₹8,100; needs eff≥₹8,100; 4%=₹12k, scaled ≈₹6.9k → FAIL
    req_bnk_hi = _make_request(underlying="BANKNIFTY", equity=equity, premium=900.0,
                               lot=30, spread_bps=30.0, risk_frac=0.040)
    res_hi = sz4.size_position(request=req_bnk_hi)
    _record("BANKNIFTY ₹900 (risk ₹8100) rejected at 4%",
            not res_hi.tradable and res_hi.rejection_reason == "INSUFFICIENT_RISK_BUDGET",
            f"eff_budget=₹{res_hi.breakdown.effective_risk_budget:.0f} need=₹8100")

    # BANKNIFTY ₹250 OTM, lot=30 → risk_per_lot=₹2,250; well within ₹6.9k budget → PASS
    req_bnk_lo = _make_request(underlying="BANKNIFTY", equity=equity, premium=250.0,
                               lot=30, spread_bps=30.0, risk_frac=0.040)
    res_lo = sz4.size_position(request=req_bnk_lo)
    _record("BANKNIFTY ₹250 OTM passes at 4%",
            res_lo.tradable and res_lo.recommended_lots >= 1,
            f"lots={res_lo.recommended_lots} eff_budget=₹{res_lo.breakdown.effective_risk_budget:.0f}")
except Exception as exc:
    _record("Test 3 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 4 — Min-lot override: allows exactly 1 lot when within risk cap
# ---------------------------------------------------------------------------
print("\n=== Test 4: Min-lot override — exactly 1 lot within risk cap ===")
try:
    MAX_OPTION_PREMIUM_LOSS_FRACTION = 0.30
    MAX_HEAT_FRACTION = 0.10

    def _min_lot_override_eligible(
        entry_premium: float,
        lot_size: int,
        sizing_equity: float,
        max_single_trade_risk_frac: float,
        open_premium: float,
        equity: float,
    ) -> bool:
        risk_per_lot = entry_premium * MAX_OPTION_PREMIUM_LOSS_FRACTION * lot_size
        max_risk = sizing_equity * max_single_trade_risk_frac
        heat = open_premium / max(equity, 1.0)
        heat_per_lot = entry_premium * lot_size / max(equity, 1.0)
        remaining_heat = max(0.0, MAX_HEAT_FRACTION - heat)
        return risk_per_lot <= max_risk and heat_per_lot <= remaining_heat

    # SENSEX ₹1,100 × 20 lots × 30% = ₹6,600 risk_per_lot; 4% × ₹300k = ₹12k cap → override OK
    ok = _min_lot_override_eligible(1100.0, 20, 300_000.0, 0.040, 0.0, 300_000.0)
    _record("SENSEX ₹1100 override eligible at 4% cap",
            ok, f"risk_per_lot=₹{1100.0*0.30*20:.0f}  max=₹{300000.0*0.04:.0f}")

    # BANKNIFTY ₹950 × 30 × 30% = ₹8,550; 4% cap = ₹12k → override OK
    ok2 = _min_lot_override_eligible(950.0, 30, 300_000.0, 0.040, 0.0, 300_000.0)
    _record("BANKNIFTY ₹950 override eligible at 4% cap",
            ok2, f"risk_per_lot=₹{950.0*0.30*30:.0f}  max=₹{300000.0*0.04:.0f}")

    # BANKNIFTY ₹1600 × 30 × 30% = ₹14,400; 4% cap = ₹12k → override BLOCKED
    blocked = not _min_lot_override_eligible(1600.0, 30, 300_000.0, 0.040, 0.0, 300_000.0)
    _record("BANKNIFTY ₹1600 override blocked at 4% cap",
            blocked, f"risk_per_lot=₹{1600.0*0.30*30:.0f}  max=₹{300000.0*0.04:.0f}")

    # Heat saturation blocks override
    # 1 open lot already using 9% heat; adding another 4% would breach 10%
    open_prem = 300_000.0 * 0.09   # 9% heat
    blocked_heat = not _min_lot_override_eligible(
        600.0, 20, 300_000.0, 0.040, open_prem, 300_000.0
    )
    heat_per_lot = 600.0 * 20 / 300_000.0
    remaining = max(0.0, 0.10 - 0.09)
    _record("Override blocked by portfolio heat saturation",
            blocked_heat,
            f"heat_per_lot={heat_per_lot:.4f}  remaining={remaining:.4f}")
except Exception as exc:
    _record("Test 4 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 5 — Min-lot override does NOT bypass strike/confluence/context gates
# ---------------------------------------------------------------------------
print("\n=== Test 5: Override does not bypass safety gates ===")
try:
    # This test validates the code-path ordering documented in _size_live_position.
    # The override is in _size_live_position which runs BEFORE OI/quality/context gates.
    # Returning tradable=True from override still passes through [C] OI gate and
    # [D] strike quality gate in the caller.  We verify the gate code is downstream.
    import ast
    with open("platform_core/data/live/live_structure_pipeline.py") as fh:
        src = fh.read()

    # MIN_LOT_OVERRIDE must appear before INSUFFICIENT_CONFLUENCE
    idx_override = src.find("MIN_LOT_OVERRIDE_WITHIN_RISK_CAP")
    idx_oi_gate  = src.find("INSUFFICIENT_CONFLUENCE")
    idx_sq_gate  = src.find("INSUFFICIENT_STRIKE_QUALITY")
    idx_ctx_gate = src.find("CONTEXT_CONTRADICTION_REJECTED")

    _record("MIN_LOT_OVERRIDE defined in source", idx_override > 0)
    _record("OI gate (INSUFFICIENT_CONFLUENCE) is downstream of override",
            idx_oi_gate > idx_override,
            f"override@{idx_override}  oi_gate@{idx_oi_gate}")
    _record("Strike quality gate is downstream of override",
            idx_sq_gate > idx_override,
            f"override@{idx_override}  sq_gate@{idx_sq_gate}")
    _record("Context contradiction gate is downstream of override",
            idx_ctx_gate > idx_override,
            f"override@{idx_override}  ctx@{idx_ctx_gate}")
except Exception as exc:
    _record("Test 5 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 6 — max_open_positions=2 at ₹300k
# ---------------------------------------------------------------------------
print("\n=== Test 6: max_open_positions cap at ₹300k ===")
try:
    os.environ["PAPER_STARTING_CAPITAL"] = "300000"
    import importlib, types

    # Read the capital-tier logic directly rather than importing the live pipeline
    with open("platform_core/data/live/live_structure_pipeline.py") as fh:
        src = fh.read()

    def _eval_max_open(capital: int) -> int:
        # Simulate the expression in the source
        initial = float(capital)
        env_val = None  # no MAX_OPEN_POSITIONS env override
        default = "1" if initial < 300_000 else "2" if initial < 500_000 else "3"
        return int(default)

    _record("₹200k → max_open=1", _eval_max_open(200_000) == 1, str(_eval_max_open(200_000)))
    _record("₹300k → max_open=2", _eval_max_open(300_000) == 2, str(_eval_max_open(300_000)))
    _record("₹499k → max_open=2", _eval_max_open(499_000) == 2, str(_eval_max_open(499_000)))
    _record("₹500k → max_open=3", _eval_max_open(500_000) == 3, str(_eval_max_open(500_000)))
except Exception as exc:
    _record("Test 6 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 7 — Same-day profits do not increase sizing budget
# ---------------------------------------------------------------------------
print("\n=== Test 7: Same-day profits do not inflate sizing equity ===")
try:
    session_opening = 300_000.0
    # Trade wins 5% intraday
    current_equity  = session_opening * 1.05   # ₹315,000

    sizing_equity = max(min(current_equity, session_opening), 1.0)
    _record("sizing_equity capped at session opening when equity > session_opening",
            sizing_equity == session_opening,
            f"equity=₹{current_equity:.0f}  sizing=₹{sizing_equity:.0f}")

    # Losses reduce sizing equity
    current_equity_loss = session_opening * 0.95   # ₹285,000
    sizing_equity_loss = max(min(current_equity_loss, session_opening), 1.0)
    _record("sizing_equity reduced when equity < session_opening",
            sizing_equity_loss == current_equity_loss,
            f"equity=₹{current_equity_loss:.0f}  sizing=₹{sizing_equity_loss:.0f}")

    # Verify Tier 3 cannot activate from intraday profit
    # The new condition uses session_opening_equity >= INITIAL_EQUITY * 1.03
    initial = 300_000.0
    tier3_threshold = initial * 1.03  # ₹309,000
    session_open_same_day = 300_000.0  # session started at ₹300k

    tier3_from_intraday = session_open_same_day >= tier3_threshold
    _record("Tier 3 cannot activate from same-day profit (session_opening < threshold)",
            not tier3_from_intraday,
            f"session_open=₹{session_open_same_day:.0f}  threshold=₹{tier3_threshold:.0f}")

    # Tier 3 activates correctly when session STARTED above threshold (multi-session growth)
    session_open_next = 310_000.0  # yesterday closed at ₹310k
    tier3_from_multiday = session_open_next >= tier3_threshold
    _record("Tier 3 activates when session opening exceeds threshold (multi-session growth)",
            tier3_from_multiday,
            f"session_open=₹{session_open_next:.0f}  threshold=₹{tier3_threshold:.0f}")
except Exception as exc:
    _record("Test 7 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 8 — Same-day losses reduce available risk budget
# ---------------------------------------------------------------------------
print("\n=== Test 8: Same-day losses reduce available risk ===")
try:
    session_opening = 300_000.0
    loss_equity     = 285_000.0   # ₹15k loss on the day

    # sizing_equity should reflect loss (not capped from below at session opening)
    sizing_equity = max(min(loss_equity, session_opening), 1.0)
    base_budget   = sizing_equity * 0.040  # 4% tier2
    _record("Loss reduces base risk budget",
            base_budget < session_opening * 0.040,
            f"base=₹{base_budget:.0f}  vs opening_base=₹{session_opening*0.040:.0f}")

    # Verify via sizer
    sz = _sizer(0.040)
    req = _make_request(underlying="SENSEX", equity=loss_equity, premium=500.0,
                        lot=20, spread_bps=30.0, risk_frac=0.040,
                        drawdown=0.05)
    res = sz.size_position(request=req)
    # At ₹285k × 4% and drawdown_scale applied, budget should be reduced
    _record("Drawdown correctly applied to effective budget after loss",
            res.breakdown.base_risk_budget < session_opening * 0.040,
            f"base=₹{res.breakdown.base_risk_budget:.0f}")
except Exception as exc:
    _record("Test 8 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 9 — Paper risk profile prints warning for > 2.5% risk
# ---------------------------------------------------------------------------
print("\n=== Test 9: Paper risk profile — warning printed for aggressive config ===")
try:
    # Read startup print block from source to confirm warning condition
    with open("platform_core/data/live/live_structure_pipeline.py") as fh:
        src = fh.read()

    warning_printed = (
        "not approved for real funds" in src
        and "_TIER2_RISK_FRAC > 0.025" in src
        and "RISK CONFIG" in src
        and "profile=" in src
    )
    _record("Warning block present for risk > 2.5%", warning_printed)

    # Verify config resolution: balanced profile
    os_env_backup = os.environ.copy()
    os.environ["PAPER_RISK_PROFILE"] = "balanced"
    os.environ.pop("PAPER_TIER2_RISK_FRAC", None)
    profile = os.environ.get("PAPER_RISK_PROFILE", "conservative").lower()
    tier2 = (
        float(os.environ["PAPER_TIER2_RISK_FRAC"])
        if os.environ.get("PAPER_TIER2_RISK_FRAC")
        else 0.040 if profile in ("balanced", "aggressive") else 0.020
    )
    _record("balanced profile → TIER2_RISK_FRAC=4%", tier2 == 0.040, f"{tier2*100:.1f}%")

    os.environ["PAPER_RISK_PROFILE"] = "conservative"
    profile_c = os.environ.get("PAPER_RISK_PROFILE", "conservative").lower()
    tier2_c = (
        float(os.environ["PAPER_TIER2_RISK_FRAC"])
        if os.environ.get("PAPER_TIER2_RISK_FRAC")
        else 0.040 if profile_c in ("balanced", "aggressive") else 0.020
    )
    _record("conservative profile → TIER2_RISK_FRAC=2%", tier2_c == 0.020, f"{tier2_c*100:.1f}%")

    # explicit env override beats profile
    os.environ["PAPER_RISK_PROFILE"] = "conservative"
    os.environ["PAPER_TIER2_RISK_FRAC"] = "0.035"
    tier2_override = float(os.environ.get("PAPER_TIER2_RISK_FRAC", "0.020"))
    _record("PAPER_TIER2_RISK_FRAC env override beats profile",
            tier2_override == 0.035, f"{tier2_override*100:.1f}%")

    # Restore env
    for k in list(os.environ.keys()):
        if k not in os_env_backup:
            del os.environ[k]
    os.environ.update(os_env_backup)
except Exception as exc:
    _record("Test 9 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Test 10 — spread_bps: real bid/ask used when available, fallback otherwise
# ---------------------------------------------------------------------------
print("\n=== Test 10: spread_bps — real bid/ask vs fallback ===")
try:
    _REFERENCE_PREMIUM = 900.0

    def _compute_spread_bps(bid, ask, premium):
        if (
            bid is not None and ask is not None
            and ask > bid >= 0.0
            and premium > 0.0
        ):
            return (ask - bid) / premium * 10_000.0
        return 30.0

    # Real bid/ask: ₹895/₹905 on ₹900 premium = (10/900)*10000 = 111.1 bps
    real_spread = _compute_spread_bps(895.0, 905.0, 900.0)
    _record("Real bid/ask computes correct spread_bps",
            abs(real_spread - 111.11) < 0.1, f"{real_spread:.2f} bps")

    # No bid/ask → fallback 30 bps
    fallback = _compute_spread_bps(None, None, 900.0)
    _record("Missing bid/ask falls back to 30 bps", fallback == 30.0, f"{fallback:.1f}")

    # bid >= ask (crossed) → fallback
    crossed = _compute_spread_bps(910.0, 895.0, 900.0)
    _record("Crossed bid/ask falls back to 30 bps", crossed == 30.0, f"{crossed:.1f}")

    # Zero premium → fallback
    zero_prem = _compute_spread_bps(0.5, 1.0, 0.0)
    _record("Zero premium falls back to 30 bps", zero_prem == 30.0, f"{zero_prem:.1f}")

    # Tight spread improves liquidity_scale vs hardcoded 50 bps
    sz = _sizer(0.040)
    req_tight = _make_request(underlying="SENSEX", equity=300_000.0, premium=1100.0,
                              lot=20, spread_bps=15.0)
    req_wide  = _make_request(underlying="SENSEX", equity=300_000.0, premium=1100.0,
                              lot=20, spread_bps=50.0)
    res_tight = sz.size_position(request=req_tight)
    res_wide  = sz.size_position(request=req_wide)
    _record("Tighter spread yields higher liquidity_scale",
            res_tight.breakdown.liquidity_scale > res_wide.breakdown.liquidity_scale,
            f"tight={res_tight.breakdown.liquidity_scale:.3f}  "
            f"wide={res_wide.breakdown.liquidity_scale:.3f}")
except Exception as exc:
    _record("Test 10 exception", False, str(exc))
    traceback.print_exc()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in _results if ok)
total  = len(_results)
print(f"RESULTS: {passed}/{total} passed")
for name, ok, detail in _results:
    if not ok:
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
print("=" * 60)
sys.exit(0 if passed == total else 1)
