"""
per_trade_loss_cap.py — hard ex-ante per-trade max-loss cap (batch-4 §1a).

The premium cap bounds OUTLAY, not LOSS: the −₹15,972 trade (9.6% outlay, 3.8% loss) passed
it. This caps the number of lots so a single trade's ex-ante worst-case loss cannot exceed a
fraction of equity — sizing DERIVES from that risk, it does not hope the 30% stop fires.

worst_case_loss(lots) = lots · lot_size · entry_premium · adverse_frac
`adverse_frac` is the assumed adverse premium move; default 0.40 is deliberately ABOVE the 30%
stop (slippage/gap headroom, i.e. not trusting the stop). Raise toward 1.0 to assume the option
can round-trip to zero; that shrinks size accordingly.

Pure + dependency-free so it is unit-testable WITHOUT importing the live pipeline (which boots
on import). Returns the max lots the loss cap permits (0 = reject the trade), or None if the cap
is disabled / inputs are non-positive (caller then applies no loss clamp).
"""
from __future__ import annotations


def max_loss_capped_lots(*, equity: float, entry_premium: float, lot_size: int,
                         max_loss_frac: float, adverse_frac: float = 0.40) -> int | None:
    if (max_loss_frac <= 0.0 or adverse_frac <= 0.0 or entry_premium <= 0.0
            or lot_size <= 0 or equity <= 0.0):
        return None
    budget = equity * max_loss_frac
    per_lot_risk = entry_premium * lot_size * adverse_frac
    if per_lot_risk <= 0.0:
        return None
    return int(budget // per_lot_risk)


def worst_case_loss(*, lots: int, lot_size: int, entry_premium: float,
                    adverse_frac: float = 0.40) -> float:
    """Ex-ante worst-case rupee loss for a sized position (for logging/verification)."""
    return max(0, lots) * max(0, lot_size) * max(0.0, entry_premium) * max(0.0, adverse_frac)
