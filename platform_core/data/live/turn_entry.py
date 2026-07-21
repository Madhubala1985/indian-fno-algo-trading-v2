"""
platform_core.data.live.turn_entry
═══════════════════════════════════════════════════════════════════════
Early flow-TURN entry seed for the OPTIONS book.

This module is import-safe (no side effects, no market connections) so the
decision can be unit-tested without importing the live pipeline (which opens a
Kite WebSocket at import) — same contract as options/strike_tiebreak.py and
execution/gate_profiles.py.

WHY THIS EXISTS
The options book enters on the STRUCTURE bias (market_ctx.bias). Measured over
16 sessions (scripts/study_confirmation_latency.py): when that bias is not
already aligned, it confirms an intraday turn a MEDIAN 194 minutes late, or
never (5 of 9 turns). On 2026-07-21 this cost a live example — the flow-TURN
detector shorted futures at 09:46 IST near the day high (+₹6.8k) while the
options book's own bias did not confirm SHORT until 10:49, entering its puts
one minute from the exact spot low = the premium peak.

The crossasset flow-TURN detector (crossasset_shadow.LAST_TURN) fires that
early signal, and the futures sleeve already trades it. This lets the OPTIONS
book seed its entry bias from the SAME fresh TURN when structure is still
NEUTRAL — so the direction flows through the ENTIRE unchanged gate stack
(HTF-opposition, strike-quality, per-trade max-loss cap, halts, position
limits). Nothing is bypassed; an earlier direction is supplied.

DELIBERATELY CONSERVATIVE
  - Only seeds when structure bias is NEUTRAL. It never overrides an active
    directional bias, and never re-activates a bias the invalidation engine
    just neutralised in the SAME direction (see same_dir_block).
  - Freshness (max_age_sec), per-day cap, and cooldown mirror the futures
    sleeve so the two books stay comparable.
  - The seeded confidence is a fixed floor, not the (inverted, saturated)
    confidence engine — so the max-loss cap, not a bogus conviction, sizes it.

Default OFF everywhere; enabling is an explicit operator/launcher decision.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnEntryConfig:
    """Tunables, all overridable by env in the live pipeline."""
    enabled: bool = False
    conf_seed: float = 0.60        # clears Tier2 (0.40); max-loss cap still binds size
    max_age_sec: float = 180.0     # a TURN older than this is stale — do not chase
    cooldown_sec: float = 1800.0   # min gap between TURN-seeded entries per underlying
    max_per_day: int = 3           # per underlying
    override_opposed: bool = False
    # ↑ default OFF. When OFF the seed fires ONLY on a NEUTRAL structure bias — the
    # safe case (nothing to override). When ON, a fresh turn may ALSO override a
    # structure bias pointing the OPPOSITE way (the 2026-07-21 BANKNIFTY case:
    # bias LONG while the turn — correctly — said SHORT, and both books missed the
    # move). This is materially more aggressive: it overrules the model's own
    # directional view with a flow signal whose detector has a documented lag-FAIL
    # history, so it stays OFF until an A/B proves it (net AND ex-best-day). It
    # never overrides a bias that AGREES with the turn (no need).


def turn_entry_direction(
    *,
    structure_bias: str,
    turn_event: dict | None,
    now_epoch: float,
    last_consumed_epoch: float,
    count_today: int,
    last_entry_epoch: float,
    just_invalidated_dir: str | None,
    cfg: TurnEntryConfig,
) -> tuple[str | None, str]:
    """
    Decide whether a fresh flow-TURN should seed a directional options entry.

    Pure function. Returns (direction, reason): direction is "LONG"/"SHORT"
    when a seed should fire, else None with a reason string for the log/audit.
    The caller is responsible for recording consumption (epoch, count, entry
    time) when it acts on a non-None result.

    Args:
        structure_bias:      market_ctx.bias BEFORE any seed ("LONG"/"SHORT"/"NEUTRAL").
        turn_event:          crossasset_shadow.LAST_TURN[underlying] or None —
                             {"epoch": float, "to": "LONG"/"SHORT", ...}.
        now_epoch:           current wall-clock epoch (time.time()).
        last_consumed_epoch: epoch of the last TURN this book already acted on.
        count_today:         TURN-seeded entries already made for this underlying today.
        last_entry_epoch:    epoch of this book's last TURN-seeded entry.
        just_invalidated_dir: a direction the invalidation engine neutralised on
                             THIS bar, if any — we refuse to re-seed the SAME
                             direction it just killed (a fresh OPPOSITE turn is fine).
        cfg:                 TurnEntryConfig.
    """
    if not cfg.enabled:
        return None, "disabled"
    if not turn_event:
        return None, "no_turn"
    to = turn_event.get("to")
    if to not in ("LONG", "SHORT"):
        return None, "bad_dir"
    if structure_bias != "NEUTRAL":
        # An active directional bias. Only proceed if override is enabled AND the
        # bias points the OPPOSITE way to the turn (the wrong-bias case worth
        # rescuing); if it already agrees, the normal path handles it.
        if not cfg.override_opposed:
            return None, "bias_not_neutral"      # safe default: never override
        if structure_bias == to:
            return None, "bias_already_agrees"
    epoch = float(turn_event.get("epoch", 0.0) or 0.0)
    if epoch <= last_consumed_epoch:
        return None, "already_consumed"
    if now_epoch - epoch > cfg.max_age_sec:
        return None, "stale"
    if count_today >= cfg.max_per_day:
        return None, "max_per_day"
    if now_epoch - last_entry_epoch < cfg.cooldown_sec:
        return None, "cooldown"
    if just_invalidated_dir is not None and to == just_invalidated_dir:
        return None, "same_dir_just_invalidated"
    return to, "ok"
