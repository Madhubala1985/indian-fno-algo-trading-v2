"""End-to-end real_armed path with a MOCK Kite client — NO real money.

Exercises the full chain the live pipeline relies on in real_armed:
interlocks gate → place_order → fill confirmation at the ACTUAL price →
reject/partial handling → position reconciliation → margin query.

A MockKite records calls and returns scripted broker responses, so the real
KiteLiveBrokerAdapter code path runs without touching Zerodha.
"""
import os

# Real-money interlocks must be set BEFORE constructing a real_armed router,
# or get_broker_adapter fail-closes to the dry-run adapter.
os.environ.update({
    "REAL_TRADING_ENABLED": "true",
    "REAL_TRADING_CONFIRMATION": "I_UNDERSTAND_REAL_MONEY_RISK",
    "BROKER_LIVE_ORDERS_ALLOWED": "true",
    "MAX_REAL_CAPITAL": "25000",
    "MAX_REAL_ORDER_VALUE": "15000",
    "MAX_REAL_DAILY_LOSS": "2000",
    "REAL_ALLOWED_UNDERLYINGS": "SENSEX",
    "REAL_ALLOWED_EXCHANGES": "BFO",
    "REAL_MAX_LOTS_PER_ORDER": "1",
})

from platform_core.execution.execution_router import ExecutionRouter
from platform_core.execution.broker_adapter import KiteLiveBrokerAdapter
from platform_core.execution.position_reconciler import reconcile_positions


class MockKite:
    """Scriptable stand-in for KiteConnect — records calls, returns canned data."""
    def __init__(self, status="COMPLETE", trades=None, positions=None, margin=500000.0):
        self.placed = []
        self.cancelled = []
        self._status = status
        self._trades = trades if trades is not None else []
        self._positions = positions if positions is not None else []
        self._margin = margin

    def place_order(self, **kw):
        self.placed.append(kw)
        return "MOCKORD1"

    def orders(self):
        return [{"order_id": "MOCKORD1", "status": self._status}]

    def order_trades(self, order_id):
        return self._trades

    def positions(self):
        return {"net": self._positions}

    def margins(self, segment="equity"):
        return {"available": {"live_balance": self._margin}}

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)


def _router(mk):
    r = ExecutionRouter(mode="real_armed", kite=mk)
    assert isinstance(r._adapter, KiteLiveBrokerAdapter), \
        f"expected live adapter, got {type(r._adapter).__name__}"
    return r


def test_real_armed_uses_live_adapter_with_interlocks():
    _router(MockKite())   # assertion inside _router


def test_entry_places_real_order_and_confirms_actual_fill():
    mk = MockKite(status="COMPLETE", trades=[{"average_price": 611.25, "quantity": 60}])
    r = _router(mk)
    res = r.route_entry(underlying="SENSEX", tradingsymbol="SENSEX26JUN77000CE",
                        lots=3, lot_size=20, premium=600.0, direction="LONG", session_tag="t")
    # real order placed with correct params; order_id returned but NOT a fill yet
    assert res.success and res.order_id == "MOCKORD1" and res.filled is False
    p = mk.placed[0]
    assert p["transaction_type"] == "BUY" and p["quantity"] == 60 and p["order_type"] == "MARKET"
    assert p["exchange"] == "BFO"
    # confirm the fill at the ACTUAL average price (not the 600 fallback)
    f = r.confirm_fill(res, expected_qty=60, fallback_premium=600.0)
    assert f.is_filled and f.filled_quantity == 60 and abs(f.average_price - 611.25) < 1e-9


def test_exit_places_sell_and_confirms():
    mk = MockKite(status="COMPLETE", trades=[{"average_price": 305.0, "quantity": 60}])
    r = _router(mk)
    res = r.route_exit(underlying="SENSEX", tradingsymbol="SENSEX26JUN77000CE",
                       lots=3, lot_size=20, exit_premium=300.0, reason="STOP_LOSS", session_tag="t")
    assert mk.placed[0]["transaction_type"] == "SELL"
    f = r.confirm_fill(res, expected_qty=60, fallback_premium=300.0)
    assert f.is_filled and abs(f.average_price - 305.0) < 1e-9


def test_rejected_order_is_failclosed():
    mk = MockKite(status="REJECTED", trades=[])
    r = _router(mk)
    res = r.route_entry(underlying="SENSEX", tradingsymbol="SENSEX26JUN77000CE",
                        lots=1, lot_size=20, premium=600.0, direction="LONG", session_tag="t")
    f = r.confirm_fill(res, expected_qty=20, fallback_premium=600.0)
    assert not f.is_filled and f.status == "REJECTED"   # caller opens NO position


def test_broker_positions_and_margin_accessors():
    mk = MockKite(positions=[{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 60, "average_price": 611.0}],
                  margin=42000.0)
    r = _router(mk)
    assert r.get_available_margin() == 42000.0
    bp = r.get_broker_positions()
    assert bp and bp[0]["tradingsymbol"] == "SENSEX26JUN77000CE"


def test_reconcile_detects_local_vs_broker_mismatch():
    # broker holds 60 but local thinks 40 → CRITICAL break ⇒ pipeline would halt
    mk = MockKite(positions=[{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 60, "average_price": 611.0}])
    r = _router(mk)
    out = reconcile_positions(
        local_positions=[("SENSEX26JUN77000CE", 40, 611.0)],
        broker_positions=r.get_broker_positions(),
    )
    assert not out.ok and out.highest_severity == "CRITICAL"


def test_reconcile_clean_when_matched():
    mk = MockKite(positions=[{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 60, "average_price": 611.0}])
    r = _router(mk)
    out = reconcile_positions(
        local_positions=[("SENSEX26JUN77000CE", 60, 611.0)],
        broker_positions=r.get_broker_positions(),
    )
    assert out.ok and out.break_count == 0


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS {fn.__name__}")
        except Exception as exc:
            failed += 1; print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
