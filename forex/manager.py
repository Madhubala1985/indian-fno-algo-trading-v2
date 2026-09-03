"""
POSITION MANAGER — runs every minute, 24/7.

Exists because of a measured failure on 2026-09-02: an open book ran to roughly +$90,
missed its 4400 target by $2.23, reversed, and was back at +$36 before a human looked at it.

WHAT IT DOES — and strictly only this:
  1. PROTECT  at +1R the stop moves to breakeven. A winner never becomes a loser.
  2. SCALP    at 75% of the way to target, book HALF. This is the "book quick" half.
  3. RUN      the remaining half trails, giving back at most GIVEBACK_R from its peak.
              This is the "let proper trades wait" half.

WHAT IT WILL NEVER DO:
  - open a position (there is no order-open code in this file, by design)
  - move a stop backwards
  - manage a position that has no stop loss set

Parameters come from measurement, not taste. The capture study on the previous book found that
tight profit-locks RAISE capture% but LOWER net, because they kill the runners. So this protects
early and books half — it does not cut the runner.
"""

import MetaTrader5 as mt5
import json
import os
import datetime as dt

SYM = "XAUUSDm"
DIR = r"C:\Users\madhu\gold_shadow"
STATE = os.path.join(DIR, "manager_state.json")
LOG = os.path.join(DIR, "manager.log")

BE_AT_R      = 1.0    # move stop to breakeven once +1R is reached
SCALP_AT_PCT = 0.75   # book half at 75% of the distance to target
SCALP_FRAC   = 0.5    # how much of the position to book
GIVEBACK_R   = 0.6    # runner may give back this much R from its peak, no more
BE_BUFFER    = 0.5    # $ above entry when moving to breakeven (covers the spread)


def now():
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def say(msg):
    line = "[mgr %s] %s" % (now().strftime("%m-%d %H:%M"), msg)
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    try:
        json.dump(s, open(STATE, "w"), indent=1)
    except Exception:
        pass


def main():
    if not mt5.initialize(timeout=60000):
        say("MT5 INIT FAILED %s" % (mt5.last_error(),))
        return
    if not mt5.terminal_info().trade_allowed:
        say("algo trading OFF in terminal - cannot manage")
        mt5.shutdown()
        return

    mt5.symbol_select(SYM, True)
    st = load_state()
    tick = mt5.symbol_info_tick(SYM)
    positions = list(mt5.positions_get(symbol=SYM) or [])

    if not positions:
        say("no open positions")
        save_state({})
        mt5.shutdown()
        return

    for p in positions:
        key = str(p.ticket)
        islong = p.type == mt5.POSITION_TYPE_BUY
        px = tick.bid if islong else tick.ask
        s = st.setdefault(key, {})

        # Record the ORIGINAL risk the first time we see this position.
        if "R" not in s:
            if p.sl and p.sl > 0:
                s["R"] = abs(p.price_open - p.sl)
                s["peak"] = 0.0
                s["scalped"] = False
                say("tracking %s: entry %.2f  R=$%.2f  tp %.2f" % (key, p.price_open, s["R"], p.tp))
            else:
                say("%s has no stop loss - refusing to manage blind" % key)
                continue

        R = s["R"]
        if R <= 0:
            continue

        profit = (px - p.price_open) if islong else (p.price_open - px)
        rmult = profit / R
        s["peak"] = max(s.get("peak", 0.0), rmult)

        # ---- 1. PROTECT + 3. RUN : compute the best stop we are willing to hold
        want = p.sl

        if rmult >= BE_AT_R:
            be = p.price_open + (BE_BUFFER if islong else -BE_BUFFER)
            want = max(want, be) if islong else min(want, be)

        if s["peak"] >= BE_AT_R + GIVEBACK_R:
            trail = p.price_open + (s["peak"] - GIVEBACK_R) * R * (1 if islong else -1)
            want = max(want, trail) if islong else min(want, trail)

        # only ever tighten, never loosen
        if (islong and want > p.sl + 0.01) or ((not islong) and want < p.sl - 0.01):
            m = mt5.order_send(dict(
                action=mt5.TRADE_ACTION_SLTP, symbol=SYM, position=p.ticket,
                sl=round(want, 2), tp=p.tp))
            ok = m is not None and m.retcode == mt5.TRADE_RETCODE_DONE
            say("%s STOP %.2f -> %.2f  (now %.2fR, peak %.2fR)  %s" % (
                key, p.sl, want, rmult, s["peak"],
                "ok" if ok else "FAIL %s" % (m.comment if m else mt5.last_error())))

        # ---- 2. SCALP : book half near the target
        if (not s["scalped"]) and p.tp and p.tp > 0:
            dist = abs(p.tp - p.price_open)
            if dist > 0 and profit >= SCALP_AT_PCT * dist:
                vol = round(p.volume * SCALP_FRAC, 2)
                vmin = mt5.symbol_info(SYM).volume_min
                if vol >= vmin and (p.volume - vol) >= vmin:
                    c = mt5.order_send(dict(
                        action=mt5.TRADE_ACTION_DEAL, symbol=SYM, volume=vol, position=p.ticket,
                        type=mt5.ORDER_TYPE_SELL if islong else mt5.ORDER_TYPE_BUY,
                        price=tick.bid if islong else tick.ask, deviation=20,
                        comment="scalp book 50pct", type_filling=mt5.ORDER_FILLING_IOC))
                    ok = c is not None and c.retcode == mt5.TRADE_RETCODE_DONE
                    say("%s SCALP-BOOK %.2f lot @ %.2f (%.0f%% to target)  %s" % (
                        key, vol, px, 100 * profit / dist,
                        "ok" if ok else "FAIL %s" % (c.comment if c else mt5.last_error())))
                    if ok:
                        s["scalped"] = True
                else:
                    say("%s at %.0f%% to target but volume %.2f cannot be split" % (
                        key, 100 * profit / dist, p.volume))

        st[key] = s

    # drop state for tickets that no longer exist
    live = [str(p.ticket) for p in (mt5.positions_get(symbol=SYM) or [])]
    save_state({k: v for k, v in st.items() if k in live})
    mt5.shutdown()


if __name__ == "__main__":
    main()
