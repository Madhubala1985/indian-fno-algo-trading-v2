import MetaTrader5 as mt5
SYM="XAUUSDm"
mt5.initialize(timeout=60000); mt5.symbol_select(SYM,True)
t=mt5.symbol_info_tick(SYM); pos=list(mt5.positions_get(symbol=SYM) or [])
print("bid %.3f  |  book P&L $%+.2f" % (t.bid, sum(p.profit for p in pos)))
for p in pos:
    if abs(p.volume-0.02)<1e-9:            # the ADD - book it, worst risk profile
        r=mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL,symbol=SYM,volume=p.volume,position=p.ticket,
            type=mt5.ORDER_TYPE_SELL,price=t.bid,deviation=20,comment="book add",type_filling=mt5.ORDER_FILLING_IOC))
        print("BOOK add %s %.2f lot @ %.3f  P&L $%+.2f  -> %s" % (p.ticket,p.volume,t.bid,p.profit,
              "OK" if r and r.retcode==mt5.TRADE_RETCODE_DONE else "FAIL %s"%(r.comment if r else "")))
    else:                                   # the RUNNER - trail the stop up hard
        new=4360.0
        if new>p.sl:
            m=mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP,symbol=SYM,position=p.ticket,sl=new,tp=p.tp))
            print("TRAIL runner %s stop %.1f -> %.1f (locks $%+.2f) -> %s" % (p.ticket,p.sl,new,(new-p.price_open)*p.volume*100,
                  "OK" if m and m.retcode==mt5.TRADE_RETCODE_DONE else "FAIL %s"%(m.comment if m else "")))
ai=mt5.account_info(); pos=list(mt5.positions_get(symbol=SYM) or [])
print("\nequity %.2f  balance %.2f" % (ai.equity,ai.balance))
for p in pos: print("  still open: %s %.2f lot @ %.3f sl %.1f tp %.1f  P&L $%+.2f" % (p.ticket,p.volume,p.price_open,p.sl,p.tp,p.profit))
print("  locked minimum from here: $%+.2f" % sum((p.sl-p.price_open)*p.volume*100 for p in pos))
mt5.shutdown()
