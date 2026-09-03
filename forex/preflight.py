"""One-shot execution test: open 0.01 lot, verify, close immediately. Demo only."""
import MetaTrader5 as mt5, datetime as dt
SYM="XAUUSDm"; LOT=0.01; MAGIC=990902
mt5.initialize(timeout=60000)
ai=mt5.account_info()
assert ai.trade_mode==0, "REFUSING: not a demo account (trade_mode=%s)"%ai.trade_mode
mt5.symbol_select(SYM,True)
t=mt5.symbol_info_tick(SYM); eq0=ai.equity
print("pre : equity %.2f | bid %.3f ask %.3f spread %.3f" % (eq0,t.bid,t.ask,t.ask-t.bid))
req=dict(action=mt5.TRADE_ACTION_DEAL,symbol=SYM,volume=LOT,type=mt5.ORDER_TYPE_BUY,
         price=t.ask,sl=round(t.ask-50,3),magic=MAGIC,deviation=20,
         comment="preflight",type_filling=mt5.ORDER_FILLING_IOC)
r=mt5.order_send(req)
print("open: retcode=%s %s ticket=%s price=%.3f" % (r.retcode,r.comment,r.order,r.price))
if r.retcode!=mt5.TRADE_RETCODE_DONE:
    print("FILLING MODE ISSUE - trying FOK"); req["type_filling"]=mt5.ORDER_FILLING_FOK
    r=mt5.order_send(req); print("retry:",r.retcode,r.comment)
if r.retcode==mt5.TRADE_RETCODE_DONE:
    pos=[p for p in mt5.positions_get(symbol=SYM) if p.magic==MAGIC]
    for p in pos:
        print("  position: ticket %s vol %.2f open %.3f sl %.3f profit %.2f" % (p.ticket,p.volume,p.price_open,p.sl,p.profit))
        t2=mt5.symbol_info_tick(SYM)
        cr=mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL,symbol=SYM,volume=p.volume,
            type=mt5.ORDER_TYPE_SELL,position=p.ticket,price=t2.bid,magic=MAGIC,deviation=20,
            comment="preflight close",type_filling=mt5.ORDER_FILLING_IOC))
        print("close: retcode=%s %s" % (cr.retcode,cr.comment))
    ai2=mt5.account_info()
    print("post: equity %.2f | ROUND-TRIP COST = $%.2f on 0.01 lot" % (ai2.equity, eq0-ai2.equity))
print("open positions now:", len([p for p in (mt5.positions_get(symbol=SYM) or []) if p.magic==MAGIC]))
mt5.shutdown()
