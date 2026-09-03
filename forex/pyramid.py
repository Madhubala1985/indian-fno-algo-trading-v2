import MetaTrader5 as mt5, json, datetime as dt, os
SYM="XAUUSDm"; MAGIC=990903; DIR=r"C:\Users\madhu\gold_shadow"
ADD_LOT=0.02; NEW_SL=4345.0; TP=4400.0
mt5.initialize(timeout=60000); mt5.symbol_select(SYM,True)
ai=mt5.account_info(); t=mt5.symbol_info_tick(SYM)
pos=list(mt5.positions_get(symbol=SYM) or [])
print("before: equity %.2f | %d position(s) | bid %.3f" % (ai.equity,len(pos),t.bid))
if NEW_SL >= t.bid:
    print("ABORT: proposed stop %.1f is not below market %.3f" % (NEW_SL,t.bid)); mt5.shutdown(); raise SystemExit

# 1) ADD on strength
r=mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL,symbol=SYM,volume=ADD_LOT,type=mt5.ORDER_TYPE_BUY,
    price=t.ask,sl=NEW_SL,tp=TP,magic=MAGIC,deviation=20,comment="pyramid add 1",
    type_filling=mt5.ORDER_FILLING_IOC))
ok=r is not None and r.retcode==mt5.TRADE_RETCODE_DONE
print("ADD  %.2f lot @ %.3f -> %s" % (ADD_LOT,getattr(r,'price',0),"OK ticket %s"%r.order if ok else "FAIL %s"%(r.comment if r else mt5.last_error())))

# 2) RAISE the stop on the ORIGINAL so total book risk goes DOWN, not up
for p in pos:
    m=mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP,symbol=SYM,position=p.ticket,sl=NEW_SL,tp=TP))
    print("RAISE stop ticket %s: %.1f -> %.1f  %s" % (p.ticket,p.sl,NEW_SL,
          "OK" if m and m.retcode==mt5.TRADE_RETCODE_DONE else "FAIL %s"%(m.comment if m else mt5.last_error())))

# 3) show the resulting risk book
pos=list(mt5.positions_get(symbol=SYM) or []); t=mt5.symbol_info_tick(SYM)
tot_risk=0.0; tot_rew=0.0
print("\nBOOK NOW:")
for p in pos:
    risk=(p.price_open-p.sl)*p.volume*100      # $ per position (0.01 lot = $1/$1 move)
    rew =(p.tp-p.price_open)*p.volume*100
    tot_risk+=risk; tot_rew+=rew
    print("  %s  %.2f lot @ %.3f  sl %.1f tp %.1f  -> risk %+.2f  reward %+.2f" % (p.ticket,p.volume,p.price_open,p.sl,p.tp,-risk,rew))
print("\n  TOTAL worst case if stopped: $%+.2f  (%.1f%% of equity)" % (-tot_risk,100*tot_risk/ai.equity))
print("  TOTAL if target hit        : $%+.2f" % tot_rew)
print("  original single-trade risk was -$41.67 -> risk has %s" % ("DECREASED" if tot_risk<41.67 else "INCREASED"))
json.dump(dict(ts=dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(),action="pyramid_add",
    add_lot=ADD_LOT,new_sl=NEW_SL,total_risk=round(-tot_risk,2),total_reward=round(tot_rew,2)),
    open(os.path.join(DIR,"discretionary.jsonl"),"a")); open(os.path.join(DIR,"discretionary.jsonl"),"a").write("\n")
mt5.shutdown()
