#!/usr/bin/env python3
"""claude_desk_monitor.py — event stream for Claude's live paper desk. 2026-08-07.

Polls live index prices once a minute and prints ONE LINE per event Claude would act on:
  TRIGGER  a pre-armed level broke              -> decide/enter
  PULSE    periodic state (default every 25m)   -> reassess even with no break
  MANAGE   an open position hit a manage level  -> stop / momentum-break check
  ERROR    quote failure (silence != success)
  CLOSE    session end
Reads triggers from data/claude_trades/triggers_<date>.json and the open book from
data/claude_trades/live_book_<date>.json, so re-arming is just editing those files.
Never trades.
"""
from __future__ import annotations
import datetime as _dt, json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

IDX = {"NIFTY": ("NSE", "NIFTY 50"), "BANKNIFTY": ("NSE", "NIFTY BANK"),
       "SENSEX": ("BSE", "SENSEX")}
D = _dt.datetime.now(IST).strftime("%Y-%m-%d")
TRIG = Path(f"data/claude_trades/triggers_{D}.json")
BOOK = Path(f"data/claude_trades/live_book_{D}.json")
PULSE_MIN = int(os.environ.get("PULSE_MIN", "25"))
END = (15, 31)


def say(s):
    print(s, flush=True)


def main() -> int:
    from kiteconnect import KiteConnect
    c = json.load(open("configs/kite_config.json"))
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(c["access_token"])
    keys = [f"{e}:{s}" for e, s in IDX.values()] + ["NSE:INDIA VIX"]
    last_pulse = _dt.datetime.now(IST) - _dt.timedelta(minutes=PULSE_MIN)
    fails = 0
    peak = {}
    while True:
        now = _dt.datetime.now(IST)
        if (now.hour, now.minute) >= END:
            say(f"CLOSE {now:%H:%M} session over — flatten and grade"); return 0
        try:
            q = k.quote(keys); fails = 0
        except Exception as e:
            fails += 1
            if fails in (2, 6):
                say(f"ERROR {now:%H:%M} quote failing x{fails}: {type(e).__name__} {str(e)[:60]}")
            time.sleep(60); continue
        spot = {n: q[f"{e}:{s}"]["last_price"] for n, (e, s) in IDX.items()
                if f"{e}:{s}" in q}
        ohlc = {n: q[f"{e}:{s}"]["ohlc"] for n, (e, s) in IDX.items() if f"{e}:{s}" in q}
        vix = q.get("NSE:INDIA VIX", {}).get("last_price")

        # ---- armed triggers ----
        if TRIG.exists():
            try: tr = json.load(open(TRIG))
            except Exception: tr = {"triggers": []}
            dirty = False
            for t in tr.get("triggers", []):
                if t.get("fired"): continue
                sp = spot.get(t["underlying"])
                if sp is None: continue
                # Probe choice matters and getting it wrong fires false signals.
                #  BREAKOUT arms: use the day's extreme, so a spike that pierced the
                #    level and came back inside one 60s poll is still detected.
                #  LTP arms (stops / position management): use LTP ONLY. The day's
                #    extreme is set at some arbitrary earlier time — for a position
                #    opened midday it may already be beyond the stop, which fires the
                #    stop instantly and permanently. (This bit me live on 2026-08-07:
                #    DIR_STOP fired "ABOVE 57,820" while price was 57,770.)
                o = ohlc.get(t["underlying"], {})
                if t.get("probe", "LTP").upper() == "BREAKOUT":
                    probe = max(sp, o.get("high", sp)) if t["dir"] == "ABOVE" \
                        else min(sp, o.get("low", sp))
                else:
                    probe = sp
                if (probe >= t["level"]) if t["dir"] == "ABOVE" else (probe <= t["level"]):
                    t["fired"] = True; t["fired_at"] = now.strftime("%H:%M:%S")
                    t["fired_px"] = probe; t["ltp_at_fire"] = sp
                    dirty = True
                    say(f"TRIGGER {now:%H:%M} {t.get('name')} {t['underlying']} "
                        f"{t['dir']} {t['level']} @ {sp:,.2f} | day hi {o.get('high',0):,.0f} "
                        f"lo {o.get('low',0):,.0f} prev {o.get('close',0):,.0f} | VIX {vix} "
                        f"| ACTION {t.get('action','')}")
            if dirty:
                json.dump(tr, open(TRIG, "w"), indent=1)

        # ---- open-position management levels ----
        if BOOK.exists():
            try: b = json.load(open(BOOK))
            except Exception: b = {"positions": []}
            for p in b.get("positions", []):
                if p.get("status") != "OPEN": continue
                u = p["underlying"]; sp = spot.get(u)
                if sp is None: continue
                lvl = p.get("stop_level")
                if lvl:
                    bad = (sp <= lvl) if p["type"] == "CE" else (sp >= lvl)
                    if bad and not p.get("_stop_said"):
                        p["_stop_said"] = True
                        say(f"MANAGE {now:%H:%M} id={p['id']} {p['tsym']} STOP LEVEL HIT "
                            f"{u} {sp:,.2f} vs stop {lvl:,.2f} — cut it")
                        json.dump(b, open(BOOK, "w"), indent=1)
                # Favourable-extreme tracking drives the momentum-break exit, which is a
                # DIRECTIONAL concept: a long CE/PE has a spot direction that can stall.
                # A short vol leg has no favourable spot extreme — it wants no movement at
                # all — so it is managed by breakeven/wing levels instead.
                # Protective wings of a vol structure are LONG but are not directional
                # scalps either — the whole VOL sleeve is managed by breakeven/wing levels.
                if p.get("side") != "LONG" or p.get("sleeve") == "VOL":
                    continue
                key = str(p["id"])
                best = peak.get(key)
                # NB: evaluate `better` only when a peak exists — computing it first
                # crashed the monitor on the first tick of a new position.
                better = best is not None and (
                    sp > best if p["type"] == "CE" else sp < best)
                if best is None or better:
                    peak[key] = sp; p["_peak_spot"] = sp; p["_peak_at"] = now.strftime("%H:%M")
                else:
                    at = p.get("_peak_at")
                    if at:
                        hh, mm = int(at[:2]), int(at[3:])
                        stale = (now.hour * 60 + now.minute) - (hh * 60 + mm)
                        if stale >= 30 and not p.get("_stall_said"):
                            p["_stall_said"] = True
                            say(f"MANAGE {now:%H:%M} id={p['id']} {p['tsym']} MOMENTUM BREAK — "
                                f"no new extreme for {stale}min (peak {peak[key]:,.0f} at {at}, "
                                f"now {sp:,.2f}) — take it or justify holding")
                            json.dump(b, open(BOOK, "w"), indent=1)

        # ---- pulse ----
        if (now - last_pulse).total_seconds() >= PULSE_MIN * 60:
            last_pulse = now
            bits = []
            for n in ("SENSEX", "NIFTY", "BANKNIFTY"):
                if n not in spot: continue
                o = ohlc[n]
                bits.append(f"{n} {spot[n]:,.0f} ({100*(spot[n]-o['close'])/o['close']:+.2f}%, "
                            f"rng {100*(o['high']-o['low'])/o['open']:.2f}%)")
            say(f"PULSE {now:%H:%M} " + " | ".join(bits) + f" | VIX {vix}")
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
