"""
Early flow-TURN entry seed for the options book (2026-07-21).

Guards the decision helper that lets the options book act on a fresh crossasset
flow-TURN when its own structure bias is still NEUTRAL. The risks worth pinning:
  - it must NEVER fire on a non-NEUTRAL bias (never override an active view);
  - it must NOT re-seed a direction the invalidation engine just neutralised;
  - freshness / cooldown / per-day caps must all bind;
  - it must be inert when disabled.
"""
from __future__ import annotations

import os
os.environ.setdefault("PAPER_EXECUTION_MODE", "paper")

import pytest

from platform_core.data.live.turn_entry import (
    TurnEntryConfig, turn_entry_direction,
)

ON = TurnEntryConfig(enabled=True)          # defaults: age180 cd1800 max3
NOW = 1_000_000.0


def _turn(to, age=10.0):
    return {"epoch": NOW - age, "to": to, "rolling": 0.2}


def _call(**kw):
    base = dict(
        structure_bias="NEUTRAL", turn_event=_turn("LONG"), now_epoch=NOW,
        last_consumed_epoch=0.0, count_today=0, last_entry_epoch=0.0,
        just_invalidated_dir=None, cfg=ON,
    )
    base.update(kw)
    return turn_entry_direction(**base)


# ── fires on the happy path ─────────────────────────────────────────────────

def test_fires_on_fresh_turn_when_neutral():
    d, r = _call()
    assert (d, r) == ("LONG", "ok")


def test_fires_short():
    d, r = _call(turn_event=_turn("SHORT"))
    assert d == "SHORT"


# ── never overrides an active bias ──────────────────────────────────────────

@pytest.mark.parametrize("bias", ["LONG", "SHORT"])
def test_never_overrides_active_bias(bias):
    d, r = _call(structure_bias=bias)
    assert (d, r) == (None, "bias_not_neutral")


# ── never re-seeds a just-neutralised direction ─────────────────────────────

def test_refuses_same_dir_the_invalidation_engine_just_killed():
    d, r = _call(turn_event=_turn("LONG"), just_invalidated_dir="LONG")
    assert (d, r) == (None, "same_dir_just_invalidated")


def test_allows_opposite_dir_after_invalidation():
    # invalidation killed LONG; a fresh SHORT turn is legitimately new info
    d, r = _call(turn_event=_turn("SHORT"), just_invalidated_dir="LONG")
    assert d == "SHORT"


# ── freshness / caps / cooldown all bind ────────────────────────────────────

def test_stale_turn_is_rejected():
    d, r = _call(turn_event=_turn("LONG", age=200.0))   # > 180s
    assert (d, r) == (None, "stale")


def test_already_consumed_epoch_is_rejected():
    ev = _turn("LONG", age=10.0)
    d, r = _call(turn_event=ev, last_consumed_epoch=ev["epoch"])
    assert (d, r) == (None, "already_consumed")


def test_per_day_cap_binds():
    d, r = _call(count_today=3)
    assert (d, r) == (None, "max_per_day")


def test_cooldown_binds():
    d, r = _call(last_entry_epoch=NOW - 100.0)   # < 1800s ago
    assert (d, r) == (None, "cooldown")


def test_cooldown_clears_after_window():
    d, r = _call(last_entry_epoch=NOW - 2000.0)  # > 1800s ago
    assert d == "LONG"


# ── bad / missing inputs ────────────────────────────────────────────────────

def test_no_turn_event():
    d, r = _call(turn_event=None)
    assert (d, r) == (None, "no_turn")


def test_bad_direction():
    d, r = _call(turn_event={"epoch": NOW - 5, "to": "NEUTRAL"})
    assert (d, r) == (None, "bad_dir")


# ── inert when disabled ─────────────────────────────────────────────────────

def test_disabled_never_fires():
    d, r = turn_entry_direction(
        structure_bias="NEUTRAL", turn_event=_turn("LONG"), now_epoch=NOW,
        last_consumed_epoch=0.0, count_today=0, last_entry_epoch=0.0,
        just_invalidated_dir=None, cfg=TurnEntryConfig(enabled=False))
    assert (d, r) == (None, "disabled")
