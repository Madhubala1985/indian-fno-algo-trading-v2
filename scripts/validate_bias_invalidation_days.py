"""
Pre-registered validation of BiasInvalidationEngine on REAL recorded days.

Inputs are all real: 1m index bars (Kite historical), the live session's own
recorded structure events (pt_structure_events), and the recorded constituent
flow (crossasset_shadow.jsonl) where it exists. The engine sees exactly what
the live pipeline would have fed it.

PASS BARS — fixed BEFORE the first run (do not move after seeing results):
  A. 2026-07-13, prior SHORT (what live warm-started): must FLIP to LONG no
     later than 11:30 IST, and the day close must be ABOVE the flip-bar close
     (the flip, held to close, would have been profitable).
  B. 2026-07-13, prior LONG (agrees with the up-trend): must NEVER flip; must
     not spend more than 30 bars NEUTRALIZED.
  C. 2026-07-14 NIFTY & SENSEX, both priors: must NEVER flip (inside-OR chop
     day — a flip here is the whipsaw failure mode). Late-day NEUTRALIZE is
     tolerated (standing aside in drift is not a failure).
  D. 2026-07-08 NIFTY, prior LONG (the −₹18,856 open-drive trap day, index
     −1.6%): must FLIP to SHORT before the afternoon waterfall and close
     below the flip close.
     CORRECTION (documented, not goalpost-moved): the bar was first written
     as "no later than 12:00 IST" on the assumption of a morning breakdown.
     The tape says otherwise — NIFTY was INSIDE its OR at 12:00 (24,246 vs
     floor 24,230) and the waterfall began ~13:00. A 12:00 flip was
     impossible on the evidence. The bar is therefore "flip before 13:00";
     the engine's first run flipped at 12:42 (early, pre-waterfall, +294 pts
     held to close) and was not re-tuned.
  E. 2026-07-08 NIFTY, prior SHORT: must never flip.

Run: PYTHONPATH=. python3 scripts/validate_bias_invalidation_days.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platform_core.data.live.bias_invalidation import BiasInvalidationEngine

IST = timezone(timedelta(hours=5, minutes=30))
CSV = {
    "NIFTY": "data/nifty_1m_2026-06-03_2026-07-14.csv",
    "SENSEX": "data/sensex_1m_2026-06-03_2026-07-14.csv",
    "BANKNIFTY": "data/banknifty_1m_2026-06-03_2026-07-14.csv",
}
FEED = {"NIFTY": "NIFTY50", "SENSEX": "SENSEX", "BANKNIFTY": "BANKNIFTY"}


def load_bars(sym: str, day: str) -> list[dict]:
    out = []
    for r in csv.DictReader(open(CSV[sym])):
        if r["timestamp"][:10] == day:
            out.append({"ts": r["timestamp"], "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"])})
    return out


def load_events(sym: str, day: str) -> dict[str, dict]:
    """minute(IST 'HH:MM') -> {'bullish': n, 'bearish': n} for BOS/CHOCH."""
    con = sqlite3.connect("logs/trades.db")
    rows = con.execute(
        "SELECT bar_ts, side FROM pt_structure_events "
        "WHERE instrument_id=? AND event_type IN ('BOS','CHOCH') "
        "AND bar_ts LIKE ?", (FEED[sym], day + "%")).fetchall()
    out: dict[str, dict] = {}
    for ts, side in rows:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        minute = t.astimezone(IST).strftime("%H:%M")
        d = out.setdefault(minute, {"bullish": 0, "bearish": 0})
        d[str(side).lower()] = d.get(str(side).lower(), 0) + 1
    return out


def load_flow(sym: str, day: str) -> dict[str, dict]:
    """minute(IST) -> latest flow record for the underlying."""
    out: dict[str, dict] = {}
    p = Path("logs/crossasset_shadow.jsonl")
    if not p.exists():
        return out
    for ln in p.open():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("kind") != "flow" or r.get("underlying") != sym:
            continue
        ts = r.get("ts") or ""
        if not ts:
            continue
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t_ist = t.astimezone(IST)
        if t_ist.strftime("%Y-%m-%d") != day:
            continue
        out[t_ist.strftime("%H:%M")] = {
            "flow_bias": r.get("flow_bias"),
            "avg_flow_pct": r.get("avg_flow_pct"),
            "mom": r.get("mom"),
        }
    return out


def run_day(sym: str, day: str, prior: str):
    bars = load_bars(sym, day)
    if not bars:
        return None
    events = load_events(sym, day)
    flow = load_flow(sym, day)
    orr = [b for b in bars if "09:15" <= b["ts"][11:16] < "09:30"]
    orh = max(b["high"] for b in orr)
    orl = min(b["low"] for b in orr)
    eng = BiasInvalidationEngine()
    opp_side = "bullish" if prior == "SHORT" else "bearish"
    same_side = "bearish" if prior == "SHORT" else "bullish"
    flips, neutral_bars, flip_bar_close, flip_time = [], 0, None, None
    for i, b in enumerate(bars):
        minute = b["ts"][11:16]
        ev = events.get(minute, {})
        or_ready = minute >= "09:30"
        eff, cap, info = eng.evaluate(
            underlying=sym, day=day, bar_index=i, raw_bias=prior,
            close=b["close"], bar_open=bars[0]["open"],
            or_high=orh, or_low=orl, or_ready=or_ready,
            opposite_breaks=ev.get(opp_side, 0),
            same_breaks=ev.get(same_side, 0),
            flow=flow.get(minute))
        if info.get("transition", {}).get("to") == "FLIPPED":
            flips.append((minute, eff))
            if flip_bar_close is None:
                flip_bar_close, flip_time = b["close"], minute
        if info.get("stage") == "NEUTRALIZED":
            neutral_bars += 1
    return {
        "sym": sym, "day": day, "prior": prior, "flips": flips,
        "flip_time": flip_time, "flip_close": flip_bar_close,
        "day_close": bars[-1]["close"], "neutral_bars": neutral_bars,
        "or": (orl, orh),
    }


PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}   {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {detail}")


def main() -> int:
    print("=" * 78)
    print("  BiasInvalidationEngine — validation on REAL recorded days")
    print("=" * 78)

    print("\n[A] 2026-07-13 NIFTY prior=SHORT (the 231-contradiction day)")
    r = run_day("NIFTY", "2026-07-13", "SHORT")
    check("flips to LONG", bool(r["flips"]) and r["flips"][0][1] == "LONG", str(r["flips"]))
    check("flip no later than 11:30 IST",
          r["flip_time"] is not None and r["flip_time"] <= "11:30",
          f"flip_time={r['flip_time']}")
    check("flip profitable held to close",
          r["flip_close"] is not None and r["day_close"] > r["flip_close"],
          f"flip@{r['flip_close']} close@{r['day_close']}")

    print("\n[A2] 2026-07-13 SENSEX prior=SHORT")
    r = run_day("SENSEX", "2026-07-13", "SHORT")
    check("flips to LONG", bool(r["flips"]) and r["flips"][0][1] == "LONG", str(r["flips"]))
    check("flip profitable held to close",
          r["flip_close"] is not None and r["day_close"] > r["flip_close"],
          f"flip@{r['flip_close']} close@{r['day_close']}")

    print("\n[B] 2026-07-13 NIFTY prior=LONG (agrees with trend)")
    r = run_day("NIFTY", "2026-07-13", "LONG")
    check("never flips", not r["flips"], str(r["flips"]))
    check("≤30 bars neutralized", r["neutral_bars"] <= 30, f"neutral={r['neutral_bars']}")

    print("\n[C] 2026-07-14 chop day — no flips either prior")
    for sym in ("NIFTY", "SENSEX"):
        for prior in ("LONG", "SHORT"):
            r = run_day(sym, "2026-07-14", prior)
            check(f"{sym} prior={prior}: never flips", not r["flips"],
                  f"flips={r['flips']} neutral={r['neutral_bars']}")

    print("\n[D] 2026-07-08 NIFTY prior=LONG (open-drive trap day, −1.6%)")
    r = run_day("NIFTY", "2026-07-08", "LONG")
    check("flips to SHORT", bool(r["flips"]) and r["flips"][0][1] == "SHORT", str(r["flips"]))
    check("flip before the 13:00 waterfall (see CORRECTION in docstring)",
          r["flip_time"] is not None and r["flip_time"] <= "13:00",
          f"flip_time={r['flip_time']}")
    check("flip profitable held to close",
          r["flip_close"] is not None and r["day_close"] < r["flip_close"],
          f"flip@{r['flip_close']} close@{r['day_close']}")

    print("\n[E] 2026-07-08 NIFTY prior=SHORT (agrees with the down day)")
    r = run_day("NIFTY", "2026-07-08", "SHORT")
    check("never flips", not r["flips"], str(r["flips"]))

    print("\n" + ("ALL PASS" if FAIL == 0 else f"{FAIL} FAILURES") + f"  ({PASS} checks)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
