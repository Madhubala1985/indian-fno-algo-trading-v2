import MetaTrader5 as mt5, json, datetime as dt, os
SYM="XAUUSDm"; LOT=0.01; MAGIC=990903
DIR=r"C:\Users\madhu\gold_shadow"; JL=os.path.join(DIR,"discretionary.jsonl")
mt5.initialize(timeout=60000)
ai=mt5.account_info()
assert ai.trade_mode==0, "not a demo account"
assert mt5.terminal_info().trade_allowed, "algo trading off"
mt5.symbol_select(SYM,True)
open_pos=[p for p in (mt5.positions_get(symbol=SYM) or [])]
if open_pos:
    print("position already open, not adding:", [(p.ticket,p.type,p.volume) for p in open_pos]); mt5.shutdown(); raise SystemExit
t=mt5.symbol_info_tick(SYM)
SL=4299.0; TP=4400.0
req=dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYM, volume=LOT, type=mt5.ORDER_TYPE_BUY,
         price=t.ask, sl=SL, tp=TP, magic=MAGIC, deviation=20,
         comment="disc long oversold bounce", type_filling=mt5.ORDER_FILLING_IOC)
r=mt5.order_send(req)
ok = r is not None and r.retcode==mt5.TRADE_RETCODE_DONE
print("ENTRY: %s  ticket=%s  fill=%.3f  sl=%.1f  tp=%.1f" % ("OK" if ok else "FAIL %s"%(r.comment if r else mt5.last_error()),
      getattr(r,"order",None), getattr(r,"price",0), SL, TP))
if ok:
    risk=r.price-SL; rew=TP-r.price
    print("  risk $%.2f/oz  reward $%.2f/oz  R:R = 1:%.2f  (%.1f%% of equity at risk)" % (risk,rew,rew/risk,100*risk/ai.equity))
    thesis=dict(ts=dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(), ticket=r.order,
      dir="LONG", entry=r.price, sl=SL, tp=TP, lot=LOT, risk_usd=round(risk,2), reward_usd=round(rew,2),
      conviction="MODERATE",
      thesis=[
        "MACRO (against me): Fed hike odds 64-70% into 16-Sep FOMC; Warsh says 'work to do' on inflation. Gold -21.8% from Jan peak, -5.5% in 5 sessions. Structural trend is DOWN. This is a counter-trend trade and I am saying so up front.",
        "SETUP (for me): the move is stretched. -5.5% in 5 sessions into a well-telegraphed event. Today reversed from 4282 and is +$57, making session highs.",
        "TAPE: steady grind up from 4304 (10:00 UTC) to 4340 now. Volume expanding into NY - 13:00 bar was the day's largest (2832 ticks) and price went UP on it. Buyers, not short-covering exhaustion.",
        "CATALYST CLEARED: ADP at 12:15 was a non-event (+$1.20 in 30 min, tick volume BELOW the 12:00 bar). Immediate binary risk has passed; next is NFP Friday 12:30 UTC.",
        "MY OWN MEASUREMENT: gold is mildly MEAN-REVERTING at intraday horizons (payoff test: demanding bigger targets gets worse-than-fair odds, k=5 -9.8pp). That property argues for fading a stretched move, not chasing the trend.",
        "RETRACEMENT MATH: decline 4594->4282 = $312. 38% retrace = 4401 (the target). Currently only 18% retraced, so there is room if this is a normal oversold bounce."],
      invalidation="Below 4299 = today's NY-session low broken = the bounce failed and the downtrend has resumed. No re-entry on the same idea.",
      exit_plan="Target 4400 (38% retrace). FLAT BEFORE NFP Friday 12:30 UTC regardless of P&L - I will not hold a counter-trend position through a binary jobs print.",
      how_to_grade="Was the DIRECTION right, and was the INVALIDATION level well chosen? Judge the reasoning, not the outcome of one trade.")
    with open(JL,"a") as f: f.write(json.dumps(thesis)+"\n")
    print("  thesis logged for grading")
mt5.shutdown()
