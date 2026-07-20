"""
Live-path adoption of the night-2026-07-19 H1 (delta05) + H6 (wide04) results.

Guards the two properties that decide whether this adoption makes or loses
money, both of which are silent failures if broken:

  1. the delta tie-break must NEVER reach SENSEX at its loose 0.015 floor
     (measured LOSS there; the whole PASS is conditional on the guard);
  2. a resolved tie-break waives ONLY the score-margin leg of the strike-quality
     gate — iv_attractiveness and oi_support must still bind.

Live floors this is written against (07-20 boot banner, real session):
    BANKNIFTY 0.050   NIFTY 0.050   SENSEX 0.015
"""
from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("PAPER_EXECUTION_MODE", "paper")

import pytest

from platform_core.options.strike_tiebreak import delta_tiebreak_pick as _delta_tiebreak_pick

ADOPTION_MIN_FLOOR = 0.05           # the proposed live config
FLOOR = {"BANKNIFTY": 0.05, "NIFTY": 0.05, "SENSEX": 0.015}


@dataclass
class _Rec:
    option_symbol: str
    delta: float


def _pair(d0: float, d1: float) -> list[_Rec]:
    return [_Rec("RANK1", d0), _Rec("RANK2", d1)]


# ── 1. the SENSEX guard — the property the PASS is conditional on ────────────

def test_sensex_is_excluded_at_its_loose_floor():
    """SENSEX floor 0.015 < 0.05 → tie-break must not fire, even on a near-tie."""
    pick, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["SENSEX"], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert (pick, fired) == (None, False)


@pytest.mark.parametrize("sym", ["NIFTY", "BANKNIFTY"])
def test_floor_005_symbols_are_admitted(sym):
    pick, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR[sym], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert fired is True
    assert pick.option_symbol == "RANK2"       # higher |delta| wins


def test_unguarded_config_would_reach_sensex():
    """Documents WHY the guard is mandatory: min_floor=0 lets delta hit SENSEX."""
    _, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["SENSEX"], "delta", 0.0, margin=0.001)
    assert fired is True


# ── 2. only near-ties, and only when the gate would have blocked ─────────────

def test_does_not_fire_when_margin_clears_the_floor():
    """Margin above floor → gate passes on its own; tie-break must stay out."""
    pick, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.20)
    assert (pick, fired) == (None, False)


def test_margin_exactly_at_floor_does_not_fire():
    pick, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.05)
    assert (pick, fired) == (None, False)


def test_none_margin_does_not_fire():
    """Fewer than 2 candidates leaves margin None — must not crash or fire."""
    pick, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=None)
    assert (pick, fired) == (None, False)


def test_single_candidate_does_not_fire():
    pick, fired = _delta_tiebreak_pick(
        [_Rec("ONLY", 0.5)], FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert (pick, fired) == (None, False)


# ── 3. default-OFF: the knob must be inert until explicitly set ─────────────

@pytest.mark.parametrize("mode", ["", "oicap", "both", "DELTA "])
def test_inert_unless_mode_is_exactly_delta(mode):
    """Only the literal "delta" mode is ported live; oicap was dropped from
    adoption (dominated by, and anti-interacting with, wide04)."""
    _, fired = _delta_tiebreak_pick(
        _pair(0.30, 0.55), FLOOR["NIFTY"], mode, ADOPTION_MIN_FLOOR, margin=0.001)
    assert fired is False


# ── 4. delta selection semantics ────────────────────────────────────────────

def test_picks_higher_absolute_delta_when_rank1_wins():
    pick, fired = _delta_tiebreak_pick(
        _pair(0.60, 0.35), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert fired is True and pick.option_symbol == "RANK1"


def test_uses_absolute_delta_for_puts():
    """PUT deltas are negative — magnitude, not sign, picks the vehicle."""
    pick, fired = _delta_tiebreak_pick(
        _pair(-0.30, -0.55), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert fired is True and pick.option_symbol == "RANK2"


def test_ties_on_delta_keep_rank1():
    pick, fired = _delta_tiebreak_pick(
        _pair(0.50, 0.50), FLOOR["NIFTY"], "delta", ADOPTION_MIN_FLOOR, margin=0.001)
    assert fired is True and pick.option_symbol == "RANK1"
