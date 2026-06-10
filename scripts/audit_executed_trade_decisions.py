"""
audit_executed_trade_decisions.py
──────────────────────────────────────────────────────────────────────
Post-market audit: correlate every executed trade with the decision
context it was entered under.  Read-only — never mutates DB/logs/state.

For each executed trade (pt_trades, latest live session by default):
  - trade core fields (id, ts, underlying, contract, entry/exit, pnl)
  - the pt_decisions ENTRY row used (same underlying, ts within ±90s)
  - the nearest strike_selection_audit.jsonl record for the contract
  - the [ENTRY HARD GATES]/[ENTRY SOFT GATES] snapshot from the live log
    (sessions before the snapshot existed report it as MISSING)
  - execution_mode + order_intent_id from pt_order_intents (entry + exit)
  - context-contradiction evidence:
      FAIL  — CONTEXT_CONTRADICTION_REJECTED at the SAME bar as entry
      WARN  — contradiction on the same underlying within 3 min before entry

Verdict:
  PASS — every trade has matching decision + strike audit (+ order intent
         when the session ran real_dry_run/real_armed), no trade entered
         while context_contradiction=True, and no paper_trade_unlock
         hard-gate bypass evidence.
  FAIL — any trade lacks context evidence, entered on a contradiction bar,
         or shows a hard-gate bypass.

Usage:
  PYTHONPATH=. python3 scripts/audit_executed_trade_decisions.py
  PYTHONPATH=. python3 scripts/audit_executed_trade_decisions.py --session live_20260610_061507
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core.execution.context_cooldown import (
    contradiction_cooldown_active,
    cooldown_bars_from_env,
)
from platform_core.utils.live_log_resolver import resolve_current_live_log

_DECISION_MATCH_WINDOW   = 90.0          # seconds: trade entry_ts ↔ decision bar_ts
_AUDIT_MATCH_WINDOW      = 300.0         # seconds: trade entry_ts ↔ strike audit ts
_INTENT_MATCH_WINDOW     = 90.0          # seconds: trade ts ↔ order intent created_ts
_CONTRA_WARN_WINDOW      = timedelta(minutes=3)
_KNOWN_MODES             = ("paper", "real_dry_run", "real_armed")


def _parse_ts(raw: str | None) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _nearest(rows: list[dict], ts: datetime | None, key: str,
             window: float) -> dict | None:
    """Row whose `key` timestamp is closest to ts and within window seconds."""
    if ts is None:
        return None
    best: tuple[float, dict] | None = None
    for r in rows:
        rts = _parse_ts(r.get(key))
        if rts is None:
            continue
        delta = abs((rts - ts).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, r)
    if best is not None and best[0] <= window:
        return best[1]
    return None


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_session_rows(db_path: Path, session_id: str | None):
    """Return (session_id, trades, decisions, intents) for the audit."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if session_id is None:
        row = conn.execute(
            "SELECT session_id FROM pt_trades WHERE run_mode='live' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.close()
            return None, [], [], []
        session_id = row["session_id"]
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM pt_trades WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()]
    decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM pt_decisions WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()]
    # Intents have no session column — filter by the session's time range later.
    intents = [dict(r) for r in conn.execute(
        "SELECT * FROM pt_order_intents ORDER BY id"
    ).fetchall()]
    conn.close()
    return session_id, trades, decisions, intents


def load_strike_audits(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    rows: list[dict] = []
    with audit_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def load_log_evidence(log_path: Path | None) -> dict:
    """Extract execution mode, gate profile, and gate-snapshot lines from log."""
    out = {"execution_mode": None, "profile": None,
           "hard_gate_lines": [], "soft_gate_lines": []}
    if log_path is None or not log_path.exists():
        return out
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"\[EXECUTION MODE\]\s+(\S+)", line)
            if m and m.group(1) in _KNOWN_MODES:
                out["execution_mode"] = m.group(1)
            m = re.search(r"\[GATE PROFILE\] profile=(\S+)", line)
            if m:
                out["profile"] = m.group(1)
            if "[ENTRY HARD GATES]" in line:
                out["hard_gate_lines"].append(line.strip())
            if "[ENTRY SOFT GATES]" in line:
                out["soft_gate_lines"].append(line.strip())
    except Exception:
        pass
    return out


# ── Per-trade audit ──────────────────────────────────────────────────────────

def audit_trade(trade: dict, decisions: list[dict], audits: list[dict],
                intents: list[dict], log_evidence: dict) -> dict:
    und       = str(trade.get("underlying", "?"))
    contract  = str(trade.get("contract_symbol", "?"))
    entry_ts  = _parse_ts(trade.get("entry_ts"))
    exit_ts   = _parse_ts(trade.get("exit_ts"))

    # Decision row used at entry
    entry_decisions = [
        d for d in decisions
        if str(d.get("underlying")) == und
        and str(d.get("outcome", "")).startswith("ENTRY_TIER")
    ]
    decision = _nearest(entry_decisions, entry_ts, "bar_ts", _DECISION_MATCH_WINDOW)

    # Contradictions on this underlying
    contras = [
        d for d in decisions
        if str(d.get("underlying")) == und
        and str(d.get("outcome", "")) == "CONTEXT_CONTRADICTION_REJECTED"
    ]
    contra_same_bar = False
    contra_recent: dict | None = None
    bars_since_contra: int | None = None
    cooldown_bars = cooldown_bars_from_env()
    cooldown_violated = False
    if decision is not None:
        entry_bar = decision.get("bar_index")
        contra_same_bar = any(
            d.get("bar_index") == entry_bar for d in contras
        )
        # Bars since the most recent contradiction at-or-before the entry bar
        prior_bars = [d.get("bar_index") for d in contras
                      if isinstance(d.get("bar_index"), int)
                      and d.get("bar_index") <= entry_bar]
        if prior_bars:
            last_contra_bar = max(prior_bars)
            cooldown_violated, bars_since_contra = contradiction_cooldown_active(
                entry_bar, last_contra_bar, cooldown_bars
            )
    elif entry_ts is not None:
        # No decision row — fall back to wall-clock evidence (WARN only)
        for d in contras:
            dts = _parse_ts(d.get("bar_ts"))
            if dts is None or dts > entry_ts:
                continue
            if entry_ts - dts <= _CONTRA_WARN_WINDOW:
                contra_recent = d
                break

    # Strike audit record for the selected contract nearest entry time
    contract_audits = [a for a in audits
                       if str(a.get("selected_symbol")) == contract]
    strike_audit = _nearest(contract_audits, entry_ts, "ts", _AUDIT_MATCH_WINDOW)

    # Order intents (entry BUY + exit SELL) for this contract
    contract_intents = [i for i in intents
                        if str(i.get("tradingsymbol")) == contract]
    entry_intent = _nearest(
        [i for i in contract_intents if i.get("transaction_type") == "BUY"],
        entry_ts, "created_ts", _INTENT_MATCH_WINDOW)
    exit_intent = _nearest(
        [i for i in contract_intents if i.get("transaction_type") == "SELL"],
        exit_ts, "created_ts", _INTENT_MATCH_WINDOW)

    intent_mode = entry_intent.get("mode") if entry_intent else None
    if intent_mode is not None and intent_mode not in _KNOWN_MODES:
        intent_mode = "UNKNOWN"
    execution_mode = intent_mode or log_evidence.get("execution_mode") or "UNKNOWN"

    # Hard/soft gate snapshot from the log (sessions before the snapshot
    # feature report MISSING — informational, not a failure by itself)
    hard_snap = next((ln for ln in log_evidence["hard_gate_lines"]
                      if f"symbol={contract}" in ln), None)
    soft_snap = log_evidence["soft_gate_lines"][0] if hard_snap and \
        log_evidence["soft_gate_lines"] else None

    profile = (log_evidence.get("profile") or "unknown").lower()
    intents_expected = execution_mode in ("real_dry_run", "real_armed")

    failures: list[str] = []
    warnings: list[str] = []
    if decision is None:
        failures.append("NO_MATCHING_DECISION_ROW")
    if strike_audit is None:
        failures.append("NO_MATCHING_STRIKE_AUDIT")
    if intents_expected and entry_intent is None:
        failures.append("NO_MATCHING_ORDER_INTENT")
    if contra_same_bar:
        failures.append("ENTERED_ON_CONTRADICTION_BAR")
        if profile == "paper_trade_unlock":
            failures.append("PAPER_TRADE_UNLOCK_HARD_GATE_BYPASS")
    if cooldown_violated and not contra_same_bar:
        failures.append(
            f"ENTERED_WITHIN_CONTRADICTION_COOLDOWN "
            f"bars_since={bars_since_contra}<={cooldown_bars}"
        )
        # Only a bypass accusation when the session ran with the cooldown gate
        # (evidenced by a hard-gate snapshot); pre-gate sessions are flagged by
        # the cooldown failure above without claiming a bypass.
        if profile == "paper_trade_unlock" and hard_snap is not None:
            failures.append("PAPER_TRADE_UNLOCK_HARD_GATE_BYPASS")
    if decision is None and contras:
        warnings.append("CONTRADICTION_HISTORY_UNMATCHED (no decision row — "
                        "cooldown could not be evaluated by bar index)")
    if contra_recent is not None and not contra_same_bar:
        warnings.append(
            f"CONTRADICTION_WITHIN_3MIN_BEFORE_ENTRY "
            f"(bar={contra_recent.get('bar_index')} ts={contra_recent.get('bar_ts')})"
        )
    if hard_snap is None:
        warnings.append("HARD_GATE_SNAPSHOT_MISSING (session pre-dates snapshot logging)")

    return {
        "trade_id":        trade.get("id"),
        "timestamp":       trade.get("entry_ts"),
        "underlying":      und,
        "contract_symbol": contract,
        "entry_price":     trade.get("entry_premium"),
        "exit_price":      trade.get("exit_premium"),
        "pnl":             trade.get("pnl"),
        "state":           trade.get("state"),
        "decision_id":     decision.get("id") if decision else None,
        "decision_bar":    decision.get("bar_index") if decision else None,
        "strike_audit_ts": strike_audit.get("ts") if strike_audit else None,
        "score_margin":    strike_audit.get("score_margin") if strike_audit else None,
        "hard_gate_snapshot": hard_snap or "MISSING",
        "soft_gate_snapshot": soft_snap or "MISSING",
        "execution_mode":  execution_mode,
        "order_intent_id": entry_intent.get("id") if entry_intent else None,
        "exit_intent_id":  exit_intent.get("id") if exit_intent else None,
        "bars_since_last_context_contradiction": bars_since_contra,
        "contradiction_cooldown_bars": cooldown_bars,
        "failures":        failures,
        "warnings":        warnings,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit executed trades against their decision context (read-only)")
    parser.add_argument("--session", default=None,
                        help="pt_trades session_id (default: latest live session)")
    args = parser.parse_args()

    db_path = LOGS_DIR / "trades.db"
    if not db_path.exists():
        print("[ERROR] logs/trades.db not found — nothing to audit.")
        return 1

    session_id, trades, decisions, intents = load_session_rows(db_path, args.session)
    if not trades:
        print(f"[AUDIT] No trades found for session={session_id or args.session} — nothing to audit.")
        return 0

    audits       = load_strike_audits(LOGS_DIR / "strike_selection_audit.jsonl")
    log_path     = resolve_current_live_log(LOGS_DIR)
    log_evidence = load_log_evidence(log_path)

    print("=" * 70)
    print("  EXECUTED TRADE DECISION AUDIT  (read-only)")
    print("=" * 70)
    print(f"  Session        : {session_id}")
    print(f"  Source log     : {log_path.name if log_path else 'NOT FOUND'}")
    print(f"  Execution mode : {log_evidence.get('execution_mode') or 'UNKNOWN'} (from log)")
    print(f"  Gate profile   : {log_evidence.get('profile') or 'UNKNOWN'} (from log)")
    print(f"  Trades         : {len(trades)}")

    results = [audit_trade(t, decisions, audits, intents, log_evidence)
               for t in trades]

    any_fail = False
    for r in results:
        verdict = "FAIL" if r["failures"] else "PASS"
        any_fail = any_fail or bool(r["failures"])
        print(f"\n  ── Trade #{r['trade_id']}  [{verdict}] ──")
        print(f"    ts={r['timestamp']}  {r['underlying']}  {r['contract_symbol']}  state={r['state']}")
        _entry = r['entry_price']
        _exit  = r['exit_price']
        _pnl   = r['pnl']
        print(f"    entry=₹{_entry:.2f}" if _entry is not None else "    entry=n/a", end="")
        print(f"  exit=₹{_exit:.2f}" if _exit is not None else "  exit=n/a", end="")
        print(f"  pnl=₹{_pnl:+,.2f}" if _pnl is not None else "  pnl=n/a")
        print(f"    decision_row    : id={r['decision_id']}  bar={r['decision_bar']}")
        print(f"    strike_audit    : ts={r['strike_audit_ts']}  "
              f"score_margin={r['score_margin']}")
        print(f"    execution_mode  : {r['execution_mode']}")
        print(f"    order_intent_id : entry={r['order_intent_id']}  exit={r['exit_intent_id']}")
        _bs = r["bars_since_last_context_contradiction"]
        print(f"    contra_cooldown : bars_since="
              f"{_bs if _bs is not None else 'none'}"
              f"  cooldown={r['contradiction_cooldown_bars']}")
        print(f"    hard_gates      : {r['hard_gate_snapshot']}")
        print(f"    soft_gates      : {r['soft_gate_snapshot']}")
        for f in r["failures"]:
            print(f"    [FAIL] {f}")
        for w in r["warnings"]:
            print(f"    [WARN] {w}")

    print(f"\n{'=' * 70}")
    if any_fail:
        print("  AUDIT RESULT: FAIL — one or more trades lack context evidence "
              "or violated a hard gate.")
    else:
        n_warn = sum(len(r["warnings"]) for r in results)
        print(f"  AUDIT RESULT: PASS — all {len(results)} trade(s) have matching "
              f"decision + strike audit + order-intent evidence"
              + (f"  ({n_warn} warning(s))" if n_warn else ""))
    print("=" * 70)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
