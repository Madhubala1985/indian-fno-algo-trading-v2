#!/usr/bin/env python3
"""
analyze_shadow_readiness.py

Read logs/strike_selection_shadow.jsonl and report whether NIFTY/SENSEX
shadow option intelligence is activation-ready for paper-trading enablement.

Does NOT import live_structure_pipeline, connect to Kite, write DB/state/CSV,
or affect BANKNIFTY execution in any way.

Exit codes
----------
0  — all requested symbols are READY_FOR_PAPER_ENABLEMENT
1  — any symbol is not ready, file missing, or no records found
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

# ── Thresholds ────────────────────────────────────────────────────────────────

DEFAULT_FILE          = Path("logs/strike_selection_shadow.jsonl")
DEFAULT_MIN_ATTEMPTS  = 20
DEFAULT_MIN_SUCCESS   = 0.50
DEFAULT_MAX_FAILURE   = 0.30
CONCENTRATION_THRESH  = 0.70    # fraction of successes from one strike → inspect
SCORE_DRIVEN_MARGIN   = 0.03    # avg margin >= this → SCORE_DRIVEN (still ready)

NO_CANDIDATE_REASONS  = frozenset({"CANDIDATES_EMPTY", "STRIKE_SELECTION_EMPTY"})


# ── Readiness statuses ────────────────────────────────────────────────────────

class R:
    READY        = "READY_FOR_PAPER_ENABLEMENT"
    INSUFF       = "NOT_READY_INSUFFICIENT_DATA"
    HIGH_FAIL    = "NOT_READY_HIGH_FAILURE_RATE"
    NO_CAND      = "NOT_READY_NO_CANDIDATES"
    CONC_SUSPECT = "NOT_READY_STRIKE_CONCENTRATION_SUSPECT"


# ── JSONL loading ─────────────────────────────────────────────────────────────

def load_records(path: Path, symbol_filter: set[str] | None) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ul = rec.get("underlying", "")
            if not ul:
                continue
            if symbol_filter and ul not in symbol_filter:
                continue
            grouped[ul].append(rec)
    return dict(grouped)


# ── Per-symbol stats ──────────────────────────────────────────────────────────

def compute_stats(underlying: str, records: list[dict]) -> dict:
    success_recs   = [r for r in records if r.get("selected_symbol") is not None]
    failure_recs   = [r for r in records if r.get("selected_symbol") is None]
    no_cand_recs   = [r for r in failure_recs
                      if r.get("failure_reason") in NO_CANDIDATE_REASONS]
    true_fail_recs = [r for r in failure_recs
                      if r.get("failure_reason") not in NO_CANDIDATE_REASONS]

    n   = len(records)
    ns  = len(success_recs)
    nnc = len(no_cand_recs)
    nf  = len(true_fail_recs)

    scores  = [r["composite_score"] for r in success_recs
               if r.get("composite_score") is not None]
    margins = [r["score_margin"]    for r in success_recs
               if r.get("score_margin") is not None]

    symbol_freq = Counter(r["selected_symbol"] for r in success_recs)
    strike_freq = Counter(
        r.get("strike") for r in success_recs if r.get("strike") is not None
    )
    top_strike_frac = (
        strike_freq.most_common(1)[0][1] / ns if (ns and strike_freq) else 0.0
    )

    return {
        "underlying":      underlying,
        "attempts":        n,
        "successes":       ns,
        "no_candidate":    nnc,
        "failures":        nf,
        "success_rate":    ns / n if n else 0.0,
        "failure_rate":    nf / n if n else 0.0,
        "no_cand_rate":    nnc / n if n else 0.0,
        "avg_score":       mean(scores)  if scores  else None,
        "avg_margin":      mean(margins) if margins else None,
        "failure_reasons": Counter(r.get("failure_reason", "UNKNOWN")
                                   for r in failure_recs),
        "symbol_freq":     symbol_freq,
        "strike_freq":     strike_freq,
        "top_strike_frac": top_strike_frac,
    }


# ── Readiness classification ──────────────────────────────────────────────────

def classify(s: dict, min_attempts: int, min_success: float, max_failure: float
             ) -> tuple[str, list[str]]:
    notes: list[str] = []

    if s["attempts"] < min_attempts:
        return R.INSUFF, [f"need {min_attempts} attempts, have {s['attempts']}"]

    if s["failure_rate"] > max_failure:
        return R.HIGH_FAIL, [
            f"failure_rate={s['failure_rate']:.1%} > threshold {max_failure:.1%}"
        ]

    if s["success_rate"] < min_success:
        if s["no_cand_rate"] > 0.30:
            return R.NO_CAND, [
                f"no_candidate_rate={s['no_cand_rate']:.1%}; "
                f"success_rate={s['success_rate']:.1%} < {min_success:.1%}"
            ]
        return R.HIGH_FAIL, [
            f"success_rate={s['success_rate']:.1%} < threshold {min_success:.1%}"
        ]

    if s["top_strike_frac"] > CONCENTRATION_THRESH:
        avg_m = s["avg_margin"]
        if avg_m is None or avg_m < SCORE_DRIVEN_MARGIN:
            return R.CONC_SUSPECT, [
                f"top strike covers {s['top_strike_frac']:.1%} of successes; "
                f"avg_margin={'n/a' if avg_m is None else f'{avg_m:.3f}'} "
                f"< {SCORE_DRIVEN_MARGIN} — SUSPICIOUS_CONCENTRATION"
            ]
        notes.append(
            f"SCORE_DRIVEN_CONCENTRATION: top strike {s['top_strike_frac']:.1%} "
            f"of successes; avg_margin={avg_m:.3f} (≥{SCORE_DRIVEN_MARGIN}, acceptable)"
        )

    return R.READY, notes


# ── Terminal report ───────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "n/a"


def _conc_label(s: dict) -> str:
    if s["top_strike_frac"] <= CONCENTRATION_THRESH:
        return "NORMAL"
    avg_m = s["avg_margin"]
    if avg_m is None or avg_m < SCORE_DRIVEN_MARGIN:
        return "SUSPICIOUS_CONCENTRATION"
    return "SCORE_DRIVEN_CONCENTRATION"


def print_symbol_report(s: dict, status: str, notes: list[str]) -> None:
    ul = s["underlying"]
    n  = s["attempts"]
    print(f"\n  {ul}")
    print(f"  {'─' * 54}")
    print(f"  attempts        : {n}")
    print(f"  success         : {s['successes']:5d}  ({_pct(s['successes'],   n)})")
    print(f"  no_candidate    : {s['no_candidate']:5d}  ({_pct(s['no_candidate'], n)})")
    print(f"  failures        : {s['failures']:5d}  ({_pct(s['failures'],    n)})")
    print(f"  success_rate    : {s['success_rate']:.1%}")
    print(f"  failure_rate    : {s['failure_rate']:.1%}")
    if s["avg_score"] is not None:
        print(f"  avg_score       : {s['avg_score']:.3f}")
    if s["avg_margin"] is not None:
        print(f"  avg_margin      : {s['avg_margin']:.3f}")

    if s["failure_reasons"]:
        print("  top failure reasons:")
        for reason, cnt in s["failure_reasons"].most_common(5):
            print(f"    {reason:<38s}: {cnt}")

    if s["symbol_freq"]:
        print("  selected symbols (top 5):")
        for sym, cnt in s["symbol_freq"].most_common(5):
            print(f"    {sym:<38s}: {cnt}  ({_pct(cnt, s['successes'])})")

    if s["strike_freq"]:
        print("  selected strikes (top 5):")
        for strike, cnt in s["strike_freq"].most_common(5):
            print(f"    {str(strike):<38s}: {cnt}  ({_pct(cnt, s['successes'])})")

    print(f"  strike conc.    : top={s['top_strike_frac']:.1%}  {_conc_label(s)}")
    print(f"\n  STATUS: {status}")
    for note in notes:
        print(f"    note: {note}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze shadow option intelligence JSONL for activation readiness."
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE,
                        metavar="PATH",
                        help=f"JSONL source (default: {DEFAULT_FILE})")
    parser.add_argument("--symbol", default="ALL", metavar="NIFTY|SENSEX|ALL",
                        help="Restrict to one underlying (default ALL)")
    parser.add_argument("--min-attempts", type=int, default=DEFAULT_MIN_ATTEMPTS,
                        help=f"Minimum attempts required (default {DEFAULT_MIN_ATTEMPTS})")
    parser.add_argument("--min-success-rate", type=float, default=DEFAULT_MIN_SUCCESS,
                        help=f"Minimum success rate 0–1 (default {DEFAULT_MIN_SUCCESS})")
    parser.add_argument("--max-failure-rate", type=float, default=DEFAULT_MAX_FAILURE,
                        help=f"Maximum failure rate 0–1 (default {DEFAULT_MAX_FAILURE})")
    args = parser.parse_args(argv)

    sym_upper = args.symbol.upper()
    symbol_filter: set[str] | None = None if sym_upper == "ALL" else {sym_upper}

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'═' * 60}")
    print(f"  SHADOW READINESS REPORT  —  {now_str}")
    print(f"  file    : {args.file}")
    print(f"  symbol  : {sym_upper}")
    print(
        f"  thresholds: min_attempts={args.min_attempts}"
        f"  min_success={args.min_success_rate:.0%}"
        f"  max_failure={args.max_failure_rate:.0%}"
    )
    print(f"{'═' * 60}")

    if not args.file.exists():
        print(f"\n  [ERROR] File not found: {args.file}")
        print("  Run live pipeline with shadow underlyings to generate data.")
        print(f"\n{'═' * 60}\n")
        return 1

    records_by_symbol = load_records(args.file, symbol_filter)

    if not records_by_symbol:
        print("\n  [WARN] No records found for the requested symbol(s).")
        print(f"\n{'═' * 60}\n")
        return 1

    results: list[tuple[str, str, list[str]]] = []
    for ul in sorted(records_by_symbol):
        s      = compute_stats(ul, records_by_symbol[ul])
        status, notes = classify(
            s,
            min_attempts=args.min_attempts,
            min_success=args.min_success_rate,
            max_failure=args.max_failure_rate,
        )
        print_symbol_report(s, status, notes)
        results.append((ul, status, notes))

    all_ready    = all(status == R.READY for _, status, _ in results)
    ready_count  = sum(1 for _, status, _ in results if status == R.READY)
    total        = len(results)

    print(f"\n{'═' * 60}")
    if all_ready:
        print("  OVERALL: READY — all requested symbols READY_FOR_PAPER_ENABLEMENT")
    else:
        print(f"  OVERALL: NOT READY  ({ready_count} of {total} symbol(s) ready)")
    print(f"{'═' * 60}\n")

    return 0 if all_ready else 1


if __name__ == "__main__":
    sys.exit(main())
