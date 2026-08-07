#!/usr/bin/env python3
"""claude_trigger_watch.py — mechanical eyes for Claude's live paper desk. 2026-08-07.

Runs every minute from cron. Records a 1m tape and fires when a pre-armed price
trigger breaks, so Claude sees exactly WHAT happened and WHEN between its (sparse)
decision wake-ups. Costs no Claude credits — this is a data collector, not an agent.
It NEVER trades; it only observes and logs.

  triggers file : data/claude_trades/triggers_<date>.json
  tape          : data/claude_trades/tape_<date>.jsonl
  fired events  : data/claude_trades/fired_<date>.jsonl
"""
from __future__ import annotations
import datetime as _dt, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

IDX = {"NIFTY": ("NSE", "NIFTY 50"), "BANKNIFTY": ("NSE", "NIFTY BANK"),
       "SENSEX": ("BSE", "SENSEX")}
D = _dt.datetime.now(IST).strftime("%Y-%m-%d")
TRIG = Path(f"data/claude_trades/triggers_{D}.json")
TAPE = Path(f"data/claude_trades/tape_{D}.jsonl")
FIRED = Path(f"data/claude_trades/fired_{D}.jsonl")


def main() -> int:
    now = _dt.datetime.now(IST)
    if not (915 <= now.hour * 100 + now.minute <= 1532) or now.weekday() > 4:
        return 0
    from kiteconnect import KiteConnect
    c = json.load(open("configs/kite_config.json"))
    k = KiteConnect(api_key=c["api_key"]); k.set_access_token(c["access_token"])
    q = k.quote([f"{e}:{s}" for e, s in IDX.values()] + ["NSE:INDIA VIX"])

    row = {"ts": now.strftime("%H:%M:%S")}
    spots = {}
    for nm, (e, s) in IDX.items():
        v = q.get(f"{e}:{s}")
        if not v: continue
        spots[nm] = v["last_price"]
        o = v["ohlc"]
        row[nm] = {"ltp": v["last_price"], "hi": o["high"], "lo": o["low"],
                   "op": o["open"], "prev": o["close"]}
    vx = q.get("NSE:INDIA VIX")
    if vx: row["VIX"] = vx["last_price"]
    TAPE.parent.mkdir(parents=True, exist_ok=True)
    with TAPE.open("a") as f: f.write(json.dumps(row) + "\n")

    if not TRIG.exists(): return 0
    try: trig = json.load(open(TRIG))
    except Exception: return 0
    changed = False
    for t in trig.get("triggers", []):
        if t.get("fired"): continue
        sp = spots.get(t["underlying"])
        if sp is None: continue
        hit = (sp >= t["level"]) if t["dir"] == "ABOVE" else (sp <= t["level"])
        if not hit: continue
        t["fired"] = True
        t["fired_at"] = now.strftime("%H:%M:%S")
        t["fired_px"] = sp
        changed = True
        with FIRED.open("a") as f:
            f.write(json.dumps({"ts": now.strftime("%H:%M:%S"), "name": t.get("name"),
                                "underlying": t["underlying"], "dir": t["dir"],
                                "level": t["level"], "px": sp,
                                "action": t.get("action", "")}) + "\n")
        print(f"[FIRED] {t.get('name')} {t['underlying']} {t['dir']} {t['level']} @ {sp}")
    if changed:
        json.dump(trig, open(TRIG, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
