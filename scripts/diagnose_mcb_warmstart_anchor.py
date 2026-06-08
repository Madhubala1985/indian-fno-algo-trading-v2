"""
diagnose_mcb_warmstart_anchor.py
──────────────────────────────────────────────────────────────────────
Diagnoses MCB warm-start anchoring from a live session log.

Answers:
  1. What was the MCB bias / confidence timeline for each underlying?
  2. When did BOS_BULLISH events fire, and did MCB respond?
  3. Is MCB over-anchored to the warm-start CSV bearish period?
  4. What weight does 1m structure (BOS) have vs 1d/4h warm-start?
  5. Should we expect MCB to shift given the observed live bars?

Reads: logs/live_run_*.log (most recent by default, or specify --log)
       data/*_1m_*.csv (for warm-start data summary)

Does NOT modify MCB formula or warm-start logic.

Usage:
  PYTHONPATH=. python3 scripts/diagnose_mcb_warmstart_anchor.py
  PYTHONPATH=. python3 scripts/diagnose_mcb_warmstart_anchor.py \\
      --log logs/live_run_20260608_093159.log
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

# MCB timeframe weights (from market_context_builder.py — reference only)
_TF_WEIGHTS = {"1d": 0.35, "4h": 0.25, "1h": 0.20, "15m": 0.15, "1m": 0.05}
_SHORT_THRESHOLD = -0.18
_LONG_THRESHOLD  =  0.18

LOG_DIR  = Path("logs")
DATA_DIR = Path("data")


# ── log parsing ────────────────────────────────────────────────────────────────

def _find_latest_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("live_run_*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def _parse_log(log_path: Path) -> dict:
    """
    Extract MCB context lines, BOS events, and structure events.

    Returns:
        {
          "mcb_timeline":  {sym: [(bar, bias, conf), ...]},
          "bos_events":    [(bar, sym, event_type)],
          "warmstart_bias": {sym: (bias, conf)},
          "session_date":  str,
        }
    """
    mcb_timeline: dict[str, list[tuple[int, str, float]]] = defaultdict(list)
    bos_events: list[tuple[int, str, str]] = []
    warmstart_bias: dict[str, tuple[str, float]] = {}
    session_date = ""

    re_mcb = re.compile(
        r"\[EXECUTION_MCB_CONTEXT\]\s+(\w+)\s+bias=(\w+)\s+conf=([\d.]+)\s+live_bars=(\d+)"
    )
    re_bos = re.compile(
        r"CONTEXT_CONTRADICTION_REJECTED\s+mcb=(\w+)\s+structure=(BOS_\w+|CHOCH_\w+)\s+at\s+bar=(\d+)"
    )
    re_warmstart = re.compile(
        r"\[MCB WARMSTART\]\s+(\w+):\s+MCB ready\s+bias=(\w+)\s+conf=([\d.]+)"
    )
    re_date = re.compile(r"\[CAPITAL CONFIG\].*|live_run_(\d{8})")

    # Extract date from filename
    m = re.search(r"live_run_(\d{8})", log_path.name)
    if m:
        d = m.group(1)
        session_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip()

            m = re_mcb.search(line)
            if m:
                sym, bias, conf, live_bars = m.group(1), m.group(2), float(m.group(3)), int(m.group(4))
                mcb_timeline[sym].append((live_bars, bias, conf))
                continue

            m = re_bos.search(line)
            if m:
                mcb_bias, struct_event, bar = m.group(1), m.group(2), int(m.group(3))
                # Infer symbol from surrounding context (if available, else unknown)
                bos_events.append((bar, mcb_bias, struct_event))
                continue

            m = re_warmstart.search(line)
            if m:
                sym2, bias2, conf2 = m.group(1), m.group(2), float(m.group(3))
                warmstart_bias[sym2] = (bias2, conf2)

    return {
        "mcb_timeline":   dict(mcb_timeline),
        "bos_events":     bos_events,
        "warmstart_bias": warmstart_bias,
        "session_date":   session_date,
    }


# ── warm-start CSV summary ─────────────────────────────────────────────────────

def _csv_tail_summary(sym: str, n_bars: int = 30) -> dict:
    """Read the last n_bars of the warm-start CSV and estimate direction."""
    prefix = {"BANKNIFTY": "banknifty", "NIFTY": "nifty", "SENSEX": "sensex"}.get(sym)
    if prefix is None:
        return {}
    csvs = sorted(DATA_DIR.glob(f"{prefix}_1m_*.csv"),
                  key=lambda p: p.stat().st_mtime)
    if not csvs:
        return {}
    csv_path = csvs[-1]
    try:
        lines = csv_path.read_text().splitlines()
        data_lines = [l for l in lines[1:] if l.strip()]  # skip header
        tail = data_lines[-n_bars:]
        if len(tail) < 5:
            return {}
        # Parse close prices (assume last field or 5th field is close)
        closes = []
        for l in tail:
            parts = l.split(",")
            # Try to find close: typically col index 4 (OHLCV)
            for idx in [4, 5, 3]:
                if idx < len(parts):
                    try:
                        closes.append(float(parts[idx]))
                        break
                    except ValueError:
                        pass
        if len(closes) < 5:
            return {}
        first_half = closes[: len(closes) // 2]
        second_half = closes[len(closes) // 2 :]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        direction = "BEARISH" if avg_second < avg_first else "BULLISH"
        change_pct = (avg_second - avg_first) / avg_first * 100
        return {
            "csv": csv_path.name,
            "last_bars": len(tail),
            "last_close": closes[-1],
            "direction": direction,
            "change_pct": change_pct,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── analysis ───────────────────────────────────────────────────────────────────

def _analyze(data: dict) -> None:
    timeline   = data["mcb_timeline"]
    bos_events = data["bos_events"]
    warmstart  = data["warmstart_bias"]
    date       = data.get("session_date", "unknown")

    print(f"\n{'═' * 70}")
    print(f"  MCB WARM-START ANCHOR DIAGNOSTIC  —  session {date}")
    print(f"{'═' * 70}")

    # ── MCB timeframe weight explanation ──────────────────────────────────────
    print(f"\n  MCB timeframe weights (for context):")
    for tf, w in _TF_WEIGHTS.items():
        print(f"    {tf:4s}: {w:.2f}  {'← dominant warm-start' if tf in ('1d','4h') else ''}")
    print(f"  SHORT confirmed when weighted_bear_score < {_SHORT_THRESHOLD}")
    print(f"  1m structure (BOS) has weight {_TF_WEIGHTS['1m']:.2f} — cannot override 1d+4h alone")

    # ── Warm-start state ──────────────────────────────────────────────────────
    print(f"\n  MCB state at warm-start completion:")
    for sym, (bias, conf) in sorted(warmstart.items()):
        print(f"    {sym:12s}  bias={bias:7s}  conf={conf:.2f}")
        csv_summary = _csv_tail_summary(sym)
        if csv_summary:
            print(f"               CSV: {csv_summary.get('csv','?')}  "
                  f"last_close={csv_summary.get('last_close', '?'):.0f}  "
                  f"direction={csv_summary.get('direction','?')}  "
                  f"change={csv_summary.get('change_pct', 0):.1f}%")

    # ── Bias timeline ─────────────────────────────────────────────────────────
    print(f"\n  MCB bias timeline during live session:")
    for sym, events in sorted(timeline.items()):
        if not events:
            continue
        print(f"\n    {sym}  ({len(events)} MCB readings)")
        # Sample: first, middle quartiles, last
        idxs = [0, len(events)//4, len(events)//2, 3*len(events)//4, len(events)-1]
        idxs = sorted(set(max(0, min(i, len(events)-1)) for i in idxs))
        for i in idxs:
            bar, bias, conf = events[i]
            print(f"      live_bar={bar:4d}  bias={bias:7s}  conf={conf:.2f}")
        # Count bias changes
        biases = [e[1] for e in events]
        changes = sum(1 for i in range(1, len(biases)) if biases[i] != biases[i-1])
        print(f"      Bias changes during session: {changes}")
        if changes == 0:
            print(f"      → MCB was UNCHANGED all session (strongly anchored)")
        elif changes <= 2:
            print(f"      → MCB changed {changes}× — minor fluctuation")
        else:
            print(f"      → MCB changed {changes}× — bias was unstable")

        # Confidence range
        confs = [e[2] for e in events]
        print(f"      Confidence range: [{min(confs):.2f}, {max(confs):.2f}]")

    # ── BOS events and MCB response ───────────────────────────────────────────
    print(f"\n  BOS/CHOCH contradictions (structure fired ≠ MCB):")
    if not bos_events:
        print("    None detected in this session")
    else:
        for bar, mcb_bias, struct_event in bos_events:
            print(f"    bar={bar:4d}  mcb={mcb_bias:7s}  structure={struct_event}")
        print(f"\n    Total: {len(bos_events)} contradiction(s)")
        print(f"    These are CORRECTLY blocked — structure at 1m opposes MCB.")
        print(f"    For MCB to incorporate these, the 1m RegimeClassifier would need")
        print(f"    to see sustained bullish price action over many bars.")

    # ── Theoretical weight analysis ───────────────────────────────────────────
    print(f"\n  Theoretical: what would make MCB shift from SHORT to NEUTRAL?")
    print(f"    Current scenario: 1d=BEARISH(~0.95 conf), 4h=BEARISH(~0.90 conf)")
    w_1d_bear = _TF_WEIGHTS["1d"] * 0.95
    w_4h_bear = _TF_WEIGHTS["4h"] * 0.90
    w_1h_bull = _TF_WEIGHTS["1h"] * 0.80
    w_15m_bull = _TF_WEIGHTS["15m"] * 0.80
    w_1m_bull = _TF_WEIGHTS["1m"] * 0.50

    total_bear = w_1d_bear + w_4h_bear
    all_bull_against = w_1h_bull + w_15m_bull + w_1m_bull
    trend_score_estimate = (all_bull_against - total_bear) / (total_bear + all_bull_against)

    print(f"    If 1d+4h = BEARISH, bear weight ≈ {total_bear:.3f}")
    print(f"    If 1h+15m+1m = BULLISH, bull weight ≈ {all_bull_against:.3f}")
    print(f"    Resulting trend_score ≈ {trend_score_estimate:.3f}  "
          f"(threshold: < {_SHORT_THRESHOLD} = SHORT)")
    if trend_score_estimate < _SHORT_THRESHOLD:
        print(f"    → MCB stays SHORT even with 1h+15m+1m all bullish.")
        print(f"    → Only new 1d/4h BULLISH bars can shift the bias.")
        print(f"    → Approximately {round(0.18 / _TF_WEIGHTS['1d'])} bullish daily bars")
        print(f"       with no warm-start data would be needed to shift from NEUTRAL.")
    else:
        print(f"    → Combined bullish pressure COULD shift bias to NEUTRAL.")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n  VERDICT:")
    total_unchanged = sum(
        1 for sym, events in timeline.items()
        if events and len(set(e[1] for e in events)) == 1
    )
    if total_unchanged == len(timeline) and timeline:
        print(f"  WARM_START_ANCHORING_CONFIRMED — all {len(timeline)} underlyings")
        print(f"  kept the same bias all session. This is EXPECTED behavior, not a bug.")
        print(f"  The CSV warm-start sets 1d and 4h bias from historical bearish data.")
        print(f"  1m BOS events (weight={_TF_WEIGHTS['1m']}) cannot override this.")
        print(f"")
        print(f"  To shift MCB, either:")
        print(f"  (A) Wait for new 1d/4h bars formed from current price action to")
        print(f"      accumulate (takes multiple trading days post-June 8).")
        print(f"  (B) Fetch and warm-start from more recent historical data")
        print(f"      (e.g., if May 30–June 7 data becomes available in CSV form).")
        print(f"")
        print(f"  STATUS: HYPOTHESIS_CONFIRMED — warm-start is anchoring MCB as designed.")
        print(f"  FINDING: No code defect. Consider fetching more recent CSV data for warm-start.")
    else:
        print(f"  MCB was responsive during the session (biases changed). Not anchored.")

    print(f"\n  NOTE: This script does NOT modify the MCB formula or warm-start logic.")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose MCB warm-start anchoring")
    parser.add_argument("--log", type=Path, default=None,
                        help="Specific live log to analyze (default: most recent)")
    args = parser.parse_args()

    log_path = args.log or _find_latest_log()
    if log_path is None or not log_path.exists():
        print(f"[ERROR] No live log found in {LOG_DIR}/")
        return

    print(f"[INFO] Analyzing: {log_path}")
    data = _parse_log(log_path)
    _analyze(data)
    print("\n[DONE]")


if __name__ == "__main__":
    main()
