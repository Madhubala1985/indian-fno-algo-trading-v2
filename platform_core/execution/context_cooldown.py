"""
platform_core.execution.context_cooldown
═══════════════════════════════════════════════════════════════════════
Context-contradiction cooldown — a profile-independent HARD safety gate.

When a CONTEXT_CONTRADICTION_REJECTED fires for an underlying (HTF
structure actively opposes MCB bias), new entries on that SAME underlying
are blocked for the next N bars.  Proven defect (2026-06-10): NIFTY
re-entered LONG 1 and 5 bars after bearish-BOS contradictions; the
session ended at 3 consecutive losses and a capital halt.

Rules:
  - Default cooldown: 5 bars (CONTEXT_CONTRADICTION_COOLDOWN_BARS env var).
  - 0 disables the cooldown — tests/debug only, never recommended.
  - Applies to ALL gate profiles (default, banknifty_experiment,
    small_capital_experiment, paper_trade_unlock) — no profile may relax
    or bypass it.
  - Applies in every execution mode: paper, real_dry_run, real_armed.
  - Evaluated BEFORE sizing and BEFORE any order-intent creation.
  - Per-underlying: a NIFTY contradiction never blocks BANKNIFTY.

This module is import-safe (no side effects) so tests and audit tooling
share the exact gate semantics with the live pipeline.
"""
from __future__ import annotations

import os

DEFAULT_COOLDOWN_BARS = 5


def cooldown_bars_from_env() -> int:
    """CONTEXT_CONTRADICTION_COOLDOWN_BARS env var (default 5, floor 0)."""
    raw = os.environ.get("CONTEXT_CONTRADICTION_COOLDOWN_BARS", "")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_BARS


def contradiction_cooldown_active(
    current_bar: int,
    last_contradiction_bar: int | None,
    cooldown_bars: int = DEFAULT_COOLDOWN_BARS,
) -> tuple[bool, int | None]:
    """
    Return (blocked, bars_since) for one underlying.

    blocked is True when a contradiction was recorded and the current bar
    is within `cooldown_bars` bars of it (bars_since <= cooldown_bars).
    bars_since 0 is the contradiction bar itself; 1..cooldown_bars are
    blocked; cooldown_bars+1 onward are allowed.

    A negative bars_since (clock/bar-counter anomaly) fails closed.
    cooldown_bars <= 0 disables the gate (tests/debug only).
    """
    if last_contradiction_bar is None:
        return False, None
    bars_since = current_bar - last_contradiction_bar
    if cooldown_bars <= 0:
        return False, bars_since
    if bars_since < 0:
        return True, bars_since      # fail-closed on anomalies
    return bars_since <= cooldown_bars, bars_since


__all__ = [
    "DEFAULT_COOLDOWN_BARS",
    "cooldown_bars_from_env",
    "contradiction_cooldown_active",
]
