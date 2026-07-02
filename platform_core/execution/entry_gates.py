# FILE: platform_core/execution/entry_gates.py
"""
entry_gates.py — import-safe, shared ENTRY-gate primitives.

Both the live pipeline (live_structure_pipeline.py) and the surgical replay
(surgical_replay.py) import these, so the gate SEMANTICS cannot drift between
the two — the replay is only a valid validation tool if it blocks exactly what
live blocks. Follows the pattern of execution/context_cooldown.py.

Gates here (2026-07-02, measured on real-premium replay + OOS live day):

1. Time-of-day entry cutoff — the afternoon-chop leak. Batch replay (SENSEX+
   NIFTY 06-24..07-01, real premiums) measured the edge as entirely morning:
   09-11 IST = +16,916 (38-56% WR) vs 12-14 IST = -15,694 (0-27% WR). The live
   2026-07-02 session then confirmed OUT-OF-SAMPLE: the two afternoon entries
   (12:42, 13:43) took -6,884 of the day's -9,754. Block NEW entries at/after
   the cutoff hour; open positions still manage/exit normally.

2. Same-contract session loss limit — the same-strike churn leak. 07-01:
   SENSEX churned 77000CE 3x (the 12-bar cooldown expired and the 3-consec
   chop brake was reset by an interspersed win). 07-02: all three losses were
   the SAME contract (77500CE). After N losing exits on one contract+direction
   the pair is blocked for the REST OF THE SESSION — a win never blocks, and
   other strikes/directions on the underlying stay tradeable, so trend
   re-entries are unaffected. This was the handoff's own fix candidate
   ("block re-entering the SAME strike for the session").
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))

# 0 disables (the DEFAULT). The blunt noon cutoff was replaced 2026-07-02 by the
# opening_range.py extension-chase gate (OR_MAX_EXTENSION): per-trade replay data
# showed the afternoon losses were EXTENSION CHASES (entering >=1.6 OR-widths into
# an already-extended move), not a time-of-day effect — a fresh afternoon breakout
# is tradeable and profitable (06-24 13:14 +1,465 at ext 1.59). Env re-enables a
# hard cutoff for A/B without code changes.
_DEFAULT_ENTRY_CUTOFF_IST = 0
# Losing exits on one contract+direction before it is blocked for the session.
_DEFAULT_MAX_CONTRACT_LOSSES = 2


def entry_cutoff_hour(env_var: str = "ENTRY_CUTOFF_IST") -> int:
    try:
        return int(os.environ.get(env_var, str(_DEFAULT_ENTRY_CUTOFF_IST))
                   or _DEFAULT_ENTRY_CUTOFF_IST)
    except Exception:
        return _DEFAULT_ENTRY_CUTOFF_IST


def time_cutoff_blocked(timestamp, cutoff_hour_ist: int) -> bool:
    """True when NEW entries are blocked (bar at/after the IST cutoff hour)."""
    if cutoff_hour_ist <= 0:
        return False
    try:
        return timestamp.astimezone(_IST).hour >= cutoff_hour_ist
    except Exception:
        return False  # fail-open: a clock fault must not silently halt entries


class SameContractLossTracker:
    """Per-session count of LOSING exits per (contract, direction).

    record_exit() on every closed trade; blocked() before every entry.
    Session-scoped — construct fresh each session/day.
    """

    def __init__(self, max_losses: int | None = None) -> None:
        if max_losses is None:
            try:
                max_losses = int(
                    os.environ.get("SAME_CONTRACT_SESSION_MAX_LOSSES",
                                   str(_DEFAULT_MAX_CONTRACT_LOSSES))
                    or _DEFAULT_MAX_CONTRACT_LOSSES)
            except Exception:
                max_losses = _DEFAULT_MAX_CONTRACT_LOSSES
        self.max_losses = max_losses
        self._losses: dict[str, int] = {}

    @staticmethod
    def _key(contract: str, direction: str) -> str:
        return f"{contract}:{direction}"

    def record_exit(self, *, contract: str, direction: str, pnl: float) -> None:
        if pnl < 0:
            k = self._key(contract, direction)
            self._losses[k] = self._losses.get(k, 0) + 1

    def losses(self, *, contract: str, direction: str) -> int:
        return self._losses.get(self._key(contract, direction), 0)

    def blocked(self, *, contract: str, direction: str) -> bool:
        if self.max_losses <= 0:
            return False
        return self.losses(contract=contract, direction=direction) >= self.max_losses

    # Persistence across a mid-session restart (supervisor crash-restart):
    def to_dict(self) -> dict[str, int]:
        return dict(self._losses)

    def restore(self, data: dict | None) -> None:
        if isinstance(data, dict):
            self._losses = {str(k): int(v) for k, v in data.items()}


class DirectionEntryCounter:
    """Per-session count of ENTRIES per direction on one underlying.

    Measured (real-premium replay 06-24..07-01): every 4th+ same-direction
    entry in a session LOST (07-01 SENSEX won #1-#3 then #4/#5 lost; NIFTY won
    #1-#3 then #4 lost) — once the day's initiative move has been harvested,
    later same-direction re-entries chase the tail of the move. Cap = 3 mirrors
    the existing 3-based risk discipline (3-consec-loss halt, 3-loss chop
    brake); a REVERSAL (opposite direction) is a fresh thesis and stays free.
    Session-scoped — construct fresh each session/day.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        if max_entries is None:
            try:
                max_entries = int(
                    os.environ.get("SAME_DIRECTION_SESSION_MAX_ENTRIES", "3") or 3)
            except Exception:
                max_entries = 3
        self.max_entries = max_entries
        self._entries: dict[str, int] = {}

    def record_entry(self, *, direction: str) -> None:
        self._entries[direction] = self._entries.get(direction, 0) + 1

    def entries(self, *, direction: str) -> int:
        return self._entries.get(direction, 0)

    def blocked(self, *, direction: str) -> bool:
        if self.max_entries <= 0:
            return False
        return self.entries(direction=direction) >= self.max_entries

    # Persistence across a mid-session restart (same pattern as the tracker):
    def to_dict(self) -> dict[str, int]:
        return dict(self._entries)

    def restore(self, data: dict | None) -> None:
        if isinstance(data, dict):
            self._entries = {str(k): int(v) for k, v in data.items()}


__all__ = [
    "entry_cutoff_hour", "time_cutoff_blocked",
    "SameContractLossTracker", "DirectionEntryCounter",
]
