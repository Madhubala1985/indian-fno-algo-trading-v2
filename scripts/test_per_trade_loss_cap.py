#!/usr/bin/env python3
"""
test_per_trade_loss_cap.py — unit test + historical verification for the §1a max-loss cap.
Pure (no pipeline boot). Verifies the cap would have shrunk the −₹15,972 trade to ≤1.5% risk.
"""
from platform_core.risk.per_trade_loss_cap import max_loss_capped_lots, worst_case_loss

FAILS = 0


def check(name, cond):
    global FAILS
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS += 1


def main():
    print("── unit: max_loss_capped_lots ──")
    # disabled / bad inputs → None (no clamp)
    check("frac=0 disables (None)", max_loss_capped_lots(equity=4e5, entry_premium=500, lot_size=20, max_loss_frac=0) is None)
    check("zero premium → None", max_loss_capped_lots(equity=4e5, entry_premium=0, lot_size=20, max_loss_frac=0.015) is None)
    # basic math: budget=6000, per-lot risk=500*20*0.40=4000 → floor(6000/4000)=1
    check("basic cap = 1 lot", max_loss_capped_lots(equity=4e5, entry_premium=500, lot_size=20, max_loss_frac=0.015, adverse_frac=0.40) == 1)
    # cheaper premium → more lots allowed
    check("cheap premium → more lots", max_loss_capped_lots(equity=4e5, entry_premium=100, lot_size=20, max_loss_frac=0.015, adverse_frac=0.40) == 7)
    # higher adverse_frac (less trust in stop) → fewer lots
    check("adverse 1.0 shrinks size", max_loss_capped_lots(equity=4e5, entry_premium=500, lot_size=20, max_loss_frac=0.015, adverse_frac=1.0) == 0)

    print("\n── historical verification: the −₹15,972 trade (SENSEX LONG 2026-06-18) ──")
    # from trades.db: lots=4, lot_size=20, entry_premium=502.95, equity_before=417,839, pnl=-15,972
    eq, prem, ls, orig_lots, actual_loss = 417839.60, 502.95, 20, 4, 15972.41
    capped = max_loss_capped_lots(equity=eq, entry_premium=prem, lot_size=ls,
                                  max_loss_frac=0.015, adverse_frac=0.40)
    print(f"  original lots={orig_lots}  actual loss=₹{actual_loss:,.0f} ({100*actual_loss/eq:.2f}% of equity)")
    print(f"  cap would allow lots={capped}")
    check("cap reduces below original 4 lots", capped is not None and capped < orig_lots)
    # ex-ante worst-case at capped size must be ≤ 1.5% of equity
    wc = worst_case_loss(lots=capped, lot_size=ls, entry_premium=prem, adverse_frac=0.40)
    print(f"  ex-ante worst-case at {capped} lot(s) = ₹{wc:,.0f} ({100*wc/eq:.2f}% of equity)")
    check("capped worst-case ≤ 1.5% equity", wc <= 0.015 * eq + 1)
    # the actual loss, scaled to the capped size, would have been proportionally smaller
    scaled = actual_loss * capped / orig_lots
    print(f"  actual loss scaled to {capped} lot(s) ≈ ₹{scaled:,.0f} ({100*scaled/eq:.2f}% of equity) vs −₹15,972 (3.82%)")
    check("scaled realised loss ≤ 1.5% equity", scaled <= 0.015 * eq + 1)

    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILURES'}")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
