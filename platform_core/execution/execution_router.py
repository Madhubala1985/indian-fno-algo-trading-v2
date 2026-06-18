"""
platform_core.execution.execution_router
═══════════════════════════════════════════════════════════════════════
Thin routing layer between the live pipeline and broker adapters.

Responsibilities:
  - Accept entry/exit requests as typed arguments.
  - Build OrderIntent from those arguments.
  - Submit to the appropriate broker adapter (paper / dry-run / live).
  - Write order lifecycle records to DB (pt_order_intents, pt_broker_orders,
    pt_order_events) when a db is provided and mode != paper.
  - Return OrderResult to the caller.

Design rules:
  - Paper mode: pass-through, no extra DB writes, no adapter calls that
    change existing paper behavior.
  - real_dry_run: logs intent + result, no real order placed.
  - real_armed: blocked unless adapter confirms interlocks pass.
  - Never raises exceptions into the trading loop.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from platform_core.execution.broker_adapter import (
    OrderIntent,
    OrderResult,
    PaperBrokerAdapter,
    get_broker_adapter,
    _required_exchange,
)
from platform_core.execution.fill_confirmation import confirm_fill, FillResult

if TYPE_CHECKING:
    from platform_core.persistence.trade_database import TradeDatabase

from datetime import timedelta
_IST = timezone(timedelta(hours=5, minutes=30))


# ── ExecutionRouter ───────────────────────────────────────────────────────────

class ExecutionRouter:
    """
    Routes entry and exit order intents through the correct broker adapter.

    Args:
        mode: execution mode string — paper / real_dry_run / real_armed
        kite: KiteConnect instance (only used in real_armed mode)

    Usage in live pipeline:
        router = ExecutionRouter(mode=_EXECUTION_MODE)
        result = router.route_entry(
            underlying="BANKNIFTY", tradingsymbol="BANKNIFTY26JUN54000PE",
            lots=1, lot_size=15, premium=80.0, direction="SHORT",
            session_tag="live_20260609", db=_db,
        )
        result = router.route_exit(
            underlying="BANKNIFTY", tradingsymbol="BANKNIFTY26JUN54000PE",
            lots=1, lot_size=15, exit_premium=70.0,
            reason="STOP_LOSS", db=_db,
        )
    """

    def __init__(self, mode: str = "paper", kite: Any = None) -> None:
        self.mode    = mode.lower().strip()
        self._adapter = get_broker_adapter(self.mode, kite=kite)
        self._is_paper = isinstance(self._adapter, PaperBrokerAdapter)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def route_entry(
        self,
        *,
        underlying:       str,
        tradingsymbol:    str,
        lots:             int,
        lot_size:         int,
        premium:          float,
        direction:        str,
        session_tag:      str        = "",
        strategy_context: dict | None = None,
        risk_context:     dict | None = None,
        strike_context:   dict | None = None,
        db: "TradeDatabase | None"   = None,
    ) -> OrderResult:
        """
        Build an entry OrderIntent and route it through the broker adapter.

        In paper mode returns immediately without any DB writes or prints.
        In real_dry_run mode logs the intent and writes to pt_order_intents.
        In real_armed mode attempts a live order after interlock checks.
        """
        intent = OrderIntent(
            underlying       = underlying,
            tradingsymbol    = tradingsymbol,
            exchange         = _required_exchange(underlying),
            transaction_type = "BUY",
            quantity         = lots * lot_size,
            lot_size         = lot_size,
            lots             = lots,
            product          = "NRML",
            order_type       = "MARKET",
            price            = 0.0,
            trigger_price    = 0.0,
            variety          = "regular",
            validity         = "DAY",
            tag              = (session_tag or "live")[:20],
            reason           = "ENTRY",
            strategy_context = strategy_context or {"direction": direction},
            risk_context     = risk_context or {},
            strike_context   = strike_context or {"premium": premium},
        )
        return self._dispatch(intent, "ENTRY", db)

    # ── Exit ──────────────────────────────────────────────────────────────────

    def route_exit(
        self,
        *,
        underlying:    str,
        tradingsymbol: str,
        lots:          int,
        lot_size:      int,
        exit_premium:  float,
        reason:        str,
        session_tag:   str        = "",
        risk_context:  dict | None = None,
        db: "TradeDatabase | None" = None,
    ) -> OrderResult:
        """
        Build an exit OrderIntent and route it through the broker adapter.

        Never uses underlying spot as option exit price — exit_premium must be
        the option's BSM-estimated or last-traded price.
        """
        intent = OrderIntent(
            underlying       = underlying,
            tradingsymbol    = tradingsymbol,
            exchange         = _required_exchange(underlying),
            transaction_type = "SELL",
            quantity         = lots * lot_size,
            lot_size         = lot_size,
            lots             = lots,
            product          = "NRML",
            order_type       = "MARKET",
            price            = 0.0,
            trigger_price    = 0.0,
            variety          = "regular",
            validity         = "DAY",
            tag              = (session_tag or "live")[:20],
            reason           = reason,
            strategy_context = {},
            risk_context     = risk_context or {"exit_premium": exit_premium},
            strike_context   = {"exit_premium": exit_premium},
        )
        return self._dispatch(intent, "EXIT", db)

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _dispatch(
        self,
        intent: OrderIntent,
        direction: str,
        db: "TradeDatabase | None",
    ) -> OrderResult:
        """Route intent to adapter; write DB records for non-paper modes."""
        try:
            if direction == "EXIT":
                result = self._adapter.place_exit_order(intent)
            else:
                result = self._adapter.place_entry_order(intent)
        except Exception as exc:
            result = OrderResult(
                success=False,
                order_id="",
                mode=self.mode,
                status="ROUTER_ERROR",
                rejection_reason=str(exc),
            )

        # DB writes only for non-paper modes (paper keeps existing pt_trades path)
        if not self._is_paper and db is not None:
            self._write_db_records(intent, result, direction, db)

        return result

    def _write_db_records(
        self,
        intent:    OrderIntent,
        result:    OrderResult,
        direction: str,
        db:        "TradeDatabase",
    ) -> None:
        try:
            intent_id = db.write_order_intent(
                mode             = self.mode,
                underlying       = intent.underlying,
                tradingsymbol    = intent.tradingsymbol,
                exchange         = intent.exchange,
                transaction_type = intent.transaction_type,
                quantity         = intent.quantity,
                order_type       = intent.order_type,
                price            = intent.price,
                status           = result.status,
                reason           = intent.reason,
                raw_intent_json  = intent.to_json(),
            )
            if result.order_id:
                db.write_broker_order(
                    intent_id       = intent_id,
                    broker          = "kite_dryrun" if self.mode == "real_dry_run" else "kite_live",
                    broker_order_id = result.order_id,
                    mode            = self.mode,
                    status          = result.status,
                    average_price   = result.average_price,
                    filled_quantity = result.filled_quantity,
                    raw_response_json = result.to_json(),
                )
                db.write_order_event(
                    broker_order_id = result.order_id,
                    event_type      = direction,
                    status          = result.status,
                    message         = f"reason={intent.reason}  success={result.success}",
                    raw_json        = result.to_json(),
                )
        except Exception:
            pass   # fire-and-forget; never interrupt trading loop

    # ── Fill confirmation ─────────────────────────────────────────────────────

    def confirm_fill(
        self,
        result: OrderResult,
        *,
        expected_qty:     int,
        fallback_premium: float,
        timeout_s:        float = 12.0,
    ) -> FillResult:
        """Resolve an OrderResult into a terminal FillResult.

        - real_armed: poll the broker until the order reaches a terminal state
          and report the ACTUAL average fill price (fail-closed: only a fully
          confirmed order returns FILLED).
        - paper / real_dry_run: there is no real order to confirm, so synthesize
          an immediate FILLED at ``fallback_premium`` (the strike's last-traded
          price) — preserving existing paper/backtest behaviour exactly.

        The caller opens its local position at ``FillResult.average_price`` and
        must NOT open anything unless ``FillResult.is_filled``.
        """
        if self.mode == "real_armed":
            if not result.success or not result.order_id:
                return FillResult(
                    "REJECTED", 0.0, 0,
                    {"reason": result.rejection_reason or result.status},
                )
            return confirm_fill(
                self._adapter, result.order_id,
                expected_qty=expected_qty, timeout_s=timeout_s,
            )
        # paper / real_dry_run — synthetic immediate fill at the intended premium
        return FillResult("FILLED", fallback_premium, expected_qty, {"synthetic": True})

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def startup_print(self) -> None:
        """Print execution mode banner at pipeline startup."""
        real_orders = "true" if self.mode == "real_armed" and self._adapter.is_ready() else "false"
        adapter_name = type(self._adapter).__name__
        print(f"[EXECUTION MODE] {self.mode}")
        print(f"[REAL ORDERS ENABLED] {real_orders}")
        print(f"[ORDER ROUTER] broker={adapter_name}")


__all__ = ["ExecutionRouter"]
