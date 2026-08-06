"""
study_iron_condor.py — HEDGED short-vol (2026-08-01). The naked short straddle harvested the vol
risk premium (+18k..+32k, 75% win) but with an open tail. Add protective WINGS: long OTM call at
ATM+W and long OTM put at ATM-W. That's an iron butterfly (short ATM straddle + long strangle
wings) — the tail is now HARD-CAPPED. Question: does it keep the edge with the tail bounded?

Position value V = (ATM_CE + ATM_PE) - (wingUP_CE + wingDN_PE)   [short straddle minus long wings]
We collect V at entry (credit), pay to close. P&L per unit = V_entry - V_exit (profit as it decays).
Max loss is bounded by the wing width. 4 legs -> cost on each. Full-session hold, skip expansion.
"""
import collections, glob, json, os, statistics
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from platform_core.execution.transaction_costs import (
    structure_round_trip_costs, exchange_for_underlying,
)

LOT = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20}


def load_day(path):
    per_min = collections.defaultdict(dict)
    und = os.path.basename(path).split("_")[0]
    for line in open(path):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not d.get("ltp") or d["ltp"] <= 0:
            continue
        per_min[d["ts"]][(round(float(d["strike"])), d["type"])] = float(d["ltp"])
    rows = []
    for ts in sorted(per_min):
        legs = per_min[ts]
        strikes = sorted({k[0] for k in legs})
        atm = None
        for s in strikes:
            ce, pe = legs.get((s, "CE")), legs.get((s, "PE"))
            if ce and pe and (atm is None or abs(ce - pe) < atm[0]):
                atm = (abs(ce - pe), s)
        if atm:
            spot = None
            rows.append((ts, atm[1], legs, strikes))
    return und, rows


def regime_at(spots, i):
    if i < 20:
        return "UNKNOWN"
    moves = [abs(spots[j] - spots[j - 1]) for j in range(i - 19, i + 1)]
    avg = statistics.mean(moves)
    if avg <= 0:
        return "UNKNOWN"
    recent = statistics.mean(moves[-3:])
    return "COMPRESSION" if recent < 0.7 * avg else ("EXPANSION" if recent > 1.5 * avg else "BALANCED")


def fly_legs(legs, atm, strikes, W):
    """The four leg premiums (atm_ce, atm_pe, wing_ce, wing_pe) or None."""
    ce_a, pe_a = legs.get((atm, 'CE')), legs.get((atm, 'PE'))
    if ce_a is None or pe_a is None:
        return None
    above = [s for s in strikes if s > atm]; below = [s for s in strikes if s < atm]
    if len(above) < W or len(below) < W:
        return None
    wu, wd = sorted(above)[W - 1], sorted(below)[-W]
    ce_u, pe_d = legs.get((wu, 'CE')), legs.get((wd, 'PE'))
    if ce_u is None or pe_d is None:
        return None
    return (ce_a, pe_a, ce_u, pe_d)


def fly_value(legs, atm, strikes, W):
    """V = short ATM straddle - long wings at ATM +/- W strikes. None if any leg missing."""
    ce_a, pe_a = legs.get((atm, "CE")), legs.get((atm, "PE"))
    if ce_a is None or pe_a is None:
        return None, None
    above = [s for s in strikes if s > atm]
    below = [s for s in strikes if s < atm]
    if len(above) < W or len(below) < W:
        return None, None
    wu, wd = sorted(above)[W - 1], sorted(below)[-W]
    ce_u, pe_d = legs.get((wu, "CE")), legs.get((wd, "PE"))
    if ce_u is None or pe_d is None:
        return None, None
    return (ce_a + pe_a) - (ce_u + pe_d), (wu - wd)


def bt(files, W, target, cost_leg, max_bars=375):
    per_day = collections.defaultdict(float); n = wins = 0; net = 0.0; worst = 0.0
    for path in files:
        und, rows = load_day(path)
        if len(rows) < 40:
            continue
        day = rows[0][0][:10]; lot = LOT.get(und, 25)
        spots = [r[1] for r in rows]                 # ATM strike as spot proxy for regime
        i = 0
        while i < len(rows) - 1:
            if regime_at(spots, i) in ("UNKNOWN", "EXPANSION"):
                i += 1; continue
            atm = rows[i][1]
            v0, width = fly_value(rows[i][2], atm, rows[i][3], W)
            if v0 is None or v0 <= 0:
                i += 1; continue
            entry_i = i; vex = None
            for j in range(i + 1, len(rows)):
                vj, _ = fly_value(rows[j][2], atm, rows[j][3], W)
                if vj is None:
                    continue
                if vj <= v0 * (1 - target):
                    vex = vj; exit_i = j; break
                if (j - entry_i) >= max_bars:
                    vex = vj; exit_i = j; break
            if vex is None:
                vex, _ = fly_value(rows[-1][2], atm, rows[-1][3], W)
                if vex is None:
                    i += 1; continue
                exit_i = len(rows) - 1
            gross = (v0 - vex) * lot                  # profit as the fly decays
            # 2026-08-07 COST CORRECTION. Was `cost_leg * 4 * mean(v0,vex) * lot`, i.e.
            # 15bps of the FLY VALUE — which ignored the flat Rs20-per-order brokerage
            # (8 orders = Rs160) and undercharged by ~5.9x. That single line is why this
            # study reported a short-vol "edge" that the live book could never realise.
            _le = fly_legs(rows[entry_i][2], atm, rows[entry_i][3], W)
            _lx = fly_legs(rows[exit_i][2], atm, rows[exit_i][3], W)
            if _le is None or _lx is None:
                i += 1; continue
            cost = structure_round_trip_costs(
                [(_le[0], _lx[0], "SELL"), (_le[1], _lx[1], "SELL"),
                 (_le[2], _lx[2], "BUY"),  (_le[3], _lx[3], "BUY")],
                quantity=lot, exchange=exchange_for_underlying(und)).total
            pnl = gross - cost
            per_day[day] += pnl; net += pnl; n += 1; wins += pnl > 0; worst = min(worst, pnl)
            i = exit_i + 1
    best = max(per_day, key=per_day.get) if per_day else None
    ex_best = net - (per_day.get(best, 0.0) if best else 0.0)
    return {"n": n, "net": round(net), "ex_best": round(ex_best),
            "wr": wins / n if n else 0, "worst": round(worst)}


def main():
    files = [p for p in glob.glob("data/option_history/*.jsonl") if not os.path.basename(p).startswith("_")]
    print(f"files: {len(files)}")
    print("=" * 84)
    print("  IRON BUTTERFLY (short ATM straddle + wings) — tail HARD-CAPPED. full-session hold")
    print("=" * 84)
    print(f"  {'wingW':>5} {'target':>6} {'cost/leg':>8}  {'n':>4} {'NET':>9} {'ex-best':>9} {'win%':>6} {'WORST':>9}")
    for W in (2, 3, 5):
        for target in (0.50, 0.70):
            for cl in (0.0015, 0.003):
                r = bt(files, W, target, cl)
                flag = "  <= CLEARS" if (r["net"] > 0 and r["ex_best"] > 0) else ""
                print(f"  {W:>5} {target:>6.0%} {cl:>8.2%}  {r['n']:>4} {r['net']:>9,} {r['ex_best']:>9,} "
                      f"{r['wr']:>6.1%} {r['worst']:>9,}{flag}")


if __name__ == "__main__":
    main()
