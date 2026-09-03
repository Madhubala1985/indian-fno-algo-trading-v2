import MetaTrader5 as mt5, pandas as pd, datetime as dt
mt5.initialize(timeout=30000)
def snap(s,label):
    if not mt5.symbol_select(s,True): print("%-8s unavailable"%label); return
    t=mt5.symbol_info_tick(s)
    d=pd.DataFrame(mt5.copy_rates_from_pos(s,mt5.TIMEFRAME_D1,0,8))
    H=pd.DataFrame(mt5.copy_rates_from_pos(s,mt5.TIMEFRAME_H1,0,30))
    if d.empty: print("%-8s no data"%label); return
    prev=d["close"].iloc[-2]
    print("%-8s %-11s %10.3f  vs prev close %+.2f%%   24h %+.2f%%   5d %+.2f%%" %
          (label,s,t.bid,100*(t.bid-prev)/prev,
           100*(t.bid-H["close"].iloc[-24])/H["close"].iloc[-24] if len(H)>=24 else float("nan"),
           100*(t.bid-d["close"].iloc[-6])/d["close"].iloc[-6] if len(d)>=6 else float("nan")))
for s,l in (("DXYm","DXY"),("US500m","S&P500"),("USTECm","NASDAQ"),("US30m","DOW"),("USOILm","WTI"),("XAUUSDm","GOLD")):
    snap(s,l)
mt5.shutdown()
