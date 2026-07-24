"""
generate_daily_reflection.py
──────────────────────────────────────────────────────────────────────
Generate a structured daily reflection markdown file from live session
artifacts:
  - Most recent live_run_*.log (block counts AND session trades — the
    cumulative logs/paper_trades.csv is deliberately NOT used for the header)
  - logs/paper_trading_state.json
  - logs/strike_selection_audit.jsonl
  - logs/live_issue_register_<date>.md (if present)

Output:
  logs/daily_reflection_<YYYY-MM-DD>.md

The script extracts facts and statistics. It does NOT modify strategy
parameters, thresholds, or engine configurations.

Usage:
  PYTHONPATH=. python3 scripts/generate_daily_reflection.py
  PYTHONPATH=. python3 scripts/generate_daily_reflection.py --date 2026-06-08
  PYTHONPATH=. python3 scripts/generate_daily_reflection.py --dry-run
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_latest_log(date_str: str | None) -> Path | None:
    if date_str:
        stamp = date_str.replace("-", "")
        candidates = sorted(LOGS_DIR.glob(f"live_run_{stamp}*.log"))
        return candidates[-1] if candidates else None
    # Latest via current_live_log pointer (repairs a stale pointer and
    # falls back to the newest existing logs/live_run_*.log)
    return resolve_current_live_log(LOGS_DIR)


def _parse_log(log_path: Path) -> dict:
    counts: dict[str, int] = defaultdict(int)

    _patterns = {
        "SCORE_MARGIN_LOW":              re.compile(r"\[SCORE_MARGIN_LOW\]"),
        "INSUFFICIENT_STRIKE_QUALITY":   re.compile(r"INSUFFICIENT_STRIKE_QUALITY"),
        "INSUFFICIENT_CONFLUENCE":       re.compile(r"INSUFFICIENT_CONFLUENCE"),
        "CONTEXT_CONTRADICTION_REJECTED":re.compile(r"CONTEXT_CONTRADICTION_REJECTED"),
        "WARM_START_LOCKOUT":            re.compile(r"WARM_START_LOCKOUT"),
        "MIN_LOT_OVERRIDE":              re.compile(r"MIN_LOT_OVERRIDE"),
        "SIZING_BREAKDOWN":              re.compile(r"SIZING_BREAKDOWN"),
        "MCB_ADAPTATION_WATCH":          re.compile(r"\[MCB ADAPTATION WATCH\]"),
        "ENTRY_CHECK_BLOCKED":           re.compile(r"ENTRY CHECK BLOCKED"),
        "WEBSOCKET_CONNECTED":           re.compile(r"\[KITE\] WebSocket connected"),
        "WEBSOCKET_LOST":                re.compile(r"\[KITE\] Connection lost"),
        "SESSION_CLOSING":               re.compile(r"session=CLOSING"),
        "EOD_FORCE_CLOSE":               re.compile(r"EOD.*force.clos|FORCE.*CLOSE", re.IGNORECASE),
        "MCB_WARMSTART_SOURCE":          re.compile(r"\[MCB WARMSTART SOURCE\]"),
        "MCB_WARMSTART_STALE":           re.compile(r"\[MCB WARMSTART STALE WARN\]"),
    }

    sm_vals:  list[float] = []
    oi_vals:  list[float] = []
    adapt_evts: list[str] = []
    ws_sources: list[str] = []

    _sm_re     = re.compile(r"score_margin[=:]?\s*([\d.]+)")
    _oi_re     = re.compile(r"oi_support[=:]?\s*([\d.]+)")
    _adapt_re  = re.compile(r"\[MCB ADAPTATION WATCH\](.*)")
    _src_re    = re.compile(r"\[MCB WARMSTART SOURCE\](.*)")
    _bias_re   = re.compile(r"MCB.*bias=(\w+).*conf=([\d.]+)")

    bias_lines: list[tuple[str, float]] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for key, pat in _patterns.items():
                if pat.search(line):
                    counts[key] += 1

            m = _sm_re.search(line)
            if m and "SCORE_MARGIN_LOW" in line:
                try:
                    sm_vals.append(float(m.group(1)))
                except ValueError:
                    pass

            m = _oi_re.search(line)
            if m and "INSUFFICIENT_CONFLUENCE" in line:
                try:
                    oi_vals.append(float(m.group(1)))
                except ValueError:
                    pass

            m = _adapt_re.search(line)
            if m:
                adapt_evts.append(m.group(1).strip()[:120])

            m = _src_re.search(line)
            if m:
                ws_sources.append(m.group(1).strip())

            m = _bias_re.search(line)
            if m:
                try:
                    bias_lines.append((m.group(1), float(m.group(2))))
                except ValueError:
                    pass

    def stats(v: list[float]) -> dict | None:
        if not v:
            return None
        return {"n": len(v), "min": round(min(v), 4),
                "max": round(max(v), 4), "avg": round(sum(v)/len(v), 4)}

    # Dominant MCB bias
    dominant_bias = "unknown"
    if bias_lines:
        bias_counts: dict[str, int] = defaultdict(int)
        for b, _ in bias_lines:
            bias_counts[b] += 1
        dominant_bias = max(bias_counts, key=bias_counts.get)  # type: ignore[arg-type]

    return {
        "counts": dict(counts),
        "score_margin_stats": stats(sm_vals),
        "oi_support_stats": stats(oi_vals),
        "mcb_adaptation_events": adapt_evts[:5],
        "warmstart_sources": ws_sources,
        "dominant_bias": dominant_bias,
    }


_TRADE_LOGGED_RE = re.compile(
    r"\[TRADE LOGGED\]\s+(?P<contract>\S+)\s+(?P<direction>LONG|SHORT)\s+"
    r"entry=₹(?P<entry>[\d,.]+)\s+exit=₹(?P<exit>[\d,.]+)\s+"
    # The live logger emits the sign on EITHER side of the rupee glyph:
    # winners as `pnl=+₹91.49`, losers as `pnl=₹-3,507.21`. Accept both.
    r"pnl=(?P<pre_sign>[-+])?₹(?P<post_sign>[-+])?(?P<pnl>[\d,.]+)"
)


def _parse_trades(log_path: Path) -> dict:
    """SESSION trades, parsed from the session log itself.

    Bug fix (2026-07-02): this previously summed the whole cumulative
    logs/paper_trades.csv, so the header claimed 52 trades / +₹25,935 on a
    3-trade -₹9,754 day. The reflection is a per-session report — everything
    in it must come from THIS session's log.

    Bug fix (2026-07-24): the pnl group required the sign AFTER the rupee
    glyph, which only losers emit — every winning trade was silently dropped,
    so 2026-07-24 reported 5 trades / -₹10,443.89 against a true 8 trades /
    -₹7,744.98. Every reflection written under the old regex is negatively
    biased; trades.db and the EOD PORTFOLIO SUMMARY were always correct.
    """
    rows: list[dict] = []
    total_pnl = 0.0
    if log_path.exists():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _TRADE_LOGGED_RE.search(line)
                if not m:
                    continue
                try:
                    pnl = float(m.group("pnl").replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if "-" in (m.group("pre_sign"), m.group("post_sign")):
                    pnl = -pnl
                rows.append({
                    "symbol": m.group("contract"),
                    "direction": m.group("direction"),
                    "entry_price": m.group("entry").replace(",", ""),
                    "exit_price": m.group("exit").replace(",", ""),
                    "pnl": pnl,
                })
                total_pnl += pnl
    return {"count": len(rows), "total_pnl": round(total_pnl, 2), "rows": rows}


def _parse_state() -> dict:
    state_path = LOGS_DIR / "paper_trading_state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


def _parse_audit() -> dict:
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
    return {"total": len(rows), "by_und": dict(by_und), "low_margin": low_margin}


# ── Render ───────────────────────────────────────────────────────────────────

def _render(
    date_str: str,
    log_path: Path,
    log_data: dict,
    trades: dict,
    state: dict,
    audit: dict,
) -> str:
    now_ist  = datetime.now(_IST).strftime("%H:%M IST")
    counts   = log_data["counts"]
    sm       = log_data["score_margin_stats"]
    oi       = log_data["oi_support_stats"]
    bias     = log_data["dominant_bias"]

    # Equity from state
    equity = state.get("equity", state.get("paper_equity", "unknown"))
    if isinstance(equity, (int, float)):
        equity_str = f"₹{equity:,.0f}"
    else:
        equity_str = str(equity)

    total_pnl = trades.get("total_pnl", 0.0)
    trade_cnt = trades.get("count", 0)

    lines: list[str] = []

    lines.append(f"# Daily Reflection — {date_str}")
    lines.append(
        f"**Auto-generated:** {now_ist}  "
        f"| **Source:** `{log_path.name}`"
    )
    lines.append(
        f"**Session trades:** {trade_cnt}  "
        f"| **Session PnL:** ₹{total_pnl:+,.2f}  "
        f"| **Equity:** {equity_str}"
    )
    lines.append("")

    # ── Section 1: Block summary
    lines.append("## 1. Decision Block Summary")
    lines.append("")
    lines.append("| Pattern | Count |")
    lines.append("|---------|-------|")
    for k in [
        "ENTRY_CHECK_BLOCKED", "WARM_START_LOCKOUT", "INSUFFICIENT_STRIKE_QUALITY",
        "SCORE_MARGIN_LOW", "INSUFFICIENT_CONFLUENCE", "CONTEXT_CONTRADICTION_REJECTED",
        "MIN_LOT_OVERRIDE", "SIZING_BREAKDOWN", "MCB_ADAPTATION_WATCH",
        "SESSION_CLOSING", "WEBSOCKET_LOST",
    ]:
        v = counts.get(k, 0)
        lines.append(f"| `{k}` | {v} |")
    lines.append("")

    # ── Section 2: Gate statistics
    lines.append("## 2. Gate Statistics")
    lines.append("")
    if sm:
        lines.append(
            f"**SCORE_MARGIN_LOW** — "
            f"n={sm['n']}  min={sm['min']}  max={sm['max']}  avg={sm['avg']}"
        )
    else:
        lines.append("SCORE_MARGIN_LOW: no samples")

    if oi:
        lines.append(
            f"\n**INSUFFICIENT_CONFLUENCE** (oi_support) — "
            f"n={oi['n']}  min={oi['min']}  max={oi['max']}  avg={oi['avg']}"
        )
    else:
        lines.append("\nINSUFFICIENT_CONFLUENCE: no samples")
    lines.append("")

    # ── Section 3: MCB state
    lines.append("## 3. MCB State")
    lines.append("")
    lines.append(f"**Dominant bias this session:** `{bias}`")
    adapt_evts = log_data["mcb_adaptation_events"]
    if adapt_evts:
        lines.append(f"\n**MCB Adaptation Watch events** ({len(adapt_evts)} captured):")
        for ev in adapt_evts[:5]:
            lines.append(f"- `{ev}`")
    ws_sources = log_data["warmstart_sources"]
    if ws_sources:
        lines.append("\n**Warm-start sources:**")
        for src in ws_sources:
            lines.append(f"- `{src}`")
    lines.append("")

    # ── Section 4: Strike audit
    if audit:
        lines.append("## 4. Strike Selection Audit")
        lines.append("")
        lines.append(
            f"Total rows: {audit['total']}  "
            f"| SCORE_MARGIN_LOW: {audit.get('low_margin', 0)}"
        )
        for und, n in sorted(audit.get("by_und", {}).items()):
            lines.append(f"- {und}: {n} rows")
        lines.append("")

    # ── Section 5: What worked
    lines.append("## 5. What Worked")
    lines.append("")
    worked: list[str] = []
    if counts.get("WEBSOCKET_LOST", 0) == 0 and counts.get("WEBSOCKET_CONNECTED", 0) > 0:
        worked.append("WebSocket feed stable (no disconnects)")
    if counts.get("WARM_START_LOCKOUT", 0) > 0 and counts.get("MCB_WARMSTART_STALE", 0) == 0:
        worked.append("MCB warm-start completed with fresh data")
    if trade_cnt > 0:
        worked.append(f"{trade_cnt} paper trade(s) executed")
    if not worked:
        worked.append("Infrastructure stable (lifecycle, feeds, state persistence)")
    for w in worked:
        lines.append(f"- {w}")
    lines.append("")

    # ── Section 6: Blocks requiring investigation
    lines.append("## 6. Blocks Requiring Investigation")
    lines.append("")
    issues: list[str] = []
    sm_n = sm["n"] if sm else 0
    if sm_n > 20:
        issues.append(
            f"SCORE_MARGIN_LOW fired {sm_n} times "
            f"(avg margin={sm['avg'] if sm else 'n/a'} vs threshold=0.05) — "
            "calibration risk, consider offline backtest with lowered threshold"
        )
    oi_n = oi["n"] if oi else 0
    if oi_n > 20:
        issues.append(
            f"INSUFFICIENT_CONFLUENCE fired {oi_n} times — "
            "run `diagnose_oi_support.py --live` during next market session to classify"
        )
    if len(adapt_evts) > 0:
        issues.append(
            f"MCB ADAPTATION WATCH: {len(adapt_evts)} event(s) — "
            "review if bias shift mid-session is unexpected"
        )
    if not issues:
        issues.append("No new issues identified — review issue register for carry-forwards")
    for iss in issues:
        lines.append(f"- {iss}")
    lines.append("")

    # ── Section 7: No automatic policy mutation
    lines.append("## 7. No Automatic Policy Mutation")
    lines.append("")
    lines.append(
        "No strategy parameters, thresholds, or engine configurations were modified "
        "based on this session. Any change requires: offline backtest evidence + "
        "explicit human review + code commit off market hours."
    )
    lines.append("")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate structured daily reflection from live session artifacts"
    )
    parser.add_argument("--date", default="",
                        help="Date YYYY-MM-DD (selects log, names output)")
    parser.add_argument("--log", default="",
                        help="Path to specific log file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print reflection but do not write file")
    args = parser.parse_args()

    if args.log:
        log_path = Path(args.log)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
    else:
        log_path = _find_latest_log(args.date or None)

    if log_path is None or not log_path.exists():
        print("[ERROR] No live run log found. Pass --log or --date.")
        return 1

    date_str = args.date or datetime.now(_IST).strftime("%Y-%m-%d")
    out_path = LOGS_DIR / f"daily_reflection_{date_str}.md"

    print(f"[REFLECTION] Source log : {log_path.name}")
    print(f"[REFLECTION] Output     : {out_path.name}")

    log_data = _parse_log(log_path)
    trades   = _parse_trades(log_path)
    state    = _parse_state()
    audit    = _parse_audit()

    content = _render(date_str, log_path, log_data, trades, state, audit)

    if args.dry_run:
        print("\n─── DRY-RUN OUTPUT ────────────────────────────────────────────")
        print(content)
        print("──────────────────────────────────────────────────────────────")
        return 0

    out_path.write_text(content, encoding="utf-8")
    print(f"[REFLECTION] Written → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
