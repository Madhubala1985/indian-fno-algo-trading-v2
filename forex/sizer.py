"""
THE COMPOUNDING LAW — one function that turns equity + measured edge into a lot size.

There is exactly one sizing rule in this programme and it lives here. The Indian system had
five conflicting caps and none of them was derived from an edge; this one is derived from
nothing else.

    lot = quarter_Kelly(measured edge) * equity / (stop_distance * contract_size)

Three properties, all deliberate:

  1. IT COMPOUNDS.      Risk is a FRACTION of equity, so size grows as the account grows and
                        shrinks after a drawdown without anyone deciding anything.
  2. IT COMPOUNDS ON EVIDENCE TOO. The edge feeding Kelly is the WILSON LOWER BOUND of the
                        win rate, not the observed one. A 70% win rate over 10 trades has a
                        lower bound of 39% and sizes to zero; the same 70% over 100 trades
                        sizes properly. Size therefore grows with sample size as well as
                        equity, which is the only honest way to scale into an unproven edge.
  3. IT CAN RETURN ZERO. A 50-52% win rate at 1:1 has no positive expectancy after the $0.39
                        toll, and this returns 0.0 lots for it. Refusing to trade is the
                        machinery working, not failing.

Quarter-Kelly, not full: at full Kelly a genuine 55% edge still carries a 10% chance of
halving the account. Quarter-Kelly on the same edge has p10 = $2,724 on a $3,000 start.
"""

CONTRACT = 100.0        # XAUUSDm: 1.00 lot = 100 oz, so $1 move = $100
VOL_MIN = 0.01
VOL_STEP = 0.01
KELLY_FRACTION = 0.25   # quarter-Kelly. Not negotiable without new evidence.
MAX_RISK_PCT = 0.02     # hard ceiling regardless of what Kelly says
MIN_N_TO_SIZE = 30      # CONSTRAINTS F: 0.01 fixed for the first 30 live trades


def kelly_fraction(win_rate_lb, payoff):
    """Kelly on the CONSERVATIVE win rate. Returns 0.0 when the edge does not exist."""
    if payoff <= 0:
        return 0.0
    f = win_rate_lb - (1.0 - win_rate_lb) / payoff
    return max(0.0, f)


def size(equity, stop_distance, edge, verbose=False):
    """
    equity        : account equity in USD
    stop_distance : distance from entry to stop, in DOLLARS OF GOLD (e.g. 20.0)
    edge          : the dict from edge_ledger.measure()

    Returns (lot, risk_usd, reason).
    """
    if stop_distance <= 0:
        return 0.0, 0.0, "no stop distance - refusing to size blind"

    n = edge.get("n", 0)

    # --- Phase 1: not enough evidence to size at all. Fixed minimum, per constraint.
    # The fixed-lot rule caps size; it does NOT license exceeding the risk ceiling. An
    # account small enough that the broker minimum breaches MAX_RISK_PCT cannot trade at
    # this stop distance at all, and saying so is the whole point of this function.
    if n < MIN_N_TO_SIZE:
        risk = stop_distance * CONTRACT * VOL_MIN
        if risk > MAX_RISK_PCT * equity:
            return 0.0, 0.0, (
                "n=%d: size would be the %.2f lot minimum, but that risks $%.2f = %.2f%% of "
                "$%.0f equity, above the %.0f%% ceiling. The BROKER MINIMUM is larger than "
                "your risk budget. Not trading - this account is too small for a $%.0f stop."
                % (n, VOL_MIN, risk, 100 * risk / equity, equity, 100 * MAX_RISK_PCT, stop_distance))
        return VOL_MIN, risk, (
            "n=%d < %d: size FIXED at %.2f lot (risk $%.2f = %.2f%% of equity). "
            "The edge estimate is not yet trustworthy enough to size off."
            % (n, MIN_N_TO_SIZE, VOL_MIN, risk, 100 * risk / equity))

    # --- Phase 2: evidence exists. Size off the conservative edge.
    f_full = kelly_fraction(edge.get("win_rate_lb95", 0.0), edge.get("payoff", 0.0))
    f = f_full * KELLY_FRACTION

    if f <= 0:
        return 0.0, 0.0, (
            "conservative edge is non-positive (win_rate_lb95=%.3f, payoff=%.2f) -> "
            "Kelly says risk NOTHING. Not trading." % (edge.get("win_rate_lb95", 0), edge.get("payoff", 0)))

    capped = min(f, MAX_RISK_PCT)
    risk = capped * equity
    raw = risk / (stop_distance * CONTRACT)
    lot = round(int(raw / VOL_STEP) * VOL_STEP, 2)   # always round DOWN

    if lot < VOL_MIN:
        return 0.0, 0.0, (
            "correct risk is $%.2f (%.2f%%) but the minimum %.2f lot with a $%.2f stop "
            "risks $%.2f (%.2f%%) - the broker minimum EXCEEDS the correct size. "
            "Not trading: this account is too small for this stop."
            % (risk, 100 * capped, VOL_MIN, stop_distance,
               stop_distance * CONTRACT * VOL_MIN,
               100 * stop_distance * CONTRACT * VOL_MIN / equity))

    actual = lot * stop_distance * CONTRACT
    return lot, actual, (
        "n=%d  win_lb %.3f  payoff %.2f  ->  Kelly %.2f%%, quarter %.2f%%%s  ->  "
        "risk $%.2f on $%.0f equity  ->  %.2f lot (stop $%.2f)"
        % (n, edge.get("win_rate_lb95", 0), edge.get("payoff", 0), 100 * f_full, 100 * f,
           " (CAPPED at %.1f%%)" % (100 * MAX_RISK_PCT) if f > MAX_RISK_PCT else "",
           actual, equity, lot, stop_distance))


def min_viable_equity(stop_distance, risk_pct=0.01):
    """The question 'is $X enough?' has an arithmetic answer: the broker minimum lot must
    fit inside the risk budget. Below this equity, trading means knowingly oversizing."""
    return (VOL_MIN * stop_distance * CONTRACT) / risk_pct


if __name__ == "__main__":
    print("MINIMUM VIABLE EQUITY (0.01 lot must fit the risk budget)\n")
    print("  %-12s %-12s %-12s %-12s" % ("stop", "@1% risk", "@1.5% risk", "@2% risk"))
    for sd in (10, 15, 20, 30, 50):
        print("  $%-11d $%-11.0f $%-11.0f $%-11.0f" % (
            sd, min_viable_equity(sd, .01), min_viable_equity(sd, .015), min_viable_equity(sd, .02)))

    print("\nWHAT $1,000 AND $3,000 CAN ACTUALLY DO (0.01 lot = $1 risk per $1 of stop)\n")
    print("  %-8s %-10s %-14s %-10s" % ("equity", "stop", "risk at 0.01", "% of equity"))
    for eq in (1000, 3000, 10000):
        for sd in (10, 20, 30):
            r = sd * CONTRACT * VOL_MIN
            flag = "  <-- oversized" if r / eq > 0.015 else ""
            print("  $%-7d $%-9d $%-13.2f %.2f%%%s" % (eq, sd, r, 100 * r / eq, flag))

    print("\nSIZE LADDER GRANULARITY (why small accounts cannot compound smoothly)\n")
    for eq in (1000, 3000, 10000, 30000):
        steps = []
        for lot in (0.01, 0.02, 0.03, 0.05):
            steps.append("%.2f=%.1f%%" % (lot, 100 * lot * 20 * CONTRACT / eq))
        print("  $%-7d  %s" % (eq, "   ".join(steps)))
