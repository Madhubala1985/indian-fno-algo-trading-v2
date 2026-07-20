"""
platform_core.options.strike_tiebreak
═══════════════════════════════════════════════════════════════════════
Score-margin tie-break between the top-2 strike candidates.

This module is import-safe (no side effects, no market connections) so tests
and tooling can validate the decision without importing the live pipeline —
same contract as platform_core.execution.gate_profiles.

Mechanism. A near-tie between the top-2 strikes is a coin the scorer cannot
call: the dominant OI wall pumps the OTM candidate's oi_support to 1.00,
exactly offsetting its worse delta/theta, and the score-margin gate reads the
resulting tie as "no conviction" and blocks. On 2026-07-17 that cost the book
the whole BANKNIFTY trend day to 115 consecutive margin blocks. The tie-break
enters the higher-|delta| of the two instead — the better *vehicle* for a
directional trade — and waives the margin leg of the gate for that bar.

Evidence (night 2026-07-19, H1 "delta05"; 30 (underlying,day) replay pairs at
per-symbol live floors, costs included): the delta05+gexspot+wide04 composite
nets +41,928 / ex-best-day +23,454 against base +9,455 / +737, with the gain
spread over 9 days (7 positive) and robust to dropping the best changed day.

The floor guard is load-bearing, not defensive. The same tie-break measured a
LOSS at SENSEX's loose 0.015 floor, where it unlocks junk near-ties; the PASS
above exists only because delta is restricted to the 0.05-floor symbols. Ship
it with min_floor=0.05 or do not ship it.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence


class _HasDelta(Protocol):
    delta: float


# Underlyings the delta tie-break was actually MEASURED on (night 2026-07-19 H1:
# NIFTY 13 + BANKNIFTY 2 of the 30 replay pairs; SENSEX ran the non-delta arm).
# The floor test below is only a PROXY for symbol identity — it was the only
# handle surgical_replay had, because MIN_SCORE_MARGIN is per-run there. Live
# knows the symbol directly, so we assert on the symbol itself.
#
# Why this matters (found by the 07-20 night agent): the SENSEX floor is 0.015
# only because the launcher forces PAPER_SENSEX_SCORE_MARGIN_MIN=0.015. Invoke
# the pipeline WITHOUT the launcher and SENSEX falls back to the profile default
# 0.050, which passes `sym_floor >= min_floor` — silently enabling delta on the
# one symbol where it measured a LOSS. The floor proxy cannot catch that; the
# symbol check can.
VALIDATED_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})


def delta_tiebreak_pick(
    recs: Sequence[Any],
    sym_floor: float,
    mode: str,
    min_floor: float,
    margin: float | None,
    underlying: str | None = None,
) -> tuple[Any | None, bool]:
    """
    Decide whether a near-tie is resolved by |delta|, and to which candidate.

    Args:
        recs:      ranked strike recommendations (rank-1 first); needs >= 2.
        sym_floor: this underlying's score-margin gate floor.
        mode:      STRIKE_TIEBREAK value; only the literal "delta" acts.
        min_floor: STRIKE_TIEBREAK_MIN_FLOOR — the SENSEX guard. With 0.05 this
                   admits NIFTY/BANKNIFTY (0.05) and excludes SENSEX (0.015).
        margin:    rank1 - rank2 score margin, or None when < 2 candidates.
        underlying: the symbol. When supplied it must be in VALIDATED_UNDERLYINGS
                   or the tie-break refuses, INDEPENDENTLY of the floor — this is
                   the check that holds when the floor env is absent. Production
                   callers must always pass it; omitting it is test-only and
                   falls back to the floor proxy alone.

    Returns:
        (pick, fired). `fired` True means the near-tie was resolved and the
        score-margin leg of the strike-quality gate is waived for this bar —
        every other leg (iv_attractiveness, oi_support, and all hard gates)
        still binds. (None, False) means the tie-break does not apply and the
        caller must keep its existing pick and its existing gate behaviour.
    """
    if mode != "delta" or len(recs) < 2:
        return None, False
    if underlying is not None and underlying.strip().upper() not in VALIDATED_UNDERLYINGS:
        return None, False              # symbol outside the measured set (e.g. SENSEX)
    if sym_floor < min_floor:
        return None, False              # guarded out (SENSEX at its loose floor)
    if margin is None or margin >= sym_floor:
        return None, False              # not a near-tie; the gate passes anyway
    r0, r1 = recs[0], recs[1]
    return (r0 if abs(r0.delta) >= abs(r1.delta) else r1), True
