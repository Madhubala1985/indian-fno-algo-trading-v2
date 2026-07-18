"""
turn_realflow_scorecard.py — Running REAL-FLOW scorecard for the deployed
hysteresis TURN detector + futures sleeve (started night 2026-07-16, sleeve
live day 1).

WHY THIS EXISTS
  The 07-15 night study graded the TURN detector FAIL on a 45-day *proxy*
  corpus (n=95, ex-best-day −₹43.5k, fires lag pivots 0–25 min) and demanded
  ≥15 days of REAL constituent-flow evidence before any live judgment. This
  script makes that judgment mechanical: run it after each session, it
  appends one row per TURN fire to logs/turn_scorecard_realflow.jsonl
  (idempotent) and prints the cumulative verdict.

PRE-REGISTERED 15-DAY CRITERIA (fixed 2026-07-16, before day-1 data was
appended — do not tune after seeing results):
  * Judged when the scorecard holds >= 15 distinct session dates.
  * PASS iff cumulative net P&L of ENTERED turn trades > 0
    AND ex-best-day net > 0.
  * Secondary diagnostics (reported, not gating): median |lag| of fires vs
    nearest 0.30% zigzag pivot within ±60 min; whipsaw rate (trades closed
    by FLOW_TURN_AGAINST at a loss / all entered).
  * Counterfactuals (margin-rejected / cooldown-suppressed fires) are
    recorded with spot-based approximate P&L but NEVER count toward the
    PASS metric — only real sleeve fills do.

TAG SEPARATION (added night 2026-07-17, sleeve signal-path day 1):
  The sleeve now fills two DIFFERENT hypotheses — tag="turn" (flow-turn
  reversals) and tag="signal" (upstream direction-signal hook, first fills
  2026-07-17). They are graded separately and never pooled. The TURN
  criteria above are unchanged and judge tag="turn" rows only. SIGNAL-path
  pre-registered criteria (fixed 2026-07-17 night, before day-2 data):
  judged at ≥15 distinct signal-fire dates; PASS iff cumulative net of
  ENTERED signal trades > 0 AND ex-best-day net > 0. Rows without a "tag"
  field (written day 1) are TURN rows.

SOURCES (all real session artifacts; no synthesis):
  logs/crossasset_shadow.jsonl  kind=turn        → turn fires
  logs/futures_sleeve.jsonl     signal_seen      → signal fires
  logs/futures_sleeve.jsonl     ENTRY/EXIT/entry_rejected → real fills
  logs/regime_shadow.jsonl      1m closes        → day profile + zigzag pivots

Usage:
  PYTHONPATH=. venv/bin/python scripts/turn_realflow_scorecard.py [YYYY-MM-DD]
  (default: today IST; safe to re-run — rows are keyed by fire ts.)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))

ZIGZAG_PCT = 0.0030          # same convention as study_hysteresis_turn_45d.py
PIVOT_MATCH_WINDOW_MIN = 60
SCORECARD = Path("logs/turn_scorecard_realflow.jsonl")

CROSSASSET = Path("logs/crossasset_shadow.jsonl")
SLEEVE = Path("logs/futures_sleeve.jsonl")
REGIME = Path("logs/regime_shadow.jsonl")


def _ist(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(_IST)


def load_fires(day: str) -> list[dict]:
    fires = []
    with CROSSASSET.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") != "turn":
                continue
            t = _ist(r["ts"])
            if t.date().isoformat() != day:
                continue
            fires.append({"underlying": r["underlying"], "ts": r["ts"],
                          "ist": t, "from": r.get("from"), "to": r.get("to"),
                          "rolling_flow_pct": r.get("rolling_flow_pct")})
    return sorted(fires, key=lambda x: x["ts"])


def load_sleeve(day: str) -> list[dict]:
    if not SLEEVE.exists():
        return []
    evts = []
    with SLEEVE.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "ts" not in r:
                continue
            if _ist(r["ts"]).date().isoformat() == day:
                evts.append(r)
    return evts


def load_closes(day: str) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, list] = {}
    with REGIME.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = _ist(r["ts"])
            if t.date().isoformat() != day:
                continue
            series.setdefault(r["underlying"], []).append((t, float(r["close"])))
    for s in series.values():
        s.sort()
    return series


def zigzag_pivots(bars: list[tuple[datetime, float]], pct: float = ZIGZAG_PCT):
    """Same pivot convention as the 45d study: 1m closes, pct reversal."""
    if len(bars) < 3:
        return []
    pivots = []
    anchor_i, anchor_px = 0, bars[0][1]
    direction = 0  # +1 seeking high, -1 seeking low, 0 unset
    ext_i, ext_px = 0, bars[0][1]
    for i, (_, px) in enumerate(bars):
        if direction >= 0 and px > ext_px or direction <= 0 and px < ext_px:
            if (direction >= 0 and px > ext_px) if direction != 0 else False:
                ext_i, ext_px = i, px
        if direction == 0:
            move = (px - anchor_px) / anchor_px
            if abs(move) >= pct:
                direction = 1 if move > 0 else -1
                ext_i, ext_px = i, px
        elif direction == 1:
            if px > ext_px:
                ext_i, ext_px = i, px
            elif (ext_px - px) / ext_px >= pct:
                pivots.append({"i": ext_i, "ts": bars[ext_i][0], "px": ext_px,
                               "kind": "HIGH"})
                direction, ext_i, ext_px = -1, i, px
        else:
            if px < ext_px:
                ext_i, ext_px = i, px
            elif (px - ext_px) / ext_px >= pct:
                pivots.append({"i": ext_i, "ts": bars[ext_i][0], "px": ext_px,
                               "kind": "LOW"})
                direction, ext_i, ext_px = 1, i, px
    return pivots


def nearest_pivot(pivots, when: datetime):
    best = None
    for p in pivots:
        d = (when - p["ts"]).total_seconds() / 60.0
        if abs(d) <= PIVOT_MATCH_WINDOW_MIN and (best is None or abs(d) < abs(best[1])):
            best = (p, d)
    return best


def spot_at(series, when: datetime):
    prev = None
    for t, px in series:
        if t > when:
            break
        prev = px
    return prev


def load_signal_fires(day: str, sleeve: list[dict]) -> list[dict]:
    """signal_seen events are the signal-path fires (logged once per bias fire)."""
    fires = []
    for e in sleeve:
        if e.get("event") != "signal_seen":
            continue
        t = _ist(e["ts"])
        fires.append({"underlying": e["underlying"], "ts": e["ts"], "ist": t,
                      "from": None, "to": e["dir"], "rolling_flow_pct": None})
    return sorted(fires, key=lambda x: x["ts"])


def build_rows(day: str) -> list[dict]:
    sleeve = load_sleeve(day)
    closes = load_closes(day)
    pivots = {u: zigzag_pivots(s) for u, s in closes.items()}

    entries = [e for e in sleeve if e.get("event") == "ENTRY"]
    exits = [e for e in sleeve if e.get("event") == "EXIT"]
    rejects = [e for e in sleeve if e.get("event") == "entry_rejected"]

    def fut_of(u):  # sleeve logs use <UNDERLYING>_FUT
        return f"{u}_FUT"

    rows = []
    tagged_fires = ([("turn", f) for f in load_fires(day)]
                    + [("signal", f) for f in load_signal_fires(day, sleeve)])
    for tag, fire in tagged_fires:
        u = fire["underlying"]
        t = fire["ist"]
        # match real sleeve action of the SAME tag within ±120 s of the fire
        outcome, fill = "NO_ACTION", None
        for e in entries:
            if (e["fut_sym"] == fut_of(u) and e.get("tag") == tag
                    and abs((_ist(e["ts"]) - t).total_seconds()) <= 120):
                outcome, fill = "ENTERED", e
                break
        if outcome == "NO_ACTION":
            for e in rejects:
                if (e["fut_sym"] == fut_of(u) and e.get("tag") == tag
                        and abs((_ist(e["ts"]) - t).total_seconds()) <= 120):
                    outcome, fill = "REJECTED", e
                    break
        if outcome == "NO_ACTION" and tag == "turn":
            # a fire that closed an opposite position (cooldown blocks re-entry)
            for e in exits:
                if (e["fut_sym"] == fut_of(u) and e.get("reason") == "FLOW_TURN_AGAINST"
                        and abs((_ist(e["ts"]) - t).total_seconds()) <= 120):
                    outcome = "EXIT_ONLY"
                    break

        net = exit_reason = None
        if outcome == "ENTERED":
            for e in exits:
                if e["fut_sym"] == fut_of(u) and _ist(e["ts"]) >= t and abs(e["entry"] - fill["px"]) < 1e-6:
                    net, exit_reason = e.get("net"), e.get("reason")
                    break

        piv = nearest_pivot(pivots.get(u, []), t)
        s = closes.get(u, [])
        # counterfactual: spot move fire→15:20 IST in the fire direction (diagnostic only)
        eod = None
        if s:
            cutoff = t.replace(hour=15, minute=20, second=0, microsecond=0)
            spot_fire, spot_eod = spot_at(s, t), spot_at(s, cutoff)
            if spot_fire and spot_eod:
                sign = 1 if fire["to"] == "LONG" else -1
                eod = round(sign * (spot_eod - spot_fire) / spot_fire * 100, 3)

        rows.append({
            "date": day, "underlying": u, "fire_ts": fire["ts"],
            "fire_ist": t.strftime("%H:%M:%S"), "tag": tag,
            "from": fire["from"], "to": fire["to"],
            "rolling_flow_pct": fire["rolling_flow_pct"],
            "outcome": outcome,
            "net": net, "exit_reason": exit_reason,
            "reject_reason": fill.get("reason") if outcome == "REJECTED" else None,
            "lag_min_vs_pivot": round(piv[1], 1) if piv else None,
            "pivot_kind": piv[0]["kind"] if piv else None,
            "pivot_ist": piv[0]["ts"].strftime("%H:%M") if piv else None,
            "spot_move_to_eod_pct": eod,
        })
    return rows


def append_rows(rows: list[dict]) -> int:
    existing = set()
    if SCORECARD.exists():
        with SCORECARD.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                    existing.add((r["date"], r["underlying"], r["fire_ts"],
                                  r.get("tag", "turn")))
                except (json.JSONDecodeError, KeyError):
                    continue
    added = 0
    with SCORECARD.open("a") as f:
        for r in rows:
            if (r["date"], r["underlying"], r["fire_ts"], r["tag"]) in existing:
                continue
            f.write(json.dumps(r) + "\n")
            added += 1
    return added


def summarize() -> None:
    rows = []
    with SCORECARD.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for tag in ("turn", "signal"):
        trows = [r for r in rows if r.get("tag", "turn") == tag]
        if not trows:
            print(f"── REAL-FLOW {tag.upper()} SCORECARD ── no rows yet")
            continue
        days = sorted({r["date"] for r in trows})
        entered = [r for r in trows if r["outcome"] == "ENTERED" and r["net"] is not None]
        by_day: dict[str, float] = {}
        for r in entered:
            by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["net"]
        total = sum(by_day.values())
        ex_best = total - max(by_day.values()) if by_day else 0.0
        lags = sorted(abs(r["lag_min_vs_pivot"]) for r in trows
                      if r["lag_min_vs_pivot"] is not None)
        med_lag = lags[len(lags) // 2] if lags else None
        whip = [r for r in entered
                if r["exit_reason"] == "FLOW_TURN_AGAINST" and r["net"] < 0]
        print(f"── REAL-FLOW {tag.upper()} SCORECARD ── days={len(days)}/15  "
              f"fires={len(trows)}  entered={len(entered)}")
        print(f"   net ₹{total:+,.0f}   ex-best-day ₹{ex_best:+,.0f}   "
              f"whipsaw {len(whip)}/{len(entered)}   median |lag| "
              f"{med_lag if med_lag is not None else 'n/a'} min")
        if len(days) >= 15:
            verdict = "PASS" if (total > 0 and ex_best > 0) else "FAIL"
            print(f"   ≥15-day PRE-REGISTERED VERDICT: {verdict}")
        else:
            print(f"   verdict pending ({15 - len(days)} more session days needed)")


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(_IST).date().isoformat()
    rows = build_rows(day)
    if not rows:
        print(f"no TURN fires found for {day} — nothing appended")
    else:
        added = append_rows(rows)
        print(f"{day}: {len(rows)} fires, {added} new rows appended")
        for r in rows:
            print(f"  {r['fire_ist']} {r['underlying']:<10} {r['from']}→{r['to']:<6} "
                  f"{r['outcome']:<10} net={r['net']} lag={r['lag_min_vs_pivot']} "
                  f"({r['pivot_kind']}@{r['pivot_ist']}) eodΔ={r['spot_move_to_eod_pct']}%")
    if SCORECARD.exists():
        summarize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
