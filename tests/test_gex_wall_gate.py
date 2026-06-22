"""Unit tests for platform_core.options.gex_gate.gex_wall_block.

Veto a directional entry chasing INTO a dealer-gamma wall (LONG below a call
wall / SHORT above a put wall); fail-open on missing data.
"""
from platform_core.options.gex_gate import gex_wall_block


class FakeGex:
    def __init__(self, call_wall=None, put_wall=None, regime="LONG_GAMMA"):
        self.call_wall_strike = call_wall
        self.put_wall_strike = put_wall
        self.net_gamma_regime = regime


PCT = 0.0025  # 0.25%


def test_long_into_nearby_call_wall_blocks():
    # spot 77000, call wall 77100 → 0.13% above → within 0.25% → BLOCK
    g = FakeGex(call_wall=77100.0)
    r = gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT)
    assert r is not None and "GEX_CALL_WALL_AHEAD" in r


def test_long_far_from_call_wall_ok():
    # call wall 77600 → 0.78% away → beyond 0.25% → no block
    g = FakeGex(call_wall=77600.0)
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_long_already_above_call_wall_ok():
    # wall below spot (already broken through) → not "ahead" → no block
    g = FakeGex(call_wall=76900.0)
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_short_into_nearby_put_wall_blocks():
    # spot 77000, put wall 76900 → 0.13% below → within range → BLOCK
    g = FakeGex(put_wall=76900.0)
    r = gex_wall_block(bias="SHORT", spot=77000.0, gex=g, proximity_pct=PCT)
    assert r is not None and "GEX_PUT_WALL_AHEAD" in r


def test_short_far_from_put_wall_ok():
    g = FakeGex(put_wall=76400.0)
    assert gex_wall_block(bias="SHORT", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_missing_gex_is_failopen():
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=None, proximity_pct=PCT) is None


def test_no_wall_data_is_failopen():
    g = FakeGex(call_wall=None, put_wall=None)
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_neutral_bias_never_blocks():
    g = FakeGex(call_wall=77050.0, put_wall=76950.0)
    assert gex_wall_block(bias="NEUTRAL", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_bad_spot_failopen():
    g = FakeGex(call_wall=77050.0)
    assert gex_wall_block(bias="LONG", spot=0.0, gex=g, proximity_pct=PCT) is None


# ── LONG_GAMMA regime veto (block_long_gamma=True) ──────────────────────────

def test_long_gamma_regime_vetoes_long_far_from_wall():
    # No wall nearby, but LONG_GAMMA regime + veto on → block the momentum chase.
    g = FakeGex(call_wall=77600.0, regime="LONG_GAMMA")
    r = gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT,
                       block_long_gamma=True)
    assert r is not None and "GEX_LONG_GAMMA_REGIME" in r


def test_long_gamma_regime_vetoes_short_too():
    g = FakeGex(put_wall=76400.0, regime="LONG_GAMMA")
    r = gex_wall_block(bias="SHORT", spot=77000.0, gex=g, proximity_pct=PCT,
                       block_long_gamma=True)
    assert r is not None and "GEX_LONG_GAMMA_REGIME" in r


def test_short_gamma_regime_not_vetoed():
    # SHORT_GAMMA regime + far from wall → no block even with veto on.
    g = FakeGex(call_wall=77600.0, regime="SHORT_GAMMA")
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT,
                          block_long_gamma=True) is None


def test_regime_veto_off_by_default():
    # Same LONG_GAMMA setup but veto flag default (False) → no regime block.
    g = FakeGex(call_wall=77600.0, regime="LONG_GAMMA")
    assert gex_wall_block(bias="LONG", spot=77000.0, gex=g, proximity_pct=PCT) is None


def test_regime_veto_neutral_bias_failopen():
    # Veto on, but NEUTRAL bias is not a momentum entry → fail open.
    g = FakeGex(regime="LONG_GAMMA")
    assert gex_wall_block(bias="NEUTRAL", spot=77000.0, gex=g, proximity_pct=PCT,
                          block_long_gamma=True) is None


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
