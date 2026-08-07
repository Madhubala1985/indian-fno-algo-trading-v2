#!/usr/bin/env python3
"""claude_live_desk.py — Claude's own live PAPER trading desk. 2026-08-07.

One command prints everything needed for a decision (indices + intraday path +
ATM ladder + my open book marked live), so a full session costs a handful of calls.
PAPER ONLY — never places a broker order. P&L is NET via the platform cost model.

  snap [IDX ...]        market state + path + my book marked live
  ladder IDX [N]        option ladder around ATM (N strikes each side, default 4)
  buy  IDX CE|PE STRIKE LOTS  "thesis"   open a long option
  sell IDX CE|PE STRIKE LOTS  "thesis"   open a short option (defined use only)
  close ID  "reason"    close an open position at live LTP
  book                  the book, marked
"""
from __future__ import annotations
import datetime as _dt, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from platform_core.execution.transaction_costs import (  # noqa: E402
    structure_round_trip_costs, exchange_for_underlying)

try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

IDX = {"NIFTY":     ("NSE", "NIFTY 50",   "NFO", 50),
       "BANKNIFTY": ("NSE", "NIFTY BANK", "NFO", 100),
       "SENSEX":    ("BSE", "SENSEX",     "BFO", 100)}
CAPITAL = 400000.0
BOOK = Path(f"data/claude_trades/live_book_{_dt.datetime.now(IST):%Y-%m-%d}.json")
LOG  = Path("data/claude_trades/claude_trades.jsonl")


def kite():
    from kiteconnect import KiteConnect
    c = json.load(open("configs/kite_config.json"))
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(c["access_token"]); return k


def load_book():
    if BOOK.exists():
        try: return json.load(open(BOOK))
        except Exception: pass
    return {"capital": CAPITAL, "realized": 0.0, "positions": [], "next_id": 1}


def save_book(b):
    BOOK.parent.mkdir(parents=True, exist_ok=True); json.dump(b, open(BOOK, "w"), indent=1)


def log(rec):
    rec["logged_at"] = _dt.datetime.now(IST).isoformat()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f: f.write(json.dumps(rec) + "\n")


_inst_cache = {}
def chain(k, name):
    """(expiry, lot, {(strike,type): tradingsymbol}) for the nearest expiry."""
    if name in _inst_cache: return _inst_cache[name]
    _, _, exch, _ = IDX[name]
    pool = k.instruments(exch)
    today = _dt.date.today()
    opts = [i for i in pool if i["name"] == name and i["instrument_type"] in ("CE", "PE")
            and i["expiry"] and i["expiry"] >= today]
    if not opts: return None
    exp = min(i["expiry"] for i in opts)
    sel = [i for i in opts if i["expiry"] == exp]
    lot = sel[0]["lot_size"]
    m = {(int(i["strike"]), i["instrument_type"]): i["tradingsymbol"] for i in sel}
    _inst_cache[name] = (exp, lot, m, exch)
    return _inst_cache[name]


def spot_quotes(k, names):
    keys = [f"{IDX[n][0]}:{IDX[n][1]}" for n in names] + ["NSE:INDIA VIX"]
    return k.quote(keys)


def path_str(k, name, minutes=200):
    """Compact intraday path + momentum from 1m bars."""
    _, sym, _, _ = IDX[name]
    tok = next((i["instrument_token"] for i in k.instruments(IDX[name][0])
                if i["tradingsymbol"] == sym), None)
    if tok is None: return "", None
    now = _dt.datetime.now(IST)
    frm = (now - _dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        bars = k.historical_data(tok, frm, now.strftime("%Y-%m-%d %H:%M:%S"), "minute")
    except Exception as e:
        return f"(bars unavailable: {str(e)[:40]})", None
    if not bars: return "", None
    c = [b["close"] for b in bars]
    step = max(1, len(c) // 14)
    p = " ".join(f"{x:,.0f}" for x in c[::step])
    def mom(n): return c[-1] - c[-min(n, len(c))]
    return (f"path: {p} (now {c[-1]:,.0f})\n"
            f"  mom: last15m {mom(15):+.0f}  last30m {mom(30):+.0f}  last60m {mom(60):+.0f}"), bars


def ltp_of(k, exch, tsym):
    q = k.quote([f"{exch}:{tsym}"])
    v = list(q.values())
    return float(v[0]["last_price"]) if v and v[0].get("last_price") else None


def mark_book(k, b):
    if not b["positions"]:
        print("  (no open positions)"); return 0.0
    tot = 0.0
    for p in b["positions"]:
        if p.get("status") != "OPEN": continue
        exp, lot, m, exch = chain(k, p["underlying"])
        px = ltp_of(k, exch, p["tsym"])
        if px is None: print(f"  [{p['id']}] {p['tsym']} — no quote"); continue
        qty = lot * p["lots"]; sgn = 1 if p["side"] == "LONG" else -1
        gross = (px - p["entry_px"]) * qty * sgn
        cost = structure_round_trip_costs(
            [(p["entry_px"], px, "BUY" if p["side"] == "LONG" else "SELL")],
            quantity=qty, exchange=exch).total
        net = gross - cost; tot += net
        peak = max(p.get("peak_px", p["entry_px"]), px) if p["side"] == "LONG" else min(
            p.get("peak_px", p["entry_px"]), px)
        p["peak_px"] = peak
        peak_net = (peak - p["entry_px"]) * qty * sgn - cost
        print(f"  [{p['id']}] {p['side']} {p['tsym']} x{p['lots']}lot @{p['entry_px']:.2f} "
              f"-> {px:.2f}  NET {net:+,.0f}  (peak {peak_net:+,.0f}, giveback {net-peak_net:+,.0f})"
              f"  stop {p.get('stop','-')}")
    return tot


def cmd_snap(args):
    k = kite()
    names = [a.upper() for a in args if a.upper() in IDX] or list(IDX)
    q = spot_quotes(k, names)
    now = _dt.datetime.now(IST)
    print(f"=== CLAUDE LIVE DESK — {now:%Y-%m-%d %H:%M:%S} IST ===")
    vix = q.get("NSE:INDIA VIX", {})
    if vix: print(f"INDIA VIX {vix['last_price']:.2f}  (open {vix['ohlc']['open']:.2f} "
                  f"lo {vix['ohlc']['low']:.2f} hi {vix['ohlc']['high']:.2f})")
    for n in names:
        v = q[f"{IDX[n][0]}:{IDX[n][1]}"]; o = v["ohlc"]
        rng = 100 * (o["high"] - o["low"]) / o["open"] if o["open"] else 0
        print(f"\n{n}: {v['last_price']:,.2f}  chg {100*(v['last_price']-o['close'])/o['close']:+.2f}% "
              f"vs prev {o['close']:,.2f} | open {o['open']:,.2f} "
              f"(gap {100*(o['open']-o['close'])/o['close']:+.2f}%) hi {o['high']:,.2f} lo {o['low']:,.2f} "
              f"range {rng:.2f}%")
        ps, _ = path_str(k, n)
        if ps: print("  " + ps)
        ch = chain(k, n)
        if ch:
            exp, lot, _, _ = ch
            print(f"  expiry {exp} (DTE {(exp - _dt.date.today()).days})  lot {lot}")
    b = load_book()
    print(f"\n--- MY PAPER BOOK (capital ₹{b['capital']:,.0f}, realized ₹{b['realized']:+,.0f}) ---")
    unreal = mark_book(k, b); save_book(b)
    print(f"  unrealized NET ₹{unreal:+,.0f}   |   DAY TOTAL ₹{b['realized']+unreal:+,.0f}")


def cmd_ladder(args):
    k = kite(); name = args[0].upper(); n = int(args[1]) if len(args) > 1 else 4
    exp, lot, m, exch = chain(k, name)
    q = spot_quotes(k, [name]); spot = q[f"{IDX[name][0]}:{IDX[name][1]}"]["last_price"]
    step = IDX[name][3]
    atm = round(spot / step) * step
    ks = [atm + i * step for i in range(-n, n + 1)]
    keys = [f"{exch}:{m[(s,t)]}" for s in ks for t in ("CE", "PE") if (s, t) in m]
    qq = k.quote(keys)
    print(f"{name} spot {spot:,.2f}  ATM {atm}  expiry {exp} DTE {(exp-_dt.date.today()).days} lot {lot}")
    print(f"{'strike':>8}  {'CE':>9} {'PE':>9}")
    for s in ks:
        ce = qq.get(f"{exch}:{m.get((s,'CE'),'')}", {}).get("last_price")
        pe = qq.get(f"{exch}:{m.get((s,'PE'),'')}", {}).get("last_price")
        mk = " <ATM" if s == atm else ""
        print(f"{s:>8}  {ce if ce else '-':>9} {pe if pe else '-':>9}{mk}")


def cmd_open(args, side):
    k = kite()
    name, typ, strike, lots = args[0].upper(), args[1].upper(), int(args[2]), int(args[3])
    thesis = args[4] if len(args) > 4 else ""
    stop = args[5] if len(args) > 5 else ""
    exp, lot, m, exch = chain(k, name)
    tsym = m.get((strike, typ))
    if not tsym: print(f"no such contract {name} {strike}{typ}"); return
    px = ltp_of(k, exch, tsym)
    if not px: print("no quote"); return
    b = load_book()
    pid = b["next_id"]; b["next_id"] += 1
    pos = {"id": pid, "underlying": name, "tsym": tsym, "strike": strike, "type": typ,
           "side": side, "lots": lots, "lot_size": lot, "entry_px": px,
           "ts_ist": _dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
           "thesis": thesis, "stop": stop, "status": "OPEN", "peak_px": px}
    b["positions"].append(pos); save_book(b)
    prem = px * lot * lots
    print(f"[OPEN {side}] id={pid} {tsym} x{lots}lot @ ₹{px:.2f}  premium ₹{prem:,.0f} "
          f"({100*prem/b['capital']:.1f}% of capital)")
    print(f"  thesis: {thesis}\n  stop: {stop}")
    log({"type": "PAPER_TRADE_ENTRY", **{x: pos[x] for x in
         ("id","underlying","tsym","strike","type","side","lots","lot_size","entry_px","ts_ist","thesis","stop")}})


def cmd_close(args):
    k = kite(); pid = int(args[0]); reason = args[1] if len(args) > 1 else ""
    b = load_book()
    p = next((x for x in b["positions"] if x["id"] == pid and x.get("status") == "OPEN"), None)
    if not p: print(f"no open position id={pid}"); return
    exp, lot, m, exch = chain(k, p["underlying"])
    px = ltp_of(k, exch, p["tsym"])
    if not px: print("no quote"); return
    qty = p["lot_size"] * p["lots"]; sgn = 1 if p["side"] == "LONG" else -1
    gross = (px - p["entry_px"]) * qty * sgn
    cost = structure_round_trip_costs(
        [(p["entry_px"], px, "BUY" if p["side"] == "LONG" else "SELL")],
        quantity=qty, exchange=exch).total
    net = gross - cost
    p.update(status="CLOSED", exit_px=px, gross=round(gross, 1), cost=round(cost, 1),
             net=round(net, 1), exit_reason=reason,
             exit_ts_ist=_dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    b["realized"] += net; save_book(b)
    print(f"[CLOSE] id={pid} {p['tsym']} @ ₹{px:.2f}  gross ₹{gross:+,.0f} cost ₹{cost:,.0f} "
          f"NET ₹{net:+,.0f}   realized today ₹{b['realized']:+,.0f}")
    print(f"  reason: {reason}")
    log({"type": "PAPER_TRADE_EXIT", **{x: p[x] for x in
         ("id","underlying","tsym","side","lots","entry_px","exit_px","gross","cost","net",
          "exit_reason","exit_ts_ist")}})


def cmd_book(_):
    k = kite(); b = load_book()
    print(f"capital ₹{b['capital']:,.0f}  realized ₹{b['realized']:+,.0f}")
    u = mark_book(k, b); save_book(b)
    print(f"unrealized ₹{u:+,.0f}  DAY TOTAL ₹{b['realized']+u:+,.0f}")
    for p in b["positions"]:
        if p.get("status") == "CLOSED":
            print(f"  [{p['id']}] CLOSED {p['tsym']} net ₹{p['net']:+,.0f} — {p.get('exit_reason','')}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snap"
    a = sys.argv[2:]
    {"snap": cmd_snap, "ladder": cmd_ladder, "book": cmd_book,
     "buy": lambda x: cmd_open(x, "LONG"), "sell": lambda x: cmd_open(x, "SHORT"),
     "close": cmd_close}[cmd](a)
