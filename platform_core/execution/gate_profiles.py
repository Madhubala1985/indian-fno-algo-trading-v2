"""
platform_core.execution.gate_profiles
═══════════════════════════════════════════════════════════════════════
Named paper gate-threshold profiles and execution-mode compatibility.

This module is import-safe (no side effects) so tests and tooling can
validate profile behavior without importing the live pipeline.

Profiles only control SOFT gates (score_margin / oi_support thresholds).
They can NEVER disable hard safety gates:
  - context contradiction rejection
  - warm-start lockout
  - closing / pre-open / session filters
  - risk budget / portfolio heat / position limits
  - stop cooldowns
  - daily halt / soft halt
  - raw index rejection / wrong exchange rejection
  - OI support below 0.10 (all profiles keep oi_support_min >= 0.10
    unless an explicit per-underlying env var overrides it)

paper_trade_unlock:
  Paper / real_dry_run ONLY.  Exists to break trade starvation and prove
  the dry-run order lifecycle.  NOT approved for real funds.  It is
  rejected (fail-closed) when EXECUTION_MODE=real_armed.
"""
from __future__ import annotations

PROFILE_GATE_TABLES: dict[str, dict[str, dict[str, float]]] = {
    "default": {
        "BANKNIFTY": {"score_margin": 0.05, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
        "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},
    },
    "banknifty_experiment": {
        "BANKNIFTY": {"score_margin": 0.03, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
        "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},  # strict — unchanged
    },
    "small_capital_experiment": {
        "BANKNIFTY": {"score_margin": 0.03, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.03, "oi_support": 0.08},
        "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},  # strict — unchanged
    },
    # Trade-unlock experiment: lowest allowed soft-gate floor.
    # oi_support stays at the 0.10 hard floor — never lowered by a profile.
    "paper_trade_unlock": {
        "BANKNIFTY": {"score_margin": 0.015, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.015, "oi_support": 0.10},
        "SENSEX":    {"score_margin": 0.015, "oi_support": 0.10},
    },
}

# Profiles that may only run with paper money (paper / real_dry_run).
# All experiment profiles are paper-only — real_armed always requires the
# default profile.  This strengthens real-funds defaults; never weaken it.
PAPER_ONLY_PROFILES = frozenset({
    "paper_trade_unlock",
    "banknifty_experiment",
    "small_capital_experiment",
})

_ALLOWED_MODES_FOR_PAPER_ONLY = frozenset({"paper", "real_dry_run"})


def validate_profile_mode(profile: str, execution_mode: str) -> tuple[bool, str]:
    """
    Return (True, "") when the profile may run in the given execution mode.
    Return (False, reason) when it must be rejected (fail-closed).
    """
    p = (profile or "default").lower().strip()
    m = (execution_mode or "paper").lower().strip()
    if p in PAPER_ONLY_PROFILES and m not in _ALLOWED_MODES_FOR_PAPER_ONLY:
        return False, (
            f"PAPER_GATE_PROFILE={p} is paper-only "
            f"(allowed: paper, real_dry_run) and cannot run with "
            f"EXECUTION_MODE={m} — refusing to start (fail-closed)."
        )
    return True, ""


def get_profile_table(profile: str) -> dict[str, dict[str, float]]:
    """Return the threshold table for a profile; unknown names → default."""
    return PROFILE_GATE_TABLES.get(
        (profile or "default").lower().strip(), PROFILE_GATE_TABLES["default"]
    )


__all__ = [
    "PROFILE_GATE_TABLES",
    "PAPER_ONLY_PROFILES",
    "validate_profile_mode",
    "get_profile_table",
]
