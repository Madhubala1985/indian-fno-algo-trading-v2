"""
EDGE LEDGER — measures the edge that actually happened, from broker truth.

This is the ONLY thing permitted to authorise a size increase. It reads closed round trips
out of MT5's own deal history (not a self-reported log, which can drift from reality) and
reports the six graduation gates from forex/CONSTRAINTS.md Section E.

Why it exists: the Indian programme lost Rs 31,948 with no artefact that could say
"this is not working". This file is that artefact. It is allowed to say NO.

Usage:
    python edge_ledger.py            # all magics (whole account)
    python edge_ledger.py 990902     # just the momentum bot
"""

import MetaTrader5 as mt5
import datetime as dt
import json
import math
import os
import sys

SYM = "XAUUSDm"
COST_PER_001 = 0.39          # measured on a real round trip, ticket 3751266690
START = dt.datetime(2026, 9, 1)
OUT = r"C:\Users\madhu\gold_shadow\edge.json"

MIN_N_TO_SIZE = 30           # CONSTRAINTS F: 0.01 fixed for the first 30 live trades
GRADUATION_N = 60            # CONSTRAINTS E1


def wilson_lower(wins, n, z=1.96):
    """Lower bound of the win-rate CI. We size off THIS, never the point estimate:
    with n=10 a 70% observed win rate is compatible with a losing strategy."""
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d)


def round_trips(magic=None):
    """Pair MT5 deals into closed round trips keyed by position_id."""
    deals = mt5.history_deals_get(START, dt.datetime.now() + dt.timedelta(days=1)) or []
    book = {}
    for d in deals:
        if d.symbol != SYM:
            continue
        if magic is not None and d.magic != magic:
            continue
        b = book.setdefault(d.position_id, {"net": 0.0, "vol": 0.0, "open": None, "close": None})
        b["net"] += d.profit + d.swap + d.commission + d.fee
        if d.entry == mt5.DEAL_ENTRY_IN:
            b["open"] = d.time
            b["vol"] += d.volume
            b["dir"] = "LONG" if d.type == mt5.DEAL_TYPE_BUY else "SHORT"
        else:
            b["close"] = d.time
    trips = [dict(pid=k, **v) for k, v in book.items() if v["open"] and v["close"]]
    trips.sort(key=lambda t: t["close"])
    return trips


def measure(trips):
    """Everything downstream keys off this dict. Returns per-0.01-lot normalised figures
    so the edge estimate is not contaminated by past sizing decisions."""
    if not trips:
        return dict(n=0, note="no closed round trips")

    # normalise to 0.01 lot so a size change does not distort the measured edge
    r = [t["net"] / max(t["vol"] / 0.01, 1.0) for t in trips]
    wins = [x for x in r if x > 0]
    losses = [x for x in r if x <= 0]
    n = len(r)
    net = sum(r)
    wr = len(wins) / n
    avg_w = (sum(wins) / len(wins)) if wins else 0.0
    avg_l = (abs(sum(losses)) / len(losses)) if losses else 0.0
    payoff = (avg_w / avg_l) if avg_l > 0 else float("inf") if wins else 0.0

    srt = sorted(r)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2

    # --- gates
    by_day = {}
    for t, x in zip(trips, r):
        by_day.setdefault(dt.datetime.utcfromtimestamp(t["close"]).date().isoformat(), 0.0)
        by_day[dt.datetime.utcfromtimestamp(t["close"]).date().isoformat()] += x
    best_day = max(by_day.values()) if by_day else 0.0
    h = n // 2
    first_half, second_half = sum(r[:h]), sum(r[h:])

    by_month = {}
    for t, x in zip(trips, r):
        k = dt.datetime.utcfromtimestamp(t["close"]).strftime("%Y-%m")
        by_month[k] = by_month.get(k, 0.0) + x
    months_pos = sum(1 for v in by_month.values() if v > 0)

    gates = {
        "1_n_ge_60":        n >= GRADUATION_N,
        "2_net_positive":   net > 0,
        "3_ex_best_day":    (net - best_day) > 0,
        "4_both_halves":    first_half > 0 and second_half > 0,
        "5_two_of_three_m": months_pos >= 2 and len(by_month) >= 3,
        "6_median_gt_0":    median > 0,
    }

    # --- edge, estimated CONSERVATIVELY (Wilson lower bound on win rate)
    wr_lb = wilson_lower(len(wins), n)
    payoff_used = min(payoff, 5.0) if payoff != float("inf") else 5.0
    kelly = wr_lb - (1 - wr_lb) / payoff_used if payoff_used > 0 else 0.0

    return dict(
        n=n, net=round(net, 2), net_per_trade=round(net / n, 2),
        win_rate=round(wr, 4), win_rate_lb95=round(wr_lb, 4),
        avg_win=round(avg_w, 2), avg_loss=round(avg_l, 2), payoff=round(payoff_used, 2),
        median_trade=round(median, 2), best_day=round(best_day, 2),
        first_half=round(first_half, 2), second_half=round(second_half, 2),
        months=by_month, gates=gates, graduated=all(gates.values()),
        kelly_full=round(max(0.0, kelly), 4),
        may_size_up=(n >= MIN_N_TO_SIZE and net > 0 and kelly > 0),
        note="net figures are per 0.01 lot, broker-truth, cost already inside profit",
    )


def main():
    magic = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if not mt5.initialize(timeout=60000):
        print("MT5 init failed", mt5.last_error())
        return
    m = measure(round_trips(magic))
    m["magic"] = magic
    m["measured_at"] = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat()
    mt5.shutdown()

    print(json.dumps(m, indent=1, default=str))
    if m.get("n"):
        print("\n--- GATES (CONSTRAINTS.md Section E) ---")
        for k, v in m["gates"].items():
            print("  %-18s %s" % (k, "PASS" if v else "fail"))
        print("\nGRADUATED: %s   |   MAY SIZE ABOVE 0.01: %s" % (m["graduated"], m["may_size_up"]))
        print("full Kelly on the CONSERVATIVE edge: %.2f%%  ->  quarter-Kelly %.2f%%"
              % (100 * m["kelly_full"], 100 * m["kelly_full"] / 4))
    try:
        open(OUT, "w").write(json.dumps(m, default=str))
    except Exception:
        pass


if __name__ == "__main__":
    main()
