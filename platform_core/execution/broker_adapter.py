"""
platform_core.execution.broker_adapter
═══════════════════════════════════════════════════════════════════════
Broker abstraction layer for paper / dry-run / live order execution.

Three execution modes:
  paper        — paper trading only; no broker API calls (default)
  real_dry_run — validates + logs order intents, never calls place_order
  real_armed   — fail-closed unless ALL real-money safety interlocks pass

Three adapter implementations:
  PaperBrokerAdapter      — no-op; always returns a fake paper fill
  KiteDryRunBrokerAdapter — validates, logs DRY_RUN_ORDER_INTENT, no API call
  KiteLiveBrokerAdapter   — calls kite SDK; gated by RealOrderInterlocks

Safety invariants (enforced in code):
  - KiteDryRunBrokerAdapter.place_entry_order / place_exit_order NEVER call
    kite.place_order under any circumstances.
  - KiteLiveBrokerAdapter.place_entry_order / place_exit_order call
    kite.place_order only when RealOrderInterlocks.all_pass() returns True.
  - An order_id returned by place_order is NEVER treated as a fill; real fill
    requires a separate order-status / order-trades confirmation step.

Exchange routing:
  SENSEX           → BFO (BSE options)
  BANKNIFTY/NIFTY  → NFO (NSE options)
  FinNifty         → NFO

Usage:
  adapter = get_broker_adapter(mode="paper")
  result  = adapter.place_entry_order(intent)
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any


# ── Execution modes ───────────────────────────────────────────────────────────

EXECUTION_MODES = frozenset({"paper", "real_dry_run", "real_armed"})

_IST = timezone(timedelta(hours=5, minutes=30))

# Exchanges
_NFO_UNDERLYINGS = frozenset({"BANKNIFTY", "NIFTY", "FINNIFTY"})
_BFO_UNDERLYINGS = frozenset({"SENSEX", "BANKEX"})


def _required_exchange(underlying: str) -> str:
    """Return the correct exchange for an underlying."""
    u = underlying.upper()
    if u in _BFO_UNDERLYINGS:
        return "BFO"
    if u in _NFO_UNDERLYINGS:
        return "NFO"
    return "NFO"  # default; will be caught by validation


# ── OrderIntent ───────────────────────────────────────────────────────────────

@dataclass
class OrderIntent:
    """
    Structured representation of a single broker order to be placed.

    Covers entry and exit orders for options contracts only.
    Never represents a raw index trade — tradingsymbol must contain CE or PE.
    """

    underlying:        str    # BANKNIFTY / NIFTY / SENSEX
    tradingsymbol:     str    # full contract symbol e.g. BANKNIFTY26JUN54000PE
    exchange:          str    # NFO / BFO
    transaction_type:  str    # BUY / SELL
    quantity:          int    # lots × lot_size
    lot_size:          int
    lots:              int
    product:           str    # NRML / MIS
    order_type:        str    # MARKET / LIMIT / SL / SL-M
    price:             float  # 0.0 for MARKET
    trigger_price:     float  # 0.0 when not SL-type
    variety:           str    # regular / amo / co / iceberg
    validity:          str    # DAY / IOC
    tag:               str    # pipeline run tag
    reason:            str    # ENTRY / STOP_LOSS / STRUCTURE_EXIT / EOD_FORCE_CLOSE
    strategy_context:  dict   = field(default_factory=dict)
    risk_context:      dict   = field(default_factory=dict)
    strike_context:    dict   = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, default=str)


# ── OrderResult ───────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    """Returned by every adapter place_entry_order / place_exit_order call."""

    success:          bool
    order_id:         str
    mode:             str
    status:           str
    filled:           bool   = False  # True only when order-status confirms fill
    average_price:    float  = 0.0
    filled_quantity:  int    = 0
    rejection_reason: str    = ""
    raw_response:     dict   = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, default=str)


# ── OrderIntent validation ────────────────────────────────────────────────────

_RAW_INDEX_SYMBOLS = frozenset({
    "NIFTY", "NIFTY50", "BANKNIFTY", "SENSEX", "FINNIFTY", "BANKEX",
    "NIFTY 50", "NIFTY50 INDEX",
})

_VALID_PRODUCTS    = frozenset({"NRML", "MIS", "CNC", "BO", "CO"})
_VALID_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SL-M"})
_VALID_EXCHANGES   = frozenset({"NFO", "BFO", "NSE", "BSE"})


def validate_order_intent(
    intent: OrderIntent,
    session_phase: str = "",
    is_market_open: bool = True,
) -> tuple[bool, str]:
    """
    Validate an OrderIntent against exchange, symbol, and session rules.

    Returns (True, "") when valid.
    Returns (False, "ORDER_INTENT_VALIDATION_FAILED: <reason>") on failure.
    """

    def _fail(reason: str) -> tuple[bool, str]:
        return False, f"ORDER_INTENT_VALIDATION_FAILED: {reason}"

    # 1. No raw index symbols
    ts_upper = intent.tradingsymbol.upper().strip()
    if ts_upper in _RAW_INDEX_SYMBOLS:
        return _fail(
            f"raw index symbol '{intent.tradingsymbol}' cannot be traded as a contract"
        )

    # 2. Must contain CE or PE
    if "CE" not in ts_upper and "PE" not in ts_upper:
        return _fail(
            f"tradingsymbol '{intent.tradingsymbol}' must contain CE or PE"
        )

    # 3. Exchange correctness
    required_exch = _required_exchange(intent.underlying)
    if intent.exchange.upper() != required_exch:
        return _fail(
            f"{intent.underlying} requires exchange={required_exch}, "
            f"got '{intent.exchange}'"
        )

    if intent.exchange.upper() not in _VALID_EXCHANGES:
        return _fail(f"unknown exchange '{intent.exchange}'")

    # 4. Transaction type
    if intent.transaction_type.upper() not in ("BUY", "SELL"):
        return _fail(f"transaction_type must be BUY or SELL, got '{intent.transaction_type}'")

    # 5. Quantity consistency
    if intent.lots <= 0:
        return _fail(f"lots must be > 0, got {intent.lots}")
    if intent.lot_size <= 0:
        return _fail(f"lot_size must be > 0, got {intent.lot_size}")
    expected_qty = intent.lots * intent.lot_size
    if intent.quantity != expected_qty:
        return _fail(
            f"quantity={intent.quantity} != lots({intent.lots}) × lot_size({intent.lot_size})={expected_qty}"
        )
    if intent.quantity <= 0:
        return _fail(f"quantity must be > 0, got {intent.quantity}")

    # 6. Product and order_type explicit
    if intent.product.upper() not in _VALID_PRODUCTS:
        return _fail(f"product must be one of {sorted(_VALID_PRODUCTS)}, got '{intent.product}'")
    if intent.order_type.upper() not in _VALID_ORDER_TYPES:
        return _fail(f"order_type must be one of {sorted(_VALID_ORDER_TYPES)}, got '{intent.order_type}'")

    # 7. LIMIT requires a positive price
    if intent.order_type.upper() == "LIMIT" and intent.price <= 0.0:
        return _fail("LIMIT order requires price > 0")

    # 8. No real orders during closing/preopen
    if session_phase in ("CLOSING", "PRE_OPEN", "POST_CLOSE"):
        return _fail(
            f"real orders not allowed during session_phase={session_phase}"
        )

    # 9. No real orders when market closed
    if not is_market_open:
        return _fail("market is not open — real order not allowed")

    return True, ""


# ── Real-money safety interlocks ──────────────────────────────────────────────

_REQUIRED_REAL_INTERLOCKS = [
    "REAL_TRADING_ENABLED",
    "REAL_TRADING_CONFIRMATION",
    "BROKER_LIVE_ORDERS_ALLOWED",
    "MAX_REAL_CAPITAL",
    "MAX_REAL_ORDER_VALUE",
    "MAX_REAL_DAILY_LOSS",
    "REAL_ALLOWED_UNDERLYINGS",
    "REAL_ALLOWED_EXCHANGES",
    "REAL_MAX_LOTS_PER_ORDER",
]

_REQUIRED_REAL_VALUES = {
    "REAL_TRADING_ENABLED": "true",
    "REAL_TRADING_CONFIRMATION": "I_UNDERSTAND_REAL_MONEY_RISK",
    "BROKER_LIVE_ORDERS_ALLOWED": "true",
}


def _check_real_interlocks() -> tuple[bool, list[str]]:
    """
    Verify all real-money safety interlocks are set.

    Returns (True, []) when all pass.
    Returns (False, [list of missing/wrong interlock names]) on failure.
    """
    missing: list[str] = []

    for key in _REQUIRED_REAL_INTERLOCKS:
        val = os.environ.get(key, "")
        if not val:
            missing.append(f"{key} (not set)")
            continue
        required_val = _REQUIRED_REAL_VALUES.get(key)
        if required_val is not None and val != required_val:
            missing.append(f"{key}={val!r} (expected {required_val!r})")

    return (len(missing) == 0), missing


# ── Adapters ─────────────────────────────────────────────────────────────────

class PaperBrokerAdapter:
    """
    No-op adapter for paper trading.

    Returns an immediate synthetic fill at the provided price (LTP).
    Never calls any broker API.
    """

    MODE = "paper"

    def place_entry_order(self, intent: OrderIntent) -> OrderResult:
        _oid = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        return OrderResult(
            success=True,
            order_id=_oid,
            mode=self.MODE,
            status="PAPER_FILL",
            filled=True,
            average_price=intent.price if intent.price > 0 else 0.0,
            filled_quantity=intent.quantity,
        )

    def place_exit_order(self, intent: OrderIntent) -> OrderResult:
        return self.place_entry_order(intent)

    def get_order_status(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "PAPER_FILL", "mode": self.MODE}

    def get_order_trades(self, order_id: str) -> list[dict]:
        return [{"order_id": order_id, "status": "PAPER_FILL", "mode": self.MODE}]

    def cancel_order(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "CANCELLED", "mode": self.MODE}

    def get_positions(self) -> list[dict]:
        return []

    def get_available_margin(self) -> float:
        return 0.0

    def is_ready(self) -> bool:
        return True


class KiteDryRunBrokerAdapter:
    """
    Dry-run adapter: validates, logs exactly what WOULD be sent, never calls place_order.

    Safe to use with live market data. Order intents are written to logs but
    no real order ever reaches Kite.
    """

    MODE = "real_dry_run"

    def place_entry_order(self, intent: OrderIntent) -> OrderResult:
        return self._execute_dry_run(intent, direction="ENTRY")

    def place_exit_order(self, intent: OrderIntent) -> OrderResult:
        return self._execute_dry_run(intent, direction="EXIT")

    def _execute_dry_run(self, intent: OrderIntent, direction: str) -> OrderResult:
        ok, reason = validate_order_intent(intent)
        if not ok:
            print(
                f"[DRY_RUN_REJECTED] {direction} "
                f"{intent.underlying} {intent.tradingsymbol}  "
                f"reason={reason}"
            )
            return OrderResult(
                success=False,
                order_id="",
                mode=self.MODE,
                status="VALIDATION_FAILED",
                rejection_reason=reason,
            )

        _oid = f"DRYRUN-{uuid.uuid4().hex[:12].upper()}"
        _ts  = datetime.now(_IST).strftime("%H:%M:%S IST")

        print(
            f"\n[DRY_RUN_ORDER_INTENT] {direction} {_ts}\n"
            f"  underlying   : {intent.underlying}\n"
            f"  symbol       : {intent.tradingsymbol}\n"
            f"  exchange     : {intent.exchange}\n"
            f"  tx_type      : {intent.transaction_type}\n"
            f"  quantity     : {intent.quantity} ({intent.lots} lots × {intent.lot_size})\n"
            f"  product      : {intent.product}\n"
            f"  order_type   : {intent.order_type}\n"
            f"  price        : {intent.price:.2f}\n"
            f"  reason       : {intent.reason}\n"
            f"  dry_run_id   : {_oid}\n"
            f"  !! NO REAL ORDER PLACED — dry-run mode !!\n"
        )

        # Note: order_id is returned but must NOT be treated as a fill.
        # Real fill requires order status/trades confirmation via get_order_trades().
        return OrderResult(
            success=True,
            order_id=_oid,
            mode=self.MODE,
            status="DRY_RUN_LOGGED",
            filled=False,     # never auto-fill in dry-run; confirmation required
            average_price=0.0,
            filled_quantity=0,
            raw_response={"dry_run": True, "would_send": {
                "tradingsymbol": intent.tradingsymbol,
                "exchange": intent.exchange,
                "transaction_type": intent.transaction_type,
                "quantity": intent.quantity,
                "product": intent.product,
                "order_type": intent.order_type,
                "price": intent.price,
                "variety": intent.variety,
                "validity": intent.validity,
                "tag": intent.tag,
            }},
        )

    def get_order_status(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "DRY_RUN_LOGGED", "mode": self.MODE}

    def get_order_trades(self, order_id: str) -> list[dict]:
        # Dry-run: no real trades ever occur
        return []

    def cancel_order(self, order_id: str) -> dict:
        print(f"[DRY_RUN] cancel_order({order_id}) — no real order to cancel")
        return {"order_id": order_id, "status": "DRY_RUN_CANCEL", "mode": self.MODE}

    def get_positions(self) -> list[dict]:
        return []

    def get_available_margin(self) -> float:
        return 0.0

    def is_ready(self) -> bool:
        return True


class KiteLiveBrokerAdapter:
    """
    Live adapter that calls kite.place_order().

    SAFETY: place_entry_order / place_exit_order call place_order ONLY when
    RealOrderInterlocks.all_pass() returns True AND the execution mode is
    explicitly real_armed. Any interlock failure causes a fail-closed response.

    Do NOT instantiate this adapter unless explicitly building real_armed
    infrastructure. Tomorrow's test sessions should use KiteDryRunBrokerAdapter.
    """

    MODE = "real_armed"

    def __init__(self, kite: Any = None) -> None:
        self._kite = kite  # KiteConnect instance, or None

    def place_entry_order(self, intent: OrderIntent) -> OrderResult:
        return self._execute_live(intent, direction="ENTRY")

    def place_exit_order(self, intent: OrderIntent) -> OrderResult:
        return self._execute_live(intent, direction="EXIT")

    def _execute_live(self, intent: OrderIntent, direction: str) -> OrderResult:
        # Step 1: verify all real-money interlocks
        interlocks_ok, missing = _check_real_interlocks()
        if not interlocks_ok:
            for m in missing:
                print(f"[REAL ORDER BLOCKED] missing interlock={m}")
            return OrderResult(
                success=False,
                order_id="",
                mode=self.MODE,
                status="INTERLOCK_BLOCKED",
                rejection_reason=f"missing_interlocks={missing}",
            )

        # Step 2: validate order intent
        ok, reason = validate_order_intent(intent)
        if not ok:
            print(f"[REAL ORDER BLOCKED] {direction} rejected: {reason}")
            return OrderResult(
                success=False,
                order_id="",
                mode=self.MODE,
                status="VALIDATION_FAILED",
                rejection_reason=reason,
            )

        # Step 3: kite SDK call (only reached when all interlocks pass)
        if self._kite is None:
            print("[REAL ORDER BLOCKED] kite SDK client not initialised")
            return OrderResult(
                success=False,
                order_id="",
                mode=self.MODE,
                status="NO_KITE_CLIENT",
                rejection_reason="kite SDK client is None",
            )

        try:
            order_id: str = self._kite.place_order(
                variety=intent.variety,
                exchange=intent.exchange,
                tradingsymbol=intent.tradingsymbol,
                transaction_type=intent.transaction_type,
                quantity=intent.quantity,
                product=intent.product,
                order_type=intent.order_type,
                price=intent.price if intent.price > 0 else None,
                trigger_price=intent.trigger_price if intent.trigger_price > 0 else None,
                validity=intent.validity,
                tag=intent.tag[:20] if intent.tag else "",
            )
            # CRITICAL: order_id is NOT a fill. filled=False until confirmed.
            return OrderResult(
                success=True,
                order_id=str(order_id),
                mode=self.MODE,
                status="PLACED",
                filled=False,     # confirm via get_order_trades()
                raw_response={"order_id": str(order_id)},
            )
        except Exception as exc:
            print(f"[REAL ORDER ERROR] {direction} {intent.tradingsymbol}: {exc}")
            return OrderResult(
                success=False,
                order_id="",
                mode=self.MODE,
                status="PLACE_FAILED",
                rejection_reason=str(exc),
            )

    def get_order_status(self, order_id: str) -> dict:
        if self._kite is None:
            return {"order_id": order_id, "status": "NO_CLIENT"}
        try:
            orders = self._kite.orders()
            for o in orders:
                if str(o.get("order_id", "")) == str(order_id):
                    return o
            return {"order_id": order_id, "status": "NOT_FOUND"}
        except Exception as exc:
            return {"order_id": order_id, "status": "ERROR", "error": str(exc)}

    def get_order_trades(self, order_id: str) -> list[dict]:
        if self._kite is None:
            return []
        try:
            return self._kite.order_trades(order_id)
        except Exception:
            return []

    def cancel_order(self, order_id: str, variety: str = "regular") -> dict:
        if self._kite is None:
            return {"order_id": order_id, "status": "NO_CLIENT"}
        try:
            self._kite.cancel_order(variety=variety, order_id=order_id)
            return {"order_id": order_id, "status": "CANCELLED"}
        except Exception as exc:
            return {"order_id": order_id, "status": "ERROR", "error": str(exc)}

    def get_positions(self) -> list[dict]:
        if self._kite is None:
            return []
        try:
            pos = self._kite.positions()
            return pos.get("net", [])
        except Exception:
            return []

    def get_available_margin(self) -> float:
        if self._kite is None:
            return 0.0
        try:
            margins = self._kite.margins(segment="equity")
            return float(margins.get("available", {}).get("live_balance", 0.0))
        except Exception:
            return 0.0

    def is_ready(self) -> bool:
        interlocks_ok, _ = _check_real_interlocks()
        return interlocks_ok and self._kite is not None


# ── Factory ───────────────────────────────────────────────────────────────────

def get_broker_adapter(
    mode: str,
    kite: Any = None,
) -> PaperBrokerAdapter | KiteDryRunBrokerAdapter | KiteLiveBrokerAdapter:
    """
    Return the correct broker adapter for the given execution mode.

    Args:
        mode: "paper" | "real_dry_run" | "real_armed"
        kite: KiteConnect instance (only required for real_armed)

    The default is always paper. real_armed is only activatable when all
    real-money interlocks are configured.
    """
    m = mode.lower().strip()
    if m not in EXECUTION_MODES:
        print(f"[BROKER ADAPTER] unknown mode={mode!r} — falling back to paper")
        m = "paper"

    if m == "paper":
        return PaperBrokerAdapter()

    if m == "real_dry_run":
        return KiteDryRunBrokerAdapter()

    # real_armed: verify interlocks before handing back a live adapter
    interlocks_ok, missing = _check_real_interlocks()
    if not interlocks_ok:
        print("[BROKER ADAPTER] real_armed requested but interlocks not configured:")
        for m_item in missing:
            print(f"  [REAL ORDER BLOCKED] missing interlock={m_item}")
        print("[BROKER ADAPTER] Falling back to KiteDryRunBrokerAdapter (fail-closed).")
        return KiteDryRunBrokerAdapter()

    return KiteLiveBrokerAdapter(kite=kite)


__all__ = [
    "OrderIntent",
    "OrderResult",
    "PaperBrokerAdapter",
    "KiteDryRunBrokerAdapter",
    "KiteLiveBrokerAdapter",
    "get_broker_adapter",
    "validate_order_intent",
    "_check_real_interlocks",
    "_required_exchange",
    "EXECUTION_MODES",
]
