#!/usr/bin/env python3
"""claude_cas_signal.py — the ENGINEERED closing-auction signal. 2026-08-07.

NSE publishes no usable closing-auction imbalance (api/market-data-pre-open?key=CAS
returns 200 with zero rows, verified across two days). So build the equivalent from
the live option book instead: a closing auction is order-flow imbalance driving the
closing print, and the option book shows that imbalance directly.

Replicates platform_core/data/live/option_flow_shadow.py EXACTLY so the live number is
comparable to the logged history it was validated on:
    book_imb(side) = (SUM bid_qty - SUM ask_qty) / (SUM bid_qty + SUM ask_qty)
                     over ATM +/- ATM_BAND strikes
    net_book_imb   = call_book_imb - put_book_imb        in [-2, +2]

MEASURED (logs/option_flow_shadow.jsonl, 2026-07-28..08-06, 3 indices):
  all day        -> corr(imb, fwd move) ~ -0.01, sign hit 49.7%  == NOTHING
  last hour      -> corr -0.17, sign hit 44.9%                   == CONTRARIAN
So the tradeable read is to FADE the book: heavy resting depth is liquidity PROVISION,
not aggression, and price trades through the heavy side. Signal only near the close.

Prints the signal; never trades.
"""
from __future__ import annotations
import datetime as _dt, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

IDX = {"NIFTY": ("NSE", "NIFTY 50", "NFO", 50),
       "BANKNIFTY": ("NSE", "NIFTY BANK", "NFO", 100),
       "SENSEX": ("BSE", "SENSEX", "BFO", 100)}
ATM_BAND = int(os.environ.get("CAS_ATM_BAND", "2"))      # 2 -> 5 strikes, matches call_n=5
DEADBAND = float(os.environ.get("CAS_DEADBAND", "0.10"))


def kite():
    from kiteconnect import KiteConnect
    c = json.load(open("configs/kite_config.json"))
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(c["access_token"]); return k


def signal(k, name):
    ex, sym, oex, step = IDX[name]
    spot = k.quote([f"{ex}:{sym}"])[f"{ex}:{sym}"]["last_price"]
    pool = k.instruments(oex)
    today = _dt.date.today()
    opts = [i for i in pool if i["name"] == name and i["instrument_type"] in ("CE", "PE")
            and i["expiry"] and i["expiry"] >= today]
    if not opts: return None
    exp = min(i["expiry"] for i in opts)
    sel = [i for i in opts if i["expiry"] == exp]
    atm = round(spot / step) * step
    lo, hi = atm - ATM_BAND * step, atm + ATM_BAND * step
    want = [i for i in sel if lo <= i["strike"] <= hi]
    if not want: return None
    q = k.quote([f"{oex}:{i['tradingsymbol']}" for i in want])
    agg = {"CE": [0.0, 0.0, 0], "PE": [0.0, 0.0, 0]}
    for i in want:
        v = q.get(f"{oex}:{i['tradingsymbol']}")
        if not v: continue
        a = agg[i["instrument_type"]]
        a[0] += float(v.get("buy_quantity") or 0.0)
        a[1] += float(v.get("sell_quantity") or 0.0)
        a[2] += 1
    def imb(a):
        t = a[0] + a[1]
        return round((a[0] - a[1]) / t, 4) if t > 0 else None
    ci, pi = imb(agg["CE"]), imb(agg["PE"])
    net = round(ci - pi, 4) if (ci is not None and pi is not None) else None
    return {"symbol": name, "spot": spot, "atm": atm, "expiry": str(exp),
            "call_n": agg["CE"][2], "put_n": agg["PE"][2],
            "call_book_imb": ci, "put_book_imb": pi, "net_book_imb": net,
            "raw": {"ce_bid": agg["CE"][0], "ce_ask": agg["CE"][1],
                    "pe_bid": agg["PE"][0], "pe_ask": agg["PE"][1]}}


def main():
    k = kite()
    now = _dt.datetime.now(IST)
    names = [a.upper() for a in sys.argv[1:] if a.upper() in IDX] or list(IDX)
    print(f"=== ENGINEERED CAS SIGNAL — {now:%H:%M:%S} IST  (FADE the book) ===")
    out = []
    for n in names:
        try:
            s = signal(k, n)
        except Exception as e:
            print(f"  {n:10} error {type(e).__name__}: {str(e)[:60]}"); continue
        if not s or s["net_book_imb"] is None:
            print(f"  {n:10} no book data"); continue
        net = s["net_book_imb"]
        act = "FADE->BUY PE" if net >= DEADBAND else ("FADE->BUY CE" if net <= -DEADBAND else "no signal (deadband)")
        print(f"  {n:10} spot {s['spot']:>10,.2f} atm {s['atm']:>8,.0f} | "
              f"call_imb {s['call_book_imb']:+.4f} put_imb {s['put_book_imb']:+.4f} | "
              f"NET {net:+.4f} -> {act}")
        out.append(s)
    p = Path(f"data/claude_trades/cas_signal_{now:%Y-%m-%d}.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for s in out: f.write(json.dumps({"ts_ist": now.strftime("%H:%M:%S"), **s}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
