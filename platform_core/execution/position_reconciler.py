"""
platform_core.execution.position_reconciler
═══════════════════════════════════════════════════════════════════════
Thin live-loop adapter over ReconciliationEngine for POSITION reconciliation:
compare the pipeline's local open positions against the broker's net positions
and report whether there is a blocking (HIGH/CRITICAL) break.

Used only in real_armed.  paper / real_dry_run hold no real broker positions, so
the caller skips reconciliation entirely.

A fresh ReconciliationEngine is created per call so the engine's non-decreasing
timestamp invariant never trips on rapid successive checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from platform_core.execution.reconciliation_engine import (
    BreakSeverity,
    PositionRecord,
    ReconciliationEngine,
    ReconciliationSnapshotInput,
)

_BLOCKING = {BreakSeverity.HIGH, BreakSeverity.CRITICAL}


@dataclass(frozen=True)
class ReconcileOutcome:
    ok:               bool          # False ⇒ a HIGH/CRITICAL break (caller should halt)
    break_count:      int
    highest_severity: str | None
    summary:          str


def _local_records(local_positions) -> tuple[PositionRecord, ...]:
    """local_positions: iterable of (tradingsymbol, net_quantity, average_price)."""
    out: list[PositionRecord] = []
    for sym, qty, avg in local_positions or []:
        try:
            q = int(qty)
        except (TypeError, ValueError):
            continue
        if not sym or q == 0:
            continue
        _avg = float(avg) if avg not in (None, "", 0, 0.0) else None
        out.append(PositionRecord(tradingsymbol=str(sym), net_quantity=q, average_price=_avg))
    return tuple(out)


def _broker_records(broker_positions) -> tuple[PositionRecord, ...]:
    """broker_positions: iterable of Kite net-position dicts."""
    out: list[PositionRecord] = []
    for p in broker_positions or []:
        try:
            sym = str(p.get("tradingsymbol", "") or "")
            q   = int(p.get("quantity") if p.get("quantity") is not None else p.get("net_quantity") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not sym or q == 0:
            continue
        _avg_raw = p.get("average_price")
        _avg = float(_avg_raw) if _avg_raw not in (None, "", 0, 0.0) else None
        out.append(PositionRecord(tradingsymbol=sym, net_quantity=q, average_price=_avg))
    return tuple(out)


def reconcile_positions(
    *,
    local_positions,
    broker_positions,
    now: datetime | None = None,
) -> ReconcileOutcome:
    """Reconcile local vs broker positions; ok=False on any HIGH/CRITICAL break.

    A position present on only one side, or a net-quantity mismatch, is a HIGH/
    CRITICAL break — meaning the pipeline's view has diverged from the broker and
    the caller must halt and surface it for manual resolution.
    """
    snapshot = ReconciliationSnapshotInput(
        timestamp=now or datetime.now(timezone.utc),
        internal_orders=(),
        broker_orders=(),
        internal_positions=_local_records(local_positions),
        broker_positions=_broker_records(broker_positions),
    )
    result = ReconciliationEngine().reconcile(snapshot=snapshot)
    sev = result.highest_severity
    ok = sev is None or sev not in _BLOCKING
    summary = "; ".join(
        f"{b.break_type.value}:{b.entity_key}" for b in result.breaks[:5]
    )
    return ReconcileOutcome(
        ok=ok,
        break_count=result.break_count,
        highest_severity=(sev.value if sev is not None else None),
        summary=summary,
    )


__all__ = ["ReconcileOutcome", "reconcile_positions"]
