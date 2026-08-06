#!/usr/bin/env python3
"""
iron_butterfly_live.py — LIVE PAPER iron-butterfly (short-vol / VRP harvest). 2026-08-01.

The one edge that cleared the bar in backtest (net+ex-best positive, tail capped ~600): in a
non-EXPANSION regime, SELL the ATM straddle + BUY protective wings (ATM +/- W strikes) = iron
butterfly, hold to session close (harvest theta), defined risk. This runs it LIVE on real NIFTY/
SENSEX chains, PAPER only (never places an order). Separate book. Kill IRON_BUTTERFLY=0.

Fly value V = (ATM_CE + ATM_PE) - (wingUP_CE + wingDN_PE). Enter (collect V), exit on decay
target or EOD flat. P&L per unit = V_entry - V_exit, net of a per-leg cost. Max loss bounded by
the wing width. Regime from the index 1m bars (skip EXPANSION). --once for a single cycle.
"""
from __future__ import annotations
import datetime as _dt, json, os, sys, time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

UNDERLYINGS = ["NIFTY", "SENSEX"]              # liquid weekly index options
WING = int(os.environ.get("IB_WING_STRIKES", "2") or 2)
DECAY_TARGET = float(os.environ.get("IB_DECAY_TARGET", "0.50") or 0.50)
LOTS = int(os.environ.get("IB_LOTS", "1") or 1)

# ── 2026-08-07 COST CORRECTION — the reason this strategy is now OFF by default ──
# The original cost model here was `COST_LEG(0.0015) * 4 * fly_value`, i.e. 15bps applied
# to the fly VALUE. That is wrong twice: real cost is driven by the LEG PREMIUM turnover,
# and above all by BROKERAGE, which is Rs20 FLAT PER ORDER — a 4-leg fly pays 8 orders =
# Rs160 before any percentage charge. It undercharged by ~Rs219/trade (~6-9x).
#
# Re-priced against 81 recorded sessions with the platform's authoritative model
# (platform_core/execution/transaction_costs.py):
#     deployed +/-2-strike fly, enter ~09:50 exit 15:20, 1 lot
#     gross across all 81 sessions:  -Rs858      (it barely decays intraday at all)
#     NET after real fees:           -Rs22,652   (fees only)
#     NET incl. the platform's 0.25%/side spread: -Rs40,233
#     profitable sessions: 1 / 81.   No wing width from +/-200 to +/-800 is net positive.
# The live book's own 7 trades reported +Rs117 and actually made -Rs1,414.
#
# WHY it cannot work intraday: a fly's value is a DIFFERENCE of two decaying things — the
# body and the wings decay together, so intraday theta on the structure is ~0 (observed
# dV of 0.15-4.6 points/session). Its P&L is really gamma. Theta harvest needs multi-day
# holding, which is what the original "hold to close" study measured but NOT what was
# deployed. Set IRON_BUTTERFLY=1 to run it for research; it will now refuse entries that
# cannot clear their own fee.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from platform_core.execution.transaction_costs import (           # noqa: E402
    iron_fly_round_trip_costs, exchange_for_underlying,
)

# Min-edge gate: realistic expected gross must clear this multiple of the round-trip
# cost, else the trade is refused. This is the general immune system — any structure
# whose expected move cannot pay its own fee bill never gets opened.
_MIN_EDGE_MULT = float(os.environ.get("IB_MIN_EDGE_MULT", "2.0") or 2.0)
# Realistic decay actually observed intraday, as a fraction of entry credit. The
# DECAY_TARGET (0.50) is an exit *target* that essentially never triggers — 7/7 live
# exits were EOD_FLAT — so sizing the edge off it is wishful. Measured median intraday
# capture is ~2% of credit; keep the gate honest by using that, not the target.
_EXPECTED_CAPTURE = float(os.environ.get("IB_EXPECTED_CAPTURE", "0.02") or 0.02)
REGIME_WINDOW = 20
# Liquidity-sweep entry (2026-08-03): the sharpest reversion trigger — price pokes beyond a
# recent extreme (grabs stops) then rejects back inside. Sell vol INTO the sweep: reversion
# likely + vol momentarily richer. Preferred over the baseline regime entry. IB_SWEEP_ENTRY=0 off.
_SWEEP_ENTRY = os.environ.get("IB_SWEEP_ENTRY", "1") == "1"
SWEEP_LOOKBACK = int(os.environ.get("IB_SWEEP_LOOKBACK", "15") or 15)
# IV filter (2026-08-05, the missing intelligence): the edge lives ONLY in RICH vol. Backtest
# split -- cheap-vol entries net +280 (flat), rich-vol +9,281 (all the edge). So only sell when
# the ATM straddle is >= IB_MIN_IV_PCT of spot (rich premium); sit out cheap vol (e.g. VIX 12.5
# days). This is what a pro vol-seller does and the model never did. IB_MIN_IV_PCT=0 disables.
_MIN_IV_PCT = float(os.environ.get("IB_MIN_IV_PCT", "1.35") or 1.35)
# DTE selection (2026-08-05, the next gap): the edge is ONLY in 4+ DTE (+9,661) — near-expiry
# (2-3 DTE) is flat/dead because gamma risk cancels the theta (a small move near expiry = big
# loss). The model grabbed the NEAREST expiry (often 0-1 DTE = pin/gamma trap). Now it picks the
# nearest expiry with DTE >= IB_MIN_DTE. A pro never sells vol into expiry-day gamma.
_MIN_DTE = int(os.environ.get("IB_MIN_DTE", "4") or 4)
# Size-to-richness (2026-08-05): the IV split proved the edge RISES with richness (highest-vol
# third +7,572 vs mid). So size PROPORTIONAL to how rich vol is: 1 unit at the threshold, up to
# IB_MAX_SIZE_MULT when vol is extreme. Concentrates capital where the edge is strongest. This
# is the ONE place sizing is justified (proven +EV, defined risk).
_SIZE_TO_IV = os.environ.get("IB_SIZE_TO_IV", "1") == "1"
_MAX_SIZE_MULT = int(os.environ.get("IB_MAX_SIZE_MULT", "3") or 3)
EOD_H, EOD_M = 15, 20
POLL_SEC = 60
STATE = Path("logs/iron_butterfly_state.json")
TRADES = Path("logs/iron_butterfly_trades.jsonl")
IDX_TOKEN = {"NIFTY": ("NSE", "NIFTY 50"), "SENSEX": ("BSE", "SENSEX"), "BANKNIFTY": ("NSE", "NIFTY BANK")}


def _load_kite():
    from kiteconnect import KiteConnect
    cfg = json.load(open("configs/kite_config.json"))
    k = KiteConnect(api_key=cfg["api_key"]); k.set_access_token(cfg["access_token"]); return k


def _resolve(k):
    nfo = k.instruments("NFO"); bfo = k.instruments("BFO")
    today = _dt.date.today(); out = {}
    for nm in UNDERLYINGS:
        pool = bfo if nm == "SENSEX" else nfo
        exch = "BFO" if nm == "SENSEX" else "NFO"
        opts = [i for i in pool if i["name"] == nm and i["instrument_type"] in ("CE", "PE") and i["expiry"] > today]
        if not opts:
            continue
        exps = sorted(set(i["expiry"] for i in opts))
        exp = next((e for e in exps if (e - today).days >= _MIN_DTE), exps[-1])
        strikes = sorted(set(i["strike"] for i in opts if i["expiry"] == exp))
        step = min((b - a for a, b in zip(strikes, strikes[1:])), default=0) or 0
        iex, isym = IDX_TOKEN[nm]
        itok = next((i["instrument_token"] for i in k.instruments(iex) if i["tradingsymbol"] == isym), None)
        lot = next((i["lot_size"] for i in opts if i["expiry"] == exp), 1)
        out[nm] = {"exch": exch, "exp": exp, "step": step, "lot": lot, "itok": itok,
                   "ce": {i["strike"]: i["tradingsymbol"] for i in opts if i["expiry"] == exp and i["instrument_type"] == "CE"},
                   "pe": {i["strike"]: i["tradingsymbol"] for i in opts if i["expiry"] == exp and i["instrument_type"] == "PE"}}
        print(f"[IB] {nm}: exp={exp} step={step} lot={lot} idx_tok={itok}")
    return out


def _sweep(bars):
    """Liquidity sweep: prior bar poked beyond the N-bar extreme, current bar rejected inside."""
    n = SWEEP_LOOKBACK
    if len(bars) < n + 2:
        return None
    w = bars[-(n + 2):]
    prior_hi = max(b["high"] for b in w[:-2])
    prior_lo = min(b["low"] for b in w[:-2])
    prev, last = w[-2], w[-1]
    if prev["high"] > prior_hi and last["close"] < prior_hi:
        return "HIGH"   # swept buy-stops above then rejected -> reversion down likely
    if prev["low"] < prior_lo and last["close"] > prior_lo:
        return "LOW"    # swept sell-stops below then rejected -> reversion up likely
    return None


def _regime(bars):
    if len(bars) < REGIME_WINDOW + 1:
        return "UNKNOWN"
    w = bars[-(REGIME_WINDOW + 1):]
    rngs = [b["high"] - b["low"] for b in w]
    cur, avg = rngs[-1], sum(rngs[:-1]) / max(len(rngs) - 1, 1)
    if avg <= 0:
        return "UNKNOWN"
    return "EXPANSION" if cur > 1.5 * avg else ("COMPRESSION" if cur < 0.7 * avg else "BALANCED")


def _fly_value(k, m, atm):
    """Live V = ATM straddle - wings, from real LTPs. None if any leg missing.
    Returns (V, width, straddle, legs) — legs are needed to cost the structure honestly."""
    up = atm + WING * m["step"]; dn = atm - WING * m["step"]
    syms = [m["ce"].get(atm), m["pe"].get(atm), m["ce"].get(up), m["pe"].get(dn)]
    if not all(syms):
        return None, None, None, None
    keys = [f"{m['exch']}:{s}" for s in syms]
    q = k.quote(keys)
    px = [q.get(kk, {}).get("last_price") for kk in keys]
    if not all(px) or any(p <= 0 for p in px):
        return None, None, None, None
    ce_a, pe_a, ce_u, pe_d = px
    return (ce_a + pe_a) - (ce_u + pe_d), (up - dn), (ce_a + pe_a), px


def _fly_cost(nm, legs, qty):
    """Authoritative round-trip toll for the 4-leg structure (8 orders)."""
    return iron_fly_round_trip_costs(
        atm_ce=legs[0], atm_pe=legs[1], wing_ce=legs[2], wing_pe=legs[3],
        quantity=qty, exchange=exchange_for_underlying(nm),
    ).total


def _fresh():
    return {"capital": 400000.0, "equity": 400000.0, "day": _dt.datetime.now(IST).date().isoformat(),
            "positions": {}, "realized": 0.0, "n": 0}


def _load_state():
    today = _dt.datetime.now(IST).date().isoformat()
    if STATE.exists():
        try:
            s = json.load(open(STATE))
        except Exception:
            return _fresh()
        if s.get("day") == today:
            return s
        eq = float(s.get("equity", 400000.0) or 400000.0)
        return {"capital": eq, "equity": eq, "day": today, "positions": {}, "realized": 0.0, "n": 0}
    return _fresh()


def _poll(k, meta, s, eod):
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for nm, m in meta.items():
        try:
            pos = s["positions"].get(nm)
            if pos:
                v, _, _, legs_now = _fly_value(k, m, pos["atm"])
                if v is None:
                    continue
                reason = "EOD_FLAT" if eod else ("DECAY_TARGET" if v <= pos["v0"] * (1 - DECAY_TARGET) else None)
                if reason:
                    qty = m["lot"] * pos.get("lots", LOTS)
                    gross = (pos["v0"] - v) * qty
                    # Authoritative cost off the REAL entry and exit legs (8 orders).
                    cost = _fly_cost(nm, pos.get("legs") or legs_now, qty)
                    net = gross - cost
                    s["realized"] += net; s["n"] += 1
                    TRADES.parent.mkdir(parents=True, exist_ok=True)
                    with TRADES.open("a") as tf:
                        tf.write(json.dumps({"ts": ts, "underlying": nm, "atm": pos["atm"], "v0": pos["v0"],
                                             "trigger": pos.get("trigger"), "iv_pct": pos.get("iv_pct"),
                                             "v_exit": round(v, 2), "lots": pos.get("lots", LOTS),
                                             "gross": round(gross, 1), "cost": round(cost, 1),
                                             "net": round(net, 1),
                                             "reason": reason, "regime": pos["regime"]}) + "\n")
                    print(f"[IB EXIT] {nm} {reason} gross={gross:,.0f} cost={cost:,.0f} "
                          f"net={net:,.0f} (realized={s['realized']:,.0f})")
                    del s["positions"][nm]
                continue
            if eod:
                continue
            # entry: regime-gated, once per session per underlying
            if m["itok"] is None:
                continue
            frm = (_dt.datetime.now(IST) - _dt.timedelta(minutes=REGIME_WINDOW + 8)).strftime("%Y-%m-%d %H:%M:%S")
            to = _dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            bars = k.historical_data(m["itok"], frm, to, "minute")
            reg = _regime(bars)
            sw = _sweep(bars) if _SWEEP_ENTRY else None
            # a stop-hunt sweep is the preferred (sharpest) reversion entry; else fall back
            # to the baseline non-expansion regime. EXPANSION w/o a sweep is skipped.
            if sw:
                trigger = f"SWEEP_{sw}"
            elif reg not in ("UNKNOWN", "EXPANSION"):
                trigger = f"REGIME_{reg}"
            else:
                continue
            spot = bars[-1]["close"]
            atm = round(spot / m["step"]) * m["step"] if m["step"] else spot
            v0, width, straddle, legs = _fly_value(k, m, atm)
            if v0 is None or v0 <= 0 or straddle is None:
                continue
            iv_pct = 100.0 * straddle / spot if spot else 0.0
            if _MIN_IV_PCT > 0 and iv_pct < _MIN_IV_PCT:
                print(f"[IB SKIP] {nm} vol cheap: straddle {iv_pct:.2f}% < {_MIN_IV_PCT}% of spot — not selling")
                continue
            n_lots = LOTS
            if _SIZE_TO_IV and _MIN_IV_PCT > 0:
                n_lots = LOTS * max(1, min(_MAX_SIZE_MULT, round(iv_pct / _MIN_IV_PCT)))

            # ── MIN-EDGE GATE ──────────────────────────────────────────────────
            # Refuse any structure whose realistic expected gross cannot clear a
            # multiple of its own round-trip fee. Brokerage is flat per order, so
            # this bites hardest on multi-leg structures at small size — exactly
            # where the old proportional cost model hid the loss.
            qty = m["lot"] * n_lots
            rt_cost = _fly_cost(nm, legs, qty)
            exp_gross = v0 * _EXPECTED_CAPTURE * qty
            if _MIN_EDGE_MULT > 0 and exp_gross < _MIN_EDGE_MULT * rt_cost:
                print(f"[IB REFUSED] {nm} {trigger} FEE-NEGATIVE: expected gross "
                      f"₹{exp_gross:,.0f} ({_EXPECTED_CAPTURE:.0%} of {v0:.1f} credit x{qty}) "
                      f"< {_MIN_EDGE_MULT:g}x round-trip cost ₹{rt_cost:,.0f} "
                      f"(8 orders). This structure cannot pay its own fee bill.")
                continue

            s["positions"][nm] = {"atm": atm, "v0": round(v0, 2), "width": width, "opened": ts,
                                  "regime": reg, "trigger": trigger, "iv_pct": round(iv_pct, 2),
                                  "lots": n_lots, "legs": legs, "rt_cost": round(rt_cost, 1)}
            print(f"[IB ENTRY] {nm} {trigger} IV={iv_pct:.2f}% x{n_lots}lot ATM={atm} "
                  f"credit={v0:.1f} cost=₹{rt_cost:,.0f} "
                  f"max_loss~{(width - v0) * m['lot'] * n_lots:,.0f}")
        except Exception as exc:
            print(f"[IB] {nm} err: {type(exc).__name__}: {str(exc)[:80]}")
    STATE.parent.mkdir(parents=True, exist_ok=True); json.dump(s, open(STATE, "w"), default=str)


def main():
    # DEFAULT OFF as of 2026-08-07. Proven fee-negative in its deployed intraday form:
    # 1 profitable session in 81, -Rs22,652 on fees alone. Opt in with IRON_BUTTERFLY=1.
    if os.environ.get("IRON_BUTTERFLY", "0") != "1":
        print("[IB] DISABLED (default). Deployed intraday +/-2 fly measured 1/81 profitable "
              "sessions, -Rs22,652 net of real fees — the old cost model undercharged by "
              "~Rs219/trade. Set IRON_BUTTERFLY=1 to run it for research."); return 0
    once = "--once" in sys.argv
    k = _load_kite(); meta = _resolve(k)
    if not meta:
        print("[IB] nothing to trade"); return 0
    s = _load_state()
    print(f"[IB] START capital={s['capital']:,.0f} equity={s['equity']:,.0f} realized={s['realized']:,.0f}")
    if once:
        _poll(k, meta, s, eod=False)
        print(f"[IB] --once done equity={s['equity']:,.0f}"); return 0
    while True:
        now = _dt.datetime.now(IST)
        eod = (now.hour, now.minute) >= (EOD_H, EOD_M)
        t0 = time.time()
        try:
            _poll(k, meta, s, eod)
        except Exception as exc:
            print(f"[IB] cycle err: {type(exc).__name__}: {str(exc)[:80]}")
        if eod and not s["positions"]:
            print(f"[IB] EOD flat — done. realized={s['realized']:,.0f} n={s['n']}"); return 0
        time.sleep(max(1.0, POLL_SEC - (time.time() - t0)))


if __name__ == "__main__":
    raise SystemExit(main())
