"""Unit tests for platform_core.execution.fill_confirmation.confirm_fill.

Deterministic: injects a fake clock (sleep advances monotonic) and a fake
broker adapter that scripts order-status / trades responses per call.
"""
from platform_core.execution.fill_confirmation import confirm_fill, FillResult, _avg_fill


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class _FakeAdapter:
    """Scripts get_order_status by call index; trades by a fixed list (or a
    callable taking the status-call count so trades can 'appear' late)."""

    def __init__(self, statuses, trades=None):
        self._statuses = list(statuses)
        self._i = 0
        self._trades = trades if trades is not None else []
        self.cancelled = []

    def get_order_status(self, order_id):
        s = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return {"order_id": order_id, "status": s}

    def get_order_trades(self, order_id):
        if callable(self._trades):
            return self._trades(self._i)
        return self._trades

    def cancel_order(self, order_id, variety="regular"):
        self.cancelled.append(order_id)
        return {"order_id": order_id, "status": "CANCELLED"}


def _run(adapter, expected_qty=60, timeout_s=3.0):
    clk = _Clock()
    return confirm_fill(
        adapter, "OID1", expected_qty=expected_qty,
        timeout_s=timeout_s, poll_interval_s=0.5,
        sleep=clk.sleep, monotonic=clk.monotonic,
    )


def test_immediate_fill():
    a = _FakeAdapter(["COMPLETE"], trades=[{"average_price": 606.85, "quantity": 60}])
    r = _run(a)
    assert r.status == "FILLED" and r.is_filled
    assert abs(r.average_price - 606.85) < 1e-9 and r.filled_quantity == 60


def test_pending_then_fill():
    a = _FakeAdapter(["OPEN", "OPEN", "COMPLETE"],
                     trades=[{"average_price": 100.0, "quantity": 30},
                             {"average_price": 102.0, "quantity": 30}])
    r = _run(a)
    assert r.status == "FILLED"
    assert r.filled_quantity == 60
    assert abs(r.average_price - 101.0) < 1e-9   # qty-weighted avg


def test_rejected():
    a = _FakeAdapter(["REJECTED"], trades=[])
    r = _run(a)
    assert r.status == "REJECTED" and not r.is_filled
    assert r.filled_quantity == 0


def test_timeout_triggers_cancel():
    a = _FakeAdapter(["OPEN"], trades=[])      # never completes
    r = _run(a, timeout_s=2.0)
    assert r.status == "TIMEOUT" and not r.is_filled
    assert a.cancelled == ["OID1"]             # best-effort cancel attempted


def test_partial_at_timeout():
    a = _FakeAdapter(["OPEN"], trades=[{"average_price": 100.0, "quantity": 20}])
    r = _run(a, expected_qty=60, timeout_s=2.0)
    assert r.status == "PARTIAL" and not r.is_filled
    assert r.filled_quantity == 20


def test_complete_but_trades_appear_late():
    # COMPLETE every poll, but trades only become visible on/after the 3rd status call
    def trades(call_i):
        return [{"average_price": 250.0, "quantity": 60}] if call_i >= 3 else []
    a = _FakeAdapter(["COMPLETE"], trades=trades)
    r = _run(a)
    assert r.status == "FILLED"
    assert r.filled_quantity == 60 and abs(r.average_price - 250.0) < 1e-9


def test_empty_order_id_is_error():
    a = _FakeAdapter(["COMPLETE"], trades=[])
    clk = _Clock()
    r = confirm_fill(a, "", expected_qty=60, sleep=clk.sleep, monotonic=clk.monotonic)
    assert r.status == "ERROR" and not r.is_filled


def test_avg_fill_ignores_bad_rows():
    avg, qty = _avg_fill([
        {"average_price": 0.0, "quantity": 50},      # zero price -> ignored
        {"average_price": 100.0, "quantity": 0},      # zero qty -> ignored
        {"average_price": 100.0, "quantity": 25},
        {"price": 120.0, "filled_quantity": 25},      # alt field names
    ])
    assert qty == 50 and abs(avg - 110.0) < 1e-9


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
