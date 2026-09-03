"""
GOLD LIVE TRADER (DEMO) — XAUUSDm, 13:00-15:00 TRUE UTC momentum, 0.01 lot.
Operator authorised live demo trading 2026-09-02, overriding the shadow-only gate.
Risk limits from forex/CONSTRAINTS.md Section F are enforced HERE, in code.
"""
import MetaTrader5 as mt5, pandas as pd, numpy as np, json, os, datetime as dt
import sizer, edge_ledger                     # the compounding law + the evidence that gates it

SYM="XAUUSDm"; LOT=0.01; LB=8; HOLD=16       # M15 bars: 2h lookback, 4h hold
WIN=(13,14)                                   # TRUE UTC bar-open hours
QTHRESH=0.70; TRAIL_BARS=60*96
MAGIC=990902
STOP_USD=50.0                                 # disaster brake (~5.5x mean 4h move), NOT a strategy stop
# Loss limits are FRACTIONS, not dollars: a compounding account whose brakes are fixed in
# dollars gets looser every time it wins. These tighten and loosen with the account.
DAILY_LOSS_PCT=0.02                           # 2% of current equity -> flat for the day
DRAWDOWN_FROM_PEAK=0.10                       # give back at most 10% of the HIGH-WATER mark
HWM_FILE=r"C:\Users\madhu\gold_shadow\hwm.json"
DIR=r"C:\Users\madhu\gold_shadow"
TLOG=os.path.join(DIR,"trades.jsonl"); STATE=os.path.join(DIR,"state.json")

def now(): return dt.datetime.now(dt.UTC).replace(tzinfo=None)
def log(rec):
    with open(TLOG,"a") as f: f.write(json.dumps(rec)+"\n")
LOGTXT=os.path.join(DIR,"trader.log")
def say(m):
    line="[trader %s] %s" % (now().strftime("%m-%d %H:%M"), m)
    print(line)
    try:
        with open(LOGTXT,"a") as f: f.write(line+"\n")
    except Exception: pass

def read_hwm():
    """High-water mark of equity. Persisted, because the drawdown brake has to remember
    a peak the account has already given back."""
    try: return float(json.load(open(HWM_FILE))["hwm"])
    except Exception: return 0.0

def write_hwm(v):
    try: json.dump(dict(hwm=round(v,2), at=now().isoformat()), open(HWM_FILE,"w"))
    except Exception: pass

def realised_today():
    """ACCOUNT-level realised P&L today. Deliberately NOT magic-filtered: the daily loss
    limit protects the account, so a discretionary loss must halt the bot too."""
    a=now().replace(hour=0,minute=0,second=0,microsecond=0)
    d=mt5.history_deals_get(a, now()+dt.timedelta(hours=1))
    return sum(x.profit+x.swap+x.commission for x in d if x.entry==mt5.DEAL_ENTRY_OUT) if d else 0.0

def main():
    if not mt5.initialize(timeout=60000):
        say("MT5 INIT FAILED %s" % (mt5.last_error(),)); return
    say("tick: init ok")
    ti=mt5.terminal_info(); ai=mt5.account_info()
    if not ti.trade_allowed:
        say("ABORT: algo trading disabled in terminal"); mt5.shutdown(); return
    mt5.symbol_select(SYM,True)

    allpos=list(mt5.positions_get(symbol=SYM) or [])
    pos=[p for p in allpos if p.magic==MAGIC]   # only OUR positions are ours to time-exit

    # ---------- 1. close matured positions (time exit = the strategy's real exit)
    for p in pos:
        age_min=(now()-dt.datetime.utcfromtimestamp(p.time)).total_seconds()/60
        if age_min >= HOLD*15:
            tick=mt5.symbol_info_tick(SYM)
            req=dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYM, volume=p.volume, magic=MAGIC,
                     position=p.ticket, deviation=20, comment="time exit 4h",
                     type=mt5.ORDER_TYPE_SELL if p.type==mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                     price=tick.bid if p.type==mt5.POSITION_TYPE_BUY else tick.ask,
                     type_filling=mt5.ORDER_FILLING_IOC)
            r=mt5.order_send(req)
            ok = r is not None and r.retcode==mt5.TRADE_RETCODE_DONE
            say("CLOSE ticket %s after %.0fmin -> %s profit=%.2f" % (p.ticket,age_min,
                 "OK" if ok else "FAIL %s"%(r.comment if r else mt5.last_error()), p.profit))
            log(dict(ev="close",ticket=p.ticket,ok=ok,profit=round(p.profit,2),age_min=round(age_min),ts=now().isoformat()))
            if ok: pos=[q for q in pos if q.ticket!=p.ticket]

    # ---------- 2. risk gates (all relative, so they scale with a compounding account)
    hwm=max(read_hwm(), ai.equity); write_hwm(hwm)
    floor=hwm*(1.0-DRAWDOWN_FROM_PEAK)
    if ai.equity < floor:
        say("HALT: equity %.2f is %.1f%% below high-water %.2f (limit %.0f%%)"
            % (ai.equity, 100*(1-ai.equity/hwm), hwm, 100*DRAWDOWN_FROM_PEAK)); mt5.shutdown(); return
    rt=realised_today(); daily_limit=-DAILY_LOSS_PCT*ai.equity
    if rt <= daily_limit:
        say("HALT: realised today %.2f at/below daily limit %.2f (%.0f%% of equity)"
            % (rt, daily_limit, 100*DAILY_LOSS_PCT)); mt5.shutdown(); return
    # one-at-a-time is ACCOUNT-WIDE, not per-magic: a discretionary position already
    # occupies the risk slot, and stacking a bot entry on top doubles exposure silently.
    if allpos:
        say("holding %d position(s) on %s (%d ours) - no new entry, one-at-a-time is ACCOUNT-wide"
            % (len(allpos), SYM, len(pos))); mt5.shutdown(); return

    # ---------- 3. signal
    r=mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_M15, 0, TRAIL_BARS+200)
    if r is None or len(r)<LB+500: say("no rates"); mt5.shutdown(); return
    df=pd.DataFrame(r); df["utc"]=pd.to_datetime(df["time"],unit="s")   # TRUE UTC
    c=df["close"].values; i=len(df)-2
    bar=df["utc"].iloc[i].to_pydatetime()
    if bar.hour not in WIN or bar.weekday()>4:
        say("bar %s UTC outside window - no entry" % bar); mt5.shutdown(); return
    done=set()
    if os.path.exists(TLOG):
        for l in open(TLOG):
            l=l.strip()
            if not l: continue
            try: j=json.loads(l)
            except Exception: continue
            if j.get("ev")=="open": done.add(j.get("bar"))
    if bar.isoformat() in done:
        say("already traded bar %s" % bar); mt5.shutdown(); return
    past=c[i]-c[i-LB]
    thr=np.quantile(np.abs(c[LB:i]-c[:i-LB]), QTHRESH)
    if abs(past)<=thr:
        say("bar %s |2h move| %.2f <= thr %.2f - no trade" % (bar,abs(past),thr)); mt5.shutdown(); return

    # ---------- 3b. SIZE. One rule, derived from the measured edge and nothing else.
    # Returns 0.01 while n<30 (constraint), scales with equity once evidence exists,
    # and returns 0.0 - refusing the trade - if the conservative edge is non-positive.
    eq = mt5.account_info().equity
    try:
        edge = edge_ledger.measure(edge_ledger.round_trips(MAGIC))
    except Exception as e:
        edge = dict(n=0, note="ledger failed: %s" % e)
    lot, risk_usd, why = sizer.size(eq, STOP_USD, edge)
    say("SIZER: %s" % why)
    if lot < sizer.VOL_MIN:
        say("NO TRADE: sizer returned %.2f lot" % lot)
        log(dict(ev="skip_size", bar=bar.isoformat(), reason=why, ts=now().isoformat()))
        mt5.shutdown(); return

    # ---------- 4. enter
    d=int(np.sign(past)); tick=mt5.symbol_info_tick(SYM)
    px = tick.ask if d>0 else tick.bid
    sl = px - d*STOP_USD
    req=dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYM, volume=lot, magic=MAGIC, deviation=20,
             type=mt5.ORDER_TYPE_BUY if d>0 else mt5.ORDER_TYPE_SELL, price=px, sl=round(sl,3),
             comment="gold mom 4h", type_filling=mt5.ORDER_FILLING_IOC)
    res=mt5.order_send(req)
    ok = res is not None and res.retcode==mt5.TRADE_RETCODE_DONE
    say("ENTRY %s %s %.2f lot @ %.3f sl %.3f risk $%.2f (2h move %+.2f vs thr %.2f) -> %s" %
        (bar, "BUY" if d>0 else "SELL", lot, px, sl, risk_usd, past, thr,
         "OK ticket %s"%res.order if ok else "FAIL %s"%(res.comment if res else mt5.last_error())))
    log(dict(ev="open",bar=bar.isoformat(),dir=d,lot=lot,price=px,sl=round(sl,3),
             risk_usd=round(risk_usd,2), equity=round(eq,2), edge_n=edge.get("n"), sizer=why,
             move_2h=round(float(past),3),thr=round(float(thr),3),ok=ok,
             ticket=getattr(res,"order",None),retcode=getattr(res,"retcode",None),ts=now().isoformat()))
    mt5.shutdown()

if __name__=="__main__": main()
