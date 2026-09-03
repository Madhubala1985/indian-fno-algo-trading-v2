import MetaTrader5 as mt5, pandas as pd, numpy as np, datetime as dt
mt5.initialize(timeout=30000)
now=dt.datetime.now(dt.UTC).replace(tzinfo=None)
print("UTC now %s (%s)\n" % (now.strftime("%Y-%m-%d %H:%M"), now.strftime("%A")))
def snap(s,label):
    if not mt5.symbol_select(s,True): return None
    t=mt5.symbol_info_tick(s)
    d1=mt5.copy_rates_from_pos(s,mt5.TIMEFRAME_D1,0,12)
    m15=mt5.copy_rates_from_pos(s,mt5.TIMEFRAME_M15,0,96)
    h1=mt5.copy_rates_from_pos(s,mt5.TIMEFRAME_H1,0,48)
    if d1 is None or m15 is None: return None
    d=pd.DataFrame(d1); m=pd.DataFrame(m15); H=pd.DataFrame(h1)
    prev=d.iloc[-2]; today=d.iloc[-1]
    chg=(t.bid-prev["close"])/prev["close"]*100
    rng10=(d["high"].iloc[-11:-1]-d["low"].iloc[-11:-1]).mean()
    print("%-10s %-9s bid %.3f  chg vs prev close %+.2f%%" % (label,s,t.bid,chg))
    print("   today  O %.3f  H %.3f  L %.3f  (range %.3f vs 10d avg %.3f = %.0f%%)" %
          (today["open"],today["high"],today["low"],today["high"]-today["low"],rng10,100*(today["high"]-today["low"])/rng10))
    print("   last 24h (H1): hi %.3f  lo %.3f | 4h chg %+.2f%% | 24h chg %+.2f%%" %
          (H["high"].tail(24).max(),H["low"].tail(24).min(),
           100*(t.bid-H["close"].iloc[-5])/H["close"].iloc[-5],
           100*(t.bid-H["close"].iloc[-24])/H["close"].iloc[-24]))
    print("   5 recent daily closes:", " ".join("%.1f"%x for x in d["close"].tail(6).iloc[:-1]))
    return dict(px=t.bid,chg=chg)
g=snap("XAUUSDm","GOLD")
print()
for s,l in (("XAGUSDm","SILVER"),("EURUSDm","EURUSD"),("USDJPYm","USDJPY"),("BTCUSDm","BTC")):
    snap(s,l); print()
# what else is available that could be macro context
syms=[x.name for x in mt5.symbols_get()]
import re
cand=[x for x in syms if re.search(r"(USDX|DXY|US10|UST|SPX|US500|USTEC|US30|VIX|OIL|WTI|BRENT|DE40|UK100)",x,re.I)]
print("macro instruments available:", cand[:20] if cand else "NONE (no indices/DXY/yields on this account)")
mt5.shutdown()
