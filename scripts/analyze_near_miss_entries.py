"""
analyze_near_miss_entries.py
──────────────────────────────────────────────────────────────────────
Post-market near-miss analyzer for blocked entries.

Reads (read-only; never mutates live policy):
  - logs/current_live_log.txt  (with stale-pointer fallback + repair)
  - logs/strike_selection_audit.jsonl
  - logs/trades.db pt_decisions rows (latest live session)

For each strike-audit candidate it reports:
  - which gate blocked it, score_margin, oi_support, runner_up_contract
  - top-5 factor quality (composite + weakest factor)
  - MCB bias/conf, whether risk would allow 1 lot
  - whether a context contradiction existed at that time
  - whether an order intent would have been generated under
    paper_trade_unlock

Classification:
  SAFE_NEAR_MISS   — no contradiction, not preopen/closing, oi >= 0.10,
                     risk OK, spread/liquidity acceptable, valid contract,
                     and only score_margin barely failed (>= 0.015 but
                     below the default 0.05)
  UNSAFE_NEAR_MISS — context contradiction, oi < 0.10, weak liquidity,
                     bad spread, closing/preopen, risk failed, or
                     wrong exchange / invalid contract
  NOT_NEAR_MISS    — passed all soft gates (not blocked by them) or
                     score_margin so low it fails even paper_trade_unlock

Also prints how many candidates would pass the soft gates under:
  default | banknifty_experiment | paper_trade_unlock

Usage:
  PYTHONPATH=. python3 scripts/analyze_near_miss_entries.py
  PYTHONPATH=. python3 scripts/analyze_near_miss_entries.py --capital 400000
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
_IST     = timezone(timedelta(hours=5, minutes=30))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core.execution.broker_adapter import (
    OrderIntent,
    _required_exchange,
    validate_order_intent,
)
from platform_core.execution.gate_profiles import PROFILE_GATE_TABLES
from platform_core.utils.live_log_resolver import resolve_current_live_log

# ── Analyzer thresholds (diagnostic only — never drives live policy) ─────────
DEFAULT_CAPITAL       = 400_000.0
HEAT_FRACTION         = 0.10    # mirrors _MAX_HEAT_FRACTION in the pipeline
OI_HARD_FLOOR         = 0.10    # mirrors _MIN_OI_SUPPORT_TIER23
IV_ATTR_MIN           = 0.05    # mirrors _MIN_IV_ATTR_TIER23
DEFAULT_SM_MIN        = 0.05    # mirrors _MIN_SCORE_MARGIN_TIER23
UNLOCK_SM_MIN         = 0.015   # paper_trade_unlock score_margin floor
MAX_SPREAD_PCT        = 1.0     # acceptable bid/ask spread (percent)
MIN_LIQUIDITY_FACTOR  = 0.30    # acceptable liquidity factor score
PREOPEN_END_IST       = (9, 30)
CLOSING_START_IST     = (15, 0)
CONTRADICTION_WINDOW  = timedelta(minutes=3)

PROFILES_COMPARED = ("default", "banknifty_experiment", "paper_trade_unlock")


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_audit_rows(audit_path: Path, date_str: str | None = None) -> list[dict]:
    """Load strike audit rows, optionally filtered to a YYYY-MM-DD date."""
    if not audit_path.exists():
        return []
    rows: list[dict] = []
    with audit_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if date_str and not str(r.get("ts", "")).startswith(date_str):
                continue
            rows.append(r)
    return rows


def count_order_intents(db_path: Path, date_str: str | None = None) -> int:
    """Count pt_order_intents rows (optionally for one YYYY-MM-DD date)."""
    return sum(order_intents_by_mode(db_path, date_str).values())


_KNOWN_EXECUTION_MODES = ("paper", "real_dry_run", "real_armed")


def order_intents_by_mode(db_path: Path, date_str: str | None = None) -> dict[str, int]:
    """
    Count pt_order_intents rows per execution mode (optionally one date).
    Missing/unrecognized modes are classified UNKNOWN, never real_dry_run.
    """
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        if date_str:
            rows = conn.execute(
                "SELECT mode, COUNT(*) FROM pt_order_intents "
                "WHERE created_ts LIKE ? GROUP BY mode",
                (f"{date_str}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT mode, COUNT(*) FROM pt_order_intents GROUP BY mode"
            ).fetchall()
        conn.close()
        counts: dict[str, int] = {}
        for mode, n in rows:
            key = mode if mode in _KNOWN_EXECUTION_MODES else "UNKNOWN"
            counts[key] = counts.get(key, 0) + int(n)
        return counts
    except Exception:
        return {}


def session_evidence(log_path: Path | None, db_path: Path,
                     date_str: str | None = None) -> dict:
    """
    Derive what the session ACTUALLY ran from evidence (log banner + DB),
    never from this analyzer's own environment.

    Returns dict with:
      execution_mode   — derived mode ("UNKNOWN" when no evidence)
      mode_source      — where it came from (log / db / log+db / none)
      mode_from_log    — [EXECUTION MODE] banner value or None
      modes_from_db    — pt_order_intents mode counts (date-filtered)
      mode_mismatch    — True when log and DB evidence disagree
      capital_from_log — [CAPITAL CONFIG] starting_capital or None
      state_equity_from_log — [STATE CAPITAL]/[STATE] Restored equity or None
    """
    mode_log: str | None = None
    capital_log: float | None = None
    state_equity_log: float | None = None
    if log_path is not None and log_path.exists():
        import re
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.search(r"\[EXECUTION MODE\]\s+(\S+)", line)
                if m and m.group(1) in _KNOWN_EXECUTION_MODES:
                    mode_log = m.group(1)
                m = re.search(r"\[CAPITAL CONFIG\] starting_capital=₹([\d,]+)", line)
                if m:
                    capital_log = float(m.group(1).replace(",", ""))
                m = re.search(r"\[STATE CAPITAL\] equity=₹([\d,]+)", line)
                if m:
                    state_equity_log = float(m.group(1).replace(",", ""))
                m = re.search(r"\[STATE\] Restored: equity=₹([\d,]+)", line)
                if m and state_equity_log is None:
                    state_equity_log = float(m.group(1).replace(",", ""))
        except Exception:
            pass

    modes_db = order_intents_by_mode(db_path, date_str)
    db_modes = [m for m in modes_db if m != "UNKNOWN"]
    mode_db  = db_modes[0] if len(db_modes) == 1 else None

    mismatch = (mode_log is not None and mode_db is not None and mode_log != mode_db)
    if mode_log and mode_db:
        derived, source = mode_log, ("log+db" if not mismatch else "log (DB DISAGREES)")
    elif mode_log:
        derived, source = mode_log, "log"
    elif mode_db:
        derived, source = mode_db, "db"
    else:
        derived, source = "UNKNOWN", "none"

    return {
        "execution_mode":        derived,
        "mode_source":           source,
        "mode_from_log":         mode_log,
        "modes_from_db":         modes_db,
        "mode_mismatch":         mismatch,
        "capital_from_log":      capital_log,
        "state_equity_from_log": state_equity_log,
    }


def load_decisions(db_path: Path, session_id: str | None = None) -> list[dict]:
    """Load pt_decisions rows for the latest live session (or a given one)."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if session_id is None:
            row = conn.execute(
                "SELECT session_id FROM pt_decisions WHERE run_mode='live' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.close()
                return []
            session_id = row["session_id"]
        out = [
            dict(r) for r in conn.execute(
                "SELECT * FROM pt_decisions WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        ]
        conn.close()
        return out
    except Exception:
        return []


def _parse_ts(raw: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def contradiction_index(decisions: list[dict]) -> dict[str, list[datetime]]:
    """underlying → timestamps of CONTEXT_CONTRADICTION_REJECTED decisions."""
    idx: dict[str, list[datetime]] = {}
    for d in decisions:
        if str(d.get("outcome", "")) != "CONTEXT_CONTRADICTION_REJECTED":
            continue
        ts = _parse_ts(d.get("bar_ts", ""))
        if ts is not None:
            idx.setdefault(str(d.get("underlying", "?")), []).append(ts)
    return idx


def nearest_conf(decisions: list[dict], underlying: str, ts: datetime | None) -> float | None:
    """Confidence of the decision row closest in time for this underlying."""
    if ts is None:
        return None
    best: tuple[float, float] | None = None
    for d in decisions:
        if str(d.get("underlying", "")) != underlying:
            continue
        dts = _parse_ts(d.get("bar_ts", ""))
        if dts is None:
            continue
        delta = abs((dts - ts).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, float(d.get("conf_raw", 0.0)))
    if best is not None and best[0] <= 600:
        return best[1]
    return None


# ── Gate simulation ──────────────────────────────────────────────────────────

def soft_gates_pass(row: dict, profile: str) -> bool:
    """Would this candidate clear the Tier2/3 soft quality gates under profile?"""
    table = PROFILE_GATE_TABLES.get(profile, PROFILE_GATE_TABLES["default"])
    und   = str(row.get("underlying", "?"))
    th    = table.get(und, {"score_margin": DEFAULT_SM_MIN, "oi_support": OI_HARD_FLOOR})
    sm    = float(row.get("score_margin", 0.0) or 0.0)
    fs    = row.get("factor_scores", {}) or {}
    oi    = float(fs.get("oi_support", 0.0) or 0.0)
    iv    = float(fs.get("iv_attractiveness", 0.0) or 0.0)
    return (
        sm >= th["score_margin"]
        and oi >= th["oi_support"]
        and iv >= IV_ATTR_MIN          # never relaxed by any profile
    )


def classify_near_miss(
    row: dict,
    *,
    had_contradiction: bool = False,
    capital: float = DEFAULT_CAPITAL,
) -> dict:
    """
    Classify one strike-audit candidate.

    Returns a dict with: classification, blocked_gate, unsafe_reasons,
    risk_ok, in_restricted_session, would_intent_under_unlock, plus the
    raw metrics used.
    """
    und      = str(row.get("underlying", "?"))
    sm       = float(row.get("score_margin", 0.0) or 0.0)
    fs       = row.get("factor_scores", {}) or {}
    oi       = float(fs.get("oi_support", 0.0) or 0.0)
    iv       = float(fs.get("iv_attractiveness", 0.0) or 0.0)
    liq      = float(fs.get("liquidity", 1.0) or 1.0)
    spread   = float(row.get("spread_pct", 0.0) or 0.0)
    premium  = float(row.get("premium", 0.0) or 0.0)
    lot_size = int(row.get("lot_size", 0) or 0)
    symbol   = str(row.get("selected_symbol", ""))

    # Session phase from candidate timestamp (IST)
    ts = _parse_ts(row.get("ts", ""))
    in_restricted = False
    if ts is not None:
        ist = ts.astimezone(_IST)
        hm  = (ist.hour, ist.minute)
        in_restricted = hm < PREOPEN_END_IST or hm >= CLOSING_START_IST

    # Risk: would 1 lot fit cash + portfolio heat budget?
    cost    = premium * lot_size
    risk_ok = (
        cost > 0
        and cost <= capital
        and cost <= capital * HEAT_FRACTION
    )

    # Contract validity / exchange routing (pure validation, no broker call)
    intent = OrderIntent(
        underlying       = und,
        tradingsymbol    = symbol,
        exchange         = _required_exchange(und),
        transaction_type = "BUY",
        quantity         = max(lot_size, 1),
        lot_size         = max(lot_size, 1),
        lots             = 1,
        product          = "NRML",
        order_type       = "MARKET",
        price            = 0.0,
        trigger_price    = 0.0,
        variety          = "regular",
        validity         = "DAY",
        tag              = "near_miss_analyzer",
        reason           = "ENTRY",
    )
    contract_ok, contract_reason = validate_order_intent(intent)

    unsafe_reasons: list[str] = []
    if had_contradiction:
        unsafe_reasons.append("CONTEXT_CONTRADICTION")
    if in_restricted:
        unsafe_reasons.append("PREOPEN_OR_CLOSING")
    if oi < OI_HARD_FLOOR:
        unsafe_reasons.append(f"LOW_OI oi_support={oi:.3f}<{OI_HARD_FLOOR}")
    if not risk_ok:
        unsafe_reasons.append(f"RISK_BLOCKED cost=₹{cost:,.0f}")
    if spread > MAX_SPREAD_PCT:
        unsafe_reasons.append(f"WIDE_SPREAD {spread:.2f}%>{MAX_SPREAD_PCT}%")
    if liq < MIN_LIQUIDITY_FACTOR:
        unsafe_reasons.append(f"WEAK_LIQUIDITY {liq:.2f}<{MIN_LIQUIDITY_FACTOR}")
    if not contract_ok:
        unsafe_reasons.append(f"INVALID_CONTRACT {contract_reason}")

    # Which soft gate blocked it under the default profile?
    blocked_gate = ""
    if oi < OI_HARD_FLOOR:
        blocked_gate = "INSUFFICIENT_CONFLUENCE"
    elif sm < DEFAULT_SM_MIN or iv < IV_ATTR_MIN:
        blocked_gate = "INSUFFICIENT_STRIKE_QUALITY"

    if unsafe_reasons:
        classification = "UNSAFE_NEAR_MISS"
    elif sm < DEFAULT_SM_MIN and sm >= UNLOCK_SM_MIN and iv >= IV_ATTR_MIN:
        # Only score_margin barely failed — everything hard-safe
        classification = "SAFE_NEAR_MISS"
    elif soft_gates_pass(row, "default"):
        classification = "NOT_NEAR_MISS"     # was not blocked by soft gates
    else:
        classification = "NOT_NEAR_MISS"     # deep miss — fails even unlock

    would_intent_under_unlock = (
        classification == "SAFE_NEAR_MISS"
        and soft_gates_pass(row, "paper_trade_unlock")
    )

    # Weakest top-rank factor (excluding composite)
    factor_items = [(k, float(v)) for k, v in fs.items() if k != "composite"]
    weakest = min(factor_items, key=lambda kv: kv[1]) if factor_items else ("n/a", 0.0)

    return {
        "underlying":               und,
        "symbol":                   symbol,
        "runner_up_contract":       row.get("runner_up_contract", ""),
        "classification":           classification,
        "blocked_gate":             blocked_gate,
        "unsafe_reasons":           unsafe_reasons,
        "score_margin":             sm,
        "oi_support":               oi,
        "iv_attr":                  iv,
        "liquidity":                liq,
        "spread_pct":               spread,
        "composite":                float(fs.get("composite", row.get("composite_score", 0.0)) or 0.0),
        "weakest_factor":           f"{weakest[0]}={weakest[1]:.3f}",
        "bias":                     row.get("bias", "?"),
        "risk_ok":                  risk_ok,
        "cost_one_lot":             cost,
        "in_restricted_session":    in_restricted,
        "had_contradiction":        had_contradiction,
        "would_intent_under_unlock": would_intent_under_unlock,
        "transaction_type":         "BUY",
        "quantity":                 lot_size,
        "ts":                       row.get("ts", ""),
    }


def analyze(
    audit_rows: list[dict],
    decisions: list[dict],
    capital: float = DEFAULT_CAPITAL,
) -> dict:
    """Classify all rows and compute profile what-if pass counts."""
    contra_idx = contradiction_index(decisions)
    results: list[dict] = []
    pass_counts = {p: 0 for p in PROFILES_COMPARED}

    for row in audit_rows:
        und = str(row.get("underlying", "?"))
        ts  = _parse_ts(row.get("ts", ""))
        had_contra = False
        if ts is not None:
            for cts in contra_idx.get(und, []):
                if abs((cts - ts).total_seconds()) <= CONTRADICTION_WINDOW.total_seconds():
                    had_contra = True
                    break
        res = classify_near_miss(row, had_contradiction=had_contra, capital=capital)
        res["mcb_conf"] = nearest_conf(decisions, und, ts)
        results.append(res)
        for p in PROFILES_COMPARED:
            if soft_gates_pass(row, p):
                pass_counts[p] += 1

    return {"results": results, "pass_counts": pass_counts, "total": len(audit_rows)}


# ── Dry-run shadow order-intent probe (Task: diagnostics only) ───────────────

def shadow_intent_probe(
    results: list[dict],
    *,
    execution_mode: str,
    intents_count: int,
) -> list[str]:
    """
    Diagnostic-only probe: when EXECUTION_MODE=real_dry_run produced zero
    order intents, report what the best valid near-miss OrderIntent WOULD
    have been.  Never writes to pt_order_intents, never places orders,
    never touches paper PnL, never marks real_funds_ready.
    """
    lines: list[str] = []
    lines.append("── Dry-run shadow order-intent probe (diagnostic only) ──")
    lines.append(f"execution_mode={execution_mode}  order_intents_recorded={intents_count}")

    if intents_count > 0:
        lines.append("Order intents were recorded — shadow probe not needed.")
        return lines

    near = [r for r in results
            if r["classification"] in ("SAFE_NEAR_MISS", "UNSAFE_NEAR_MISS")]
    if not near:
        lines.append("No near-miss candidates — nothing the probe could report.")
        return lines

    safe = sorted(
        (r for r in near if r["would_intent_under_unlock"]),
        key=lambda r: -r["score_margin"],
    )
    if safe:
        best = safe[0]
        lines.append("DRY_RUN_NEAR_MISS_SHADOW_INTENT (NOT a real order intent — passed all hard gates):")
        lines.append(
            f"  underlying={best['underlying']} symbol={best['symbol']} "
            f"side={best['transaction_type']} qty={best['quantity']} "
            f"est_cost=₹{best['cost_one_lot']:,.0f}"
        )
        lines.append(
            f"  score_margin={best['score_margin']:.4f} "
            f"oi_support={best['oi_support']:.3f} ts={best['ts']}"
        )
        lines.append(
            "  This entry would have produced a dry-run OrderIntent under "
            "paper_trade_unlock."
        )
    else:
        best = sorted(near, key=lambda r: -r["score_margin"])[0]
        reason = "; ".join(best["unsafe_reasons"]) or best["blocked_gate"] or "soft_gate_fail"
        lines.append(
            f"DRY_RUN_NEAR_MISS_ORDER_INTENT_SKIPPED reason={reason} "
            f"underlying={best['underlying']} symbol={best['symbol']} "
            f"score_margin={best['score_margin']:.4f}"
        )
    return lines


# ── Report ───────────────────────────────────────────────────────────────────

def print_report(analysis: dict, capital: float, log_path: Path | None) -> None:
    results     = analysis["results"]
    pass_counts = analysis["pass_counts"]

    print("=" * 70)
    print("  NEAR-MISS ENTRY ANALYSIS  (diagnostic only — no policy mutation)")
    print("=" * 70)
    print(f"  Source log     : {log_path.name if log_path else 'NOT FOUND'}")
    print(f"  Candidates     : {analysis['total']}")
    print(f"  Capital model  : ₹{capital:,.0f}  heat={HEAT_FRACTION:.0%}")

    by_class: dict[str, int] = {}
    for r in results:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
    print(f"\n  Classification : {by_class or 'no candidates'}")

    print("\n  Soft-gate what-if (candidates passing score_margin+oi+iv):")
    for p in PROFILES_COMPARED:
        print(f"    {p:24s} → {pass_counts[p]:4d} / {analysis['total']}")

    # Top near-misses per underlying (closest to passing, by score_margin)
    near = [r for r in results if r["classification"] in ("SAFE_NEAR_MISS", "UNSAFE_NEAR_MISS")]
    by_und: dict[str, list[dict]] = {}
    for r in near:
        by_und.setdefault(r["underlying"], []).append(r)

    for und in sorted(by_und):
        rows = sorted(by_und[und], key=lambda r: -r["score_margin"])[:5]
        print(f"\n  ── Top near-misses: {und} ({len(by_und[und])} total) ──")
        for r in rows:
            conf = f"{r['mcb_conf']:.2f}" if r.get("mcb_conf") is not None else "n/a"
            print(
                f"    [{r['classification']}] {r['symbol']}\n"
                f"      gate={r['blocked_gate'] or 'none'}  "
                f"score_margin={r['score_margin']:.4f}  oi_support={r['oi_support']:.3f}  "
                f"iv_attr={r['iv_attr']:.3f}\n"
                f"      runner_up={r['runner_up_contract']}  "
                f"composite={r['composite']:.3f}  weakest={r['weakest_factor']}\n"
                f"      bias={r['bias']}  conf={conf}  "
                f"risk_1lot={'OK' if r['risk_ok'] else 'BLOCKED'} (₹{r['cost_one_lot']:,.0f})  "
                f"contradiction={r['had_contradiction']}\n"
                f"      would_intent_under_paper_trade_unlock={r['would_intent_under_unlock']}"
                + (f"\n      unsafe: {'; '.join(r['unsafe_reasons'])}" if r["unsafe_reasons"] else "")
            )

    safe_unlock = sum(1 for r in results if r["would_intent_under_unlock"])
    print(
        f"\n  SAFE near-misses that would generate a dry-run order intent under "
        f"paper_trade_unlock: {safe_unlock}"
    )
    print("=" * 70)


def write_markdown_report(
    analysis: dict,
    capital: float,
    log_path: Path | None,
    probe_lines: list[str],
    out_dir: Path,
) -> Path | None:
    """Write the near-miss report under logs/ (only when logs/ exists)."""
    if not out_dir.is_dir():
        return None
    results     = analysis["results"]
    pass_counts = analysis["pass_counts"]
    date_str    = datetime.now(_IST).strftime("%Y%m%d")
    out_path    = out_dir / f"near_miss_report_{date_str}.md"

    by_class: dict[str, int] = {}
    for r in results:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1

    lines: list[str] = []
    lines.append(f"# Near-Miss Entry Report — {date_str}")
    lines.append("")
    lines.append("_Diagnostic only — no policy mutation, no orders, no PnL impact._")
    lines.append("")
    lines.append(f"- **Source log:** `{log_path.name if log_path else 'NOT FOUND'}`")
    lines.append(f"- **Candidates:** {analysis['total']}")
    lines.append(f"- **Capital model:** ₹{capital:,.0f} (heat {HEAT_FRACTION:.0%})")
    lines.append(f"- **Classification:** {by_class or 'no candidates'}")
    lines.append("")
    lines.append("## Soft-gate what-if")
    lines.append("")
    lines.append("| profile | pass | total |")
    lines.append("|---|---|---|")
    for p in PROFILES_COMPARED:
        lines.append(f"| {p} | {pass_counts[p]} | {analysis['total']} |")
    lines.append("")

    near = [r for r in results
            if r["classification"] in ("SAFE_NEAR_MISS", "UNSAFE_NEAR_MISS")]
    by_und: dict[str, list[dict]] = {}
    for r in near:
        by_und.setdefault(r["underlying"], []).append(r)
    for und in sorted(by_und):
        rows = sorted(by_und[und], key=lambda r: -r["score_margin"])[:5]
        lines.append(f"## Top near-misses — {und} ({len(by_und[und])} total)")
        lines.append("")
        for r in rows:
            conf = f"{r['mcb_conf']:.2f}" if r.get("mcb_conf") is not None else "n/a"
            lines.append(
                f"- **[{r['classification']}]** `{r['symbol']}` "
                f"gate={r['blocked_gate'] or 'none'} "
                f"score_margin={r['score_margin']:.4f} oi={r['oi_support']:.3f} "
                f"runner_up=`{r['runner_up_contract']}` weakest={r['weakest_factor']} "
                f"bias={r['bias']} conf={conf} "
                f"risk_1lot={'OK' if r['risk_ok'] else 'BLOCKED'} "
                f"contradiction={r['had_contradiction']} "
                f"would_intent_under_unlock={r['would_intent_under_unlock']}"
                + (f" — unsafe: {'; '.join(r['unsafe_reasons'])}" if r["unsafe_reasons"] else "")
            )
        lines.append("")

    lines.append("## Dry-run shadow order-intent probe")
    lines.append("")
    lines.append("```")
    lines.extend(probe_lines)
    lines.append("```")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Near-miss entry analyzer (read-only)")
    parser.add_argument("--capital", type=float, default=None,
                        help="Capital model for risk feasibility "
                             "(default: session's [CAPITAL CONFIG] from the live log, "
                             f"else {DEFAULT_CAPITAL:,.0f})")
    parser.add_argument("--date", default="",
                        help="Filter audit rows to YYYY-MM-DD (default: all)")
    args = parser.parse_args()

    log_path = resolve_current_live_log(LOGS_DIR)
    if log_path is None:
        print("[ERROR] No logs/live_run_*.log found — run a live session first.")
        return 1

    # Session evidence: execution mode + capital come from the actual session
    # (log banner + DB records), never from this analyzer's own environment.
    evidence = session_evidence(log_path, LOGS_DIR / "trades.db",
                                args.date or None)
    if args.capital is not None:
        capital, capital_src = args.capital, "--capital"
    elif evidence["capital_from_log"] is not None:
        capital, capital_src = evidence["capital_from_log"], "session log [CAPITAL CONFIG]"
    else:
        capital, capital_src = DEFAULT_CAPITAL, "analyzer default"

    audit_rows = load_audit_rows(LOGS_DIR / "strike_selection_audit.jsonl",
                                 args.date or None)
    decisions  = load_decisions(LOGS_DIR / "trades.db")

    if not audit_rows:
        print("[NEAR-MISS] No strike audit rows found — nothing to analyze.")
        return 0

    analysis = analyze(audit_rows, decisions, capital=capital)
    print_report(analysis, capital, log_path)
    print(f"\n  Capital source : {capital_src}")
    if evidence["state_equity_from_log"] is not None:
        print(f"  Session state  : equity=₹{evidence['state_equity_from_log']:,.0f} (from log)")
    print(
        f"  Execution mode : {evidence['execution_mode']} "
        f"(source={evidence['mode_source']}  "
        f"log={evidence['mode_from_log'] or 'n/a'}  "
        f"db_modes={evidence['modes_from_db'] or '{}'})"
    )
    if evidence["mode_mismatch"]:
        print("  [WARN] Execution-mode evidence MISMATCH between log and DB — investigate.")

    # Dry-run shadow probe — diagnostic only, never writes order intents
    intents_count = count_order_intents(LOGS_DIR / "trades.db", args.date or None)
    probe_lines = shadow_intent_probe(
        analysis["results"],
        execution_mode=evidence["execution_mode"],
        intents_count=intents_count,
    )
    print()
    for ln in probe_lines:
        print(f"  {ln}")

    report = write_markdown_report(
        analysis, capital, log_path, probe_lines, LOGS_DIR
    )
    if report is not None:
        print(f"\n  Markdown report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
