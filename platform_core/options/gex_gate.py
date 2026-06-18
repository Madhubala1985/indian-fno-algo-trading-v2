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


def gex_wall_block(*, bias: Any, spot: float, gex: Any, proximity_pct: float) -> str | None:
    """Return a block-reason string if the entry is chasing into a gamma wall, else None.

    - LONG within ``proximity_pct`` *below* a call wall (resistance) → block.
    - SHORT within ``proximity_pct`` *above* a put wall (support)   → block.

    Fail-open: missing ``gex``, non-positive ``spot``, or a non-LONG/SHORT bias
    returns None (never blocks) — this is an edge filter, not a safety control.
    ``gex`` is any object exposing ``call_wall_strike`` / ``put_wall_strike`` /
    ``net_gamma_regime`` (e.g. GammaExposureSnapshot).
    """
    if gex is None or spot <= 0 or proximity_pct < 0:
        return None
    b = str(bias).upper()
    regime = getattr(gex, "net_gamma_regime", "?")
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
