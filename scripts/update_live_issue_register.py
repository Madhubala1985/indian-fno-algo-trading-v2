"""
update_live_issue_register.py
──────────────────────────────────────────────────────────────────────
Parse the most recent live session log (and optionally the strike
selection audit JSONL) and append a machine-extracted issue summary to
the live_issue_register_<date>.md file.

Patterns extracted:
  - SCORE_MARGIN_LOW         — strike quality gate block
  - INSUFFICIENT_STRIKE_QUALITY — consolidated gate block variant
  - INSUFFICIENT_CONFLUENCE  — OI support gate block
  - CONTEXT_CONTRADICTION_REJECTED — MCB vs structure contradiction
  - WARM_START_LOCKOUT       — early lockout bars
  - MIN_LOT_OVERRIDE         — capital-engine min-lot correction
  - SIZING_BREAKDOWN         — sizing diagnostic printed
  - MCB ADAPTATION WATCH     — MCB shift detected mid-session
  - WebSocket errors          — reconnects or close events
  - STALE WARN               — warm-start data freshness warning
  - ENTRY CHECK BLOCKED       — total blocked decisions

Output:
  logs/live_issue_register_<YYYY-MM-DD>.md — creates or appends

Usage:
  PYTHONPATH=. python3 scripts/update_live_issue_register.py
  PYTHONPATH=. python3 scripts/update_live_issue_register.py --log logs/live_run_20260608_093159.log
  PYTHONPATH=. python3 scripts/update_live_issue_register.py --date 2026-06-08
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
_IST     = timezone(timedelta(hours=5, minutes=30))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core.utils.live_log_resolver import resolve_current_live_log


# ── Pattern catalogue ────────────────────────────────────────────────────────

_PATTERNS: dict[str, re.Pattern] = {
    "SCORE_MARGIN_LOW":              re.compile(r"\[SCORE_MARGIN_LOW\]"),
    "INSUFFICIENT_STRIKE_QUALITY":   re.compile(r"INSUFFICIENT_STRIKE_QUALITY"),
    "INSUFFICIENT_CONFLUENCE":       re.compile(r"INSUFFICIENT_CONFLUENCE"),
    "CONTEXT_CONTRADICTION_REJECTED":re.compile(r"CONTEXT_CONTRADICTION_REJECTED"),
    "WARM_START_LOCKOUT":            re.compile(r"WARM_START_LOCKOUT"),
    "MIN_LOT_OVERRIDE":              re.compile(r"MIN_LOT_OVERRIDE"),
    "SIZING_BREAKDOWN":              re.compile(r"SIZING_BREAKDOWN"),
    "MCB_ADAPTATION_WATCH":          re.compile(r"\[MCB ADAPTATION WATCH\]"),
    "WEBSOCKET_ERROR":               re.compile(r"\[KITE\].*(?:Connection lost|Error|reconnect)", re.IGNORECASE),
    "MCB_WARMSTART_STALE":           re.compile(r"\[MCB WARMSTART STALE WARN\]"),
    "MCB_WARMSTART_SOURCE":          re.compile(r"\[MCB WARMSTART SOURCE\]"),
    "ENTRY_CHECK_BLOCKED":           re.compile(r"ENTRY CHECK BLOCKED"),
}

# Pattern to extract underlying from STRIKE AUDIT line
_AUDIT_UNDERLYING_RE = re.compile(
    r"\[STRIKE AUDIT\]\s+(BANKNIFTY|NIFTY|SENSEX)"
)
_CONFLUENCE_BLOCK_RE = re.compile(
    r"INSUFFICIENT_CONFLUENCE.*?underlying=([A-Z]+)"
)
_BLOCK_UNDERLYING_RE = re.compile(
    r"underlying=([A-Z]+)"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_latest_log(date_str: str | None) -> Path | None:
    if date_str:
        # Find logs matching the date
        stamp = date_str.replace("-", "")
        candidates = sorted(LOGS_DIR.glob(f"live_run_{stamp}*.log"))
        return candidates[-1] if candidates else None
    # Latest via current_live_log pointer (repairs a stale pointer and
    # falls back to the newest existing logs/live_run_*.log)
    return resolve_current_live_log(LOGS_DIR)


def _parse_log(log_path: Path) -> dict:
    counts: dict[str, int] = defaultdict(int)
    underlying_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    mcb_adaptation_events: list[str] = []
    warmstart_sources: list[str] = []
    websocket_events: list[str] = []
    score_margins: list[float] = []
    oi_support_vals: list[float] = []

    _sm_re   = re.compile(r"score_margin[=:]?\s*([\d.]+)")
    _oi_re   = re.compile(r"oi_support[=:]?\s*([\d.]+)")
    _und_re  = re.compile(r"underlying=([A-Z]+)")
    _adapt_re = re.compile(r"\[MCB ADAPTATION WATCH\](.*)")
    _src_re  = re.compile(r"\[MCB WARMSTART SOURCE\](.*)")
    _ws_re   = re.compile(r"(\[KITE\].*(?:Connection|Error|reconnect).*)", re.IGNORECASE)

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for key, pat in _PATTERNS.items():
                if pat.search(line):
                    counts[key] += 1

            # score_margin samples
            m = _sm_re.search(line)
            if m and "SCORE_MARGIN_LOW" in line:
                try:
                    score_margins.append(float(m.group(1)))
                except ValueError:
                    pass

            # oi_support samples
            m = _oi_re.search(line)
            if m and "INSUFFICIENT_CONFLUENCE" in line:
                try:
                    oi_support_vals.append(float(m.group(1)))
                except ValueError:
                    pass

            # MCB adaptation events
            m = _adapt_re.search(line)
            if m:
                mcb_adaptation_events.append(m.group(1).strip()[:120])

            # Warmstart source
            m = _src_re.search(line)
            if m:
                warmstart_sources.append(m.group(1).strip())

            # WebSocket events
            m = _ws_re.search(line)
            if m:
                websocket_events.append(m.group(1).strip()[:120])

    sm_stats = _stats(score_margins)
    oi_stats = _stats(oi_support_vals)

    return {
        "counts": dict(counts),
        "score_margin_stats": sm_stats,
        "oi_support_stats": oi_stats,
        "mcb_adaptation_events": mcb_adaptation_events[:10],
        "warmstart_sources": warmstart_sources,
        "websocket_events": websocket_events[:5],
    }


def _stats(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {
        "n":   len(vals),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "avg": round(sum(vals) / len(vals), 4),
    }


def _parse_audit_jsonl() -> dict:
    audit_path = LOGS_DIR / "strike_selection_audit.jsonl"
    if not audit_path.exists():
        return {}

    rows = []
    with audit_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    if not rows:
        return {}

    total    = len(rows)
    by_und: dict[str, int] = defaultdict(int)
    low_margin = 0
    for r in rows:
        und = r.get("underlying") or r.get("feed_symbol", "?")
        by_und[und] += 1
        flags = r.get("flags", [])
        if isinstance(flags, list) and "SCORE_MARGIN_LOW" in flags:
            low_margin += 1
        elif isinstance(flags, str) and "SCORE_MARGIN_LOW" in flags:
            low_margin += 1

    return {
        "total_audit_rows": total,
        "by_underlying":    dict(by_und),
        "low_margin_rows":  low_margin,
    }


# ── Format output ─────────────────────────────────────────────────────────────

def _render_issue_section(
    log_path: Path,
    parsed: dict,
    audit: dict,
    date_str: str,
) -> str:
    now  = datetime.now(_IST)
    ts   = now.strftime("%H:%M IST")
    counts = parsed["counts"]

    lines: list[str] = []
    lines.append(f"\n---\n")
    lines.append(f"## Auto-extracted issues — {date_str} {ts}\n")
    lines.append(f"**Source log:** `{log_path.name}`\n")

    # Decision block counts
    lines.append("\n### Block event counts\n")
    lines.append("| Pattern | Count |")
    lines.append("|---------|-------|")
    ordered_keys = [
        "ENTRY_CHECK_BLOCKED",
        "WARM_START_LOCKOUT",
        "INSUFFICIENT_STRIKE_QUALITY",
        "SCORE_MARGIN_LOW",
        "INSUFFICIENT_CONFLUENCE",
        "CONTEXT_CONTRADICTION_REJECTED",
        "MIN_LOT_OVERRIDE",
        "SIZING_BREAKDOWN",
        "MCB_ADAPTATION_WATCH",
        "WEBSOCKET_ERROR",
        "MCB_WARMSTART_STALE",
    ]
    for k in ordered_keys:
        v = counts.get(k, 0)
        if v > 0:
            lines.append(f"| {k} | {v} |")

    # Score margin stats
    sm = parsed["score_margin_stats"]
    if sm:
        lines.append(f"\n### SCORE_MARGIN_LOW statistics\n")
        lines.append(
            f"n={sm['n']}  min={sm['min']}  max={sm['max']}  avg={sm['avg']}"
        )
        lines.append(
            "\n> **Classification**: Calibration Risk — "
            "BANKNIFTY near-ATM options structurally similar in composite score. "
            "Evidence: consistent low margin all session."
        )

    # OI support stats
    oi = parsed["oi_support_stats"]
    if oi:
        lines.append(f"\n### INSUFFICIENT_CONFLUENCE (oi_support) statistics\n")
        lines.append(
            f"n={oi['n']}  min={oi['min']}  max={oi['max']}  avg={oi['avg']}"
        )
        lines.append(
            "\n> **Classification**: Hypothesis — "
            "could be genuine thin OI at near-ATM strike, or normalization artifact. "
            "Run `diagnose_oi_support.py --live` during next market session."
        )

    # MCB adaptation watch
    adapt_evts = parsed["mcb_adaptation_events"]
    if adapt_evts:
        lines.append(f"\n### MCB Adaptation Watch events ({len(adapt_evts)} captured)\n")
        for ev in adapt_evts[:5]:
            lines.append(f"- `{ev}`")

    # Warmstart sources
    sources = parsed["warmstart_sources"]
    if sources:
        lines.append(f"\n### MCB warm-start sources\n")
        for src in sources:
            lines.append(f"- `{src}`")

    # WebSocket events
    ws = parsed["websocket_events"]
    if ws:
        lines.append(f"\n### WebSocket events\n")
        for ev in ws:
            lines.append(f"- `{ev}`")

    # Strike audit JSONL summary
    if audit:
        lines.append(f"\n### Strike selection audit (from JSONL)\n")
        lines.append(
            f"Total rows: {audit['total_audit_rows']}  "
            f"SCORE_MARGIN_LOW: {audit.get('low_margin_rows', 0)}"
        )
        by_und = audit.get("by_underlying", {})
        if by_und:
            for und, n in sorted(by_und.items()):
                lines.append(f"- {und}: {n} rows")

    lines.append("\n")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-extract issues from live session log into issue register"
    )
    parser.add_argument("--log",  default="",
                        help="Path to live run log (default: most recent logs/live_run_*.log)")
    parser.add_argument("--date", default="",
                        help="Date string YYYY-MM-DD (selects log and names output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print extracted content but do not write to file")
    args = parser.parse_args()

    # Resolve log
    if args.log:
        log_path = Path(args.log)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
    else:
        log_path = _find_latest_log(args.date or None)

    if log_path is None or not log_path.exists():
        print(f"[ERROR] No live run log found. Pass --log or --date.")
        return 1

    date_str = args.date or datetime.now(_IST).strftime("%Y-%m-%d")
    register = LOGS_DIR / f"live_issue_register_{date_str}.md"

    print(f"[ISSUE REGISTER] Source log : {log_path.name}")
    print(f"[ISSUE REGISTER] Output     : {register.name}")

    parsed = _parse_log(log_path)
    audit  = _parse_audit_jsonl()

    section = _render_issue_section(log_path, parsed, audit, date_str)

    if args.dry_run:
        print("\n─── DRY-RUN OUTPUT ───────────────────────────────────────────")
        print(section)
        print("─────────────────────────────────────────────────────────────")
        return 0

    # Create or append
    if not register.exists():
        header = (
            f"# Live Issue Register — {date_str}\n"
            f"**Auto-generated by update_live_issue_register.py**\n"
        )
        register.write_text(header, encoding="utf-8")
        print("[ISSUE REGISTER] Created new issue register file")
    else:
        print("[ISSUE REGISTER] Appending to existing file")

    with register.open("a", encoding="utf-8") as fh:
        fh.write(section)

    print(f"[ISSUE REGISTER] Done → {register}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
