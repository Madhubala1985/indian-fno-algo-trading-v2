import MetaTrader5 as mt5, pandas as pd, datetime as dt
mt5.initialize(timeout=30000); mt5.symbol_select("XAUUSDm",True)
now_utc=dt.datetime.now(dt.UTC).replace(tzinfo=None)
t=mt5.symbol_info_tick("XAUUSDm")
r=mt5.copy_rates_from_pos("XAUUSDm", mt5.TIMEFRAME_M15, 0, 3)
df=pd.DataFrame(r)
raw_last=pd.to_datetime(df["time"],unit="s").iloc[-1]      # last (forming) bar, raw
raw_prev=pd.to_datetime(df["time"],unit="s").iloc[-2]      # last CLOSED bar, raw
tick_raw=pd.to_datetime(t.time,unit="s")
print("true UTC now          :", now_utc.strftime("%Y-%m-%d %H:%M:%S"))
print("tick time  (raw epoch):", tick_raw)
print("forming bar (raw)     :", raw_last)
print("last closed bar (raw) :", raw_prev)
off=(tick_raw-now_utc).total_seconds()/3600
print("\n=> SERVER OFFSET FROM UTC = %+.1f hours" % off)
print("   so: utc_of_bar = raw_bar - %.0fh" % off)
print("   last closed bar in TRUE UTC = %s" % (raw_prev - pd.Timedelta(hours=off)))
mt5.shutdown()
