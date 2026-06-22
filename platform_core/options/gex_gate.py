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
    ``gex`` exposes ``call_wall_strike`` / ``put_wall_strike`` / ``net_gamma_regime``.
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
        cw = getattr(gex, "call_wall_strike", None)
        if cw and cw >= spot and (cw - spot) / spot <= proximity_pct:
            return (f"GEX_CALL_WALL_AHEAD spot={spot:.0f} call_wall={cw:.0f} "
                    f"dist={(cw - spot) / spot * 100:.2f}% regime={regime}")
    elif b == "SHORT":
        pw = getattr(gex, "put_wall_strike", None)
        if pw and pw <= spot and (spot - pw) / spot <= proximity_pct:
            return (f"GEX_PUT_WALL_AHEAD spot={spot:.0f} put_wall={pw:.0f} "
                    f"dist={(spot - pw) / spot * 100:.2f}% regime={regime}")
    return None


__all__ = ["gex_wall_block"]
