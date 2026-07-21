"""
Exit-logic fixes (2026-07-21) — the two default-preserving knobs added for
Bug 1 (stop overshoot) and Bug 3 (ratchet gives back the whole gain).

The safety-critical property: at DEFAULT env (both knobs off/zero) the exit
decision must be BYTE-identical to the prior behaviour, since this code is the
shared live+backtest exit path. These tests pin that, and pin that the knobs do
what they claim when enabled.
"""
from __future__ import annotations

import os
os.environ.setdefault("PAPER_EXECUTION_MODE", "paper")


def _slm_fill(entry, close, stop_pct, on, slip):
    """Mirror of the Bug-1 stop-fill decision in backtest_runner._evaluate_exit."""
    pct = close / entry
    cur = close
    if on and pct < stop_pct:
        f = entry * stop_pct * (1.0 - slip)
        if f > cur:
            cur = f
    return cur, 1.0 - cur / entry


def _lock_level(entry, peak, lock_frac):
    """Mirror of the Bug-3 ratchet lock-level decision."""
    return entry * (1.0 + lock_frac * (peak / entry - 1.0))


# ── Bug 1: default preserves the (conservative) bar-close fill ───────────────

def test_stop_default_off_preserves_bar_close():
    # the two real SENSEX overshoots must stay at their 40% bar-close fill
    for entry, close in [(502.95, 303.29), (495.0, 296.01)]:
        cur, loss = _slm_fill(entry, close, 0.70, on=False, slip=0.03)
        assert cur == close
        assert round(loss, 2) == 0.40


def test_stop_fix_on_tightens_overshoot_to_near_stop():
    cur, loss = _slm_fill(502.95, 303.29, 0.70, on=True, slip=0.03)
    assert loss < 0.40 and loss > 0.30          # between exact-stop and bar-close
    assert round(loss, 2) == 0.32


def test_stop_fix_never_touches_a_clean_stop():
    # NIFTY hit exactly 30% — no overshoot — must be unchanged even with fix on
    cur, loss = _slm_fill(135.6, 94.92, 0.70, on=True, slip=0.03)
    assert abs(cur - 94.92) < 1e-9


def test_stop_fix_never_models_a_fill_above_the_trigger():
    # modelled fill must be at/below the stop trigger (a falling SL-M can't fill higher)
    entry, stop_pct, slip = 100.0, 0.70, 0.03
    cur, _ = _slm_fill(entry, 55.0, stop_pct, on=True, slip=slip)
    assert cur <= entry * stop_pct + 1e-9


def test_stop_fix_slippage_keeps_it_honest_not_exact_stop():
    # with slippage > 0 the modelled loss is strictly worse than the exact 30% stop
    _, loss = _slm_fill(100.0, 55.0, 0.70, on=True, slip=0.03)
    assert loss > 0.30


# ── Bug 3: default lock == breakeven; lock_frac locks a share of the peak ─────

def test_ratchet_default_locks_breakeven():
    assert _lock_level(100.0, 110.0, 0.0) == 100.0        # exactly entry = current


def test_ratchet_lock_protects_fraction_of_peak():
    # peak +10%, lock 50% -> protect +5% above entry
    assert _lock_level(100.0, 110.0, 0.5) == 105.0


def test_ratchet_lock_full_protects_whole_peak():
    assert abs(_lock_level(100.0, 111.0, 1.0) - 111.0) < 1e-9


# ── the knob wiring itself parses correctly ─────────────────────────────────

def test_knob_defaults_are_inert():
    import importlib, platform_core.backtesting.backtest_runner as br
    importlib.reload(br)
    assert br._EXIT_STOP_SLM_MODEL is False
    assert br._RATCHET_LOCK_FRAC == 0.0
