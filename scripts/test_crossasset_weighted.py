"""Unit tests — weighted constituent-flow engine v2 (batch 1, 2026-07-15).

Pure, no network: a fake feed serves scripted LTPs. Verifies the weighted
math, the legacy-continuity field, breadth, the CROSSASSET_WEIGHTED=0 revert,
missing-LTP handling, the batched-fetch path, and TURN detection on the
weighted series.

Run: PYTHONPATH=. python3 scripts/test_crossasset_weighted.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["CROSSASSET_WEIGHTED"] = "1"

from platform_core.data.live.crossasset_shadow import (  # noqa: E402
    _CONSTITUENTS, _CONSTITUENTS_W, CrossAssetShadow, all_basket_symbols,
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


class FakeFeeds:
    """Scripted LTPs; counts per-symbol vs batch calls."""

    def __init__(self, prices: dict) -> None:
        self.prices = dict(prices)
        self.single_calls = 0
        self.batch_calls = 0

    def constituent_ltp(self, symbol):
        self.single_calls += 1
        return self.prices.get(symbol)

    def constituent_ltps(self, symbols):
        self.batch_calls += 1
        return {s: self.prices[s] for s in symbols if self.prices.get(s)}

    def gift_nifty_gap_pct(self, session_date):
        return None

    def overnight_sp_pct(self, session_date):
        return None

    def brent_momentum_pct(self, session_date, days=2):
        return None

    def india_vix(self):
        return None


def shadow_with(prices: dict, out: Path) -> tuple[CrossAssetShadow, FakeFeeds]:
    feeds = FakeFeeds(prices)
    return CrossAssetShadow(feeds=feeds, out_path=str(out)), feeds


def flow(sh, und="NIFTY", *, is_open=False, snap=None):
    return sh.intraday_flow(underlying=und, timestamp=datetime.now(timezone.utc),
                            is_session_open=is_open, ltp_snapshot=snap)


NIFTY8 = dict(_CONSTITUENTS_W["NIFTY"])
SYMS8 = list(NIFTY8)

with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "xa.jsonl"

    print("[1] weighted math: avg_flow_pct == Σ(w·move)/Σw (hand-computed)")
    base = {s: 100.0 for s in SYMS8}
    sh, feeds = shadow_with(base, out)
    flow(sh, is_open=True)                       # reference snapshot at open
    # scripted moves (%): HDFCBANK +1, ICICIBANK -0.5, RELIANCE +0.2, rest 0
    feeds.prices.update({"HDFCBANK": 101.0, "ICICIBANK": 99.5, "RELIANCE": 100.2})
    rec = flow(sh)
    wsum = sum(NIFTY8.values())
    expected = round((NIFTY8["HDFCBANK"] * 1.0 + NIFTY8["ICICIBANK"] * -0.5
                      + NIFTY8["RELIANCE"] * 0.2) / wsum, 3)
    check("weighted avg exact", rec["avg_flow_pct"] == expected,
          f"{rec['avg_flow_pct']} != {expected}")
    check("all 8 constituents in moves", len(rec["constituent_moves_pct"]) == 8)
    check("weights_mode tagged", rec.get("weights_mode") == "weighted_top8")

    print("[2] legacy continuity: avg_flow_pct_eq == equal mean of the old top-3")
    eq_expected = round((1.0 + 0.2 + -0.5) / 3, 3)   # HDFCBANK, RELIANCE, ICICIBANK
    check("eq value matches old formula", rec["avg_flow_pct_eq"] == eq_expected,
          f"{rec['avg_flow_pct_eq']} != {eq_expected}")

    print("[3] breadth: weighted fraction agreeing with the flow sign")
    # avg > 0; agreeing = HDFCBANK + RELIANCE (movers >0). Flat (0.0) disagrees.
    agree_w = NIFTY8["HDFCBANK"] + NIFTY8["RELIANCE"]
    b_expected = round(agree_w / wsum, 3)
    check("breadth exact", rec["breadth"] == b_expected,
          f"{rec['breadth']} != {b_expected}")

    print("[4] missing LTPs drop from numerator AND denominator")
    sh2, feeds2 = shadow_with({s: 100.0 for s in SYMS8}, out)
    flow(sh2, is_open=True)
    feeds2.prices = {"HDFCBANK": 102.0, "ICICIBANK": 101.0}   # only 2 live
    rec2 = flow(sh2)
    w2 = NIFTY8["HDFCBANK"] + NIFTY8["ICICIBANK"]
    exp2 = round((NIFTY8["HDFCBANK"] * 2.0 + NIFTY8["ICICIBANK"] * 1.0) / w2, 3)
    check("renormalized over live names", rec2["avg_flow_pct"] == exp2,
          f"{rec2['avg_flow_pct']} != {exp2}")
    check("dead names logged as None",
          rec2["constituent_moves_pct"]["TCS"] is None)
    check("breadth 1.0 when all live names agree", rec2["breadth"] == 1.0)

    print("[5] CROSSASSET_WEIGHTED=0 reverts to the legacy top-3 equal basket")
    os.environ["CROSSASSET_WEIGHTED"] = "0"
    sh3, feeds3 = shadow_with({s: 100.0 for s in SYMS8}, out)
    flow(sh3, is_open=True)
    feeds3.prices.update({"HDFCBANK": 101.0, "ICICIBANK": 99.5, "RELIANCE": 100.2})
    rec3 = flow(sh3)
    check("legacy basket only", set(rec3["constituent_moves_pct"]) ==
          set(_CONSTITUENTS["NIFTY"]), str(rec3["constituent_moves_pct"]))
    check("avg == old equal mean", rec3["avg_flow_pct"] == eq_expected,
          f"{rec3['avg_flow_pct']} != {eq_expected}")
    check("eq field self-consistent", rec3["avg_flow_pct_eq"] == rec3["avg_flow_pct"])
    check("mode tagged eq_top3", rec3.get("weights_mode") == "eq_top3")
    os.environ["CROSSASSET_WEIGHTED"] = "1"

    print("[6] batched fetch: snapshot path does zero per-symbol calls")
    sh4, feeds4 = shadow_with({}, out)
    snap = {s: 100.0 for s in SYMS8}
    flow(sh4, is_open=True, snap=snap)
    snap2 = dict(snap, HDFCBANK=101.0)
    rec4 = flow(sh4, snap=snap2)
    check("no per-symbol calls with snapshot", feeds4.single_calls == 0,
          str(feeds4.single_calls))
    check("no feed batch calls with snapshot", feeds4.batch_calls == 0)
    check("snapshot math correct",
          rec4["avg_flow_pct"] == round(NIFTY8["HDFCBANK"] * 1.0 / wsum, 3))

    print("[7] without snapshot, ONE batch call — never a per-symbol loop")
    sh5, feeds5 = shadow_with({s: 100.0 for s in SYMS8}, out)
    flow(sh5, is_open=True)
    check("batch used", feeds5.batch_calls == 1, str(feeds5.batch_calls))
    check("no per-symbol calls", feeds5.single_calls == 0, str(feeds5.single_calls))

    print("[8] all_basket_symbols: union across underlyings, ≤24, no dupes")
    syms = all_basket_symbols(("NIFTY", "SENSEX", "BANKNIFTY"))
    check("no duplicates", len(syms) == len(set(syms)))
    check("union covers every basket",
          set(syms) >= {s for u in ("NIFTY", "SENSEX", "BANKNIFTY")
                        for s, _ in _CONSTITUENTS_W[u]})
    check("one batch is ~24 symbols max", len(syms) <= 24, str(len(syms)))

    print("[9] TURN detector still fires on the weighted series")
    sh6, feeds6 = shadow_with({s: 100.0 for s in SYMS8}, out)
    flow(sh6, und="BANKNIFTY", is_open=True)
    bn = dict(_CONSTITUENTS_W["BANKNIFTY"])
    for _ in range(6):                                   # negative rolling flow
        feeds6.prices = {s: 99.0 for s in bn}
        flow(sh6, und="BANKNIFTY")
    for _ in range(8):                                   # decisive flip positive
        feeds6.prices = {s: 101.5 for s in bn}
        flow(sh6, und="BANKNIFTY")
    turns = [json.loads(l) for l in out.read_text().splitlines()
             if json.loads(l).get("kind") == "turn"]
    check("turn logged", any(t["underlying"] == "BANKNIFTY" and t["to"] == "LONG"
                             for t in turns), str(turns[-1:]))

    print("[10] weight sanity: documented baskets are top-8, positive, banks-only BN")
    for und, pairs in _CONSTITUENTS_W.items():
        check(f"{und}: 8 names", len(pairs) == 8)
        check(f"{und}: weights positive+descending",
              all(w > 0 for _, w in pairs)
              and all(pairs[i][1] >= pairs[i + 1][1] for i in range(len(pairs) - 1)))
    check("NIFTY/SENSEX weights differ (separate tables)",
          dict(_CONSTITUENTS_W["NIFTY"]) != dict(_CONSTITUENTS_W["SENSEX"]))
    check("BANKNIFTY basket is banks-only",
          all(s in ("HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
                    "BOB", "PNB", "CANBK", "INDUSINDBK", "IDFCFIRSTB",
                    "FEDERALBNK", "AUBANK")
              for s, _ in _CONSTITUENTS_W["BANKNIFTY"]))

print(f"\n{'ALL PASS' if FAIL == 0 else f'{FAIL} FAILURES'}  ({PASS} checks)")
sys.exit(0 if FAIL == 0 else 1)
