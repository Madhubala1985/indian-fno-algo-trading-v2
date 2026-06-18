"""Position reconciliation: local vs broker. ok=False on any HIGH/CRITICAL break
(qty mismatch, or a position present on only one side)."""
from platform_core.execution.position_reconciler import reconcile_positions


def test_exact_match_ok():
    local  = [("SENSEX26JUN77000CE", 60, 606.85)]
    broker = [{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 60, "average_price": 606.85}]
    r = reconcile_positions(local_positions=local, broker_positions=broker)
    assert r.ok and r.break_count == 0


def test_both_flat_ok():
    r = reconcile_positions(local_positions=[], broker_positions=[])
    assert r.ok and r.break_count == 0


def test_quantity_mismatch_blocks():
    local  = [("SENSEX26JUN77000CE", 60, 606.85)]
    broker = [{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 40, "average_price": 606.85}]
    r = reconcile_positions(local_positions=local, broker_positions=broker)
    assert not r.ok and r.highest_severity == "CRITICAL"


def test_local_position_missing_at_broker_blocks():
    # pipeline thinks it holds a position the broker does not show
    local  = [("NIFTY2662324100CE", 65, 143.7)]
    broker = []
    r = reconcile_positions(local_positions=local, broker_positions=broker)
    assert not r.ok and r.highest_severity in ("HIGH", "CRITICAL")


def test_broker_position_unknown_to_local_blocks():
    # broker holds a position the pipeline doesn't know about (e.g. unconfirmed exit)
    local  = []
    broker = [{"tradingsymbol": "SENSEX26JUN77000CE", "quantity": 60, "average_price": 600.0}]
    r = reconcile_positions(local_positions=local, broker_positions=broker)
    assert not r.ok


def test_zero_qty_rows_ignored():
    # a closed broker leg (net 0) and a zero local leg should not create breaks
    local  = [("X", 0, 0.0)]
    broker = [{"tradingsymbol": "X", "quantity": 0, "average_price": 0.0}]
    r = reconcile_positions(local_positions=local, broker_positions=broker)
    assert r.ok and r.break_count == 0


def test_router_accessors_noop_in_paper():
    from platform_core.execution.execution_router import ExecutionRouter
    r = ExecutionRouter(mode="paper")
    assert r.get_broker_positions() == []
    assert r.get_available_margin() == float("inf")   # paper never blocked by margin


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
