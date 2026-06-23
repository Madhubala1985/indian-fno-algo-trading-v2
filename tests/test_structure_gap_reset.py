"""Tests for the feed-gap structure continuity reset.

When the 1-minute bar stream has a discontinuity (host suspend, feed stall,
reconnect hole), the live pipeline resets the per-symbol structure + swing state
before analysing the post-gap bar, so no swing / BOS / CHOCH / FVG signal is
computed across the hole.

The pipeline module runs a live loop at import and cannot be imported here, so we
test (a) the reset primitives the fix relies on — that reset() truly clears
structure + swing state — and (b) the gap-decision arithmetic the guard uses.
"""
from datetime import datetime, timedelta, timezone

from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.swing_detector import (
    SwingDetector, SwingDetectorConfig,
)
from platform_core.analytics.structure.structure_engine import (
    StructureEngine, StructureEngineConfig,
)
from platform_core.analytics.structure.liquidity_sweep_detector import (
    LiquiditySweepDetector, LiquiditySweepDetectorConfig,
)
from platform_core.analytics.structure.bos_detector import BOSDetector, BOSDetectorConfig
from platform_core.analytics.structure.choch_detector import CHOCHDetector, CHOCHDetectorConfig
from platform_core.analytics.structure.fvg_engine import FVGEngine, FVGEngineConfig
from platform_core.analytics.structure.support_resistance_engine import (
    SupportResistanceEngine, SupportResistanceEngineConfig,
)

SYM = "SENSEX"
TF = "MIN_1"
T0 = datetime(2026, 6, 23, 4, 0, tzinfo=timezone.utc)


def _bar(i: int, px: float) -> StructureBar:
    return StructureBar(
        instrument_id=SYM, timeframe=TF,
        bar_timestamp=T0 + timedelta(minutes=i),
        open_price=px, high_price=px + 5, low_price=px - 5,
        close_price=px, volume=1000.0,
    )


def _build_engine() -> StructureEngine:
    return StructureEngine(
        config=StructureEngineConfig(instrument_id=SYM, timeframe=TF),
        liquidity_sweep_detector=LiquiditySweepDetector(
            config=LiquiditySweepDetectorConfig(instrument_id=SYM, timeframe=TF)),
        bos_detector=BOSDetector(config=BOSDetectorConfig(instrument_id=SYM, timeframe=TF)),
        choch_detector=CHOCHDetector(config=CHOCHDetectorConfig(instrument_id=SYM, timeframe=TF)),
        fvg_engine=FVGEngine(config=FVGEngineConfig(instrument_id=SYM, timeframe=TF)),
        support_resistance_engine=SupportResistanceEngine(
            config=SupportResistanceEngineConfig(instrument_id=SYM, timeframe=TF)),
    )


def _swing() -> SwingDetector:
    return SwingDetector(config=SwingDetectorConfig(instrument_id=SYM, timeframe=TF))


# ── reset primitives clear state (the fix depends on these) ──────────────────

def test_swing_detector_reset_clears_state():
    sd = _swing()
    for i in range(8):
        sd.process_bar(_bar(i, 77000 + (i % 3) * 20))
    assert sd.state.processed_bar_count == 8
    sd.reset()
    assert sd.state.processed_bar_count == 0
    # After reset the next bar is treated as the first of a fresh context.
    sd.process_bar(_bar(50, 77100))
    assert sd.state.processed_bar_count == 1


def test_structure_engine_reset_clears_state():
    eng = _build_engine()
    sd = _swing()
    for i in range(8):
        b = _bar(i, 77000 + (i % 3) * 20)
        res = sd.process_bar(b)
        eng.process(payload=_payload(b, res.confirmed_points))
    assert eng.state.processed_bar_count == 8
    eng.reset()
    assert eng.state.processed_bar_count == 0
    assert eng.state.last_bar_timestamp is None


def _payload(bar, points):
    from platform_core.analytics.structure.structure_engine import StructureEngineInput
    return StructureEngineInput(bar=bar, swing_points=points)


# ── gap-decision arithmetic (mirrors the live guard) ─────────────────────────

def _is_gap(prev_ts, cur_ts, threshold_min):
    if prev_ts is None:
        return False
    return (cur_ts - prev_ts).total_seconds() / 60.0 > threshold_min


def test_first_bar_never_resets():
    assert _is_gap(None, T0, 10) is False


def test_normal_one_minute_cadence_no_reset():
    assert _is_gap(T0, T0 + timedelta(minutes=1), 10) is False


def test_minor_few_minute_gap_no_reset():
    assert _is_gap(T0, T0 + timedelta(minutes=4), 10) is False


def test_large_gap_triggers_reset():
    # ~2.8h host-suspend hole, as seen 2026-06-23
    assert _is_gap(T0, T0 + timedelta(minutes=168), 10) is True


def test_gap_just_over_threshold_resets():
    assert _is_gap(T0, T0 + timedelta(minutes=11), 10) is True


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
