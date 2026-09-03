import MetaTrader5 as mt5, datetime as dt
mt5.initialize(timeout=30000)
a=dt.datetime.now(dt.UTC).replace(tzinfo=None)-dt.timedelta(days=1)
d=mt5.history_deals_get(a, dt.datetime.now(dt.UTC).replace(tzinfo=None)+dt.timedelta(hours=2))
print("ALL DEALS ON THE ACCOUNT (last 24h):")
tot=0
for x in (d or []):
    print("  %s  %-8s %-9s vol=%.2f  price=%.3f  profit=%+.2f  comment='%s'" % (
        dt.datetime.utcfromtimestamp(x.time).strftime("%H:%M:%S"),
        {0:"BUY",1:"SELL"}.get(x.type,x.type),
        {0:"IN",1:"OUT"}.get(x.entry,x.entry),
        x.volume, x.price, x.profit, x.comment))
    tot+=x.profit+x.swap+x.commission
print("\n  net across all deals: %+.2f" % tot)
ai=mt5.account_info(); print("  balance %.2f  equity %.2f  (started 3000.00)" % (ai.balance,ai.equity))
print("  open positions now:", mt5.positions_total())
mt5.shutdown()
