#!/usr/bin/env python3
"""
scripts/validate_live_lifecycle_state.py
════════════════════════════════════════════════════════════════════════
Offline analyzer for live paper trading run artifacts.

Reads DB, state JSON, and CSV — writes NOTHING.  Does not import any
live pipeline modules or connect to Kite.  Safe to run at any time.

Checks:
  1. DB pt_trades — counts, integrity, per-underlying open/closed state
  2. State JSON   — format version, open position / open_positions keys,
                   cross-reference with DB
  3. CSV          — duplicate detection, DB/CSV symbol cross-reference
  4. Safety       — no raw-index trades, no SENSEX-on-NFO, no NIFTY50 symbols

Exit codes:
  0 = PASS  (no issues found)
  1 = WARN  (non-critical findings; trading unaffected)
  2 = FAIL  (consistency broken; investigate before next live session)

Usage:
    PYTHONPATH=. python3 scripts/validate_live_lifecycle_state.py
    PYTHONPATH=. python3 scripts/validate_live_lifecycle_state.py \\
        --db logs/trades.db \\
        --state logs/paper_trading_state.json \\
        --csv logs/paper_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple


# ── Config ────────────────────────────────────────────────────────────────────

_KNOWN_UNDERLYINGS: frozenset[str] = frozenset({"BANKNIFTY", "NIFTY", "SENSEX"})
_RAW_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {"BANKNIFTY", "NIFTY", "NIFTY50", "SENSEX", "FINNIFTY"}
)
_W = 72


# ── Severity counters ─────────────────────────────────────────────────────────

class Report:
    def __init__(self) -> None:
        self.issues:   list[tuple[str, str, str]] = []   # (severity, area, message)

    def ok(self, area: str, msg: str) -> None:
        print(f"  {'OK':<6}  [{area}] {msg}")

    def warn(self, area: str, msg: str) -> None:
        self.issues.append(("WARN", area, msg))
        print(f"  {'WARN':<6}  [{area}] {msg}")

    def fail(self, area: str, msg: str) -> None:
        self.issues.append(("FAIL", area, msg))
        print(f"  {'FAIL':<6}  [{area}] {msg}")

    def info(self, area: str, msg: str) -> None:
        print(f"  {'INFO':<6}  [{area}] {msg}")

    def exit_code(self) -> int:
        if any(s == "FAIL" for s, _, _ in self.issues):
            return 2
        if any(s == "WARN" for s, _, _ in self.issues):
            return 1
        return 0


rpt = Report()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hdr(text: str, ch: str = "─") -> None:
    print(f"\n{ch * _W}")
    print(f"  {text}")
    print(f"{ch * _W}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Offline validator for live paper trading artifacts."
    )
    p.add_argument("--db",    default="logs/trades.db",
                   help="Path to SQLite DB (default: logs/trades.db)")
    p.add_argument("--state", default="logs/paper_trading_state.json",
                   help="Path to state JSON (default: logs/paper_trading_state.json)")
    p.add_argument("--csv",   default="logs/paper_trades.csv",
                   help="Path to trade CSV (default: logs/paper_trades.csv)")
    return p.parse_args()


# ── Section 1: DB checks ──────────────────────────────────────────────────────

def _check_db(db_path: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    Returns (open_rows_by_underlying, closed_rows_by_underlying).
    Each value is a list of row dicts from pt_trades.
    """
    open_by:   dict[str, list[dict]] = {}
    closed_by: dict[str, list[dict]] = {}

    if not Path(db_path).exists():
        rpt.warn("DB", f"File not found: {db_path}  (no trades yet, or wrong path)")
        return open_by, closed_by

    _hdr("DB — pt_trades")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        rpt.fail("DB", f"Cannot open {db_path}: {exc}")
        return open_by, closed_by

    try:
        # Total trade counts
        rows = conn.execute(
            "SELECT underlying, state, COUNT(*) AS n FROM pt_trades "
            "GROUP BY underlying, state ORDER BY underlying, state"
        ).fetchall()
        if not rows:
            rpt.info("DB", "pt_trades is empty — no trades recorded in this DB.")
        for r in rows:
            rpt.info("DB", f"underlying={r['underlying']}  state={r['state']}  count={r['n']}")

        # Fetch all rows for deeper checks
        all_rows = conn.execute(
            "SELECT * FROM pt_trades ORDER BY underlying, id"
        ).fetchall()
        all_dicts = [dict(r) for r in all_rows]

        # Partition
        for row in all_dicts:
            und = row.get("underlying", "UNKNOWN")
            if row["state"] == "OPEN":
                open_by.setdefault(und, []).append(row)
            else:
                closed_by.setdefault(und, []).append(row)

        # Check 1a: no more than 1 OPEN per underlying
        for und, rows_list in open_by.items():
            if len(rows_list) > 1:
                rpt.fail("DB", f"{und}: {len(rows_list)} OPEN rows — expected at most 1")
            else:
                rpt.ok("DB", f"{und}: exactly 1 OPEN row (id={rows_list[0]['id']})")

        # Check 1b: CLOSED rows completeness
        for und, rows_list in closed_by.items():
            missing_exit = [
                r["id"] for r in rows_list
                if r.get("exit_premium") is None or r.get("exit_ts") is None
                   or r.get("pnl") is None or r.get("equity_after") is None
            ]
            if missing_exit:
                rpt.fail("DB", f"{und}: CLOSED rows with missing exit fields: ids={missing_exit}")
            else:
                rpt.ok("DB", f"{und}: {len(rows_list)} CLOSED row(s) — all exit fields present")

        # Check 1c: OPEN rows completeness
        for und, rows_list in open_by.items():
            missing_entry = [
                r["id"] for r in rows_list
                if r.get("entry_premium") is None or r.get("entry_ts") is None
                   or r.get("lots") is None or r.get("lot_size") is None
            ]
            if missing_entry:
                rpt.fail("DB", f"{und}: OPEN rows with missing entry fields: ids={missing_entry}")
            else:
                rpt.ok("DB", f"{und}: OPEN row entry fields complete")

        # Check 1d: contract_symbol contains CE or PE
        bad_symbols = [
            (r["id"], r["contract_symbol"])
            for r in all_dicts
            if "CE" not in str(r.get("contract_symbol", ""))
            and "PE" not in str(r.get("contract_symbol", ""))
        ]
        if bad_symbols:
            rpt.fail("DB", f"Rows with non-option contract_symbol (missing CE/PE): {bad_symbols}")
        else:
            if all_dicts:
                rpt.ok("DB", "All contract_symbols contain CE or PE")

        # Check 1e: underlying is known
        unknown_und = [
            (r["id"], r["underlying"])
            for r in all_dicts
            if r.get("underlying") not in _KNOWN_UNDERLYINGS
        ]
        if unknown_und:
            rpt.fail("DB", f"Rows with unknown underlying: {unknown_und}")
        else:
            if all_dicts:
                rpt.ok("DB", "All underlyings are BANKNIFTY/NIFTY/SENSEX")

        # Check 1f: no raw index contract symbols
        raw_index_rows = [
            (r["id"], r["contract_symbol"])
            for r in all_dicts
            if r.get("contract_symbol") in _RAW_INDEX_SYMBOLS
        ]
        if raw_index_rows:
            rpt.fail("DB", f"Raw index trades detected (no CE/PE): {raw_index_rows}")
        else:
            if all_dicts:
                rpt.ok("DB", "No raw index trades in pt_trades")

        # Check 1g: no SENSEX on NFO
        sensex_nfo = [
            (r["id"], r.get("underlying"), r.get("contract_symbol"))
            for r in all_dicts
            if r.get("underlying") == "SENSEX"
            and str(r.get("contract_symbol", "")).startswith("NIFTY")
        ]
        if sensex_nfo:
            rpt.fail("DB", f"SENSEX trade with NFO-style contract symbol: {sensex_nfo}")
        else:
            if any(r.get("underlying") == "SENSEX" for r in all_dicts):
                rpt.ok("DB", "SENSEX trades use BFO-style symbols")

        # Check 1h: no NIFTY50 contract symbols
        nifty50_rows = [
            (r["id"], r.get("contract_symbol"))
            for r in all_dicts
            if str(r.get("contract_symbol", "")).startswith("NIFTY50")
        ]
        if nifty50_rows:
            rpt.fail("DB", f"NIFTY50 contract symbol in trades (should be NIFTY): {nifty50_rows}")
        else:
            if all_dicts:
                rpt.ok("DB", "No NIFTY50 contract symbols in trades")

    except Exception as exc:
        rpt.fail("DB", f"Query error: {exc}")
    finally:
        conn.close()

    return open_by, closed_by


# ── Section 2: State JSON checks ─────────────────────────────────────────────

def _check_state(
    state_path: str,
    db_open_by: dict[str, list[dict]],
) -> dict[str, dict | None]:
    """Returns {underlying: pos_dict_or_None} from state file."""
    state_positions: dict[str, dict | None] = {}

    if not Path(state_path).exists():
        rpt.warn("STATE", f"File not found: {state_path}  (not yet written or wrong path)")
        return state_positions

    _hdr("STATE — paper_trading_state.json")
    try:
        raw = json.loads(Path(state_path).read_text())
    except Exception as exc:
        rpt.fail("STATE", f"Cannot parse {state_path}: {exc}")
        return state_positions

    # Format detection
    has_new_format = "open_positions" in raw and raw["open_positions"] is not None
    has_legacy      = "open_position" in raw

    if has_new_format:
        rpt.ok("STATE", "Format: Phase-C (open_positions dict present)")
        for und, pos in raw["open_positions"].items():
            state_positions[und] = pos
    elif has_legacy:
        rpt.warn(
            "STATE",
            "Format: pre-Phase-C (open_position key only, no open_positions dict).  "
            "Next pipeline write will upgrade to Phase-C format."
        )
        pos = raw.get("open_position")
        if pos is not None:
            und = pos.get("underlying_symbol", "BANKNIFTY")
            state_positions[und] = pos
        else:
            rpt.info("STATE", "open_position is null — no position in legacy state")
    else:
        rpt.warn("STATE", "Neither open_position nor open_positions found — fresh state?")

    # Report per-underlying state
    for und in _KNOWN_UNDERLYINGS:
        pos = state_positions.get(und)
        if pos is None:
            rpt.info("STATE", f"{und}: no open position in state")
        else:
            rpt.info(
                "STATE",
                f"{und}: open — {pos.get('contract_symbol')}  "
                f"lots={pos.get('lots')}  entry=₹{pos.get('entry_premium')}  "
                f"lot_size={pos.get('lot_size')}  direction={pos.get('direction')}"
            )

    # Cross-check state vs DB
    for und, pos in state_positions.items():
        if pos is None:
            continue
        db_open = db_open_by.get(und, [])
        state_sym = pos.get("contract_symbol")
        if not db_open:
            rpt.warn(
                "STATE",
                f"{und}: state has open position {state_sym!r} "
                f"but DB has no OPEN row — possible stale state or DB from a different session"
            )
        else:
            db_sym = db_open[0].get("contract_symbol")
            if db_sym == state_sym:
                rpt.ok("STATE", f"{und}: state and DB agree on open position ({state_sym})")
            else:
                rpt.fail(
                    "STATE",
                    f"{und}: state/DB mismatch — state={state_sym!r} DB={db_sym!r}"
                )

    # Check for DB OPEN rows that have no state position
    for und, db_rows in db_open_by.items():
        if und not in state_positions or state_positions.get(und) is None:
            db_sym = db_rows[0].get("contract_symbol")
            rpt.warn(
                "STATE",
                f"{und}: DB has OPEN row {db_sym!r} "
                f"but state has no open position — stale DB row or state reset"
            )

    # Legacy open_position must not contradict open_positions if both exist
    if has_new_format and has_legacy and raw.get("open_position") is not None:
        legacy_pos = raw["open_position"]
        legacy_sym = legacy_pos.get("contract_symbol")
        legacy_und = legacy_pos.get("underlying_symbol", "BANKNIFTY")
        new_pos    = raw["open_positions"].get(legacy_und)
        if new_pos is not None and new_pos.get("contract_symbol") != legacy_sym:
            rpt.warn(
                "STATE",
                f"Legacy open_position ({legacy_sym}) differs from "
                f"open_positions[{legacy_und}] ({new_pos.get('contract_symbol')}) — "
                f"open_positions is authoritative"
            )
        else:
            rpt.ok("STATE", "Legacy open_position consistent with open_positions dict")

    # Equity / peak sanity
    equity      = raw.get("equity", 0)
    peak_equity = raw.get("peak_equity", 0)
    if equity > 0 and peak_equity >= equity * 0.5:
        rpt.ok("STATE", f"equity=₹{equity:,.0f}  peak=₹{peak_equity:,.0f}")
    else:
        rpt.warn("STATE", f"Unusual equity values: equity={equity}  peak={peak_equity}")

    # Bar history
    if "bar_history_by_underlying" in raw:
        rpt.ok("STATE", "bar_history_by_underlying present (Phase-C format)")
    elif "bar_history" in raw:
        rpt.warn("STATE", "Only legacy bar_history present — NIFTY/SENSEX MCB will cold-start")
    else:
        rpt.warn("STATE", "No bar history in state — all underlyings MCB will cold-start")

    return state_positions


# ── Section 3: CSV checks ─────────────────────────────────────────────────────

def _check_csv(
    csv_path: str,
    closed_by: dict[str, list[dict]],
) -> None:
    if not Path(csv_path).exists():
        rpt.warn("CSV", f"File not found: {csv_path}  (no completed trades yet)")
        return

    _hdr("CSV — paper_trades.csv")
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            csv_rows = list(reader)
    except Exception as exc:
        rpt.fail("CSV", f"Cannot read {csv_path}: {exc}")
        return

    rpt.info("CSV", f"{len(csv_rows)} data row(s) found")

    if not csv_rows:
        rpt.info("CSV", "Empty file — no completed trades logged yet")
        return

    # Detect duplicate rows (same symbol + direction + entry_price)
    seen: set[tuple] = set()
    dupes: list[int] = []
    for i, row in enumerate(csv_rows):
        key = (
            row.get("symbol", row.get("contract_symbol", "")),
            row.get("direction", ""),
            row.get("entry_price", row.get("entry_premium", "")),
        )
        if key in seen:
            dupes.append(i + 2)  # 1-indexed + header
        seen.add(key)
    if dupes:
        rpt.fail("CSV", f"Duplicate rows detected at line(s): {dupes}")
    else:
        rpt.ok("CSV", "No duplicate rows")

    # Symbol-level cross-reference with DB closed rows
    # CSV uses 'symbol' column; DB uses 'contract_symbol'
    sym_col = "symbol" if "symbol" in (csv_rows[0] if csv_rows else {}) else "contract_symbol"
    csv_symbols: set[str] = {row.get(sym_col, "") for row in csv_rows}
    db_closed_symbols: set[str] = {
        r["contract_symbol"]
        for rows_list in closed_by.values()
        for r in rows_list
    }

    in_db_not_csv = db_closed_symbols - csv_symbols
    in_csv_not_db = csv_symbols - db_closed_symbols - {""}

    if in_db_not_csv:
        rpt.warn(
            "CSV",
            f"DB-CLOSED trades not found in CSV (possible crash before CSV write): "
            f"{sorted(in_db_not_csv)}"
        )
    else:
        if db_closed_symbols:
            rpt.ok("CSV", "All DB-CLOSED trades have a matching symbol in CSV")

    if in_csv_not_db:
        rpt.warn(
            "CSV",
            f"CSV symbols not in DB-CLOSED (from a previous session DB not retained): "
            f"{sorted(in_csv_not_db)}"
        )

    # Safety: no raw index symbol rows
    raw_csv_trades = [
        (i + 2, row.get(sym_col, ""))
        for i, row in enumerate(csv_rows)
        if row.get(sym_col, "") in _RAW_INDEX_SYMBOLS
    ]
    if raw_csv_trades:
        rpt.fail("CSV", f"Raw index symbol in CSV trades: {raw_csv_trades}")
    else:
        rpt.ok("CSV", "No raw index symbols in CSV")


# ── Section 4: Cross-section open-premium sanity ─────────────────────────────

def _check_premium_sanity(
    state_path: str,
    state_positions: dict[str, dict | None],
) -> None:
    _hdr("PREMIUM SANITY")
    if not Path(state_path).exists():
        rpt.info("PREMIUM", "State file absent — skipping premium sanity check")
        return

    try:
        raw = json.loads(Path(state_path).read_text())
    except Exception:
        return

    computed = 0.0
    for und in _KNOWN_UNDERLYINGS:
        pos = state_positions.get(und)
        if pos is not None:
            ep = pos.get("entry_premium", 0) or 0
            ls = pos.get("lot_size", 0) or 0
            lt = pos.get("lots", 0) or 0
            computed += ep * ls * lt

    if computed > 0:
        rpt.info("PREMIUM", f"Computed open premium cost from state positions: ₹{computed:,.2f}")
    else:
        rpt.info("PREMIUM", "No open positions in state — open premium cost = ₹0")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()

    print("═" * _W)
    print("  LIVE PAPER TRADING LIFECYCLE VALIDATOR")
    print(f"  DB    : {args.db}")
    print(f"  STATE : {args.state}")
    print(f"  CSV   : {args.csv}")
    print("═" * _W)

    db_open_by, db_closed_by = _check_db(args.db)
    state_positions = _check_state(args.state, db_open_by)
    _check_csv(args.csv, db_closed_by)
    _check_premium_sanity(args.state, state_positions)

    # Final summary
    _hdr("SUMMARY", "═")
    fails = [(a, m) for s, a, m in rpt.issues if s == "FAIL"]
    warns = [(a, m) for s, a, m in rpt.issues if s == "WARN"]

    print(f"  FAILs : {len(fails)}")
    for a, m in fails:
        print(f"    [{a}] {m}")
    print(f"  WARNs : {len(warns)}")
    for a, m in warns:
        print(f"    [{a}] {m}")

    code = rpt.exit_code()
    verdict = {0: "PASS", 1: "WARN", 2: "FAIL"}[code]
    print(f"\n  Overall: {verdict}")
    print("═" * _W)
    print()
    return code


if __name__ == "__main__":
    sys.exit(main())
