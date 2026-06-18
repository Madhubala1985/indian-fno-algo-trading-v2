"""Intra-bar gap-protection stop in OptionPaperTrader.evaluate_and_close.

A fast adverse move (bar low for a CALL, bar high for a PUT) that would have
breached the stop intra-bar caps the loss near stop_loss_pct (30%) instead of
being discovered at the worse close. Defaults (no extremes) preserve old behaviour.
"""
from datetime import date, timedelta
from platform_core.backtesting.backtest_runner import OptionPaperTrader, OptionPosition
from platform_core.options.greeks_engine import OptionGreeksInput

TODAY = date.today()
EXPIRY = TODAY + timedelta(days=6)
TTE = max((EXPIRY - TODAY).days, 1) / 365.25


def _premium(t, spot, strike, otype, iv):
    r = t._greeks_engine.compute(option_input=OptionGreeksInput(
        instrument_id="x", underlying_symbol="X", option_symbol="XO",
        option_type=otype, spot_price=spot, strike_price=strike,
        time_to_expiry_years=TTE, implied_volatility=iv,
        risk_free_rate=0.068, dividend_yield=0.0, lot_size=20))
    return max(r.option_price, 0.01)


def _pos(t, es, k, otype, iv, ep):
    t._position = OptionPosition(
        underlying_symbol="X", contract_symbol="XO", strike=k, option_type=otype,
        expiry_date=EXPIRY, lot_size=20, lots=4, entry_premium=ep,
        entry_spot=es, entry_iv=iv, direction="LONG", bar_index=0)


def _close(t, close_spot, low=None, high=None):
    return t.evaluate_and_close(
        current_spot=close_spot, current_iv=0.12, current_date=TODAY, bar_index=10,
        stop_loss_pct=0.70, take_profit_pct=2.0, min_dte=2, force_reason=None,
        bar_low_spot=low, bar_high_spot=high)


def test_intrabar_stop_caps_call_loss_at_30pct():
    t = OptionPaperTrader(); es, k, iv = 77200.0, 77200.0, 0.12
    ep = _premium(t, es, k, "CALL", iv); _pos(t, es, k, "CALL", iv, ep)
    # close barely below entry (no breach), but bar LOW gaps far down (breach)
    tr = _close(t, close_spot=es - 5, low=es - 700, high=es + 5)
    assert tr is not None and "intrabar" in tr.exit_reason
    loss_pct = 1.0 - tr.exit_premium / ep
    assert abs(loss_pct - 0.30) < 0.005, f"loss {loss_pct:.3f} not ~30%"
    assert tr.pnl < 0


def test_no_intrabar_when_extremes_not_passed():
    t = OptionPaperTrader(); es, k, iv = 77200.0, 77200.0, 0.12
    ep = _premium(t, es, k, "CALL", iv); _pos(t, es, k, "CALL", iv, ep)
    tr = _close(t, close_spot=es - 5)   # no extremes, close near entry → no exit
    assert tr is None


def test_close_based_stop_still_fires_without_intrabar():
    t = OptionPaperTrader(); es, k, iv = 77200.0, 77200.0, 0.12
    ep = _premium(t, es, k, "CALL", iv); _pos(t, es, k, "CALL", iv, ep)
    tr = _close(t, close_spot=es - 700)  # close itself breaches; no extremes passed
    assert tr is not None and "STOP_LOSS" in tr.exit_reason and "intrabar" not in tr.exit_reason


def test_put_uses_bar_high():
    t = OptionPaperTrader(); es, k, iv = 77200.0, 77200.0, 0.12
    ep = _premium(t, es, k, "PUT", iv); _pos(t, es, k, "PUT", iv, ep)
    # a PUT is hurt by spot RISING → bar high gaps up (breach)
    tr = _close(t, close_spot=es + 5, low=es - 5, high=es + 700)
    assert tr is not None and "intrabar" in tr.exit_reason


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
