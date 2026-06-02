"""
platform_core.persistence.trade_database
═══════════════════════════════════════════════════════════════════════
Fire-and-forget SQLite writer for the paper trading pipeline.

Design principles:
  - Never raises an exception into calling code.
  - All writes are enqueued and processed by a background daemon thread.
  - If the DB is unavailable or a write fails, the failure is silently
    logged to stderr and the trading loop continues unaffected.
  - Default db_writer=None pattern: zero overhead when not configured.
  - Compatible with both synchronous backtest runner and live pipeline.

Tables written:
  pt_trades          — one row per trade (inserted at entry, updated at exit)
  pt_decisions       — one row per entry-gate evaluation
  pt_structure_events— one row per StructureEvent emitted
  pt_runtime_events  — one row per pipeline lifecycle / protection event
"""
from __future__ import annotations

import queue
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Schema DDL ─────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS pt_trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL,
    run_mode          TEXT    NOT NULL,
    underlying        TEXT    NOT NULL,
    contract_symbol   TEXT    NOT NULL,
    strike            REAL    NOT NULL,
    option_type       TEXT    NOT NULL,
    expiry_date       TEXT    NOT NULL,
    direction         TEXT    NOT NULL,
    lots              INTEGER NOT NULL,
    lot_size          INTEGER NOT NULL,
    entry_premium     REAL    NOT NULL,
    entry_spot        REAL    NOT NULL,
    entry_iv          REAL    NOT NULL,
    entry_bar_index   INTEGER NOT NULL,
    entry_ts          TEXT    NOT NULL,
    conf_raw          REAL,
    conf_eff          REAL,
    fvg_quality       REAL,
    drawdown_at_entry REAL,
    equity_before     REAL,
    exit_premium      REAL,
    pnl               REAL,
    bars_held         INTEGER,
    exit_reason       TEXT,
    exit_ts           TEXT,
    equity_after      REAL,
    state             TEXT    NOT NULL DEFAULT 'OPEN',
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pt_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    run_mode      TEXT    NOT NULL,
    bar_index     INTEGER NOT NULL,
    bar_ts        TEXT    NOT NULL,
    underlying    TEXT    NOT NULL,
    bias          TEXT    NOT NULL,
    conf_raw      REAL    NOT NULL,
    conf_eff      REAL    NOT NULL,
    outcome       TEXT    NOT NULL,
    filter_reason TEXT,
    session_phase TEXT,
    fvg_quality   REAL,
    equity        REAL    NOT NULL,
    drawdown      REAL    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pt_structure_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    run_mode        TEXT    NOT NULL,
    bar_index       INTEGER NOT NULL,
    bar_ts          TEXT    NOT NULL,
    instrument_id   TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    trigger_price   REAL    NOT NULL,
    confidence      REAL,
    struct_event_id TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pt_runtime_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    run_mode   TEXT    NOT NULL,
    event_type TEXT    NOT NULL,
    severity   TEXT    NOT NULL,
    message    TEXT    NOT NULL,
    equity     REAL,
    drawdown   REAL,
    bar_index  INTEGER,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pt_trades_session    ON pt_trades (session_id, state);
CREATE INDEX IF NOT EXISTS idx_pt_decisions_session ON pt_decisions (session_id);
CREATE INDEX IF NOT EXISTS idx_pt_se_session        ON pt_structure_events (session_id, bar_index);
CREATE INDEX IF NOT EXISTS idx_pt_re_session        ON pt_runtime_events (session_id);
"""

# ── Queue sentinels ─────────────────────────────────────────────────────────────

_STOP_SENTINEL = object()


# ── TradeDatabase ──────────────────────────────────────────────────────────────

class TradeDatabase:
    """
    Fire-and-forget SQLite persistence for the paper trading pipeline.

    All public methods enqueue a write operation and return immediately.
    A background daemon thread processes the queue.  Any SQLite error is
    printed to stderr and silently swallowed — the trading loop is never
    interrupted.

    Lifecycle:
        db = TradeDatabase("logs/trades.db")
        db.start(session_id="bt_BANKNIFTY_20260602", run_mode="backtest")
        # ... trading loop ...
        db.flush()   # wait for queue to drain (call at end of backtest)
        db.stop()    # signal worker to exit (optional; daemon thread exits anyway)
    """

    def __init__(self, db_path: str = "logs/trades.db") -> None:
        self._db_path = db_path
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="TradeDB-writer")
        self._session_id: str = "default"
        self._run_mode: str = "live"
        self._started: bool = False
        self._open_trade_rowid: int | None = None   # rowid of the currently open trade row
        self._drops_since_last_warn: int = 0        # writes dropped since last stderr warning

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self, session_id: str, run_mode: str = "live") -> None:
        """Initialize schema and start the background writer thread."""
        if self._started:
            return
        self._session_id = session_id
        self._run_mode = run_mode
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path)
            conn.executescript(_DDL)
            conn.commit()
            conn.close()
        except Exception as exc:
            print(
                f"\n[DB WARNING] Schema init failed — persistence DISABLED for this session.\n"
                f"  Path: {self._db_path}\n"
                f"  Reason: {exc}\n"
                f"  Trading continues unaffected; trades still logged to CSV.\n",
                file=sys.stderr,
            )
            return
        self._thread.start()
        self._started = True

    def flush(self, timeout: float = 30.0) -> None:
        """Block until the write queue drains (call at end of backtest)."""
        if not self._started:
            return
        try:
            self._queue.join()
        except Exception:
            pass

    def stop(self) -> None:
        """Signal the writer thread to exit cleanly after draining the queue."""
        if not self._started:
            return
        self._queue.put(_STOP_SENTINEL)

    # ── Internal: thread-health guard ─────────────────────────────────────────

    def _is_operational(self) -> bool:
        """
        Return True if writes should proceed.

        Checks both that start() succeeded and that the background thread is
        still alive.  When the thread has died, emits a rate-limited stderr
        warning (first occurrence + every 50 subsequent dropped writes) so an
        operator watching the terminal sees the problem without being flooded.
        The trading loop is never interrupted.
        """
        if not self._started:
            return False
        if self._thread.is_alive():
            return True
        # Thread is dead — emit a rate-limited warning, then drop the write.
        self._drops_since_last_warn += 1
        if self._drops_since_last_warn == 1 or self._drops_since_last_warn % 50 == 0:
            print(
                f"\n[DB WARNING] Writer thread is not alive — DB write dropped "
                f"(total dropped: {self._drops_since_last_warn}).  "
                f"Trading continues; trades still logged to CSV.\n",
                file=sys.stderr,
            )
        return False

    # ── Public write API ───────────────────────────────────────────────────────

    def write_trade_entry(
        self,
        *,
        underlying: str,
        contract_symbol: str,
        strike: float,
        option_type: str,
        expiry_date: str,
        direction: str,
        lots: int,
        lot_size: int,
        entry_premium: float,
        entry_spot: float,
        entry_iv: float,
        entry_bar_index: int,
        entry_ts: str,
        conf_raw: float | None = None,
        conf_eff: float | None = None,
        fvg_quality: float | None = None,
        drawdown_at_entry: float | None = None,
        equity_before: float | None = None,
    ) -> None:
        if not self._is_operational():
            return
        self._queue.put(("trade_entry", {
            "session_id":        self._session_id,
            "run_mode":          self._run_mode,
            "underlying":        underlying,
            "contract_symbol":   contract_symbol,
            "strike":            strike,
            "option_type":       option_type,
            "expiry_date":       expiry_date,
            "direction":         direction,
            "lots":              lots,
            "lot_size":          lot_size,
            "entry_premium":     entry_premium,
            "entry_spot":        entry_spot,
            "entry_iv":          entry_iv,
            "entry_bar_index":   entry_bar_index,
            "entry_ts":          entry_ts,
            "conf_raw":          conf_raw,
            "conf_eff":          conf_eff,
            "fvg_quality":       fvg_quality,
            "drawdown_at_entry": drawdown_at_entry,
            "equity_before":     equity_before,
            "created_at":        _utc_now(),
        }))

    def write_trade_exit(
        self,
        *,
        exit_premium: float,
        pnl: float,
        bars_held: int,
        exit_reason: str,
        exit_ts: str,
        equity_after: float,
    ) -> None:
        if not self._is_operational():
            return
        self._queue.put(("trade_exit", {
            "exit_premium":  exit_premium,
            "pnl":           pnl,
            "bars_held":     bars_held,
            "exit_reason":   exit_reason,
            "exit_ts":       exit_ts,
            "equity_after":  equity_after,
            "created_at":    _utc_now(),
        }))

    def write_decision(
        self,
        *,
        bar_index: int,
        bar_ts: str,
        underlying: str,
        bias: str,
        conf_raw: float,
        conf_eff: float,
        outcome: str,
        filter_reason: str | None = None,
        session_phase: str | None = None,
        fvg_quality: float | None = None,
        equity: float,
        drawdown: float,
    ) -> None:
        if not self._is_operational():
            return
        self._queue.put(("decision", {
            "session_id":    self._session_id,
            "run_mode":      self._run_mode,
            "bar_index":     bar_index,
            "bar_ts":        bar_ts,
            "underlying":    underlying,
            "bias":          bias,
            "conf_raw":      conf_raw,
            "conf_eff":      conf_eff,
            "outcome":       outcome,
            "filter_reason": filter_reason,
            "session_phase": session_phase,
            "fvg_quality":   fvg_quality,
            "equity":        equity,
            "drawdown":      drawdown,
            "created_at":    _utc_now(),
        }))

    def write_structure_events(
        self,
        events: tuple,
        *,
        bar_index: int,
        bar_ts: str,
    ) -> None:
        """Batch-write a tuple of StructureEvent objects."""
        if not self._is_operational() or not events:
            return
        rows = []
        now = _utc_now()
        for evt in events:
            try:
                evt_type = evt.event_type.value if hasattr(evt.event_type, "value") else str(evt.event_type)
                side     = evt.side.value      if hasattr(evt.side, "value")       else str(evt.side)
                rows.append({
                    "session_id":       self._session_id,
                    "run_mode":         self._run_mode,
                    "bar_index":        bar_index,
                    "bar_ts":           bar_ts,
                    "instrument_id":    evt.instrument_id,
                    "timeframe":        evt.timeframe,
                    "event_type":       evt_type,
                    "side":             side,
                    "trigger_price":    evt.trigger_price,
                    "confidence":       getattr(evt, "confidence_score", None),
                    "struct_event_id":  evt.event_id,
                    "created_at":       now,
                })
            except Exception:
                pass
        if rows:
            self._queue.put(("structure_events_batch", rows))

    def write_runtime_event(
        self,
        event_type: str,
        *,
        severity: str = "INFO",
        message: str,
        equity: float | None = None,
        drawdown: float | None = None,
        bar_index: int | None = None,
    ) -> None:
        if not self._is_operational():
            return
        self._queue.put(("runtime_event", {
            "session_id":  self._session_id,
            "run_mode":    self._run_mode,
            "event_type":  event_type,
            "severity":    severity,
            "message":     message,
            "equity":      equity,
            "drawdown":    drawdown,
            "bar_index":   bar_index,
            "created_at":  _utc_now(),
        }))

    # ── Background worker ──────────────────────────────────────────────────────

    def _worker(self) -> None:
        """Drain the queue into SQLite.  Runs in a daemon thread."""
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as exc:
            print(
                f"\n[DB WARNING] Worker thread failed to open DB — persistence DISABLED.\n"
                f"  Path: {self._db_path}\n"
                f"  Reason: {exc}\n"
                f"  Trading continues unaffected; trades still logged to CSV.\n",
                file=sys.stderr,
            )
            # Drain the queue so task_done() stays balanced and flush() doesn't deadlock.
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            return

        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is _STOP_SENTINEL:
                self._queue.task_done()
                break

            try:
                kind, payload = item
                if kind == "trade_entry":
                    self._insert_trade_entry(conn, payload)
                elif kind == "trade_exit":
                    self._update_trade_exit(conn, payload)
                elif kind == "decision":
                    self._insert_decision(conn, payload)
                elif kind == "structure_events_batch":
                    self._insert_structure_events_batch(conn, payload)
                elif kind == "runtime_event":
                    self._insert_runtime_event(conn, payload)
                conn.commit()
            except Exception as exc:
                print(f"[DB] Write error ({exc})", file=sys.stderr)
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                self._queue.task_done()

        try:
            conn.close()
        except Exception:
            pass

    # ── DB insert/update helpers ───────────────────────────────────────────────

    def _insert_trade_entry(self, conn: sqlite3.Connection, p: dict) -> None:
        if self._open_trade_rowid is not None:
            # An OPEN row already exists for this session — unexpected duplicate entry.
            # This should not happen in normal operation; log a prominent warning so
            # the operator can investigate, then continue (overwrite the rowid pointer).
            print(
                f"\n[DB WARNING] write_trade_entry called while rowid={self._open_trade_rowid} "
                f"is still OPEN.  Possible duplicate entry or missed exit write.  "
                f"Old OPEN row will remain unresolved in pt_trades.\n",
                file=sys.stderr,
            )
        cur = conn.execute(
            """
            INSERT INTO pt_trades (
                session_id, run_mode, underlying, contract_symbol,
                strike, option_type, expiry_date, direction,
                lots, lot_size, entry_premium, entry_spot, entry_iv,
                entry_bar_index, entry_ts,
                conf_raw, conf_eff, fvg_quality, drawdown_at_entry, equity_before,
                state, created_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?
            )
            """,
            (
                p["session_id"], p["run_mode"], p["underlying"], p["contract_symbol"],
                p["strike"], p["option_type"], p["expiry_date"], p["direction"],
                p["lots"], p["lot_size"], p["entry_premium"], p["entry_spot"], p["entry_iv"],
                p["entry_bar_index"], p["entry_ts"],
                p["conf_raw"], p["conf_eff"], p["fvg_quality"],
                p["drawdown_at_entry"], p["equity_before"],
                p["created_at"],
            ),
        )
        self._open_trade_rowid = cur.lastrowid

    def _update_trade_exit(self, conn: sqlite3.Connection, p: dict) -> None:
        if self._open_trade_rowid is None:
            # No rowid recorded for this session — most likely a crash-recovery scenario
            # where the position was restored from the JSON state file but the DB entry
            # belongs to a previous session.  The exit is not recorded in pt_trades for
            # this session.  The CSV log (paper_trades.csv) remains authoritative.
            print(
                f"\n[DB WARNING] write_trade_exit called with no open trade rowid.  "
                f"Likely crash-recovery: position restored from state file, DB entry "
                f"belongs to a previous session.  Exit not written to pt_trades.  "
                f"CSV log is unaffected and remains authoritative.\n",
                file=sys.stderr,
            )
            return
        conn.execute(
            """
            UPDATE pt_trades SET
                exit_premium = ?,
                pnl          = ?,
                bars_held    = ?,
                exit_reason  = ?,
                exit_ts      = ?,
                equity_after = ?,
                state        = 'CLOSED'
            WHERE id = ?
            """,
            (
                p["exit_premium"], p["pnl"], p["bars_held"],
                p["exit_reason"], p["exit_ts"], p["equity_after"],
                self._open_trade_rowid,
            ),
        )
        self._open_trade_rowid = None

    def _insert_decision(self, conn: sqlite3.Connection, p: dict) -> None:
        conn.execute(
            """
            INSERT INTO pt_decisions (
                session_id, run_mode, bar_index, bar_ts, underlying,
                bias, conf_raw, conf_eff, outcome, filter_reason,
                session_phase, fvg_quality, equity, drawdown, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                p["session_id"], p["run_mode"], p["bar_index"], p["bar_ts"], p["underlying"],
                p["bias"], p["conf_raw"], p["conf_eff"], p["outcome"], p["filter_reason"],
                p["session_phase"], p["fvg_quality"], p["equity"], p["drawdown"], p["created_at"],
            ),
        )

    def _insert_structure_events_batch(self, conn: sqlite3.Connection, rows: list[dict]) -> None:
        conn.executemany(
            """
            INSERT INTO pt_structure_events (
                session_id, run_mode, bar_index, bar_ts, instrument_id,
                timeframe, event_type, side, trigger_price, confidence,
                struct_event_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    r["session_id"], r["run_mode"], r["bar_index"], r["bar_ts"],
                    r["instrument_id"], r["timeframe"], r["event_type"], r["side"],
                    r["trigger_price"], r["confidence"], r["struct_event_id"], r["created_at"],
                )
                for r in rows
            ],
        )

    def _insert_runtime_event(self, conn: sqlite3.Connection, p: dict) -> None:
        conn.execute(
            """
            INSERT INTO pt_runtime_events (
                session_id, run_mode, event_type, severity,
                message, equity, drawdown, bar_index, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                p["session_id"], p["run_mode"], p["event_type"], p["severity"],
                p["message"], p["equity"], p["drawdown"], p["bar_index"], p["created_at"],
            ),
        )


# ── Module-level helpers ───────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


__all__ = ["TradeDatabase"]
