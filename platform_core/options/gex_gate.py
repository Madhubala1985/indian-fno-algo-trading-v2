"""
platform_core.options.gex_gate
═══════════════════════════════════════════════════════════════════════
Dealer-gamma (GEX) entry gate: veto a directional entry that is chasing INTO a
gamma wall.  Gamma walls act as intraday resistance (call wall) / support (put
wall) — price tends to stall or reverse there, so a momentum entry into a wall
is a high-reversal-risk "chase".

Pure, side-effect-free, broker-agnostic so it is unit-testable in isolation.
Used by the live pipeline's [A7] entry gate (prototype, opt-in via GEX_REGIME_GATE).
"""
from __future__ import annotations

from typing import Any

_MISSING = object()


def _wall(gex: Any, spot_side_attr: str, legacy_attr: str) -> float | None:
    """Spot-side-constrained wall when the snapshot provides the field (even if
    None — None means "no strike on that side", which must NOT fall back to the
    unconstrained wall or the wrong-side bug returns); legacy wall otherwise."""
    value = getattr(gex, spot_side_attr, _MISSING)
    if value is _MISSING:
        return getattr(gex, legacy_attr, None)
    return value


def gex_wall_block(
    *,
    bias: Any,
    spot: float,
    gex: Any,
    proximity_pct: float,
    block_long_gamma: bool = False,
) -> str | None:
    """Return a block-reason string if the entry should be vetoed on GEX, else None.

    Two complementary vetoes (both target chasing a momentum entry into a reversal):
    - REGIME (when ``block_long_gamma``): in a LONG_GAMMA regime dealers are long
      gamma → they sell rallies / buy dips → the tape mean-reverts / pins, so a
      directional momentum chase is low-edge.  Veto any LONG/SHORT entry.
    - WALL proximity: LONG within ``proximity_pct`` *below* a call wall
      (resistance) → block; SHORT within ``proximity_pct`` *above* a put wall
      (support) → block.

    Fail-open: missing ``gex``, non-positive ``spot``, or a non-LONG/SHORT bias
    returns None (never blocks) — this is an edge filter, not a safety control.

    Wall source (night 2026-07-18, consumer switch for the 07-17 §5 degeneracy
    fix): prefer the spot-side-constrained ``call_wall_above_strike`` /
    ``put_wall_below_strike`` when the snapshot carries them — the legacy
    unconstrained walls rank one dominant strike on BOTH sides in ~40% of
    recorded snapshots, leaving this gate silently fail-open on the wrong-side
    one. Snapshots without the new fields (older recordings) keep the legacy
    ``call_wall_strike`` / ``put_wall_strike`` behaviour unchanged.
    """
    if gex is None or spot <= 0 or proximity_pct < 0:
        return None
    b = str(bias).upper()
    if b not in ("LONG", "SHORT"):
        return None
    regime = getattr(gex, "net_gamma_regime", "?")
    if block_long_gamma and str(regime).upper() == "LONG_GAMMA":
        return f"GEX_LONG_GAMMA_REGIME (mean-reverting / pinning — no momentum chase) regime={regime}"
    if b == "LONG":
        cw = _wall(gex, "call_wall_above_strike", "call_wall_strike")
        if cw and cw >= spot and (cw - spot) / spot <= proximity_pct:
            return (f"GEX_CALL_WALL_AHEAD spot={spot:.0f} call_wall={cw:.0f} "
                    f"dist={(cw - spot) / spot * 100:.2f}% regime={regime}")
    elif b == "SHORT":
        pw = _wall(gex, "put_wall_below_strike", "put_wall_strike")
        if pw and pw <= spot and (spot - pw) / spot <= proximity_pct:
            return (f"GEX_PUT_WALL_AHEAD spot={spot:.0f} put_wall={pw:.0f} "
                    f"dist={(spot - pw) / spot * 100:.2f}% regime={regime}")
    return None


__all__ = ["gex_wall_block"]
