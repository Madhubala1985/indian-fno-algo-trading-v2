import MetaTrader5 as mt5, pandas as pd, datetime as dt
mt5.initialize(timeout=30000); mt5.symbol_select("XAUUSDm",True)
r=mt5.copy_rates_from_pos("XAUUSDm", mt5.TIMEFRAME_M5, 0, 150)
df=pd.DataFrame(r); df["utc"]=pd.to_datetime(df["time"],unit="s")
df=df[df["utc"]>=df["utc"].iloc[-1]-pd.Timedelta(hours=8)]
print("XAUUSD M5 tape (UTC) — ADP released 12:15, Durable Goods 12:30\n")
print("%-6s %9s %9s %9s %9s %7s" % ("time","open","high","low","close","ticks"))
for _,x in df.iterrows():
    t=x["utc"].strftime("%H:%M")
    mark=""
    if t=="12:15": mark="  <<< ADP"
    elif t=="12:30": mark="  <<< DURABLE GOODS"
    elif t in ("13:00",): mark="  <<< NY equity pre-open"
    if x["utc"].minute%15==0 or mark:
        print("%-6s %9.2f %9.2f %9.2f %9.2f %7d%s" % (t,x["open"],x["high"],x["low"],x["close"],x["tick_volume"],mark))
d=df.set_index("utc")
pre=d.loc[:"2026-09-02 12:14"]["close"]
post=d.loc["2026-09-02 12:15":]["close"]
if len(pre) and len(post):
    print("\n  price just before 12:15 : %.2f" % pre.iloc[-1])
    print("  30 min after release    : %.2f  (%+.2f)" % (post.iloc[min(5,len(post)-1)], post.iloc[min(5,len(post)-1)]-pre.iloc[-1]))
    print("  now                     : %.2f  (%+.2f from pre-release)" % (post.iloc[-1], post.iloc[-1]-pre.iloc[-1]))
    print("  session low / high      : %.2f / %.2f" % (d["low"].min(), d["high"].max()))
mt5.shutdown()
