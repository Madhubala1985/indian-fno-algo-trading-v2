"""Unit tests — BiasInvalidationEngine lifecycle (pure, no I/O).

Run: PYTHONPATH=. python3 scripts/test_bias_invalidation.py
"""
from __future__ import annotations

import os
import sys

os.environ["BIAS_INVALIDATION"] = "1"

from platform_core.data.live.bias_invalidation import (  # noqa: E402
    BiasInvalidationEngine, NEUTRALIZE_SCORE, FLIP_SCORE,
)

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def bar(engine, i, *, bias="SHORT", close, orh=100.0, orl=99.0, opp=0, same=0,
        flow=None, day="2026-07-13", und="T", op=None):
    return engine.evaluate(
        underlying=und, day=day, bar_index=i, raw_bias=bias, close=close,
        bar_open=op if op is not None else close, or_high=orh, or_low=orl,
        or_ready=True, opposite_breaks=opp, same_breaks=same, flow=flow)


print("[1] confirm: agreeing price never transitions")
e = BiasInvalidationEngine()
for i in range(120):
    eff, cap, info = bar(e, i, close=98.5)   # SHORT bias, price below OR low
check("stays CONFIRM", info["stage"] == "CONFIRM", str(info))
check("effective == raw", eff == "SHORT")
check("no confidence cap", cap is None)

print("[2] neutralize: sustained trade beyond opposite OR extreme")
e = BiasInvalidationEngine()
stages = []
for i in range(int(NEUTRALIZE_SCORE) + 3):
    eff, cap, info = bar(e, i, close=100.4)  # SHORT bias, price ABOVE OR high
    stages.append(info["stage"])
check("reaches NEUTRALIZED", info["stage"] == "NEUTRALIZED", str(stages[-3:]))
check("effective NEUTRAL", eff == "NEUTRAL")
check("thesis dead for SHORT position",
      e.position_direction_invalidated("T", "SHORT"))

print("[3] chop forgiveness: oscillation never accumulates")
e = BiasInvalidationEngine()
for i in range(200):
    close = 100.4 if i % 2 == 0 else 99.5    # alternate outside/inside
    eff, cap, info = bar(e, i, close=close)
check("chop stays CONFIRM", info["stage"] == "CONFIRM", str(info))

print("[4] flip: contradiction + opposite structure break")
e = BiasInvalidationEngine()
transitioned_at = None
for i in range(80):
    opp = 1 if i in (5, 12) else 0           # two opposite BOS along the way
    eff, cap, info = bar(e, i, close=100.4, opp=opp)
    if info.get("transition", {}).get("to") == "FLIPPED" and transitioned_at is None:
        transitioned_at = i
check("flips", info["stage"] == "FLIPPED", str(info))
check("effective is LONG (opposite of SHORT)", eff == "LONG")
check("confidence capped", cap is not None and cap <= 0.65, str(cap))
check("flip within 30 bars given breaks", transitioned_at is not None and transitioned_at <= 30,
      str(transitioned_at))
check("thesis dead for SHORT position",
      e.position_direction_invalidated("T", "SHORT"))
check("LONG position fine", not e.position_direction_invalidated("T", "LONG"))

print("[5] flow acceleration: opposing flow speeds the clock")
e_noflow = BiasInvalidationEngine()
e_flow = BiasInvalidationEngine()
t_noflow = t_flow = None
fl = {"flow_bias": "LONG", "avg_flow_pct": 0.2, "mom": 0.05}
for i in range(200):
    _, _, i1 = bar(e_noflow, i, close=100.4, opp=1 if i == 3 else 0)
    _, _, i2 = bar(e_flow, i, close=100.4, opp=1 if i == 3 else 0, flow=fl)
    if i1.get("transition", {}).get("to") == "FLIPPED" and t_noflow is None:
        t_noflow = i
    if i2.get("transition", {}).get("to") == "FLIPPED" and t_flow is None:
        t_flow = i
check("both flip eventually", t_noflow is not None and t_flow is not None,
      f"{t_noflow} {t_flow}")
check("flow flips faster", t_flow < t_noflow, f"flow={t_flow} noflow={t_noflow}")

print("[6] humility: counter-evidence de-escalates a flip; dwell before re-arm")
e = BiasInvalidationEngine()
for i in range(60):
    eff, cap, info = bar(e, i, close=100.4, opp=1 if i == 3 else 0)
assert info["stage"] == "FLIPPED"
deesc_bar = rearm_bar = None
neutral_effs = []
for i in range(60, 60 + int(NEUTRALIZE_SCORE) + 60):
    # price collapses back BELOW OR low: contra vs the flipped LONG (and
    # agreeing with the original SHORT prior)
    eff, cap, info = bar(e, i, close=98.5)
    tr = info.get("transition", {}).get("to")
    if tr == "NEUTRALIZED" and deesc_bar is None:
        deesc_bar = i
        check("flipped-direction position thesis-dead at de-escalation",
              e.position_direction_invalidated("T", "LONG"))
        neutral_effs.append(eff)
    elif tr == "CONFIRM" and deesc_bar is not None and rearm_bar is None:
        rearm_bar = i
check("de-escalates to NEUTRALIZED first", deesc_bar is not None)
check("effective NEUTRAL at de-escalation",
      neutral_effs and neutral_effs[0] == "NEUTRAL", str(neutral_effs))
check("re-arms the (now market-agreeing) prior only AFTER the dwell",
      rearm_bar is not None and rearm_bar - deesc_bar >= 15,
      f"deesc={deesc_bar} rearm={rearm_bar}")

print("[7] MCB catches up with flip → engine steps aside")
e = BiasInvalidationEngine()
for i in range(60):
    eff, cap, info = bar(e, i, close=100.4, opp=1 if i == 3 else 0)
assert info["stage"] == "FLIPPED" and eff == "LONG"
eff, cap, info = bar(e, 61, bias="LONG", close=100.4)
check("resets to CONFIRM", info["stage"] == "CONFIRM", str(info))
check("effective follows raw LONG", eff == "LONG")
check("no cap after handover", cap is None)

print("[8] raw bias change resets evidence")
e = BiasInvalidationEngine()
for i in range(10):
    bar(e, i, close=100.4)
eff, cap, info = bar(e, 10, bias="LONG", close=100.4)
check("reset on raw change", info["stage"] == "CONFIRM" and info["score"] <= 1.0, str(info))

print("[9] day roll resets state")
e = BiasInvalidationEngine()
for i in range(int(NEUTRALIZE_SCORE) + 2):
    eff, cap, info = bar(e, i, close=100.4)
assert info["stage"] == "NEUTRALIZED"
eff, cap, info = bar(e, 0, close=100.4, day="2026-07-14")
check("fresh day starts CONFIRM", info["stage"] == "CONFIRM", str(info))

print("[10] NEUTRAL raw bias: engine silent")
e = BiasInvalidationEngine()
eff, cap, info = bar(e, 0, bias="NEUTRAL", close=100.4)
check("returns NEUTRAL untouched", eff == "NEUTRAL" and cap is None)

print("[11] kill switch")
os.environ["BIAS_INVALIDATION"] = "0"
e = BiasInvalidationEngine()
for i in range(100):
    eff, cap, info = bar(e, i, close=100.4, opp=1)
check("disabled → raw bias always", eff == "SHORT" and cap is None)
check("disabled → never invalidates positions",
      not e.position_direction_invalidated("T", "SHORT"))
os.environ["BIAS_INVALIDATION"] = "1"

print("[12] never raises: garbage inputs fail open")
e = BiasInvalidationEngine()
eff, cap, info = e.evaluate(
    underlying="T", day="x", bar_index=0, raw_bias="SHORT", close=float("nan"),
    bar_open=None, or_high="bad", or_low=None, or_ready=True,
    opposite_breaks="x", same_breaks=None, flow={"avg_flow_pct": "junk"})
check("fails open to raw bias", eff == "SHORT", str(info))

print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILURES'}  ({PASS} checks)")
sys.exit(0 if FAIL == 0 else 1)
