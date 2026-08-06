"""Real round-trip transaction costs for index option trades.

Every P&L number this platform produced before 2026-07-05 was GROSS — a pure
LTP difference with no brokerage, statutory charges, or bid/ask spread. On the
guarded-stack batch that fiction was ~52% of the measured edge (median toll
₹132/trade vs ₹256 avg gross P&L/trade at 2% risk). This module is the single
source of truth for the toll; paper/replay P&L must be reported NET of it.

Rates (FY 2025-26, Zerodha, long index options, premium turnover):
  brokerage  flat ₹20 per executed order (2 orders per round trip)
  STT        0.1%    of SELL-side premium
  exch txn   NSE (NFO) 0.03503% / BSE (BFO) 0.0325%, both sides
  SEBI       ₹10/crore (0.0001%), both sides
  GST        18% on (brokerage + exch txn + SEBI)
  stamp      0.003%  of BUY-side premium

Spread crossing: cached/replayed prices are 1m LTP (≈ last trade, roughly mid);
a live market order buys the ask and sells the bid. Modelled as a configurable
half-spread fraction of premium per side (COST_SPREAD_FRAC_PER_SIDE, default
0.25%). real_armed fills already embed the spread — pass spread_frac_per_side=0
when the exit price is an actual broker fill.

COST_MODEL=0 disables the whole model (gross P&L, for comparing against
pre-2026-07-05 baselines).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

COST_MODEL_ENABLED: bool = (os.environ.get("COST_MODEL", "1") == "1")
SPREAD_FRAC_PER_SIDE: float = float(
    os.environ.get("COST_SPREAD_FRAC_PER_SIDE", "0.0025") or 0.0025
)

_BROKERAGE_PER_ORDER = 20.0
_STT_SELL_FRAC       = 0.001
_TXN_FRAC            = {"NFO": 0.0003503, "BFO": 0.000325}
_SEBI_FRAC           = 0.000001
_GST_FRAC            = 0.18
_STAMP_BUY_FRAC      = 0.00003


def exchange_for_underlying(underlying: str) -> str:
    """Option exchange for an index underlying (SENSEX/BANKEX → BFO, else NFO)."""
    return "BFO" if underlying.upper() in ("SENSEX", "BANKEX") else "NFO"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    brokerage: float
    stt: float
    txn: float
    sebi: float
    gst: float
    stamp: float
    spread: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.txn + self.sebi
                + self.gst + self.stamp + self.spread)


def option_round_trip_costs(*, entry_premium: float, exit_premium: float,
                            quantity: int, exchange: str = "NFO",
                            spread_frac_per_side: float | None = None) -> CostBreakdown:
    """Full toll for one buy→sell round trip. `quantity` = lot_size × lots."""
    buy_turn = max(entry_premium, 0.0) * max(quantity, 0)
    sell_turn = max(exit_premium, 0.0) * max(quantity, 0)
    txn_frac = _TXN_FRAC.get(exchange.upper(), _TXN_FRAC["NFO"])
    brokerage = _BROKERAGE_PER_ORDER * 2
    stt = _STT_SELL_FRAC * sell_turn
    txn = txn_frac * (buy_turn + sell_turn)
    sebi = _SEBI_FRAC * (buy_turn + sell_turn)
    gst = _GST_FRAC * (brokerage + txn + sebi)
    stamp = _STAMP_BUY_FRAC * buy_turn
    sf = SPREAD_FRAC_PER_SIDE if spread_frac_per_side is None else spread_frac_per_side
    spread = sf * (buy_turn + sell_turn)
    return CostBreakdown(brokerage=brokerage, stt=stt, txn=txn, sebi=sebi,
                         gst=gst, stamp=stamp, spread=spread)


def structure_round_trip_costs(legs, *, quantity: int, exchange: str = "NFO",
                               spread_frac_per_side: float | None = None) -> CostBreakdown:
    """Full toll for a MULTI-LEG structure (iron fly, straddle, spread).

    `legs` is an iterable of (entry_premium, exit_premium, opening_side) where
    opening_side is "SELL" for a short leg and "BUY" for a long leg; the closing
    order is the opposite side, so STT (sell-side) and stamp (buy-side) land on
    the correct halves of each leg.

    The term that matters here is BROKERAGE: it is FLAT PER EXECUTED ORDER, so a
    4-leg structure pays 8 × ₹20 = ₹160 before any percentage charge, and that
    does not shrink with size. Structures whose expected move is only a few points
    of premium cannot clear it — see the min-edge gate in iron_butterfly_live.py.
    """
    txn_frac = _TXN_FRAC.get(exchange.upper(), _TXN_FRAC["NFO"])
    sf = SPREAD_FRAC_PER_SIDE if spread_frac_per_side is None else spread_frac_per_side
    q = max(int(quantity), 0)
    brokerage = stt = txn = sebi = stamp = spread = 0.0
    for entry_px, exit_px, open_side in legs:
        open_side = str(open_side).upper()
        entry_turn = max(float(entry_px), 0.0) * q
        exit_turn  = max(float(exit_px), 0.0) * q
        # opening order + closing order
        brokerage += _BROKERAGE_PER_ORDER * 2
        txn       += txn_frac * (entry_turn + exit_turn)
        sebi      += _SEBI_FRAC * (entry_turn + exit_turn)
        spread    += sf * (entry_turn + exit_turn)
        if open_side == "SELL":          # sell to open, buy to close
            stt   += _STT_SELL_FRAC * entry_turn
            stamp += _STAMP_BUY_FRAC * exit_turn
        else:                            # buy to open, sell to close
            stt   += _STT_SELL_FRAC * exit_turn
            stamp += _STAMP_BUY_FRAC * entry_turn
    gst = _GST_FRAC * (brokerage + txn + sebi)
    return CostBreakdown(brokerage=brokerage, stt=stt, txn=txn, sebi=sebi,
                         gst=gst, stamp=stamp, spread=spread)


def iron_fly_round_trip_costs(*, atm_ce: float, atm_pe: float, wing_ce: float,
                              wing_pe: float, quantity: int, exchange: str = "NFO",
                              exit_fraction: float = 0.85,
                              spread_frac_per_side: float | None = None) -> CostBreakdown:
    """Toll for a 4-leg iron butterfly (8 orders). `exit_fraction` approximates the
    average exit premium when exact exit legs are unknown; cost is only weakly
    sensitive to it because the dominant terms are flat per order."""
    f = float(exit_fraction)
    return structure_round_trip_costs(
        [(atm_ce, atm_ce * f, "SELL"), (atm_pe, atm_pe * f, "SELL"),
         (wing_ce, wing_ce * f, "BUY"), (wing_pe, wing_pe * f, "BUY")],
        quantity=quantity, exchange=exchange,
        spread_frac_per_side=spread_frac_per_side)


def apply_costs(gross_pnl: float, *, entry_premium: float, exit_premium: float,
                quantity: int, underlying: str,
                spread_frac_per_side: float | None = None) -> tuple[float, float]:
    """(net_pnl, costs_total). No-op (gross, 0.0) when COST_MODEL=0. Never raises."""
    if not COST_MODEL_ENABLED:
        return gross_pnl, 0.0
    try:
        c = option_round_trip_costs(
            entry_premium=entry_premium, exit_premium=exit_premium,
            quantity=quantity, exchange=exchange_for_underlying(underlying),
            spread_frac_per_side=spread_frac_per_side,
        )
        return gross_pnl - c.total, c.total
    except Exception:
        return gross_pnl, 0.0
