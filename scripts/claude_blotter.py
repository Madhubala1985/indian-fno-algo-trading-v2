"""Full trade blotter for the regime-matched plan (pin days -> iron butterfly short vol;
trend day -> active PUT/CALL scalp), priced on the REAL recorded chains. Emits EVERY trade
with entry/exit time, strike, side, prices, P&L and reason, then totals per lot.

Honest framing: regimes here are assigned with hindsight (Aug 4 = the trend day), params are
post-hoc defaults, 1 lot, 3 observed days. This is the CEILING of the idea, not OOS proof.
"""
import glob, os, sys

HARNESS = os.path.join(os.path.dirname(__file__), "study_iron_condor.py")
ns = {}
exec(open(HARNESS).read().split("def bt(")[0], ns)
load_day, LOT, fly_value = ns["load_day"], ns["LOT"], ns["fly_value"]

FAST, SLOW, STOP, MIN_HOLD, WING = 9, 21, 0.30, 2, 2
# 2026-08-07: costs now come from the platform's single source of truth. The old
# proportional `COST_LEG * premium` model ignored the flat Rs20-per-order brokerage
# and undercharged 4-leg structures ~6-9x.
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platform_core.execution.transaction_costs import (
    structure_round_trip_costs, exchange_for_underlying,
)
PIN_DAYS = {"2026-08-03", "2026-08-05"}      # short vol
TREND_DAYS = {"2026-08-04"}                  # active scalp


def ema(series, n):
    k = 2.0 / (n + 1); e = series[0]; out = []
    for v in series:
        e = v * k + e * (1 - k); out.append(e)
    return out


def synth(row):
    atm = row[1]; ce, pe = row[2].get((atm, "CE")), row[2].get((atm, "PE"))
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


def hm(ts):  # 'HH:MM' IST from a UTC iso ts
    t = ts[11:16]
    h, m = int(t[:2]) + 5, int(t[3:]) + 30
    if m >= 60: m -= 60; h += 1
    if h >= 24: h -= 24
    return f"{h:02d}:{m:02d}"


def iron_butterfly(path):
    und, rows = load_day(path); lot = LOT.get(und, 25)
    e, x = rows[SLOW], rows[-1]; atm = e[1]
    v0, _ = fly_value(e[2], atm, e[3], WING)
    vx, _ = fly_value(x[2], atm, x[3], WING)
    if v0 is None or vx is None:
        return []
    _step = (e[3][1] - e[3][0]) if len(e[3]) > 1 else 100
    _w = WING * _step
    _legs = [(price(e[2], e[3], atm, "CE"), price(x[2], x[3], atm, "CE"), "SELL"),
             (price(e[2], e[3], atm, "PE"), price(x[2], x[3], atm, "PE"), "SELL"),
             (price(e[2], e[3], atm + _w, "CE"), price(x[2], x[3], atm + _w, "CE"), "BUY"),
             (price(e[2], e[3], atm - _w, "PE"), price(x[2], x[3], atm - _w, "PE"), "BUY")]
    if any(a is None or b is None for a, b, _ in _legs):
        return []
    _cost = structure_round_trip_costs(
        _legs, quantity=lot, exchange=exchange_for_underlying(und)).total
    pnl = (v0 - vx) * lot - _cost
    return [{"und": und, "kind": "IRON_FLY", "in": hm(e[0]), "out": hm(x[0]),
             "strike": atm, "side": "short straddle +/-2 wings", "epx": v0, "xpx": vx,
             "pnl": pnl, "reason": "eod"}]


def scalp(path):
    und, rows = load_day(path); lot = LOT.get(und, 25)
    seq = [(r, synth(r)) for r in rows]; seq = [(r, s) for r, s in seq if s is not None]
    if len(seq) < SLOW + 5:
        return []
    ef, es = ema([s for _, s in seq], FAST), ema([s for _, s in seq], SLOW)
    out = []; pos = None
    for i in range(SLOW, len(seq)):
        r = seq[i][0]; legs, strikes, atm = r[2], r[3], r[1]
        up, prev_up = ef[i] > es[i], ef[i - 1] > es[i - 1]; cross = up != prev_up
        if pos:
            cur = price(legs, strikes, pos["strike"], pos["side"])
            if cur is not None:
                held = i - pos["ei"]
                stop = held >= MIN_HOLD and cur <= pos["epx"] * (1 - STOP)
                flip = cross and (up == (pos["side"] == "PE"))
                if stop or flip or i == len(seq) - 1:
                    pnl = (cur - pos["epx"]) * lot - structure_round_trip_costs(
                        [(pos["epx"], cur, "BUY")], quantity=lot,
                        exchange=exchange_for_underlying(und)).total
                    out.append({"und": und, "kind": "SCALP", "in": pos["in"], "out": hm(r[0]),
                                "strike": pos["strike"], "side": pos["side"],
                                "epx": pos["epx"], "xpx": cur, "pnl": pnl,
                                "reason": "stop" if stop else ("flip" if flip else "eod")})
                    pos = None
        if pos is None and cross and i < len(seq) - 1:
            side = "CE" if up else "PE"; px = price(legs, strikes, atm, side)
            if px:
                pos = {"side": side, "strike": atm, "epx": px, "ei": i, "in": hm(r[0])}
    return out


def main():
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "option_history")
    grand = 0.0; nt = 0; nw = 0
    for day in ["2026-08-03", "2026-08-04", "2026-08-05"]:
        tactic = "SCALP (active directional)" if day in TREND_DAYS else "IRON BUTTERFLY (short vol)"
        print(f"\n===== {day}  regime={'TREND' if day in TREND_DAYS else 'PIN'}  -> {tactic} =====")
        print(f"  {'idx':<10}{'in':>6}{'out':>7}{'  strike':>9}{'  side':>20}{'entry':>9}{'exit':>9}{'pnl':>10}{'  why':>7}")
        day_tot = 0.0
        for path in sorted(glob.glob(os.path.join(base, f"*_{day}_*.jsonl"))):
            if os.path.basename(path).startswith("_"):
                continue
            trades = scalp(path) if day in TREND_DAYS else iron_butterfly(path)
            for t in trades:
                day_tot += t["pnl"]; grand += t["pnl"]; nt += 1; nw += t["pnl"] > 0
                print(f"  {t['und']:<10}{t['in']:>6}{t['out']:>7}{t['strike']:>9.0f}"
                      f"{t['side']:>20}{t['epx']:>9.1f}{t['xpx']:>9.1f}{t['pnl']:>+10,.0f}{t['reason']:>7}")
        print(f"  {'-'*88}\n  DAY TOTAL: Rs {day_tot:>+10,.0f}")
    print(f"\n{'='*70}")
    print(f"  REGIME-MATCHED TOTAL (1 lot, 3 days):  Rs {grand:>+12,.0f}")
    print(f"  trades: {nt}   winners: {nw} ({100*nw/max(nt,1):.0f}%)")
    print(f"  Model (buy+hold call) same 3 days:     Rs      -14,582")
    print(f"  -> MY PROFIT ~ +{grand:,.0f}/lot ; the ~31k figure is the SWING vs the model, not profit.")
    print(f"\n  CAVEAT: hindsight regime labels, post-hoc params, 1 lot, 3 observed days. Ceiling, not proof.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
