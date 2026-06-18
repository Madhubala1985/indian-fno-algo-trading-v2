"""
platform_core.execution.fill_confirmation
═══════════════════════════════════════════════════════════════════════
Confirm the ACTUAL fill of a real broker order.

A real ``order_id`` returned by ``place_order`` is NOT a fill — the order may
still be open, partially filled, rejected, or cancelled.  This module polls the
broker adapter's status/trades endpoints until the order reaches a terminal
state, and reports the actual average fill price and filled quantity.

Used only by real execution modes (``real_armed``).  Paper / ``real_dry_run``
never call this — they have no real order to confirm.

Design rules:
  - Fail-closed: anything other than a fully-confirmed FILLED is reported as
    such, so the caller NEVER opens a position on an unconfirmed order.
  - Never raises into the trading loop — all adapter calls are defensive.
  - ``sleep`` is injectable so the polling loop is deterministically testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# Terminal + non-terminal Kite order states (uppercased).
_KITE_COMPLETE  = {"COMPLETE"}
_KITE_DEAD      = {"REJECTED", "CANCELLED"}
# Everything else ("OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED",
# "VALIDATION PENDING", "MODIFY PENDING", ...) is treated as still-pending.


class _SupportsFillQueries(Protocol):
    def get_order_status(self, order_id: str) -> dict: ...
    def get_order_trades(self, order_id: str) -> list[dict]: ...
    def cancel_order(self, order_id: str, variety: str = ...) -> dict: ...


@dataclass(frozen=True)
class FillResult:
    """Terminal outcome of confirming a real order.

    status:
      FILLED   — fully filled; average_price/filled_quantity are authoritative
      PARTIAL  — partially filled at timeout (some quantity in the market)
      REJECTED — broker rejected the order; no quantity filled
      CANCELLED— order cancelled; filled_quantity may be >0 if partial-then-cancel
      TIMEOUT  — never reached a terminal state within the deadline (cancel attempted)
      ERROR    — could not query the order at all
    Only FILLED is safe to open a full position on.
    """
    status:          str
    average_price:   float = 0.0
    filled_quantity: int   = 0
    raw:             dict  = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"


def _avg_fill(trades: list[dict]) -> tuple[float, int]:
    """Quantity-weighted average fill price and total filled quantity.

    Kite trade rows expose ``average_price``/``price`` and ``quantity``/
    ``filled_quantity``.  Robust to missing/zero fields.
    """
    total_qty = 0
    notional  = 0.0
    for t in trades or []:
        try:
            q = int(t.get("quantity") or t.get("filled_quantity") or 0)
            p = float(t.get("average_price") or t.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if q > 0 and p > 0.0:
            total_qty += q
            notional  += p * q
    if total_qty <= 0:
        return 0.0, 0
    return notional / total_qty, total_qty


def confirm_fill(
    adapter:        _SupportsFillQueries,
    order_id:       str,
    *,
    expected_qty:   int,
    timeout_s:      float = 12.0,
    poll_interval_s: float = 0.5,
    cancel_on_timeout: bool = True,
    variety:        str = "regular",
    sleep:          Callable[[float], None] = time.sleep,
    monotonic:      Callable[[], float] = time.monotonic,
) -> FillResult:
    """Poll ``adapter`` until ``order_id`` reaches a terminal state.

    Returns a FillResult.  Only ``FILLED`` (full ``expected_qty`` confirmed via
    trades) is safe for the caller to open a position on.  On timeout the order
    is cancelled (best-effort) so it cannot fill later unobserved.
    """
    if not order_id:
        return FillResult("ERROR", raw={"reason": "empty_order_id"})

    deadline = monotonic() + max(timeout_s, 0.0)
    last_status_raw: dict = {}

    while monotonic() < deadline:
        try:
            st = adapter.get_order_status(order_id) or {}
        except Exception as exc:  # defensive — never raise into the loop
            st = {"status": "ERROR", "error": str(exc)}
        last_status_raw = st
        status = str(st.get("status", "")).upper()

        if status in _KITE_COMPLETE:
            try:
                trades = adapter.get_order_trades(order_id)
            except Exception:
                trades = []
            avg, qty = _avg_fill(trades)
            if qty >= expected_qty and avg > 0.0:
                return FillResult("FILLED", avg, qty, st)
            # Status COMPLETE but trades not yet visible / short — brief retry.
            sleep(poll_interval_s)
            continue

        if status in _KITE_DEAD:
            avg, qty = _avg_fill(_safe_trades(adapter, order_id))
            return FillResult(status, avg, qty, st)

        # Still pending — keep polling.
        sleep(poll_interval_s)

    # ── Deadline exceeded ───────────────────────────────────────────────────
    avg, qty = _avg_fill(_safe_trades(adapter, order_id))
    if cancel_on_timeout:
        try:
            adapter.cancel_order(order_id, variety)
        except Exception:
            pass
    if qty > 0:
        return FillResult("PARTIAL", avg, qty, last_status_raw)
    return FillResult("TIMEOUT", 0.0, 0, last_status_raw)


def _safe_trades(adapter: _SupportsFillQueries, order_id: str) -> list[dict]:
    try:
        return adapter.get_order_trades(order_id) or []
    except Exception:
        return []


__all__ = ["FillResult", "confirm_fill"]
