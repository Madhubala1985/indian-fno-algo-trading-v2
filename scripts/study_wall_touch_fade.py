#!/usr/bin/env python3
"""H8 (night 2026-07-19) — unconditional wall-touch fade on the H7 touch corpus.

Pre-registered in logs/night_explorer_2026-07-19.md before computation.
Trade: SHORT spot at CE-wall touch from below, LONG at PE-wall touch from above;
exit at the recorded outcome mark (+60 min or 15:20 IST). Costs 0.03% round-trip.
PASS-as-hypothesis iff mean net > 0, win >= 60%, positive ex-best day, both sides
non-negative. RESULT: FAIL (mean -0.040%, win 38%) — rejection frequency does not
monetize; breaks are fat-tailed while rejects barely retreat (the wall pins price).
"""
import json
from collections import defaultdict

COST = 0.0003
rows = [json.loads(l) for l in open("logs/study_oi_wall_defend.jsonl")]

evs = []
for r in rows:
    t, o = r["touch_close"], r["outcome_close"]
    ret = (t - o) / t if r["side"] == "CE" else (o - t) / t
    evs.append({**r, "ret": ret - COST})

for side in ("CE", "PE"):
    sub = [e["ret"] for e in evs if e["side"] == side]
    print(f"{side}: n={len(sub)} mean={sum(sub)/len(sub)*100:+.3f}% "
          f"win={sum(1 for x in sub if x > 0)}/{len(sub)}")

by_day = defaultdict(float)
for e in evs:
    by_day[e["day"]] += e["ret"]
tot = sum(e["ret"] for e in evs)
best = max(by_day, key=by_day.get)
n = len(evs)
win = sum(1 for e in evs if e["ret"] > 0)
print(f"ALL: n={n} mean={tot/n*100:+.3f}% win={win}/{n}={win/n:.0%} "
      f"total={tot*100:+.2f}% best-day {best} {by_day[best]*100:+.2f}% "
      f"ex-best {(tot-by_day[best])*100:+.2f}%")
for s in ("DEFEND", "COVER", "neutral"):
    sub = [e["ret"] for e in evs if e["signal"] == s]
    if sub:
        print(f"  {s}: n={len(sub)} mean={sum(sub)/len(sub)*100:+.3f}%")
