"""
analyze_gate_calibration.py
──────────────────────────────────────────────────────────────────────
Post-market gate calibration analysis.

Reads logs/strike_selection_audit.jsonl and logs/trades.db.

Reports per underlying:
  - score_margin distribution (p50/p75/p90/p95/max)
  - oi_support distribution (same)
  - How many bars would pass at each threshold relaxation level
  - Whether lower thresholds would have allowed trades
  - Whether those allowed trades had strong or weak other factors
  - Final recommendation: KEEP_THRESHOLDS / INVESTIGATE_MORE /
                          PROPOSE_BACKTEST_THRESHOLD_CHANGE

This script DOES NOT change any code thresholds.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median, quantiles, mean
from typing import Any

AUDIT_PATH = Path("logs/strike_selection_audit.jsonl")
DB_PATH    = Path("logs/trades.db")

# Current live gate thresholds (reference only — not modified)
_LIVE_SCORE_MARGIN_THRESHOLD = 0.05
_LIVE_OI_SUPPORT_THRESHOLD   = 0.10

# Threshold candidates to test
_MARGIN_CANDIDATES  = [0.05, 0.04, 0.03, 0.02, 0.01]
_OI_CANDIDATES      = [0.10, 0.08, 0.06, 0.05, 0.03]


def _load_audit() -> list[dict]:
    if not AUDIT_PATH.exists():
        print(f"[WARN] {AUDIT_PATH} not found")
        return []
    records: list[dict] = []
    with AUDIT_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return [r for r in records if r.get("mode") != "shadow"]


def _load_decisions() -> list[dict]:
    """Load decision rows from trades.db pt_decisions table (if present)."""
    if not DB_PATH.exists():
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT * FROM pt_decisions ORDER BY ts"
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()
    except Exception:
        return []


def _percentile(data: list[float], p: float) -> float:
    """Simple percentile by interpolation."""
    if not data:
        return float("nan")
    data = sorted(data)
    k = (len(data) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def _analyze_underlying(sym: str, recs: list[dict]) -> str:
    """Analyze one underlying. Returns recommendation string."""
    print(f"\n{'═' * 70}")
    print(f"  {sym}  ({len(recs)} audit records)")
    print(f"{'═' * 70}")

    margins    : list[float] = []
    oi_supps   : list[float] = []
    all_delta_f: list[float] = []
    all_iv_att : list[float] = []
    all_liq    : list[float] = []
    all_theta  : list[float] = []

    for r in recs:
        sm = r.get("score_margin")
        if sm is not None:
            margins.append(sm)

        fs = r.get("factor_scores") or {}
        oi = fs.get("oi_support")
        if oi is not None:
            oi_supps.append(oi)

        # Track quality of "other" factors
        if fs:
            all_delta_f.append(fs.get("delta_fitness", 0.5))
            all_iv_att.append(fs.get("iv_attractiveness", 0.5))
            all_liq.append(fs.get("liquidity", 0.5))
            all_theta.append(fs.get("theta_efficiency", 0.5))

    # ── Score margin distribution ──────────────────────────────────────────────
    print(f"\n  Score Margin Distribution  (threshold={_LIVE_SCORE_MARGIN_THRESHOLD})")
    if margins:
        print(f"    count : {len(margins)}")
        print(f"    min   : {min(margins):.4f}")
        print(f"    p50   : {_percentile(margins, 50):.4f}")
        print(f"    p75   : {_percentile(margins, 75):.4f}")
        print(f"    p90   : {_percentile(margins, 90):.4f}")
        print(f"    p95   : {_percentile(margins, 95):.4f}")
        print(f"    max   : {max(margins):.4f}")
        print(f"\n    Threshold sensitivity (# bars that would pass):")
        for th in _MARGIN_CANDIDATES:
            n = sum(1 for v in margins if v >= th)
            marker = " ← current" if abs(th - _LIVE_SCORE_MARGIN_THRESHOLD) < 1e-9 else ""
            print(f"    margin ≥ {th:.2f} : {n:4d}/{len(margins)} "
                  f"({n/len(margins)*100:.1f}%){marker}")
    else:
        print("    (no data)")

    # ── OI support distribution ────────────────────────────────────────────────
    print(f"\n  OI Support Distribution  (threshold={_LIVE_OI_SUPPORT_THRESHOLD})")
    if oi_supps:
        print(f"    count : {len(oi_supps)}")
        print(f"    min   : {min(oi_supps):.4f}")
        print(f"    p50   : {_percentile(oi_supps, 50):.4f}")
        print(f"    p75   : {_percentile(oi_supps, 75):.4f}")
        print(f"    p90   : {_percentile(oi_supps, 90):.4f}")
        print(f"    p95   : {_percentile(oi_supps, 95):.4f}")
        print(f"    max   : {max(oi_supps):.4f}")
        print(f"\n    Threshold sensitivity (# bars that would pass):")
        for th in _OI_CANDIDATES:
            n = sum(1 for v in oi_supps if v >= th)
            marker = " ← current" if abs(th - _LIVE_OI_SUPPORT_THRESHOLD) < 1e-9 else ""
            print(f"    oi_supp ≥ {th:.2f} : {n:4d}/{len(oi_supps)} "
                  f"({n/len(oi_supps)*100:.1f}%){marker}")
    else:
        print("    (no data)")

    # ── Combined gate sensitivity ──────────────────────────────────────────────
    if margins and oi_supps and len(margins) == len(oi_supps):
        print(f"\n  Combined gate sensitivity (both conditions must pass):")
        combined = list(zip(margins, oi_supps))
        for m_th, oi_th in [(0.05, 0.10), (0.04, 0.08), (0.03, 0.08), (0.03, 0.05)]:
            n = sum(1 for m, o in combined if m >= m_th and o >= oi_th)
            marker = " ← current" if abs(m_th - 0.05) < 1e-9 and abs(oi_th - 0.10) < 1e-9 else ""
            print(f"    margin≥{m_th:.2f} AND oi_supp≥{oi_th:.2f} : "
                  f"{n:4d}/{len(combined)} ({n/len(combined)*100:.1f}%){marker}")

    # ── Other factor quality at potential new threshold ────────────────────────
    if margins and all_delta_f:
        # Records that would newly pass at margin ≥ 0.03
        newly_pass_idxs = [i for i, (m, r) in enumerate(zip(margins, recs))
                           if m >= 0.03 and m < 0.05]
        if newly_pass_idxs:
            fs_new = [recs[i].get("factor_scores") or {} for i in newly_pass_idxs]
            df_new = [f.get("delta_fitness", 0.5) for f in fs_new]
            oi_new = [f.get("oi_support", 0.0)    for f in fs_new]
            iv_new = [f.get("iv_attractiveness", 0.5) for f in fs_new]
            lq_new = [f.get("liquidity", 0.5)     for f in fs_new]
            print(f"\n  Factor quality for newly-passing records at margin ≥ 0.03:")
            print(f"    count         : {len(newly_pass_idxs)}")
            print(f"    avg delta_fit  : {mean(df_new):.3f}  (1.0 = optimal)")
            print(f"    avg oi_support : {mean(oi_new):.3f}  (higher = more institutional)")
            print(f"    avg iv_attract : {mean(iv_new):.3f}  (1.0 = cheap premium)")
            print(f"    avg liquidity  : {mean(lq_new):.3f}  (1.0 = tight spread/high vol)")
        else:
            print(f"\n  (No records fall in [0.03, 0.05) margin range)")

    # ── Candidate dominance quality ────────────────────────────────────────────
    print(f"\n  Candidate dominance (score_margin tells us separation quality):")
    if margins:
        very_tight = sum(1 for m in margins if m < 0.02)
        tight      = sum(1 for m in margins if 0.02 <= m < 0.04)
        adequate   = sum(1 for m in margins if 0.04 <= m < 0.05)
        passing    = sum(1 for m in margins if m >= 0.05)
        print(f"    < 0.02 (top-2 essentially tied):  {very_tight}")
        print(f"    0.02–0.04 (very thin separation): {tight}")
        print(f"    0.04–0.05 (marginal):             {adequate}")
        print(f"    ≥ 0.05 (clear winner):            {passing}")
        pct_tied = (very_tight + tight) / len(margins) * 100
        print(f"    {pct_tied:.0f}% of evaluations had a virtually tied top-2")

    # ── Recommendation ────────────────────────────────────────────────────────
    print(f"\n  RECOMMENDATION for {sym}:")

    oi_max = max(oi_supps) if oi_supps else 0
    m_max  = max(margins)  if margins  else 0

    if oi_max < 0.10 and margins:
        # OI never passes — could be data or genuine
        passing_at_3pct = sum(1 for m in margins if m >= 0.03) if margins else 0
        if passing_at_3pct == 0:
            verdict = "INVESTIGATE_MORE"
            reason  = ("OI never reached threshold (oi_support_max={:.4f} < 0.10). "
                       "Also, score_margin never reached 0.03. "
                       "Both gates failing simultaneously — diagnose OI data first "
                       "(run --live --symbol {sym}).".format(oi_max))
        else:
            verdict = "INVESTIGATE_MORE"
            reason  = ("OI gate blocks all entries (max={:.4f}). "
                       "Need raw chain OI analysis before relaxing. "
                       "Run diagnose_oi_support.py --live --symbol {sym}.".format(oi_max))
    elif m_max < 0.05:
        # Score margin never passes — consistent structural issue
        p90 = _percentile(margins, 90) if margins else 0
        if p90 >= 0.04:
            verdict = "PROPOSE_BACKTEST_THRESHOLD_CHANGE"
            reason  = (f"score_margin p90={p90:.4f} is just below 0.05 threshold. "
                       f"A controlled backtest with margin=0.03 is warranted.")
        else:
            verdict = "INVESTIGATE_MORE"
            reason  = (f"score_margin max={m_max:.4f}. "
                       f"Even at 0.03 threshold, few entries would pass. "
                       f"Chain geometry may structurally prevent clear winners. "
                       f"Do not lower threshold without multi-day backtest.")
    else:
        verdict = "KEEP_THRESHOLDS"
        reason  = "Some records pass current thresholds — gates are not over-restrictive."

    print(f"    {verdict}")
    print(f"    {reason}")
    return verdict


def main() -> None:
    audit = _load_audit()

    print(f"\n{'═' * 70}")
    print(f"  GATE CALIBRATION ANALYZER")
    print(f"  audit: {len(audit)} live records  |  db: {DB_PATH}")
    print(f"  score_margin threshold: {_LIVE_SCORE_MARGIN_THRESHOLD}")
    print(f"  oi_support threshold:   {_LIVE_OI_SUPPORT_THRESHOLD}")
    print(f"{'═' * 70}")

    if not audit:
        print("[WARN] No live audit records. Run a live session first.")
        return

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in audit:
        by_sym[r.get("underlying", "UNKNOWN")].append(r)

    verdicts: dict[str, str] = {}
    for sym, recs in sorted(by_sym.items()):
        verdicts[sym] = _analyze_underlying(sym, recs)

    print(f"\n{'═' * 70}")
    print(f"  SUMMARY")
    print(f"{'═' * 70}")
    for sym, v in verdicts.items():
        print(f"  {sym:12s} → {v}")

    print(f"\n  NOTE: No thresholds were changed by this script.")
    print(f"        To act on PROPOSE_BACKTEST_THRESHOLD_CHANGE, run a replay")
    print(f"        backtest (scripts/run_backtest.py) with the candidate threshold")
    print(f"        and compare trade quality metrics before deploying.")


if __name__ == "__main__":
    main()
