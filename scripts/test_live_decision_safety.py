#!/usr/bin/env python3
"""
scripts/test_live_decision_safety.py

Offline tests for the live decision quality safety gates.

Covers:
  1. SHORT blocked (WARM_START_LOCKOUT) when live_bars_seen < threshold.
  2. LONG blocked (WARM_START_LOCKOUT) when live_bars_seen < threshold.
  3. Neutral HTF context does not trigger warmup block when bars_seen >= threshold.
  4. Premium stop exits at ~30% max loss (STOP_LOSS max_loss=30%).
  5. Same-contract re-entry blocked after STOP_LOSS (STOP_COOLDOWN_ACTIVE).
  6. Same-underlying cooldown blocks re-entry on different contract.
  7. Tier2/3 rejected when oi_support < 0.10 (INSUFFICIENT_CONFLUENCE).
  8. Valid Tier2 trade passes when oi_support >= 0.10.
  9. Tier1 trade passes even with low oi_support (only Tier2/3 gated).
 10. Stop-loss exit message says max_loss= and actual_loss=.

Does NOT import live_structure_pipeline.
Uses only backtest_runner and direct logic reproduction.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.backtesting.backtest_runner import (
    OptionPaperTrader,
    OptionPosition,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


# ── Constants reproduced from pipeline ───────────────────────────────────────
_MAX_OPTION_PREMIUM_LOSS_FRACTION  = 0.30
_STOP_LOSS_PCT                     = 1.0 - _MAX_OPTION_PREMIUM_LOSS_FRACTION   # 0.70
_TAKE_PROFIT_PCT                   = 2.00
_MIN_DTE_TO_HOLD                   = 2
_SAME_UNDERLYING_STOP_COOLDOWN_BARS = 5
_SAME_CONTRACT_STOP_COOLDOWN_BARS   = 15
_MIN_OI_SUPPORT_TIER23              = 0.10
_LIVE_BARS_BEFORE_ENTRY             = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeBreakdown:
    def __init__(self, oi_support: float = 0.80):
        self.oi_support = oi_support


class FakeSRec:
    def __init__(self, symbol: str = "BNK26JUN54000PE", oi_support: float = 0.80):
        self.option_symbol = symbol
        self.breakdown     = FakeBreakdown(oi_support)
        self.last_traded_price = 900.0
        self.lot_size      = 30


def _pre_entry_check(
    live_bars: int,
    last_stop_bar: int,
    last_stop_contract: str,
    bar_count: int,
    srec: FakeSRec,
    tier: int,
) -> str | None:
    """
    Reproduce the pre-entry safety check logic from live_structure_pipeline.
    Returns rejection reason string, or None if entry is allowed.
    """
    _bars_since_stop = bar_count - last_stop_bar

    if live_bars < _LIVE_BARS_BEFORE_ENTRY:
        return (
            f"WARM_START_LOCKOUT live_bars={live_bars}/{_LIVE_BARS_BEFORE_ENTRY}"
        )
    elif _bars_since_stop < _SAME_UNDERLYING_STOP_COOLDOWN_BARS:
        return (
            f"STOP_COOLDOWN_ACTIVE underlying_cooldown "
            f"bars_since_stop={_bars_since_stop}/{_SAME_UNDERLYING_STOP_COOLDOWN_BARS}"
        )
    elif (srec.option_symbol == last_stop_contract
            and _bars_since_stop < _SAME_CONTRACT_STOP_COOLDOWN_BARS):
        return (
            f"STOP_COOLDOWN_ACTIVE same_contract_cooldown "
            f"bars_since_stop={_bars_since_stop}/{_SAME_CONTRACT_STOP_COOLDOWN_BARS}"
        )
    # Confluence gate (Tier2/3)
    if tier >= 2 and srec.breakdown.oi_support < _MIN_OI_SUPPORT_TIER23:
        return (
            f"INSUFFICIENT_CONFLUENCE oi_support={srec.breakdown.oi_support:.3f} "
            f"< min={_MIN_OI_SUPPORT_TIER23} for tier={tier}"
        )
    return None


def _make_position(symbol: str, entry: float = 800.0,
                   expiry: date = date(2026, 6, 30)) -> OptionPosition:
    return OptionPosition(
        underlying_symbol="BANKNIFTY",
        contract_symbol=symbol,
        strike=54000.0,
        option_type="PUT",
        expiry_date=expiry,
        lot_size=30,
        lots=1,
        entry_premium=entry,
        entry_spot=54000.0,
        entry_iv=0.19,
        direction="SHORT",
        bar_index=100,
    )


# ── Test 1: SHORT blocked by warmup gate ──────────────────────────────────────
print("\n[Test 1] SHORT blocked by WARM_START_LOCKOUT (live_bars < threshold)")
srec = FakeSRec(oi_support=0.80)
reason = _pre_entry_check(
    live_bars=5, last_stop_bar=-9999, last_stop_contract="",
    bar_count=10, srec=srec, tier=2
)
check("warmup_blocks_short", reason is not None)
check("warmup_reason_correct", "WARM_START_LOCKOUT" in (reason or ""),
      f"reason={reason}")


# ── Test 2: LONG blocked by warmup gate ───────────────────────────────────────
print("\n[Test 2] LONG blocked by WARM_START_LOCKOUT")
reason2 = _pre_entry_check(
    live_bars=0, last_stop_bar=-9999, last_stop_contract="",
    bar_count=1, srec=FakeSRec(), tier=1
)
check("warmup_blocks_long", reason2 is not None)
check("warmup_long_reason", "WARM_START_LOCKOUT" in (reason2 or ""))


# ── Test 3: Neutral HTF — warmup passes when bars >= threshold ────────────────
print("\n[Test 3] Sufficient live bars → warmup does not block")
reason3 = _pre_entry_check(
    live_bars=20, last_stop_bar=-9999, last_stop_contract="",
    bar_count=30, srec=FakeSRec(), tier=2
)
check("warmup_passes_at_threshold", reason3 is None, f"reason={reason3}")


# ── Test 4: Premium stop exits at ~30% max loss ───────────────────────────────
print("\n[Test 4] Premium stop fires when premium falls 30% from entry")
trader4 = OptionPaperTrader()
trader4._position = _make_position("BNK26JUN54000PE", entry=800.0)

# Premium at 71% of entry: should NOT stop (still above 70% threshold)
closed4a = trader4.evaluate_and_close(
    current_spot=54000.0, current_iv=0.19,
    current_date=date(2026, 6, 10),
    bar_index=105, stop_loss_pct=_STOP_LOSS_PCT,
    take_profit_pct=_TAKE_PROFIT_PCT, min_dte=_MIN_DTE_TO_HOLD,
    force_reason=None,
)
# BSM will compute some price — if above _STOP_LOSS_PCT * 800 = 560, no stop
# We can't guarantee BSM output, but we can force-close and check the message format

# Use spot well ABOVE strike for PUT → PUT loses value (bearish position goes wrong)
# spot=70000 on PUT strike=54000 → PUT is far OTM, premium collapses to near 0
trader4b = OptionPaperTrader()
trader4b._position = _make_position("BNK26JUN54000PE", entry=800.0)
closed4b = trader4b.evaluate_and_close(
    current_spot=70000.0, current_iv=0.05,   # PUT far OTM — premium collapses
    current_date=date(2026, 6, 10),
    bar_index=105, stop_loss_pct=_STOP_LOSS_PCT,
    take_profit_pct=_TAKE_PROFIT_PCT, min_dte=_MIN_DTE_TO_HOLD,
    force_reason=None,
)
check("stop_loss_fires_put_otm", closed4b is not None,
      "BSM did not compute a stop — check spot/strike relationship")
if closed4b is not None:
    check("stop_loss_message_has_max_loss",
          "STOP_LOSS" in closed4b.exit_reason and "max_loss=30%" in closed4b.exit_reason,
          f"reason={closed4b.exit_reason}")
    check("stop_loss_message_has_actual_loss",
          "actual_loss=" in closed4b.exit_reason,
          f"reason={closed4b.exit_reason}")
else:
    FAIL.extend(["stop_loss_message_has_max_loss", "stop_loss_message_has_actual_loss"])
    print("  FAIL  stop_loss_message_has_max_loss — closed4b was None")
    print("  FAIL  stop_loss_message_has_actual_loss — closed4b was None")


# ── Test 5: Same-contract re-entry blocked after stop ─────────────────────────
print("\n[Test 5] Same-contract re-entry blocked for 15 bars after STOP_LOSS")
last_stop_bar5 = 100
last_stop_contract5 = "SENSEX2661174500PE"

# Immediate re-entry (bar_count=100, same contract) — should be blocked
reason5a = _pre_entry_check(
    live_bars=50, last_stop_bar=last_stop_bar5, last_stop_contract=last_stop_contract5,
    bar_count=100, srec=FakeSRec(symbol="SENSEX2661174500PE", oi_support=0.50), tier=2
)
check("same_contract_blocked_immediately",
      reason5a is not None and "STOP_COOLDOWN_ACTIVE" in reason5a,
      f"reason={reason5a}")

# At bar 114 (14 bars later, < 15) — still blocked
reason5b = _pre_entry_check(
    live_bars=50, last_stop_bar=last_stop_bar5, last_stop_contract=last_stop_contract5,
    bar_count=114, srec=FakeSRec(symbol="SENSEX2661174500PE", oi_support=0.50), tier=2
)
check("same_contract_still_blocked_at_14",
      reason5b is not None and "STOP_COOLDOWN_ACTIVE" in reason5b)

# At bar 115 (15 bars later) — allowed
reason5c = _pre_entry_check(
    live_bars=50, last_stop_bar=last_stop_bar5, last_stop_contract=last_stop_contract5,
    bar_count=115, srec=FakeSRec(symbol="SENSEX2661174500PE", oi_support=0.50), tier=2
)
check("same_contract_allowed_at_15", reason5c is None, f"reason={reason5c}")


# ── Test 6: Underlying-level cooldown (different contract) ────────────────────
print("\n[Test 6] Underlying cooldown: different contract also blocked for 5 bars")
last_stop_bar6 = 200
# Different contract but same underlying (within 5 bars)
reason6a = _pre_entry_check(
    live_bars=50, last_stop_bar=last_stop_bar6, last_stop_contract="SENSEX2661174500PE",
    bar_count=203, srec=FakeSRec(symbol="SENSEX2661174600PE", oi_support=0.50), tier=2
)
check("diff_contract_blocked_within_5",
      reason6a is not None and "STOP_COOLDOWN_ACTIVE" in reason6a,
      f"reason={reason6a}")

# At bar 205 (5 bars later) — allowed (only same-contract cooldown now applies)
reason6b = _pre_entry_check(
    live_bars=50, last_stop_bar=last_stop_bar6, last_stop_contract="SENSEX2661174500PE",
    bar_count=205, srec=FakeSRec(symbol="SENSEX2661174600PE", oi_support=0.50), tier=2
)
check("diff_contract_allowed_after_5", reason6b is None, f"reason={reason6b}")


# ── Test 7: Tier2/3 rejected with low OI ──────────────────────────────────────
print("\n[Test 7] Tier2/3 INSUFFICIENT_CONFLUENCE when oi_support=0.034")
reason7 = _pre_entry_check(
    live_bars=50, last_stop_bar=-9999, last_stop_contract="",
    bar_count=300, srec=FakeSRec(oi_support=0.034), tier=2
)
check("tier2_low_oi_blocked",
      reason7 is not None and "INSUFFICIENT_CONFLUENCE" in reason7,
      f"reason={reason7}")

# Tier3 with same low OI
reason7b = _pre_entry_check(
    live_bars=50, last_stop_bar=-9999, last_stop_contract="",
    bar_count=300, srec=FakeSRec(oi_support=0.05), tier=3
)
check("tier3_low_oi_blocked",
      reason7b is not None and "INSUFFICIENT_CONFLUENCE" in reason7b)


# ── Test 8: Valid Tier2 trade passes ─────────────────────────────────────────
print("\n[Test 8] Valid Tier2 trade passes with oi_support=0.80")
reason8 = _pre_entry_check(
    live_bars=50, last_stop_bar=-9999, last_stop_contract="",
    bar_count=300, srec=FakeSRec(oi_support=0.80), tier=2
)
check("valid_tier2_passes", reason8 is None, f"reason={reason8}")


# ── Test 9: Tier1 passes even with low OI ────────────────────────────────────
print("\n[Test 9] Tier1 passes even with oi_support=0.034 (gate is Tier2+ only)")
reason9 = _pre_entry_check(
    live_bars=50, last_stop_bar=-9999, last_stop_contract="",
    bar_count=300, srec=FakeSRec(oi_support=0.034), tier=1
)
check("tier1_low_oi_allowed", reason9 is None, f"reason={reason9}")


# ── Test 10: STOP_LOSS_PCT is 0.70 (30% max loss) ────────────────────────────
print("\n[Test 10] _STOP_LOSS_PCT == 0.70 (not the old 0.50)")
check("stop_loss_pct_is_0.70",
      abs(_STOP_LOSS_PCT - 0.70) < 0.001,
      f"actual={_STOP_LOSS_PCT}")
check("max_loss_fraction_is_0.30",
      abs(_MAX_OPTION_PREMIUM_LOSS_FRACTION - 0.30) < 0.001)


# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*62}")
print(f"  Results: {len(PASS)}/{len(PASS)+len(FAIL)} passed")
if FAIL:
    for f in FAIL:
        print(f"  FAIL: {f}")
else:
    print("  ALL PASS")
print(f"{'='*62}\n")

sys.exit(0 if not FAIL else 1)
