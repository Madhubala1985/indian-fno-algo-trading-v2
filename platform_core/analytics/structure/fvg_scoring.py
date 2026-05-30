# FILE: platform_core/analytics/structure/fvg_scoring.py
"""
Institutional FVG Quality Scorer — multi-factor upgrade over gap_ratio proxy.

Replaces the single-dimensional confidence_score=gap.gap_ratio with a
six-factor weighted score. The scorer is stateless and additive to the
existing filter chain (session / PDH-PDL / opening-range).

Six factors (normalized [0, 1] each):
  1. Displacement strength  — middle bar body/range and gap_ratio
  2. Session quality        — when the FVG was created (IST session phase)
  3. Sweep origin           — liquidity sweep alignment, recency, quality
  4. HTF alignment          — FVG direction vs current market bias
  5. Freshness              — 1 - mitigation_ratio
  6. Volume participation   — middle bar volume relative to left bar

Sweep origin factor upgrade (factor 3):
  Previously binary (had_sweep → 1.0 / 0.30).
  Now uses actual LiquiditySweep objects from LiquiditySweepDetector:
    - Direction alignment: sweep.side == fvg.side → institutional origin
    - Recency: how many minutes since the sweep reversed
    - Confirmation quality: sweep.confirmation_score from the detector
  This is additive to all existing filters — does not replace session/PDL/OR.

Output: FVGQualityScore with .total in [0, 1]
Entry blocking threshold: total < 0.40 → treat as failed FVG context

Integration:
  BacktestRunner.run():
    1. Carries SR zones → LiquidityLevel → StructureEngineInput (activates sweeps)
    2. Passes recent_sweeps=tuple(liquidity_detector.recent_sweeps) to score_event()
    3. Multiplies fvg_quality into effective_confidence alongside session/PDL/OR
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from platform_core.analytics.structure.models import (
    LiquiditySweep, SweepOutcome, StructureEvent, StructureSide,
)

_IST = timezone(timedelta(hours=5, minutes=30))

# Factor weights — must sum to 1.0
_W_DISPLACEMENT = 0.25
_W_SESSION      = 0.15
_W_SWEEP        = 0.20
_W_HTF          = 0.20
_W_FRESHNESS    = 0.10
_W_VOLUME       = 0.10

# Session quality table
_SESSION_SCORES: dict[str, float] = {
    "MAIN":      1.00,
    "PRE_CLOSE": 0.65,
    "OPENING":   0.30,
    "PRE_OPEN":  0.00,
    "CLOSING":   0.00,
    "CLOSED":    0.00,
}

# Sweep scoring constants
_SWEEP_MAX_AGE_MINUTES: float = 15.0   # sweeps older than this are ignored
_SWEEP_NO_SWEEP_SCORE:  float = 0.20   # no sweeps available at all
_SWEEP_MISALIGNED_SCORE: float = 0.15  # sweep exists but wrong direction

# Threshold below which BacktestRunner hard-blocks the entry
FVG_QUALITY_BLOCK_THRESHOLD: float = 0.40

# Bars after FVG creation before stored quality resets to neutral (1.0)
FVG_QUALITY_STALENESS_BARS: int = 25


def _confluence_bonus(
    fvg_midpoint: float,
    fvg_side: StructureSide,
    active_htf_fvgs: dict[str, tuple[Any, ...]],
) -> float:
    """
    Additive bonus [0.0, 0.15] when a lower-TF FVG sits inside an active
    higher-TF FVG of the same direction.

    Supported pairings:
      1m inside 15m   → +0.07
      1m inside 1h    → +0.10
      1m inside both  → +0.15
      15m inside 1h   → +0.10   (when called from a 15m scorer context)
    """
    def _overlaps(gaps: tuple[Any, ...]) -> bool:
        for gap in gaps:
            if gap.is_fully_mitigated:
                continue
            if gap.side != fvg_side:
                continue
            if gap.lower_price <= fvg_midpoint <= gap.upper_price:
                return True
        return False

    in_15m = _overlaps(active_htf_fvgs.get("MIN_15", ()))
    in_1h  = _overlaps(active_htf_fvgs.get("HOUR_1", ()))

    if in_15m and in_1h:
        return 0.15
    if in_1h:
        return 0.10
    if in_15m:
        return 0.07
    return 0.0


def _session_phase_of(ts: datetime) -> str:
    ist = ts.astimezone(_IST)
    tod = ist.hour * 60 + ist.minute
    if tod < 540:  return "CLOSED"
    if tod < 555:  return "PRE_OPEN"
    if tod < 570:  return "OPENING"
    if tod < 870:  return "MAIN"
    if tod < 900:  return "PRE_CLOSE"
    if tod < 930:  return "CLOSING"
    return "CLOSED"


def _sweep_origin_score(
    recent_sweeps: tuple[LiquiditySweep, ...],
    fvg_side: StructureSide,
    event_timestamp: datetime,
) -> float:
    """
    Compute sweep origin score from actual LiquiditySweep objects.

    Algorithm (for each confirmed sweep within _SWEEP_MAX_AGE_MINUTES):
      alignment = 1.0 if sweep.side == fvg_side else _SWEEP_MISALIGNED_SCORE
      recency   = 1.0 - age_min / _SWEEP_MAX_AGE_MINUTES   (0 = old, 1 = fresh)
      quality   = min(sweep.confirmation_score / 2.0, 1.0)  (detector's own score)
      score     = alignment × (0.50 + 0.30×recency + 0.20×quality)

    Returns the best score across all eligible sweeps.
    Falls back to _SWEEP_NO_SWEEP_SCORE when no recent confirmed sweeps exist.
    """
    if not recent_sweeps:
        return _SWEEP_NO_SWEEP_SCORE

    best = 0.0
    for sweep in recent_sweeps:
        if sweep.outcome != SweepOutcome.CONFIRMED:
            continue
        if sweep.reversal_bar_timestamp is None:
            continue

        # Ensure both timestamps are comparable
        ts_event = event_timestamp
        ts_sweep = sweep.reversal_bar_timestamp
        if ts_event.tzinfo is None:
            ts_event = ts_event.replace(tzinfo=timezone.utc)
        if ts_sweep.tzinfo is None:
            ts_sweep = ts_sweep.replace(tzinfo=timezone.utc)

        age_sec = (ts_event - ts_sweep).total_seconds()
        if age_sec < 0 or age_sec > _SWEEP_MAX_AGE_MINUTES * 60:
            continue

        age_min = age_sec / 60.0
        recency   = max(0.0, 1.0 - age_min / _SWEEP_MAX_AGE_MINUTES)
        quality   = min(sweep.confirmation_score / 2.0, 1.0)
        alignment = 1.0 if sweep.side == fvg_side else _SWEEP_MISALIGNED_SCORE

        score = alignment * (0.50 + 0.30 * recency + 0.20 * quality)
        if score > best:
            best = score

    # best == 0.0 means all sweeps were too old, unconfirmed, or in wrong dir
    return best if best > 0.0 else _SWEEP_NO_SWEEP_SCORE


@dataclass(frozen=True, slots=True)
class FVGQualityScore:
    total: float
    displacement: float
    session: float
    sweep_origin: float
    htf_alignment: float
    freshness: float
    volume: float
    confluence: float = 0.0   # additive MTF bonus [0.0, 0.15]

    @property
    def blocks_entry(self) -> bool:
        return self.total < FVG_QUALITY_BLOCK_THRESHOLD

    def confidence_multiplier(self) -> float:
        """Smooth mapping: quality ≤0.30 → 0.0, quality ≥0.70 → 1.0."""
        if self.total <= 0.30:
            return 0.0
        if self.total >= 0.70:
            return 1.0
        return (self.total - 0.30) / (0.70 - 0.30)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": round(self.total, 3),
            "displacement": round(self.displacement, 3),
            "session": round(self.session, 3),
            "sweep_origin": round(self.sweep_origin, 3),
            "htf_alignment": round(self.htf_alignment, 3),
            "freshness": round(self.freshness, 3),
            "volume": round(self.volume, 3),
            "confluence": round(self.confluence, 3),
        }


class FVGQualityScorer:
    """
    Stateless multi-factor FVG quality scorer.

    score_event() accepts recent_sweeps from LiquiditySweepDetector.
    All six factors are computed independently and combined as a weighted sum.
    """

    def score_event(
        self,
        *,
        event: StructureEvent,
        market_bias: str,
        recent_sweeps: tuple[LiquiditySweep, ...] = (),
        active_htf_fvgs: dict[str, tuple[Any, ...]] | None = None,
        mitigation_ratio: float = 0.0,
    ) -> FVGQualityScore:
        """
        Score an FVG_CREATED or FVG_UPDATED StructureEvent.

        Parameters
        ----------
        event:
            StructureEvent from FVGEngine (must carry displacement metadata).
        market_bias:
            Current bias string: "LONG", "SHORT", or "NEUTRAL".
        recent_sweeps:
            Confirmed LiquiditySweep objects from LiquiditySweepDetector.
        active_htf_fvgs:
            Active FairValueGap objects keyed by timeframe string
            (e.g. {"MIN_15": fvg_15m.active_gaps, "HOUR_1": fvg_1h.active_gaps}).
            Used for MTF confluence bonus (additive, does not replace any factor).
        mitigation_ratio:
            Current FVG mitigation progress [0, 1].
        """
        meta = event.metadata

        # ── 1. Displacement strength ───────────────────────────────────────
        body_ratio = float(meta.get("displacement_body_ratio", 0.0))
        gap_ratio  = float(meta.get("gap_ratio", event.confidence_score))
        disp_score = min(body_ratio / 0.65, 1.0) * 0.7 + min(gap_ratio / 0.004, 1.0) * 0.3

        # ── 2. Session quality ─────────────────────────────────────────────
        phase      = _session_phase_of(event.event_timestamp)
        sess_score = _SESSION_SCORES.get(phase, 0.0)

        # ── 3. Sweep origin (upgraded: directional alignment + recency + quality)
        sweep_score = _sweep_origin_score(recent_sweeps, event.side, event.event_timestamp)

        # ── 4. HTF alignment ───────────────────────────────────────────────
        fvg_bullish = event.side == StructureSide.BULLISH
        if market_bias == "LONG" and fvg_bullish:
            htf_score = 1.0
        elif market_bias == "SHORT" and not fvg_bullish:
            htf_score = 1.0
        elif market_bias == "NEUTRAL":
            htf_score = 0.50
        else:
            htf_score = 0.15

        # ── 5. Freshness ───────────────────────────────────────────────────
        fresh_score = max(0.0, 1.0 - mitigation_ratio)

        # ── 6. Volume participation ────────────────────────────────────────
        vol_ratio  = float(meta.get("displacement_volume_ratio", 1.0))
        vol_score  = min(vol_ratio / 1.5, 1.0)

        base = (
            _W_DISPLACEMENT * disp_score
            + _W_SESSION      * sess_score
            + _W_SWEEP        * sweep_score
            + _W_HTF          * htf_score
            + _W_FRESHNESS    * fresh_score
            + _W_VOLUME       * vol_score
        )

        # MTF confluence: additive bonus, does not change the six factors
        fvg_midpoint = float(event.trigger_price)
        conf_bonus = _confluence_bonus(
            fvg_midpoint, event.side, active_htf_fvgs or {}
        )
        total = max(0.0, min(1.0, base + conf_bonus))

        return FVGQualityScore(
            total=total,
            displacement=disp_score,
            session=sess_score,
            sweep_origin=sweep_score,
            htf_alignment=htf_score,
            freshness=fresh_score,
            volume=vol_score,
            confluence=conf_bonus,
        )


__all__ = [
    "FVGQualityScore",
    "FVGQualityScorer",
    "FVG_QUALITY_BLOCK_THRESHOLD",
    "FVG_QUALITY_STALENESS_BARS",
]
