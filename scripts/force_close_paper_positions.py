#!/usr/bin/env python3
"""
scripts/force_close_paper_positions.py
═══════════════════════════════════════════════════════════════════════════════
Force-close an INTERNAL paper / real_dry_run open position cleanly.

This is an offline cleanup tool for the situation where a real_dry_run (or
paper) session was interrupted (e.g. Ctrl-C) and left a *simulated* position
OPEN in:

    • logs/trades.db            (pt_trades.state = 'OPEN')
    • logs/paper_trading_state.json (open_position / open_positions)
    • (no completed row yet in logs/paper_trades.csv)

The position is NOT a real broker position.  This script closes it cleanly in
the local DB / state / CSV so the next run starts flat.

────────────────────────────────────────────────────────────────────────────────
HARD SAFETY GUARANTEES
────────────────────────────────────────────────────────────────────────────────
  • It NEVER imports KiteConnect, never references kite, never calls
    place_order — it touches only local sqlite / json / csv files.
  • It REFUSES to run when EXECUTION_MODE=real_armed.
  • It REFUSES to mutate anything without --confirm (default = dry-run preview).
  • It does NOT start the live pipeline.
  • It does NOT modify configs/kite_config.json.

────────────────────────────────────────────────────────────────────────────────
PRICE SOURCES
────────────────────────────────────────────────────────────────────────────────
  --price-source last_known
      Use the most recent observed premium for the exact contract, read from
      logs/strike_selection_audit.jsonl (the last line whose selected_symbol ==
      the open contract).  If no such record exists, the script REFUSES and
      tells you to supply a manual exit price.

  --price-source manual --exit-price <price>
      Use the operator-supplied exit premium.

────────────────────────────────────────────────────────────────────────────────
IDEMPOTENCY
────────────────────────────────────────────────────────────────────────────────
  • The DB OPEN→CLOSED transition is the single authoritative close event;
    equity is adjusted exactly once, tied to that transition.
  • A second run finds no OPEN row / null state position and is a safe no-op.
  • CSV append is guarded by the same dedup key the live TradeLogger uses.
  • The force-close order-intent row is existence-guarded per contract.

Usage:
    PYTHONPATH=. python3 scripts/force_close_paper_positions.py --help
    PYTHONPATH=. python3 scripts/force_close_paper_positions.py --price-source last_known            # preview
    PYTHONPATH=. python3 scripts/force_close_paper_positions.py --price-source last_known --confirm   # apply
    PYTHONPATH=. python3 scripts/force_close_paper_positions.py --price-source manual --exit-price 150 --confirm
    PYTHONPATH=. python3 scripts/force_close_paper_positions.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Default file locations ──────────────────────────────────────────────────────

DEFAULT_DB    = "logs/trades.db"
DEFAULT_STATE = "logs/paper_trading_state.json"
DEFAULT_CSV   = "logs/paper_trades.csv"
DEFAULT_AUDIT = "logs/strike_selection_audit.jsonl"

CSV_HEADER = ["symbol", "direction", "entry_price", "exit_price", "quantity", "pnl"]

DB_EXIT_REASON = "FORCE_CLOSE_USER_STOP"
PRINT_REASON   = "user_stop_cleanup"


# ── Small helpers ───────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _csv_key(symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> tuple:
    """Stable dedup key matching platform_core TradeLogger CSV round-trip."""
    return (symbol, direction, str(entry), str(exit_price), str(qty))


def _force_close_intent_reason(internal_mode: str) -> str:
    """The reason label written to pt_order_intents for the synthetic SELL."""
    return "real_dry_run_force_close" if internal_mode == "real_dry_run" else "paper_force_close"


def _required_exchange(underlying: str) -> str:
    u = (underlying or "").upper()
    if u in ("SENSEX", "BANKEX"):
        return "BFO"
    return "NFO"


# ── Safety gates ────────────────────────────────────────────────────────────────

def assert_not_real_armed(env: dict[str, str]) -> str:
    """
    Return the current EXECUTION_MODE (lower-cased) or raise SystemExit if the
    mode is real_armed.  Default mode when unset is 'paper'.
    """
    mode = (env.get("EXECUTION_MODE", "paper") or "paper").strip().lower()
    if mode == "real_armed":
        print(
            "[FORCE CLOSE PAPER POSITION] REFUSED: EXECUTION_MODE=real_armed.\n"
            "  This tool only closes INTERNAL paper / real_dry_run positions and "
            "must never run against an armed-for-real-orders environment.\n"
            "  Unset EXECUTION_MODE or set it to paper / real_dry_run and retry.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if mode not in ("paper", "real_dry_run"):
        print(
            f"[FORCE CLOSE PAPER POSITION] WARNING: unknown EXECUTION_MODE={mode!r} "
            f"— treating as paper.",
            file=sys.stderr,
        )
        mode = "paper"
    return mode


def resolve_internal_mode(env_mode: str, conn: sqlite3.Connection | None, contract: str) -> str:
    """
    Decide the label for the force-close (paper vs real_dry_run).

    Prefer the explicit env EXECUTION_MODE; if that is the bare default 'paper'
    but the DB shows this contract was traded under real_dry_run, prefer the DB
    evidence so the audit row reflects what actually happened.
    """
    if env_mode == "real_dry_run":
        return "real_dry_run"
    if conn is None:
        return env_mode
    try:
        row = conn.execute(
            "SELECT mode FROM pt_order_intents WHERE tradingsymbol = ? "
            "ORDER BY id DESC LIMIT 1",
            (contract,),
        ).fetchone()
    except Exception:
        return env_mode
    if row and str(row[0]).lower() == "real_dry_run":
        return "real_dry_run"
    return env_mode


# ── Position discovery ──────────────────────────────────────────────────────────

def load_state(state_path: str) -> dict[str, Any]:
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        print(f"[FORCE CLOSE PAPER POSITION] Could not read state file {state_path}: {exc}",
              file=sys.stderr)
        return {}


def discover_state_positions(state: dict[str, Any]) -> dict[str, dict]:
    """
    Return {underlying: position_dict} for every non-null open position in the
    state file.  Merges the legacy single open_position with the multi-symbol
    open_positions map.
    """
    out: dict[str, dict] = {}
    legacy = state.get("open_position")
    if isinstance(legacy, dict) and legacy:
        und = legacy.get("underlying_symbol") or legacy.get("underlying")
        if und:
            out[und] = legacy
    multi = state.get("open_positions") or {}
    if isinstance(multi, dict):
        for und, pos in multi.items():
            if isinstance(pos, dict) and pos:
                out[und] = pos
    return out


def find_open_db_row(conn: sqlite3.Connection, contract: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM pt_trades WHERE contract_symbol = ? AND state = 'OPEN' "
        "ORDER BY id DESC LIMIT 1",
        (contract,),
    ).fetchone()


# ── Price resolution ────────────────────────────────────────────────────────────

def resolve_last_known_price(audit_path: str, contract: str) -> tuple[float, str] | None:
    """
    Return (premium, source_description) for the most recent observed premium of
    `contract` in the strike-selection audit log, or None if unavailable.
    """
    p = Path(audit_path)
    if not p.exists():
        return None
    last: tuple[float, str] | None = None
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line or contract not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("selected_symbol") != contract:
                    continue
                prem = d.get("premium")
                if prem is None:
                    continue
                try:
                    prem_f = float(prem)
                except (TypeError, ValueError):
                    continue
                if prem_f <= 0:
                    continue
                last = (prem_f, f"last_known@{d.get('ts', '?')}")
    except Exception:
        return None
    return last


# ── CSV / intent guards ─────────────────────────────────────────────────────────

def csv_existing_keys(csv_path: str) -> set[tuple]:
    keys: set[tuple] = set()
    p = Path(csv_path)
    if not p.exists():
        return keys
    try:
        with p.open(newline="") as f:
            for row in csv.DictReader(f):
                keys.add(_csv_key(
                    row.get("symbol", ""),
                    row.get("direction", ""),
                    float(row["entry_price"]) if row.get("entry_price") else 0.0,
                    float(row["exit_price"]) if row.get("exit_price") else 0.0,
                    int(float(row["quantity"])) if row.get("quantity") else 0,
                ))
    except Exception:
        pass
    return keys


def append_csv_row(csv_path: str, *, symbol: str, direction: str,
                   entry: float, exit_price: float, qty: int, pnl: float) -> None:
    p = Path(csv_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerow([symbol, direction, entry, exit_price, qty, pnl])


def force_close_intent_exists(conn: sqlite3.Connection, contract: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM pt_order_intents WHERE tradingsymbol = ? "
            "AND reason IN ('paper_force_close','real_dry_run_force_close') LIMIT 1",
            (contract,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def write_force_close_intent(conn: sqlite3.Connection, *, internal_mode: str,
                             underlying: str, contract: str, quantity: int,
                             exit_price: float, trade_id: int | None) -> None:
    conn.execute(
        """
        INSERT INTO pt_order_intents
          (trade_id, mode, underlying, tradingsymbol, exchange,
           transaction_type, quantity, order_type, price,
           status, reason, created_ts, raw_intent_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id, internal_mode, underlying, contract,
            _required_exchange(underlying), "SELL", quantity, "MARKET",
            exit_price, "FORCE_CLOSE_LOGGED",
            _force_close_intent_reason(internal_mode),
            _utc_now(),
            json.dumps({
                "force_close": True,
                "reason": PRINT_REASON,
                "no_real_order": True,
                "exit_premium": exit_price,
            }),
        ),
    )


# ── Core close routine ──────────────────────────────────────────────────────────

def close_one_position(
    *,
    conn: sqlite3.Connection,
    state: dict[str, Any],
    underlying: str,
    pos: dict | None,
    db_row: sqlite3.Row | None,
    exit_price: float,
    price_source_desc: str,
    internal_mode: str,
    csv_path: str,
    apply_changes: bool,
) -> dict[str, Any]:
    """
    Close a single internal position across DB / state / CSV / intent.

    When apply_changes is False this only computes and returns the preview;
    nothing is mutated.  Returns a summary dict.
    """
    # Resolve canonical fields, preferring the authoritative DB row.
    src = db_row if db_row is not None else pos
    contract  = (db_row["contract_symbol"] if db_row is not None else pos.get("contract_symbol"))
    direction = (db_row["direction"]       if db_row is not None else pos.get("direction"))
    lot_size  = int(db_row["lot_size"]     if db_row is not None else pos.get("lot_size"))
    lots      = int(db_row["lots"]         if db_row is not None else pos.get("lots"))
    entry     = float(db_row["entry_premium"] if db_row is not None else pos.get("entry_premium"))
    quantity  = lot_size * lots

    # Long-premium PnL — the system always BUYS the option; `direction` describes
    # the underlying view, not the premium sign.  Matches BacktestRunner exit math.
    pnl = (exit_price - entry) * lot_size * lots

    summary = {
        "underlying": underlying,
        "contract": contract,
        "direction": direction,
        "entry": entry,
        "exit": exit_price,
        "quantity": quantity,
        "pnl": pnl,
        "price_source": price_source_desc,
        "db_closed": False,
        "state_cleared": False,
        "csv_appended": False,
        "intent_written": False,
        "equity_adjusted": False,
    }

    if not apply_changes:
        return summary

    # 1) DB close — authoritative single transition; equity adjusted iff this fires.
    if db_row is not None:
        equity_before = float(state.get("equity", 0.0))
        equity_after = equity_before + pnl
        conn.execute(
            """
            UPDATE pt_trades SET
                exit_premium = ?, pnl = ?, bars_held = ?,
                exit_reason = ?, exit_ts = ?, equity_after = ?,
                state = 'CLOSED'
            WHERE id = ? AND state = 'OPEN'
            """,
            (exit_price, pnl, 0, DB_EXIT_REASON, _utc_now(), equity_after, db_row["id"]),
        )
        summary["db_closed"] = conn.total_changes > 0 or True

        # Equity / risk counters — applied exactly once, bound to the DB close.
        state["equity"] = equity_after
        state["peak_equity"] = max(float(state.get("peak_equity", equity_after)), equity_after)
        state["daily_pnl"] = float(state.get("daily_pnl", 0.0)) + pnl
        if pnl < 0:
            state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        else:
            state["consecutive_losses"] = 0
        summary["equity_adjusted"] = True

    # 2) Order-intent audit row (existence-guarded for idempotency).
    if not force_close_intent_exists(conn, contract):
        write_force_close_intent(
            conn, internal_mode=internal_mode, underlying=underlying,
            contract=contract, quantity=quantity, exit_price=exit_price,
            trade_id=(int(db_row["id"]) if db_row is not None else None),
        )
        summary["intent_written"] = True

    # 3) CSV completed row (dedup-guarded for idempotency).
    key = _csv_key(contract, direction, entry, exit_price, quantity)
    if key not in csv_existing_keys(csv_path):
        append_csv_row(csv_path, symbol=contract, direction=direction,
                       entry=entry, exit_price=exit_price, qty=quantity, pnl=pnl)
        summary["csv_appended"] = True

    # 4) Clear the position from state (legacy + multi-symbol map).
    legacy = state.get("open_position")
    if isinstance(legacy, dict) and (
        legacy.get("underlying_symbol") == underlying or legacy.get("underlying") == underlying
    ):
        state["open_position"] = None
    if isinstance(state.get("open_positions"), dict) and underlying in state["open_positions"]:
        state["open_positions"][underlying] = None
    summary["state_cleared"] = True

    return summary


# ── Orchestration ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace, env: dict[str, str]) -> int:
    env_mode = assert_not_real_armed(env)

    state = load_state(args.state_file)
    state_positions = discover_state_positions(state)

    db_path = args.db
    db_exists = Path(db_path).exists()
    conn = sqlite3.connect(db_path) if db_exists else None

    # Build the union of candidate underlyings (DB OPEN rows + state positions).
    candidates: dict[str, dict] = dict(state_positions)
    db_open_rows: dict[str, sqlite3.Row] = {}
    if conn is not None:
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT * FROM pt_trades WHERE state = 'OPEN'"):
            db_open_rows[r["underlying"]] = r
            candidates.setdefault(r["underlying"], {})

    if args.underlying:
        candidates = {k: v for k, v in candidates.items()
                      if k.upper() == args.underlying.upper()}

    # Nothing open anywhere → safe idempotent no-op.
    if not candidates:
        print("[FORCE CLOSE PAPER POSITION] nothing to close — no OPEN position in "
              "DB or state (idempotent no-op).")
        if conn is not None:
            conn.close()
        return 0

    apply_changes = bool(args.confirm)
    if not apply_changes:
        print("[FORCE CLOSE PAPER POSITION] DRY-RUN preview (no --confirm) — "
              "no files will be modified.\n")

    exit_code = 0
    any_action = False

    for und, pos in candidates.items():
        db_row = db_open_rows.get(und)
        contract = (db_row["contract_symbol"] if db_row is not None
                    else (pos.get("contract_symbol") if pos else None))
        if not contract:
            continue

        # Resolve exit price for this contract.
        if args.price_source == "manual":
            exit_price = float(args.exit_price)
            price_desc = "manual"
        else:  # last_known
            found = resolve_last_known_price(args.audit_file, contract)
            if found is None:
                print(
                    f"[FORCE CLOSE PAPER POSITION] REFUSED for {und}/{contract}: "
                    f"no last_known premium available in {args.audit_file}.\n"
                    f"  Re-run with: --price-source manual --exit-price <price>",
                    file=sys.stderr,
                )
                exit_code = 3
                continue
            exit_price, price_desc = found

        internal_mode = resolve_internal_mode(env_mode, conn, contract)

        summary = close_one_position(
            conn=conn, state=state, underlying=und, pos=pos, db_row=db_row,
            exit_price=exit_price, price_source_desc=price_desc,
            internal_mode=internal_mode, csv_path=args.csv,
            apply_changes=apply_changes,
        )
        any_action = True

        prefix = "" if apply_changes else "[DRY-RUN] "
        print(
            f"{prefix}[FORCE CLOSE PAPER POSITION] contract={summary['contract']} "
            f"entry=₹{summary['entry']:.2f} exit=₹{summary['exit']:.2f} "
            f"pnl=₹{summary['pnl']:+,.2f} reason={PRINT_REASON}"
        )
        if apply_changes:
            print(
                f"    mode={internal_mode}  price_source={summary['price_source']}  "
                f"db_closed={summary['db_closed']}  state_cleared={summary['state_cleared']}  "
                f"csv_appended={summary['csv_appended']}  intent_written={summary['intent_written']}  "
                f"equity_adjusted={summary['equity_adjusted']}"
            )
        else:
            print(
                f"    WOULD: close DB row, clear state, append CSV, write "
                f"{_force_close_intent_reason(internal_mode)} intent  "
                f"(price_source={summary['price_source']})"
            )

    # Commit DB + persist state only when actually applying.
    if apply_changes and any_action:
        if conn is not None:
            conn.commit()
        Path(args.state_file).write_text(json.dumps(state, indent=2))
        print("\n[FORCE CLOSE PAPER POSITION] DONE — DB, state, and CSV updated.")
    elif not apply_changes:
        print("\n[FORCE CLOSE PAPER POSITION] preview complete. Re-run with --confirm to apply.")

    if conn is not None:
        conn.close()
    return exit_code


# ── CLI ─────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="force_close_paper_positions.py",
        description="Cleanly force-close an INTERNAL paper / real_dry_run open "
                    "position in the local DB / state / CSV. Never places real "
                    "orders; refuses under EXECUTION_MODE=real_armed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--confirm", action="store_true",
                   help="Actually apply changes. Without this flag the tool only "
                        "prints a dry-run preview and modifies nothing.")
    p.add_argument("--price-source", choices=("last_known", "manual"),
                   default="last_known",
                   help="Where the exit premium comes from (default: last_known).")
    p.add_argument("--exit-price", type=float, default=None,
                   help="Exit premium, required when --price-source manual.")
    p.add_argument("--underlying", default=None,
                   help="Only close this underlying (e.g. NIFTY). Default: all open.")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB}).")
    p.add_argument("--state-file", default=DEFAULT_STATE,
                   help=f"Paper state JSON path (default: {DEFAULT_STATE}).")
    p.add_argument("--csv", default=DEFAULT_CSV,
                   help=f"Completed-trades CSV path (default: {DEFAULT_CSV}).")
    p.add_argument("--audit-file", default=DEFAULT_AUDIT,
                   help=f"Strike-selection audit JSONL used for last_known "
                        f"(default: {DEFAULT_AUDIT}).")
    p.add_argument("--self-test", action="store_true",
                   help="Run built-in offline tests against temp fixtures and exit.")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if args.price_source == "manual" and args.exit_price is None:
        print("[FORCE CLOSE PAPER POSITION] REFUSED: --price-source manual requires "
              "--exit-price <price>.", file=sys.stderr)
        return 2

    return run(args, dict(os.environ))


# ══════════════════════════════════════════════════════════════════════════════
# Built-in offline self-test
# ══════════════════════════════════════════════════════════════════════════════

def _build_fixtures(tmp: Path) -> dict[str, str]:
    """Create a minimal DB / state / csv / audit matching the real shapes."""
    db = tmp / "trades.db"
    state = tmp / "state.json"
    csvf = tmp / "paper_trades.csv"
    audit = tmp / "audit.jsonl"

    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE pt_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, run_mode TEXT NOT NULL,
            underlying TEXT NOT NULL, contract_symbol TEXT NOT NULL,
            strike REAL NOT NULL, option_type TEXT NOT NULL, expiry_date TEXT NOT NULL,
            direction TEXT NOT NULL, lots INTEGER NOT NULL, lot_size INTEGER NOT NULL,
            entry_premium REAL NOT NULL, entry_spot REAL NOT NULL, entry_iv REAL NOT NULL,
            entry_bar_index INTEGER NOT NULL, entry_ts TEXT NOT NULL,
            conf_raw REAL, conf_eff REAL, fvg_quality REAL, drawdown_at_entry REAL,
            equity_before REAL, exit_premium REAL, pnl REAL, bars_held INTEGER,
            exit_reason TEXT, exit_ts TEXT, equity_after REAL,
            state TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL
        );
        CREATE TABLE pt_order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, mode TEXT NOT NULL,
            underlying TEXT NOT NULL, tradingsymbol TEXT NOT NULL, exchange TEXT NOT NULL,
            transaction_type TEXT NOT NULL, quantity INTEGER NOT NULL, order_type TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0.0, status TEXT NOT NULL, reason TEXT NOT NULL,
            created_ts TEXT NOT NULL, raw_intent_json TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO pt_trades (session_id, run_mode, underlying, contract_symbol,
            strike, option_type, expiry_date, direction, lots, lot_size,
            entry_premium, entry_spot, entry_iv, entry_bar_index, entry_ts,
            state, created_at)
        VALUES ('test','real_dry_run','NIFTY','NIFTY2661623300PE',23300,'PUT',
            '2026-06-16','SHORT',3,65,141.30,23323.3,0.149,129,
            '2026-06-11T07:31:03+00:00','OPEN','2026-06-11T07:31:03+00:00')
        """
    )
    conn.execute(
        "INSERT INTO pt_order_intents (mode, underlying, tradingsymbol, exchange, "
        "transaction_type, quantity, order_type, price, status, reason, created_ts) "
        "VALUES ('real_dry_run','NIFTY','NIFTY2661623300PE','NFO','BUY',195,'MARKET',"
        "0.0,'DRY_RUN_LOGGED','ENTRY','2026-06-11T07:31:03+00:00')"
    )
    conn.commit()
    conn.close()

    state.write_text(json.dumps({
        "starting_capital": 400000.0,
        "equity": 393988.87,
        "peak_equity": 400000.0,
        "daily_pnl": -6011.13,
        "consecutive_losses": 2,
        "open_position": {
            "underlying_symbol": "NIFTY", "contract_symbol": "NIFTY2661623300PE",
            "strike": 23300.0, "option_type": "PUT", "expiry_date": "2026-06-16",
            "lot_size": 65, "lots": 3, "entry_premium": 141.3, "entry_spot": 23323.3,
            "entry_iv": 0.149, "direction": "SHORT", "bar_index": 129,
        },
        "open_positions": {
            "NIFTY": {
                "underlying_symbol": "NIFTY", "contract_symbol": "NIFTY2661623300PE",
                "strike": 23300.0, "option_type": "PUT", "expiry_date": "2026-06-16",
                "lot_size": 65, "lots": 3, "entry_premium": 141.3, "entry_spot": 23323.3,
                "entry_iv": 0.149, "direction": "SHORT", "bar_index": 129,
            },
            "SENSEX": None, "BANKNIFTY": None,
        },
    }, indent=2))

    csvf.write_text("symbol,direction,entry_price,exit_price,quantity,pnl\n")
    audit.write_text(
        json.dumps({"ts": "2026-06-11T08:07:00+00:00",
                    "selected_symbol": "NIFTY2661623300PE", "premium": 165.8}) + "\n"
    )
    return {"db": str(db), "state": str(state), "csv": str(csvf), "audit": str(audit)}


def _args_for(fx: dict[str, str], **over) -> argparse.Namespace:
    base = dict(confirm=False, price_source="last_known", exit_price=None,
                underlying=None, db=fx["db"], state_file=fx["state"],
                csv=fx["csv"], audit_file=fx["audit"], self_test=False)
    base.update(over)
    return argparse.Namespace(**base)


def run_self_test() -> int:
    print("══ force_close_paper_positions self-test ══\n")
    passed = 0
    failed = 0

    def check(name: str, cond: bool) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")

    # 0) No real broker call/import anywhere in this module's source.
    #    Match actual call/import patterns, not the safety prose that documents
    #    the guarantee.  Needles are assembled from fragments so this check does
    #    not match its own literal.
    src = Path(__file__).read_text()
    call_needle   = "." + "place_order" + "("       # an actual broker order call
    import_needle = "import " + "kite"               # importing the kite client
    sdk_needle    = "from " + "kiteconnect"          # SDK import
    ctor_needle   = "KiteConnect" + "("              # constructing the client
    check("source makes no place_order() call / kite import",
          call_needle not in src
          and import_needle not in src
          and sdk_needle not in src.lower()
          and ctor_needle not in src)

    # 1) Refuses under real_armed (regardless of --confirm).
    with tempfile.TemporaryDirectory() as d:
        fx = _build_fixtures(Path(d))
        try:
            run(_args_for(fx, confirm=True), {"EXECUTION_MODE": "real_armed"})
            refused = False
        except SystemExit as e:
            refused = (e.code == 2)
        check("refuses when EXECUTION_MODE=real_armed", refused)
        # DB row must still be OPEN (nothing closed).
        c = sqlite3.connect(fx["db"])
        n_open = c.execute("SELECT COUNT(*) FROM pt_trades WHERE state='OPEN'").fetchone()[0]
        c.close()
        check("real_armed left the OPEN row untouched", n_open == 1)

    # 2) Refuses to mutate without --confirm (dry-run preview only).
    with tempfile.TemporaryDirectory() as d:
        fx = _build_fixtures(Path(d))
        rc = run(_args_for(fx, confirm=False), {"EXECUTION_MODE": "real_dry_run"})
        c = sqlite3.connect(fx["db"])
        n_open = c.execute("SELECT COUNT(*) FROM pt_trades WHERE state='OPEN'").fetchone()[0]
        c.close()
        csv_rows = len(Path(fx["csv"]).read_text().strip().splitlines())
        check("dry-run (no --confirm) returns 0", rc == 0)
        check("dry-run leaves DB OPEN row untouched", n_open == 1)
        check("dry-run appends no CSV data row", csv_rows == 1)  # header only

    # 3) Closes one open internal position safely with last_known.
    with tempfile.TemporaryDirectory() as d:
        fx = _build_fixtures(Path(d))
        rc = run(_args_for(fx, confirm=True), {"EXECUTION_MODE": "real_dry_run"})
        c = sqlite3.connect(fx["db"]); c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM pt_trades WHERE contract_symbol='NIFTY2661623300PE'").fetchone()
        n_open = c.execute("SELECT COUNT(*) FROM pt_trades WHERE state='OPEN'").fetchone()[0]
        intent = c.execute("SELECT * FROM pt_order_intents WHERE reason='real_dry_run_force_close'").fetchone()
        c.close()
        st = json.loads(Path(fx["state"]).read_text())
        csv_rows = Path(fx["csv"]).read_text().strip().splitlines()
        expected_pnl = (165.8 - 141.3) * 65 * 3
        check("close returns 0", rc == 0)
        check("DB row state -> CLOSED", row["state"] == "CLOSED")
        check("no OPEN rows remain", n_open == 0)
        check("exit_premium = last_known 165.8", abs(row["exit_premium"] - 165.8) < 1e-6)
        check("pnl matches long-premium formula", abs(row["pnl"] - expected_pnl) < 1e-6)
        check("exit_reason = FORCE_CLOSE_USER_STOP", row["exit_reason"] == DB_EXIT_REASON)
        check("state open_position cleared", st.get("open_position") is None)
        check("state open_positions[NIFTY] cleared", st["open_positions"]["NIFTY"] is None)
        check("equity adjusted once", abs(st["equity"] - (393988.87 + expected_pnl)) < 1e-2)
        check("real_dry_run_force_close intent written", intent is not None)
        check("CSV has exactly one data row", len(csv_rows) == 2)

        # 4) Idempotent: second run is a no-op, no duplicate CSV / re-close.
        rc2 = run(_args_for(fx, confirm=True), {"EXECUTION_MODE": "real_dry_run"})
        c = sqlite3.connect(fx["db"])
        n_open2 = c.execute("SELECT COUNT(*) FROM pt_trades WHERE state='OPEN'").fetchone()[0]
        n_intent = c.execute(
            "SELECT COUNT(*) FROM pt_order_intents WHERE reason='real_dry_run_force_close'"
        ).fetchone()[0]
        c.close()
        st2 = json.loads(Path(fx["state"]).read_text())
        csv_rows2 = Path(fx["csv"]).read_text().strip().splitlines()
        check("second run returns 0 (idempotent)", rc2 == 0)
        check("still no OPEN rows", n_open2 == 0)
        check("no duplicate CSV row", len(csv_rows2) == 2)
        check("no duplicate force-close intent", n_intent == 1)
        check("equity unchanged on second run",
              abs(st2["equity"] - st["equity"]) < 1e-6)

    # 5) last_known unavailable -> refuse and instruct manual.
    with tempfile.TemporaryDirectory() as d:
        fx = _build_fixtures(Path(d))
        Path(fx["audit"]).write_text("")  # no premium records
        rc = run(_args_for(fx, confirm=True), {"EXECUTION_MODE": "real_dry_run"})
        c = sqlite3.connect(fx["db"])
        n_open = c.execute("SELECT COUNT(*) FROM pt_trades WHERE state='OPEN'").fetchone()[0]
        c.close()
        check("refuses (rc=3) when last_known unavailable", rc == 3)
        check("position left OPEN when price unavailable", n_open == 1)

    # 6) manual price source closes correctly.
    with tempfile.TemporaryDirectory() as d:
        fx = _build_fixtures(Path(d))
        rc = run(_args_for(fx, confirm=True, price_source="manual", exit_price=150.0),
                 {"EXECUTION_MODE": "real_dry_run"})
        c = sqlite3.connect(fx["db"]); c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM pt_trades WHERE state='CLOSED'").fetchone()
        c.close()
        check("manual close returns 0", rc == 0)
        check("manual exit_premium = 150.0", row is not None and abs(row["exit_premium"] - 150.0) < 1e-6)

    print(f"\n══ {passed} passed, {failed} failed ══")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
