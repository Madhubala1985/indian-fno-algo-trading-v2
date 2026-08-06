"""ACTIVE intraday scalp replay — how an intelligent trader works the moves, not parks a
position. Minute-resolution, trend-following, MANY entries/exits, trading the ATM option in
the direction of the intraday drift, cutting losers fast and riding to the next flip.

Mechanical rule (NOT hand-picked — same params every day/index):
  spot(t)   = ATM strike + (CE_atm - PE_atm)         # put-call parity, minute resolution
  signal    = EMA(FAST) vs EMA(SLOW) of spot         # up -> buy ATM CALL, down -> buy ATM PUT
  entry     = on a fresh crossover, buy the ATM option in the new direction (1 lot)
  exit      = opposite crossover  OR  stop at -STOP% of premium  OR  EOD flat
  re-enter  = on every subsequent crossover -> multiple scalps per day

Prices from the REAL recorded chains (data/option_history/), net of cost. Compared to the
model's buy-and-hold ATM call. Honest caveat: observed days + a fixed rule I chose after
seeing the week -> illustrative of ACTIVE vs PARKED, not out-of-sample proof.
"""
import glob, os, sys

HARNESS = os.path.join(os.path.dirname(__file__), "study_iron_condor.py")
ns = {}
exec(open(HARNESS).read().split("def bt(")[0], ns)
load_day, LOT = ns["load_day"], ns["LOT"]

DAYS = ["2026-08-03", "2026-08-04", "2026-08-05"]
FAST, SLOW = 9, 21          # EMA lengths in minutes
STOP = 0.30                 # cut a scalp at -30% of premium paid
# 2026-08-07: costs via the platform's single source of truth (flat Rs20/order matters).
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from platform_core.execution.transaction_costs import (
    structure_round_trip_costs, exchange_for_underlying,
)
MIN_HOLD = 2                # min minutes before stop can trigger (avoid entry-bar noise)


def ema(series, n):
    k = 2.0 / (n + 1)
    out = []
    e = series[0]
    for v in series:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def synth_spot(row):
    atm = row[1]
    ce, pe = row[2].get((atm, "CE")), row[2].get((atm, "PE"))
    return atm + (ce - pe) if (ce and pe) else None


def price(legs, strikes, strike, typ):
    if (strike, typ) in legs:
        return legs[(strike, typ)]
    step = (strikes[1] - strikes[0]) if len(strikes) > 1 else 50
    for d in range(1, 8):
        for s in (strike + d * step, strike - d * step):
            if (s, typ) in legs:
                return legs[(s, typ)]
    return None


def scalp_day(path):
    und, rows = load_day(path)
    lot = LOT.get(und, 25)
    # build aligned minute series with a valid synthetic spot
    seq = []
    for r in rows:
        s = synth_spot(r)
        if s is not None:
            seq.append((r, s))
    if len(seq) < SLOW + 5:
        return None
    spots = [s for _, s in seq]
    ef, es = ema(spots, FAST), ema(spots, SLOW)

    trades = []
    pos = None  # dict: side('CE'/'PE'), strike, entry_px, entry_i
    for i in range(SLOW, len(seq)):
        r = seq[i][0]
        legs, strikes, atm = r[2], r[3], r[1]
        up = ef[i] > es[i]
        prev_up = ef[i - 1] > es[i - 1]
        cross = up != prev_up  # fresh crossover this minute

        # manage open position
        if pos:
            cur = price(legs, strikes, pos["strike"], pos["side"])
            if cur is not None:
                held = i - pos["entry_i"]
                stop_hit = held >= MIN_HOLD and cur <= pos["entry_px"] * (1 - STOP)
                flip = cross and (up == (pos["side"] == "PE"))  # signal now opposes our side
                if stop_hit or flip or i == len(seq) - 1:
                    pnl = (cur - pos["entry_px"]) * lot - structure_round_trip_costs(
                        [(pos["entry_px"], cur, "BUY")], quantity=lot,
                        exchange=exchange_for_underlying(und)).total
                    trades.append({"side": pos["side"], "pnl": pnl,
                                   "reason": "stop" if stop_hit else ("flip" if flip else "eod"),
                                   "held": held})
                    pos = None

        # enter on a fresh crossover if flat
        if pos is None and cross and i < len(seq) - 1:
            side = "CE" if up else "PE"
            px = price(legs, strikes, atm, side)
            if px:
                pos = {"side": side, "strike": atm, "entry_px": px, "entry_i": i}

    # model reference: buy ATM call at ~09:30 (index SLOW) hold to EOD
    e = seq[SLOW][0]; x = seq[-1][0]
    ce_e = price(e[2], e[3], e[1], "CE"); ce_x = price(x[2], x[3], e[1], "CE")
    model_pnl = ((ce_x - ce_e) * lot - structure_round_trip_costs(
        [(ce_e, ce_x, "BUY")], quantity=lot,
        exchange=exchange_for_underlying(und)).total) if (ce_e and ce_x) else 0.0
    return {"und": und, "lot": lot, "trades": trades,
            "scalp_pnl": sum(t["pnl"] for t in trades), "model_pnl": model_pnl,
            "n": len(trades), "wins": sum(1 for t in trades if t["pnl"] > 0)}


def main():
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "option_history")
    print(f"ACTIVE intraday scalp (EMA {FAST}/{SLOW} cross, stop -{int(STOP*100)}%, ATM option, 1 lot)\n")
    print(f"{'day':<12}{'idx':<11}{'scalps':>7}{'win%':>6}{'  SCALP net':>13}{'  MODEL hold-call':>18}")
    print("-" * 74)
    stot = mtot = 0.0; sn = sw = 0
    for day in DAYS:
        for path in sorted(glob.glob(os.path.join(base, f"*_{day}_*.jsonl"))):
            if os.path.basename(path).startswith("_"):
                continue
            r = scalp_day(path)
            if not r:
                continue
            stot += r["scalp_pnl"]; mtot += r["model_pnl"]; sn += r["n"]; sw += r["wins"]
            wr = 100 * r["wins"] / r["n"] if r["n"] else 0
            print(f"{day:<12}{r['und']:<11}{r['n']:>7}{wr:>5.0f}%"
                  f"{r['scalp_pnl']:>+13,.0f}{r['model_pnl']:>+18,.0f}")
    print("-" * 74)
    print(f"{'TOTAL':<23}{sn:>7}{(100*sw/sn if sn else 0):>5.0f}%{stot:>+13,.0f}{mtot:>+18,.0f}")
    print(f"\n  ACTIVE scalp:      net Rs {stot:>+10,.0f}   {sw}/{sn} scalps won = {100*sw/max(sn,1):.0f}%")
    print(f"  MODEL hold-call:   net Rs {mtot:>+10,.0f}")
    print(f"\n  Caveat: observed days, fixed rule chosen post-hoc -> ACTIVE-vs-PARKED illustration,")
    print(f"  not OOS proof. Whipsaw risk is real on chop; params (9/21, -30%) are untuned defaults.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
