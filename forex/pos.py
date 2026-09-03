import MetaTrader5 as mt5, pandas as pd, datetime as dt
mt5.initialize(timeout=30000); mt5.symbol_select("XAUUSDm",True)
ai=mt5.account_info(); t=mt5.symbol_info_tick("XAUUSDm")
print("UTC %s | bid %.3f ask %.3f" % (dt.datetime.now(dt.UTC).replace(tzinfo=None).strftime("%H:%M"),t.bid,t.ask))
print("equity %.2f  balance %.2f  free margin %.2f" % (ai.equity,ai.balance,ai.margin_free))
for p in (mt5.positions_get(symbol="XAUUSDm") or []):
    mv=t.bid-p.price_open
    print("\nticket %s  %s %.2f lot @ %.3f" % (p.ticket,"BUY" if p.type==0 else "SELL",p.volume,p.price_open))
    print("  sl %.1f  tp %.1f  |  now %.3f  moved %+.2f  |  P&L $%+.2f  (swap %+.2f)" % (p.sl,p.tp,t.bid,mv,p.profit,p.swap))
    print("  progress to target: %.0f%%   distance to stop: $%.2f" % (100*mv/(p.tp-p.price_open), t.bid-p.sl))
r=pd.DataFrame(mt5.copy_rates_from_pos("XAUUSDm",mt5.TIMEFRAME_M5,0,24))
r["utc"]=pd.to_datetime(r["time"],unit="s")
print("\nlast 2h M5:")
for _,x in r.tail(10).iterrows():
    print("  %s  O %.2f H %.2f L %.2f C %.2f  ticks %d" % (x["utc"].strftime("%H:%M"),x["open"],x["high"],x["low"],x["close"],x["tick_volume"]))
print("\n2h high %.2f  2h low %.2f" % (r["high"].max(),r["low"].min()))
mt5.shutdown()
