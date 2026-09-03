"""Exercise the compounding law against scenarios, including the live account.
Run: python test_sizer.py   (no orders, no MT5 required for the synthetic cases)"""
import sizer

STOP = 50.0   # trader.py disaster stop


def show(title, eq, stop, edge):
    lot, risk, why = sizer.size(eq, stop, edge)
    print("\n%s\n  equity $%-8.0f stop $%-5.0f ->  LOT %.2f   risk $%.2f (%.2f%%)"
          % (title, eq, stop, lot, risk, 100 * risk / eq if eq else 0))
    print("  %s" % why)


print("=" * 78)
print("PHASE 1 — not enough evidence: size is pinned at the minimum, whatever the equity")
print("=" * 78)
for eq in (1000, 3000, 10000):
    show("n=3 (where we are today)", eq, STOP, dict(n=3, win_rate_lb95=0.21, payoff=5.0))

print("\n" + "=" * 78)
print("PHASE 2 — 60 trades in. Size now derives from the edge and compounds with equity.")
print("=" * 78)
# a genuinely good edge: 45% win rate at 2:1 payoff (the profile Part 3 says to want)
good = dict(n=60, win_rate_lb95=0.45, payoff=2.0)
for eq in (1000, 3000, 10000, 30000, 100000):
    show("60 trades, 45%% win / 2:1 payoff", eq, STOP, good)

print("\n" + "=" * 78)
print("PHASE 3 — the machinery refusing to trade (this is it WORKING)")
print("=" * 78)
show("coin flip at 1:1 - no edge after cost", 3000, STOP, dict(n=60, win_rate_lb95=0.50, payoff=1.0))
show("52%% at 1:1 - the 'almost an edge' case", 3000, STOP, dict(n=60, win_rate_lb95=0.52, payoff=1.0))
show("good edge but the account is too small for the stop", 400, STOP,
     dict(n=60, win_rate_lb95=0.45, payoff=2.0))

print("\n" + "=" * 78)
print("PHASE 4 — LIVE ACCOUNT, real equity, real measured edge")
print("=" * 78)
try:
    import MetaTrader5 as mt5, edge_ledger
    if mt5.initialize(timeout=60000):
        eq = mt5.account_info().equity
        edge = edge_ledger.measure(edge_ledger.round_trips(990902))
        show("bot only (magic 990902)", eq, STOP, edge)
        edge_all = edge_ledger.measure(edge_ledger.round_trips(None))
        show("whole account", eq, STOP, edge_all)
        mt5.shutdown()
    else:
        print("  MT5 not available:", mt5.last_error())
except Exception as e:
    print("  skipped (%s)" % e)
