"""
GOLD SHADOW RECORDER — records signals, places NO orders.
Hypothesis under test (formed 2026-09-02 on 2022-06..2026-09 M15 data):
  XAUUSD momentum, 13:00-15:00 TRUE UTC, 2h lookback, 4h hold, top-30% moves.
Known caveat: in-sample it was 89% driven by 2026. This records FORWARD data to judge it
on observations that did not exist when the hypothesis was formed.

HARD CONSTRAINT: this file must never send an order. There is no order code here by design.
"""
import MetaTrader5 as mt5, pandas as pd, numpy as np, json, os, datetime as dt

SYM="XAUUSDm"; LB=8; HOLD=16          # M15 bars: 8=2h lookback, 16=4h hold
WIN=(13,14)                            # TRUE UTC hours (bar OPEN hour). Backtest window was
                                       # mislabelled 11-12; it is really 13:00-15:00 UTC = US data/NY open.
QTHRESH=0.70                           # act only on the top 30% of |2h moves|
TRAIL_DAYS=60                          # trailing window for the threshold (NO lookahead)
DIR=r"C:\Users\madhu\gold_shadow"; LOG=os.path.join(DIR,"signals.jsonl")

def utc_now(): return dt.datetime.now(dt.UTC).replace(tzinfo=None)

def load_log():
    if not os.path.exists(LOG): return []
    return [json.loads(l) for l in open(LOG) if l.strip()]

def append(rec):
    with open(LOG,"a") as f: f.write(json.dumps(rec)+"\n")

def rewrite(recs):
    with open(LOG,"w") as f:
        for r in recs: f.write(json.dumps(r)+"\n")

def main():
    if not mt5.initialize(timeout=60000):
        print("[shadow] MT5 init failed", mt5.last_error()); return
    mt5.symbol_select(SYM,True)
    r=mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_M15, 0, TRAIL_DAYS*96+200)
    if r is None or len(r)<LB+50:
        print("[shadow] no rates"); mt5.shutdown(); return
    df=pd.DataFrame(r); df["utc"]=pd.to_datetime(df["time"],unit="s")   # MT5 python API returns TRUE UTC (verified 2026-09-02)
    c=df["close"].values

    # --- resolve any matured signals first
    recs=load_log(); changed=False
    for rec in recs:
        if rec.get("status")!="OPEN": continue
        exit_at=dt.datetime.fromisoformat(rec["exit_utc"])
        if utc_now() < exit_at: continue
        m=df[df["utc"]>=exit_at]
        if m.empty: continue
        px=float(m["close"].iloc[0])
        gross=rec["dir"]*(px-rec["entry_px"])
        rec.update(status="CLOSED", exit_px=px,
                   exit_actual_utc=m["utc"].iloc[0].isoformat(),
                   gross_per_oz=round(gross,3),
                   net_per_oz=round(gross-rec["spread_cost"],3))
        changed=True
        print("[shadow] RESOLVED %s dir=%+d entry %.3f exit %.3f net $%.3f/oz" %
              (rec["signal_utc"],rec["dir"],rec["entry_px"],px,rec["net_per_oz"]))
    if changed: rewrite(recs)

    # --- new signal? only on a just-closed M15 bar whose OPEN hour is in WIN
    last=df.iloc[-2]                      # -1 is the still-forming bar
    bar_utc=last["utc"].to_pydatetime()
    if bar_utc.hour not in WIN or bar_utc.weekday()>4:
        print("[shadow] %s outside window (bar %s UTC) - no signal" % (utc_now().strftime("%H:%M"),bar_utc)); mt5.shutdown(); return
    if any(r_["signal_utc"]==bar_utc.isoformat() for r_ in recs):
        print("[shadow] already logged %s" % bar_utc); mt5.shutdown(); return

    i=len(df)-2
    if i<LB+TRAIL_DAYS*96*0.2: print("[shadow] not enough history"); mt5.shutdown(); return
    past=c[i]-c[i-LB]
    # trailing threshold — computed only from bars STRICTLY BEFORE now
    hist=np.abs(c[LB:i]-c[:i-LB])
    thr=np.quantile(hist,QTHRESH)
    tick=mt5.symbol_info_tick(SYM)
    spread=tick.ask-tick.bid
    if abs(past)<=thr:
        print("[shadow] %s |2h move| %.2f <= thr %.2f - no trade" % (bar_utc,abs(past),thr)); mt5.shutdown(); return
    d=int(np.sign(past))
    rec=dict(status="OPEN", signal_utc=bar_utc.isoformat(),
             exit_utc=(bar_utc+dt.timedelta(minutes=15*HOLD)).isoformat(),
             dir=d, entry_px=float(c[i]), move_2h=round(float(past),3), thresh=round(float(thr),3),
             bid=tick.bid, ask=tick.ask, spread=round(spread,3),
             spread_cost=round(2*spread,3),          # conservative: pay spread both ways
             logged_at=utc_now().isoformat())
    append(rec)
    print("[shadow] SIGNAL %s dir=%+d entry %.3f (2h move %+.2f vs thr %.2f) spread %.3f -> exit %s"
          % (bar_utc,d,rec["entry_px"],past,thr,spread,rec["exit_utc"]))
    mt5.shutdown()

if __name__=="__main__": main()
