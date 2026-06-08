#!/usr/bin/env python3
"""
scripts/analyze_live_trade_quality.py

Read-only post-session trade quality analyzer.
Reads: logs/trades.db, logs/paper_trades.csv, logs/strike_selection_audit.jsonl,
       latest logs/live_run_*.log (if present).

No Kite, no live_structure_pipeline import, no DB writes.

Final status:
  PASS_FOR_NEXT_PAPER_RUN   — no blocking defects found
  BLOCKED_BY_RISK_DEFECTS   — one or more blocking defects found
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
DB_PATH     = LOGS / "trades.db"
CSV_PATH    = LOGS / "paper_trades.csv"
AUDIT_PATH  = LOGS / "strike_selection_audit.jsonl"

# Thresholds (mirrors live_structure_pipeline constants)
_MIN_OI_SUPPORT  = 0.10
_MIN_SCORE_MARGIN = 0.05
_WARM_START_BARS  = 20

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(v: float | None, fmt: str = ".2f") -> str:
    if v is None:
        return "n/a"
    return format(v, fmt)


def _load_db_trades() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM pt_trades ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _load_db_decisions() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM pt_decisions ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _load_csv_trades() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    try:
        with open(CSV_PATH, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_audit() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    records = []
    with open(AUDIT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def _latest_log() -> Path | None:
    pattern = str(LOGS / "live_run_*.log")
    files = sorted(glob.glob(pattern))
    return Path(files[-1]) if files else None


# ── Analysis ──────────────────────────────────────────────────────────────────

def main() -> int:
    trades     = _load_db_trades()
    decisions  = _load_db_decisions()
    csv_trades = _load_csv_trades()
    audit      = _load_audit()

    print("=" * 68)
    print("  LIVE TRADE QUALITY REPORT")
    print("=" * 68)

    # ── Data sources ──────────────────────────────────────────────────────────
    print(f"\n[DATA SOURCES]")
    print(f"  trades.db        : {'present' if DB_PATH.exists() else 'MISSING'} "
          f"({len(trades)} trade rows, {len(decisions)} decision rows)")
    print(f"  paper_trades.csv : {'present' if CSV_PATH.exists() else 'MISSING'} "
          f"({len(csv_trades)} rows)")
    print(f"  strike audit     : {'present' if AUDIT_PATH.exists() else 'MISSING'} "
          f"({len(audit)} records)")
    log_path = _latest_log()
    print(f"  latest log       : {log_path.name if log_path else 'none found'}")

    # ── Trade counts ─────────────────────────────────────────────────────────
    open_trades   = [t for t in trades if t.get("state") == "OPEN"]
    closed_trades = [t for t in trades if t.get("state") == "CLOSED"]
    print(f"\n[TRADE COUNTS]")
    print(f"  Total DB trades : {len(trades)}")
    print(f"  Open            : {len(open_trades)}")
    print(f"  Closed          : {len(closed_trades)}")

    # ── PnL by underlying ─────────────────────────────────────────────────────
    pnl_by_und: dict[str, float] = {}
    count_by_und: dict[str, int] = {}
    for t in closed_trades:
        und = t.get("underlying", "UNKNOWN")
        pnl = t.get("pnl") or 0.0
        pnl_by_und[und]   = pnl_by_und.get(und, 0.0) + pnl
        count_by_und[und] = count_by_und.get(und, 0) + 1
    print(f"\n[PnL BY UNDERLYING]")
    if pnl_by_und:
        for und, pnl in sorted(pnl_by_und.items()):
            print(f"  {und:20s}  trades={count_by_und[und]:3d}  pnl=₹{pnl:+,.2f}")
    else:
        print("  (no closed trades)")

    # ── Stop-loss count ───────────────────────────────────────────────────────
    sl_trades = [t for t in closed_trades if "STOP_LOSS" in (t.get("exit_reason") or "")]
    print(f"\n[STOP-LOSS EXITS]  count={len(sl_trades)}")
    for t in sl_trades:
        print(f"  {t['contract_symbol']:30s}  pnl=₹{(t.get('pnl') or 0):+,.2f}"
              f"  bars_held={t.get('bars_held', '?')}")

    # ── Same-contract re-entry after stop ─────────────────────────────────────
    stop_contracts: set[str] = {t["contract_symbol"] for t in sl_trades}
    reentry_after_stop: list[dict] = []
    for t in trades:
        if t.get("contract_symbol") in stop_contracts:
            # check if another trade with same symbol was opened after a stop
            same = [
                x for x in trades
                if x["contract_symbol"] == t["contract_symbol"]
                and x["id"] != t["id"]
                and (x.get("entry_bar_index") or 0) > (t.get("entry_bar_index") or 0)
            ]
            reentry_after_stop.extend(same)
    reentry_ids = {r["id"] for r in reentry_after_stop}
    print(f"\n[SAME-CONTRACT RE-ENTRY AFTER STOP]  count={len(reentry_ids)}")
    for t in reentry_after_stop:
        print(f"  id={t['id']}  {t['contract_symbol']}  bar={t.get('entry_bar_index', '?')}")

    # ── Decision gate statistics ───────────────────────────────────────────────
    def _count_outcome(keyword: str) -> int:
        return sum(1 for d in decisions if keyword in (d.get("outcome") or ""))

    def _count_filter(keyword: str) -> int:
        return sum(1 for d in decisions if keyword in (d.get("filter_reason") or ""))

    warm_locked    = _count_outcome("WARM_START_LOCKOUT")
    insuf_conf     = _count_outcome("INSUFFICIENT_CONFLUENCE")
    insuf_sq       = _count_outcome("INSUFFICIENT_STRIKE_QUALITY")
    ctx_contra     = _count_filter("CONTEXT_CONTRADICTION_REJECTED")

    print(f"\n[DECISION GATE STATISTICS]")
    print(f"  WARM_START_LOCKOUT blocks         : {warm_locked}")
    print(f"  INSUFFICIENT_CONFLUENCE blocks    : {insuf_conf}")
    print(f"  INSUFFICIENT_STRIKE_QUALITY blocks: {insuf_sq}")
    print(f"  CONTEXT_CONTRADICTION_REJECTED    : {ctx_contra}")

    # Entries during WARM_START lockout (should be 0 if gate is working)
    entries_during_warmup: list[dict] = [
        t for t in trades
        if (t.get("entry_bar_index") or 0) < _WARM_START_BARS
    ]
    print(f"\n[WARM_START entries (bar<{_WARM_START_BARS})]  count={len(entries_during_warmup)}")
    for t in entries_during_warmup:
        print(f"  id={t['id']}  {t['contract_symbol']}  bar={t.get('entry_bar_index','?')}")

    # ── Strike audit analysis ─────────────────────────────────────────────────
    low_oi_entries: list[dict] = []
    low_sm_entries: list[dict] = []
    for rec in audit:
        if rec.get("mode") == "shadow":
            continue
        fs = rec.get("factor_scores") or {}
        oi = fs.get("oi_support")
        sm = rec.get("score_margin")
        if oi is not None and oi < _MIN_OI_SUPPORT:
            low_oi_entries.append(rec)
        if sm is not None and sm < _MIN_SCORE_MARGIN:
            low_sm_entries.append(rec)

    print(f"\n[STRIKE AUDIT — LOW OI (oi<{_MIN_OI_SUPPORT})]  count={len(low_oi_entries)}")
    for r in low_oi_entries[:5]:
        fs_r = r.get("factor_scores") or {}
        print(f"  {r.get('selected_symbol','?'):30s}  oi={_fmt(fs_r.get('oi_support'), '.3f')}"
              f"  ts={r.get('ts','?')[:19]}")
    if len(low_oi_entries) > 5:
        print(f"  ... ({len(low_oi_entries)-5} more)")

    print(f"\n[STRIKE AUDIT — LOW SCORE MARGIN (margin<{_MIN_SCORE_MARGIN})]  "
          f"count={len(low_sm_entries)}")
    for r in low_sm_entries[:5]:
        print(f"  {r.get('selected_symbol','?'):30s}  margin={_fmt(r.get('score_margin'), '.3f')}"
              f"  ts={r.get('ts','?')[:19]}")
    if len(low_sm_entries) > 5:
        print(f"  ... ({len(low_sm_entries)-5} more)")

    # ── Enhanced audit analysis (new format: runner-up, top-5, dominance) ────
    enhanced = [r for r in audit if r.get("mode") != "shadow" and r.get("runner_up_contract")]
    old_fmt   = [r for r in audit if r.get("mode") != "shadow" and not r.get("runner_up_contract")]
    print(f"\n[STRIKE AUDIT — RUNNER-UP ANALYSIS]")
    if not enhanced:
        print(f"  (no enhanced-format records — run a new session after bfc30e8 commit)")
        print(f"  {len(old_fmt)} old-format records available (no runner-up data)")
    else:
        # Per underlying: selected vs runner-up score separation
        from collections import defaultdict as _dd
        by_sym_enh: dict[str, list[dict]] = _dd(list)
        for r in enhanced:
            by_sym_enh[r.get("underlying", "?")].append(r)

        for sym, recs_e in sorted(by_sym_enh.items()):
            margins = [r["score_margin"] for r in recs_e if r.get("score_margin") is not None]
            rup_same: list[str] = []
            for r in recs_e:
                sel  = r.get("selected_symbol", "")
                rup  = r.get("runner_up_contract", "")
                if sel and rup and sel == rup:
                    rup_same.append(sel)
            print(f"  {sym:12s}  records={len(recs_e)}"
                  f"  avg_margin={sum(margins)/len(margins):.4f}" if margins else "")
            if rup_same:
                print(f"    !! {len(rup_same)} bars: selected == runner-up (suspicious)")
            # Check strike dominance: is repeated strike score-driven?
            strike_counts: dict[str, int] = _dd(int)
            for r in recs_e:
                sel = r.get("selected_symbol") or r.get("strike") or "?"
                strike_counts[sel] += 1
            top_strike, top_cnt = max(strike_counts.items(), key=lambda x: x[1])
            print(f"    Most-selected strike: {top_strike}  ({top_cnt}/{len(recs_e)} bars)")
            if top_cnt > len(recs_e) * 0.8:
                print(f"    Score-driven (OI=1.0 for this strike every bar) — expected")

        # OI block classification: true tie or missing data?
        print(f"\n  Strike-quality block root cause (enhanced records):")
        for r in enhanced[:3]:
            fs  = r.get("factor_scores") or {}
            sm  = r.get("score_margin") or 0
            oi  = fs.get("oi_support") or 0
            rup = r.get("runner_up_contract", "n/a")
            top5 = r.get("top5") or []
            oi_in_top5 = [c.get("oi_support", 0) for c in top5 if c.get("oi_support") is not None]
            if sm < _MIN_SCORE_MARGIN and oi >= _MIN_OI_SUPPORT:
                block_type = "SCORE_MARGIN_ONLY"
            elif sm >= _MIN_SCORE_MARGIN and oi < _MIN_OI_SUPPORT:
                block_type = "OI_ONLY"
            elif sm < _MIN_SCORE_MARGIN and oi < _MIN_OI_SUPPORT:
                block_type = "BOTH_GATES"
            else:
                block_type = "PASSES"
            oi_range = f"{min(oi_in_top5):.3f}–{max(oi_in_top5):.3f}" if oi_in_top5 else "n/a"
            print(f"    {r.get('underlying','?'):12s}  sm={sm:.3f}  oi={oi:.3f}  "
                  f"block={block_type}  runner_up={rup}  top5_oi_range={oi_range}")
        if len(enhanced) > 3:
            print(f"    ... ({len(enhanced)-3} more enhanced records)")

    # ── Best and worst trade ──────────────────────────────────────────────────
    print(f"\n[BEST / WORST TRADE]")
    if closed_trades:
        best  = max(closed_trades, key=lambda t: t.get("pnl") or 0.0)
        worst = min(closed_trades, key=lambda t: t.get("pnl") or 0.0)
        print(f"  BEST  : {best['contract_symbol']:30s}  pnl=₹{(best.get('pnl') or 0):+,.2f}"
              f"  reason={best.get('exit_reason','?')}")
        print(f"  WORST : {worst['contract_symbol']:30s}  pnl=₹{(worst.get('pnl') or 0):+,.2f}"
              f"  reason={worst.get('exit_reason','?')}")
    else:
        print("  (no closed trades)")

    # ── Blocking defect check ─────────────────────────────────────────────────
    blocking_defects: list[str] = []

    if entries_during_warmup:
        blocking_defects.append(
            f"ENTRIES_DURING_WARMUP: {len(entries_during_warmup)} trade(s) opened before "
            f"bar {_WARM_START_BARS} — warm-start gate may have failed"
        )

    if reentry_ids:
        blocking_defects.append(
            f"SAME_CONTRACT_REENTRY: {len(reentry_ids)} re-entry(s) on stopped contracts"
        )

    # Check for stop-loss rate > 80% (all trades stopped out = systematic problem)
    if closed_trades and len(sl_trades) / len(closed_trades) > 0.80:
        blocking_defects.append(
            f"HIGH_STOP_RATE: {len(sl_trades)}/{len(closed_trades)} closed trades are stops "
            f"({len(sl_trades)/len(closed_trades):.0%})"
        )

    print(f"\n{'=' * 68}")
    if blocking_defects:
        print("  FINAL STATUS: BLOCKED_BY_RISK_DEFECTS")
        for d in blocking_defects:
            print(f"    !! {d}")
    else:
        print("  FINAL STATUS: PASS_FOR_NEXT_PAPER_RUN")
    print("=" * 68)

    return 1 if blocking_defects else 0


if __name__ == "__main__":
    sys.exit(main())
