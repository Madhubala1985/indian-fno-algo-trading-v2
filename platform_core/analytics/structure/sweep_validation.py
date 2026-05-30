# FILE: platform_core/analytics/structure/sweep_validation.py
"""
LiquiditySweepValidationEngine — sequential ICT/SMC setup validator.

Validates that a complete institutional setup exists before entry by checking
five conditions in the BacktestRunner entry gate:

  1. Liquidity sweep: confirmed sweep against an SR zone, directionally aligned
  2. Structure break: BOS or CHOCH in the bias direction (within MAX_AGE_BARS)
  3. Displacement FVG: strong-body displacement bar created an FVG recently
  4. FVG formation: implicit — engine only runs when an FVG_CREATED event fired
  5. Bias alignment: enforced upstream by MarketContextBuilder (already gated)

Scoring:
  3/3 conditions → is_valid=True,  setup_score=1.00  (no penalty)
  2/3 conditions → is_valid=True,  setup_score=0.65  (confidence reduced)
  ≤1/3 conditions → is_valid=False, setup_score=0.00  (entry blocked)

Warm-up bypass: first WARMUP_BARS bars allow all entries (structure not yet built).

Integration:
  BacktestRunner calls update() after each bar's structure analysis,
  then validate() in step 8 (effective-confidence composition).
  Result is additive to session / PDL / OR / FVG-quality filter chain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from platform_core.analytics.structure.models import (
    StructureEvent, StructureEventType, StructureSide, SweepOutcome,
)

# ── Configuration ─────────────────────────────────────────────────────────────

WARMUP_BARS: int = 60            # bars before validation activates
MAX_AGE_BARS: int = 50           # conditions older than this are stale
MIN_DISP_BODY_RATIO: float = 0.40  # FVG middle-bar body/range threshold
SWEEP_MAX_AGE_MINUTES: float = 30.0  # sweep recency window

_BEARISH = "bearish"
_BULLISH = "bullish"


# ── Result model ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SetupValidation:
    is_valid: bool
    setup_score: float       # 1.0 = full, 0.65 = partial, 0.0 = blocked
    reason: str              # empty when fully valid
    sweep_ok: bool = False
    bos_ok: bool = False
    disp_ok: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "is_valid": self.is_valid,
            "setup_score": round(self.setup_score, 2),
            "reason": self.reason,
            "sweep_ok": self.sweep_ok,
            "bos_ok": self.bos_ok,
            "disp_ok": self.disp_ok,
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class LiquiditySweepValidationEngine:
    """
    Tracks sequential conditions and validates entry setups.

    Call update() after each bar's structure_result.
    Call validate() in the entry gate alongside session/PDL/OR/FVG filters.
    """

    def __init__(self) -> None:
        self._processed_bars: int = 0
        # Last bar index where a directional BOS or CHOCH fired
        self._last_bos_bar: dict[str, int] = {_BEARISH: -9999, _BULLISH: -9999}
        # Best FVG displacement ratio seen recently and the bar it occurred on
        self._last_disp_bar: int = -9999
        self._last_disp_ratio: float = 0.0

    def update(
        self,
        bar_index: int,
        structure_events: tuple[StructureEvent, ...],
        fvg_events: list[StructureEvent],
    ) -> None:
        """
        Feed one bar's structure events. Call every bar before validate().

        Parameters
        ----------
        bar_index:
            Sequential bar counter from BacktestRunner.run().
        structure_events:
            All events from StructureEngineResult.emitted_events.
        fvg_events:
            FVG_CREATED events filtered from structure_events (already computed).
        """
        self._processed_bars += 1

        for event in structure_events:
            et = (
                event.event_type.value
                if hasattr(event.event_type, "value")
                else str(event.event_type)
            )
            side = (
                event.side.value
                if hasattr(event.side, "value")
                else str(event.side)
            )
            if "BOS" in et or "CHOCH" in et:
                if side in (_BEARISH, _BULLISH):
                    self._last_bos_bar[side] = bar_index

        # Track best recent FVG displacement
        for fvg_event in fvg_events:
            ratio = float(fvg_event.metadata.get("displacement_body_ratio", 0.0))
            if ratio >= MIN_DISP_BODY_RATIO:
                if ratio > self._last_disp_ratio or bar_index > self._last_disp_bar + MAX_AGE_BARS:
                    self._last_disp_ratio = ratio
                    self._last_disp_bar = bar_index

    def validate(
        self,
        bar_index: int,
        bar_ts: datetime,
        bias: str,
        recent_sweeps: tuple,
    ) -> SetupValidation:
        """
        Validate the full setup at the current bar for the given bias.

        Returns SetupValidation with is_valid=True (full or partial) or
        is_valid=False (blocked) based on how many of the 3 conditions are met.
        """
        # Warm-up: no validation until structure has had time to build
        if self._processed_bars < WARMUP_BARS:
            return SetupValidation(
                is_valid=True, setup_score=1.0, reason="",
                sweep_ok=True, bos_ok=True, disp_ok=True,
            )

        age_limit = bar_index - MAX_AGE_BARS
        direction = _BEARISH if bias == "SHORT" else _BULLISH if bias == "LONG" else None

        # ── Condition 1: confirmed directional sweep within recency window ────
        sweep_ok = False
        if recent_sweeps:
            ts_cutoff = bar_ts - timedelta(minutes=SWEEP_MAX_AGE_MINUTES)
            for sweep in recent_sweeps:
                if sweep.outcome != SweepOutcome.CONFIRMED:
                    continue
                if sweep.reversal_bar_timestamp is None:
                    continue
                ts_sweep = sweep.reversal_bar_timestamp
                if ts_sweep.tzinfo is None:
                    ts_sweep = ts_sweep.replace(tzinfo=timezone.utc)
                ts_bar = bar_ts if bar_ts.tzinfo else bar_ts.replace(tzinfo=timezone.utc)
                if ts_sweep < ts_cutoff:
                    continue
                # Direction check: sweep.side is the liquidity level side,
                # a BEARISH sweep (swept resistance) → price fell → SHORT aligned
                # a BULLISH sweep (swept support)    → price rose → LONG aligned
                if direction is None or sweep.side.value == direction:
                    sweep_ok = True
                    break

        # ── Condition 2: BOS or CHOCH in correct direction ────────────────────
        if direction:
            bos_ok = self._last_bos_bar.get(direction, -9999) >= age_limit
        else:
            bos_ok = any(v >= age_limit for v in self._last_bos_bar.values())

        # ── Condition 3: strong displacement FVG ─────────────────────────────
        disp_ok = (
            self._last_disp_bar >= age_limit
            and self._last_disp_ratio >= MIN_DISP_BODY_RATIO
        )

        # ── Score ─────────────────────────────────────────────────────────────
        n_met = sum([sweep_ok, bos_ok, disp_ok])

        if n_met == 3:
            return SetupValidation(
                is_valid=True, setup_score=1.0, reason="",
                sweep_ok=sweep_ok, bos_ok=bos_ok, disp_ok=disp_ok,
            )

        if n_met == 2:
            missing = _missing_labels(sweep_ok, bos_ok, disp_ok)
            return SetupValidation(
                is_valid=True, setup_score=0.65,
                reason=f"partial setup — no {missing}",
                sweep_ok=sweep_ok, bos_ok=bos_ok, disp_ok=disp_ok,
            )

        if n_met == 1:
            present = _present_labels(sweep_ok, bos_ok, disp_ok)
            missing = _missing_labels(sweep_ok, bos_ok, disp_ok)
            return SetupValidation(
                is_valid=False, setup_score=0.0,
                reason=f"weak setup — only {present} present, missing {missing}",
                sweep_ok=sweep_ok, bos_ok=bos_ok, disp_ok=disp_ok,
            )

        return SetupValidation(
            is_valid=False, setup_score=0.0,
            reason="no setup — sweep, BOS/CHOCH, and displacement all absent",
            sweep_ok=False, bos_ok=False, disp_ok=False,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _missing_labels(sweep: bool, bos: bool, disp: bool) -> str:
    labels = []
    if not sweep: labels.append("sweep")
    if not bos:   labels.append("BOS/CHOCH")
    if not disp:  labels.append("displacement")
    return " + ".join(labels) if labels else "—"


def _present_labels(sweep: bool, bos: bool, disp: bool) -> str:
    labels = []
    if sweep: labels.append("sweep")
    if bos:   labels.append("BOS/CHOCH")
    if disp:  labels.append("displacement")
    return " + ".join(labels) if labels else "—"


__all__ = [
    "LiquiditySweepValidationEngine",
    "SetupValidation",
    "WARMUP_BARS",
    "MAX_AGE_BARS",
]
