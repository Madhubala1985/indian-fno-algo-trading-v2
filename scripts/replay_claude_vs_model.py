"""Replay: how would an intelligent read (Claude) have traded the last 3 days vs the model's
instinct, priced on the REAL recorded option chains (data/option_history/).

Two expressions, identical entry (~09:30 IST) and exit (EOD), net of costs:
  MODEL  = buy the ATM CALL  (its documented behaviour: LONG bias, conf 1.00 -> long premium)
  CLAUDE = sell the ATM STRADDLE on a PIN day (short premium) + STAND ASIDE on direction

Honest caveat: these 3 days are already observed, so this illustrates the buy-vs-sell-premium
thesis on a known low-vol week — it is NOT out-of-sample proof. The forward mark (today,
data/claude_trades) is the real test. Prints per-instance P&L + win rate for each approach.
"""
import glob, os, sys, statistics, collections

HARNESS = os.path.join(os.path.dirname(__file__), "study_iron_condor.py")
ns = {}
exec(open(HARNESS).read().split("def bt(")[0], ns)
load_day, LOT, fly_value = ns["load_day"], ns["LOT"], ns["fly_value"]
WING = 2  # iron butterfly wing width in strikes (defined risk)

COST_LEG = 0.0015           # fraction of premium per leg (matches iron_butterfly cost model)
ENTRY_SKIP = 20             # skip ~first 20 snapshots (opening range) -> enter ~09:30 IST
DAYS = ["2026-08-03", "2026-08-04", "2026-08-05"]


def nearest(legs, strikes, atm, typ):
    if (atm, typ) in legs:
        return legs[(atm, typ)]
    for d in range(1, 6):
        for s in (atm + d * (strikes[1] - strikes[0] if len(strikes) > 1 else 50),
                  atm - d * (strikes[1] - strikes[0] if len(strikes) > 1 else 50)):
            if (s, typ) in legs:
                return legs[(s, typ)]
    return None


def replay_file(path):
    und, rows = load_day(path)
    if len(rows) < ENTRY_SKIP + 5:
        return None
    lot = LOT.get(und, 25)
    e = rows[ENTRY_SKIP]                 # entry snapshot (~09:30)
    x = rows[-1]                         # exit snapshot (EOD)
    atm = e[1]
    legs_e, legs_x = e[2], x[2]
    strikes_e = e[3]

    ce_e = legs_e.get((atm, "CE")); pe_e = legs_e.get((atm, "PE"))
    ce_x = nearest(legs_x, x[3], atm, "CE"); pe_x = nearest(legs_x, x[3], atm, "PE")
    if not all([ce_e, pe_e, ce_x, pe_x]):
        return None

    # underlying drift over the session (ATM strike proxy): the "move" the day delivered
    move = x[1] - e[1]

    # MODEL: long ATM call
    m_gross = (ce_x - ce_e) * lot
    m_cost = COST_LEG * (ce_e + ce_x) * lot
    m_pnl = m_gross - m_cost

    # CLAUDE: short ATM straddle (sell CE+PE at entry, buy back EOD)
    strad_e = ce_e + pe_e
    strad_x = ce_x + pe_x
    c_gross = (strad_e - strad_x) * lot
    c_cost = COST_LEG * 2 * (strad_e + strad_x) * lot
    c_pnl = c_gross - c_cost

    # IRON BUTTERFLY: short ATM straddle + long wings at +/-WING strikes (defined risk) —
    # the actual deployed vehicle. Same entry strikes held to EOD.
    v0, _ = fly_value(legs_e, atm, strikes_e, WING)
    vx, _ = fly_value(legs_x, atm, x[3], WING)
    ib_pnl = None
    if v0 is not None and vx is not None:
        ib_pnl = (v0 - vx) * lot - COST_LEG * 4 * ((v0 + vx) / 2) * lot

    return {"und": und, "lot": lot, "atm": atm, "move": move,
            "model_pnl": m_pnl, "claude_pnl": c_pnl, "ib_pnl": ib_pnl,
            "call_e": ce_e, "call_x": ce_x, "strad_e": strad_e, "strad_x": strad_x}


def main():
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "option_history")
    rows = []
    for day in DAYS:
        for path in sorted(glob.glob(os.path.join(base, f"*_{day}_*.jsonl"))):
            if os.path.basename(path).startswith("_"):
                continue
            r = replay_file(path)
            if r:
                r["day"] = day
                rows.append(r)

    if not rows:
        print("no replayable chains found for", DAYS); return 1

    print(f"{'day':<12}{'idx':<10}{'atm':>9}{'move':>7}{'  call e->x':>16}"
          f"{'MODEL buy-call':>16}{'naked straddle':>16}{'IRON BUTTERFLY':>16}")
    print("-" * 98)
    mtot = ctot = itot = 0.0
    mwin = cwin = iwin = 0
    ins = 0
    for r in rows:
        mtot += r["model_pnl"]; ctot += r["claude_pnl"]
        mwin += r["model_pnl"] > 0; cwin += r["claude_pnl"] > 0
        ibs = r["ib_pnl"]
        ibcol = f"{ibs:>+16,.0f}" if ibs is not None else f"{'n/a':>16}"
        if ibs is not None:
            itot += ibs; iwin += ibs > 0; ins += 1
        print(f"{r['day']:<12}{r['und']:<10}{r['atm']:>9.0f}{r['move']:>+7.0f}"
              f"{r['call_e']:>7.1f}->{r['call_x']:<6.1f}"
              f"{r['model_pnl']:>+16,.0f}{r['claude_pnl']:>+16,.0f}{ibcol}")
    n = len(rows)
    print("-" * 98)
    print(f"{'TOTAL':<12}{'n='+str(n):<10}{'':>22}"
          f"{mtot:>+16,.0f}{ctot:>+16,.0f}{itot:>+16,.0f}")
    print(f"\n  MODEL  buy ATM call:      net Rs {mtot:>+10,.0f}   win {mwin}/{n} = {100*mwin/n:.0f}%   avg {mtot/n:>+,.0f}/trade")
    print(f"  naive  sell straddle:     net Rs {ctot:>+10,.0f}   win {cwin}/{n} = {100*cwin/n:.0f}%   avg {ctot/n:>+,.0f}/trade")
    if ins:
        print(f"  IRON BUTTERFLY (armed):   net Rs {itot:>+10,.0f}   win {iwin}/{ins} = {100*iwin/ins:.0f}%   avg {itot/ins:>+,.0f}/trade   (defined risk, tail capped)")
    print(f"  CLAUDE also STANDS ASIDE directionally on these low-vol days -> avoids the buy-call bleed entirely.")
    print(f"\n  NOTE: 3 observed days, low-vol week (VIX ~12). Illustrative of buy-vs-sell premium,")
    print(f"        not OOS proof. Forward mark = data/claude_trades/claude_trades.jsonl.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
