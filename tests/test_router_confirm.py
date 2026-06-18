"""ExecutionRouter.confirm_fill: paper/dry-run synthesize a fill at LTP;
real_armed delegates to the broker and fail-closes on a missing order_id."""
from platform_core.execution.execution_router import ExecutionRouter
from platform_core.execution.broker_adapter import OrderResult


def test_paper_synthesizes_fill_at_ltp():
    r = ExecutionRouter(mode="paper")
    res = OrderResult(success=True, order_id="PAPER-X", mode="paper", status="PAPER_FILL", filled=True)
    f = r.confirm_fill(res, expected_qty=60, fallback_premium=143.7)
    assert f.is_filled and f.filled_quantity == 60
    assert abs(f.average_price - 143.7) < 1e-9   # opens at LTP, exactly as before


def test_dry_run_synthesizes_fill_at_ltp():
    r = ExecutionRouter(mode="real_dry_run")
    res = OrderResult(success=True, order_id="DRY-1", mode="real_dry_run", status="DRY_RUN")
    f = r.confirm_fill(res, expected_qty=20, fallback_premium=606.85)
    assert f.is_filled and abs(f.average_price - 606.85) < 1e-9


def test_real_armed_rejects_without_order_id():
    r = ExecutionRouter(mode="real_armed")          # KiteLive adapter, kite=None
    res = OrderResult(success=False, order_id="", mode="real_armed",
                      status="INTERLOCK_BLOCKED", rejection_reason="missing_interlocks")
    f = r.confirm_fill(res, expected_qty=60, fallback_premium=100.0)
    assert not f.is_filled and f.status == "REJECTED"   # fail-closed: no position opened


def test_real_armed_uses_actual_broker_fill():
    r = ExecutionRouter(mode="real_armed")

    class _Adapter:
        def get_order_status(self, oid): return {"status": "COMPLETE"}
        def get_order_trades(self, oid): return [{"average_price": 611.25, "quantity": 60}]
        def cancel_order(self, oid, variety="regular"): return {}
    r._adapter = _Adapter()                          # inject confirmable order

    res = OrderResult(success=True, order_id="REAL-1", mode="real_armed", status="PLACED", filled=False)
    f = r.confirm_fill(res, expected_qty=60, fallback_premium=606.85)
    assert f.is_filled and f.filled_quantity == 60
    assert abs(f.average_price - 611.25) < 1e-9      # ACTUAL fill, not the LTP fallback


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
