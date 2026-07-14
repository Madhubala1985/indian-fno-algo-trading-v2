"""
BiasInvalidationEngine — intraday adaptivity for the MCB execution bias.

THE PROBLEM (measured, not theoretical)
---------------------------------------
The MCB blends 1m/15m/1h/4h/1d, with 4h+1d carrying ~60% of the weight. A
start-of-day bias therefore CANNOT flip intraday no matter what the market
does. 2026-07-13 made this maximally visible: warm-started SHORT into a
gap-down that trended UP all day (+0.7–0.9% on all three indices, low at
09:15 sharp), the model shorted the low of the day, lost, and then logged
231 consecutive OR-contradiction blocks — it KNEW price was breaking against
its bias every single bar and sat frozen. Loser autopsy (n=64): losers enter
at median bias-age 864 bars vs winners 112. Same disease on 2026-07-08
(−₹18,856 vs a discretionary short-the-breakdown read +₹1,67,517).

THE MODEL (how Fable actually reads a session — encoded 1:1)
------------------------------------------------------------
1. PRIOR      — the start-of-day bias is a hypothesis, not an identity.
2. EVIDENCE   — every finalized bar either confirms or contradicts it:
                price holding beyond the OPPOSITE opening-range extreme,
                opposite-side structure breaks (BOS/CHOCH), constituent
                flow pushing the other way with fresh momentum.
3. FORGIVENESS— evidence that fades is forgiven fast (score decays on any
                agreeing bar); chop must never accumulate into a flip.
4. NEUTRALIZE — enough sustained contradiction → STOP trading the prior
                (stand aside; being flat is a position).
5. FLIP       — sustained contradiction PLUS a structural/flow confirmer →
                trade the market, not the prior, at humble confidence.
6. HUMILITY   — a flip is also just a hypothesis: symmetric counter-evidence
                de-escalates it back to neutral (never straight back to the
                old prior — whipsaw guard); if the slow MCB later catches up
                and agrees with the flip, the engine steps aside (CONFIRM).

WIRING CONTRACT
---------------
Pure, self-contained, lock-free, never raises past evaluate()'s guard. The
live pipeline calls evaluate() once per finalized 1m bar per underlying,
UPSTREAM of strike selection, so every downstream consumer (chain fetch,
CE/PE side, gates, sizing, futures sleeve) sees ONE consistent effective
bias. Disable instantly with BIAS_INVALIDATION=0 (env) — evaluate() then
returns the raw bias untouched. State is per-day, in-memory only: a
crash-restart re-accumulates from scratch (conservative by design).

Every transition is returned in the info dict for loud terminal logging and
the bias_invalidation.jsonl shadow trail, so the wiring can be graded
against outcomes like every other intelligence candidate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# ── Pre-registered thresholds (fixed BEFORE the validation run on recorded
#    days — see scripts/validate_bias_invalidation_days.py) ──────────────────
# A pure OR-contradiction bar scores +1. ~15 minutes of sustained trade beyond
# the opposite OR extreme = stand aside; ~30 (or less with structure/flow
# confirmation, which weight heavier) = flip. An agreeing bar halves the
# score, so chop that oscillates around the range cannot ratchet into a flip.
NEUTRALIZE_SCORE = _envf("BIAS_INVAL_NEUTRALIZE_SCORE", 15.0)
FLIP_SCORE = _envf("BIAS_INVAL_FLIP_SCORE", 30.0)
OR_BUFFER_FRAC = _envf("BIAS_INVAL_OR_BUFFER_FRAC", 0.0005)   # 0.05% grazing guard
OPPOSITE_BREAK_POINTS = _envf("BIAS_INVAL_BREAK_POINTS", 5.0)  # per BOS/CHOCH
FLOW_LEVEL_MIN = _envf("BIAS_INVAL_FLOW_LEVEL_MIN", 0.10)      # % from open
FLOW_MULT = _envf("BIAS_INVAL_FLOW_MULT", 1.5)
STALE_BIAS_BARS = _envf("BIAS_INVAL_STALE_BARS", 240.0)        # ≈4h of 1m bars
STALE_MULT = _envf("BIAS_INVAL_STALE_MULT", 1.25)
DECAY_ON_AGREE = _envf("BIAS_INVAL_DECAY", 0.5)
FLIP_CONFIDENCE = _envf("BIAS_INVAL_FLIP_CONF", 0.60)
# Day-move fallback evidence (when OR is not ready — e.g. seed failed): close
# beyond day-open against the bias by this much counts as a half-strength bar.
DAY_MOVE_FRAC = _envf("BIAS_INVAL_DAY_MOVE_FRAC", 0.0025)      # 0.25%
# Whipsaw guard: a NEUTRALIZED stage must dwell this many bars before the
# prior can be re-armed, even if the contradiction score has decayed.
REARM_DWELL_BARS = int(_envf("BIAS_INVAL_REARM_DWELL_BARS", 15))

_OPP = {"LONG": "SHORT", "SHORT": "LONG"}


@dataclass
class InvalidationState:
    """Per-underlying intraday state. Reset on day roll and on raw-bias flip."""
    day: str = ""
    day_open: float | None = None
    raw_bias: str = "NEUTRAL"
    raw_bias_age: int = 0          # bars since the RAW bias last changed
    score: float = 0.0             # contradiction evidence vs the active hypothesis
    stage: str = "CONFIRM"         # CONFIRM | NEUTRALIZED | FLIPPED
    bars_in_stage: int = 0         # dwell counter (whipsaw guard on re-arm)
    flipped_dir: str = ""          # set while stage == FLIPPED
    saw_opposite_break: bool = False
    saw_opposite_flow: bool = False
    transitions: list = field(default_factory=list)


class BiasInvalidationEngine:
    """One instance serves all underlyings (keyed internally)."""

    def __init__(self) -> None:
        self._st: dict[str, InvalidationState] = {}
        self.enabled = os.environ.get("BIAS_INVALIDATION", "1") != "0"

    # ── public ───────────────────────────────────────────────────────────────
    def evaluate(
        self,
        *,
        underlying: str,
        day: str,
        bar_index: int,
        raw_bias: str,
        close: float,
        bar_open: float,
        or_high: float | None,
        or_low: float | None,
        or_ready: bool,
        opposite_breaks: int = 0,
        same_breaks: int = 0,
        flow: dict | None = None,
    ) -> tuple[str, float | None, dict]:
        """
        Returns (effective_bias, confidence_cap_or_None, info).

        effective_bias == raw_bias and cap is None whenever the engine has
        nothing to say (disabled, NEUTRAL raw, no evidence). info always
        carries stage/score plus a 'transition' key on state changes.
        NEVER raises: any internal error returns the raw bias untouched.
        """
        try:
            return self._evaluate(
                underlying=underlying, day=day, bar_index=bar_index,
                raw_bias=raw_bias, close=close, bar_open=bar_open,
                or_high=or_high, or_low=or_low, or_ready=or_ready,
                opposite_breaks=opposite_breaks, same_breaks=same_breaks,
                flow=flow,
            )
        except Exception as exc:  # fail-open: raw bias, loud info
            return raw_bias, None, {"stage": "ERROR", "error": str(exc)[:200]}

    def flip_active(self, underlying: str, direction: str) -> bool:
        """True when the engine is currently FLIPPED into `direction` for this
        underlying. Entry gates that encode the STALE prior view (HTF
        opposition, flow-evidence requirements) should not re-block a flip —
        the flip already demanded sustained contradiction PLUS a structural or
        flow confirmer; the engine is the adjudicator of stale-vs-fresh."""
        st = self._st.get(underlying)
        return (self.enabled and st is not None
                and st.stage == "FLIPPED" and st.flipped_dir == direction)

    def position_direction_invalidated(self, underlying: str, direction: str) -> bool:
        """True when the engine currently stands NEUTRALIZED-or-FLIPPED against
        `direction` — the thesis behind an open position in that direction is
        gone. Used by the exit path (THESIS_INVALIDATED force-exit)."""
        st = self._st.get(underlying)
        if st is None or not self.enabled or direction not in ("LONG", "SHORT"):
            return False
        if st.stage == "NEUTRALIZED":
            # No thesis at all — a directional position in EITHER direction has
            # lost its reason to exist (covers both raw-thesis death and a
            # de-escalated flip; "being flat is a position").
            return True
        # CONFIRM / FLIPPED: a position OPPOSING the current effective stance is
        # thesis-dead (this is the roadmap-#4 invalidation exit: exit on thesis
        # death, not on a visible %-level where liquidity gets taken).
        eff = st.flipped_dir if st.stage == "FLIPPED" else st.raw_bias
        return eff in ("LONG", "SHORT") and direction != eff

    # ── internals ────────────────────────────────────────────────────────────
    def _evaluate(self, *, underlying, day, bar_index, raw_bias, close,
                  bar_open, or_high, or_low, or_ready, opposite_breaks,
                  same_breaks, flow):
        st = self._st.get(underlying)
        if st is None or st.day != day:
            st = InvalidationState(day=day, day_open=bar_open, raw_bias=raw_bias)
            self._st[underlying] = st
        if st.day_open is None:
            st.day_open = bar_open

        info: dict = {}
        raw_bias = str(raw_bias).upper()

        # Raw-bias change: the MCB moved on its own — the hypothesis changed.
        if raw_bias != st.raw_bias:
            if st.stage == "FLIPPED" and raw_bias == st.flipped_dir:
                self._transition(st, info, bar_index, "CONFIRM",
                                 "MCB caught up with the flip — engine steps aside")
            else:
                self._transition(st, info, bar_index, "CONFIRM",
                                 f"raw bias changed {st.raw_bias}→{raw_bias} — evidence reset")
            st.stage = "CONFIRM"
            st.raw_bias = raw_bias
            st.raw_bias_age = 0
            st.score = 0.0
            st.bars_in_stage = 0
            st.flipped_dir = ""
            st.saw_opposite_break = st.saw_opposite_flow = False
        else:
            st.raw_bias_age += 1

        if not self.enabled or raw_bias not in ("LONG", "SHORT"):
            info.update(stage=st.stage, score=round(st.score, 2))
            return raw_bias, None, info

        # The hypothesis under test: the raw bias normally; the flipped
        # direction once flipped (humility — a flip is also just a hypothesis).
        hypo = st.flipped_dir if st.stage == "FLIPPED" else raw_bias
        against = _OPP[hypo]

        # ── per-bar evidence vs `hypo` ───────────────────────────────────────
        increment = 0.0
        contra_or = False
        if or_ready and or_high is not None and or_low is not None and or_high > or_low:
            buf = close * OR_BUFFER_FRAC
            if hypo == "SHORT" and close > or_high + buf:
                contra_or = True
            elif hypo == "LONG" and close < or_low - buf:
                contra_or = True
            if contra_or:
                increment += 1.0
        elif st.day_open:
            move = (close - st.day_open) / st.day_open
            if (hypo == "SHORT" and move > DAY_MOVE_FRAC) or \
               (hypo == "LONG" and move < -DAY_MOVE_FRAC):
                increment += 0.5

        if opposite_breaks > 0 and hypo == raw_bias:
            increment += OPPOSITE_BREAK_POINTS * opposite_breaks
            st.saw_opposite_break = True
        if same_breaks > 0 and hypo != raw_bias:
            # while FLIPPED, a break in the RAW direction is evidence vs the flip
            increment += OPPOSITE_BREAK_POINTS * same_breaks

        flow_against = False
        if flow:
            try:
                fb = str(flow.get("flow_bias") or "").upper()
                lvl = abs(float(flow.get("avg_flow_pct") or 0.0))
                mom = flow.get("mom")
                mom_ok = (mom is None
                          or (float(mom) >= 0.02 if against == "LONG" else float(mom) <= -0.02))
                flow_against = (fb == against and lvl >= FLOW_LEVEL_MIN and mom_ok)
            except (TypeError, ValueError):
                flow_against = False
        if flow_against:
            increment *= FLOW_MULT
            if increment > 0:
                st.saw_opposite_flow = True

        if increment > 0 and st.raw_bias_age > STALE_BIAS_BARS and hypo == raw_bias:
            increment *= STALE_MULT   # stale priors get invalidated faster

        if increment > 0:
            st.score += increment
        else:
            st.score *= DECAY_ON_AGREE  # forgiveness: chop cannot ratchet a flip

        # ── stage machine ────────────────────────────────────────────────────
        st.bars_in_stage += 1
        if st.stage in ("CONFIRM", "NEUTRALIZED") and hypo == raw_bias:
            if (st.score >= FLIP_SCORE
                    and (st.saw_opposite_break or st.saw_opposite_flow)):
                st.stage = "FLIPPED"
                st.bars_in_stage = 0
                st.flipped_dir = against
                st.score = 0.0
                st.saw_opposite_break = st.saw_opposite_flow = False
                self._transition(
                    st, info, bar_index, "FLIPPED",
                    f"sustained contradiction vs {raw_bias} + structural/flow "
                    f"confirmer → trade the market: {against}")
            elif st.stage == "CONFIRM" and st.score >= NEUTRALIZE_SCORE:
                st.stage = "NEUTRALIZED"
                st.bars_in_stage = 0
                self._transition(
                    st, info, bar_index, "NEUTRALIZED",
                    f"sustained contradiction vs {raw_bias} — standing aside")
            elif (st.stage == "NEUTRALIZED" and st.score < NEUTRALIZE_SCORE / 2
                    and st.bars_in_stage >= REARM_DWELL_BARS):
                st.stage = "CONFIRM"
                st.bars_in_stage = 0
                self._transition(st, info, bar_index, "CONFIRM",
                                 "contradiction faded — prior bias restored")
        elif st.stage == "FLIPPED":
            if st.score >= NEUTRALIZE_SCORE:
                st.stage = "NEUTRALIZED"
                st.bars_in_stage = 0
                st.flipped_dir = ""
                st.score = NEUTRALIZE_SCORE  # decays through the dwell window
                self._transition(
                    st, info, bar_index, "NEUTRALIZED",
                    "counter-evidence vs the flip — de-escalate to flat "
                    "(never straight back to the old prior)")

        # ── effective output ─────────────────────────────────────────────────
        if st.stage == "NEUTRALIZED":
            eff, cap = "NEUTRAL", None
        elif st.stage == "FLIPPED":
            eff, cap = st.flipped_dir, FLIP_CONFIDENCE
        else:
            eff, cap = raw_bias, None

        info.update(stage=st.stage, score=round(st.score, 2),
                    raw_bias=raw_bias, effective_bias=eff,
                    raw_bias_age=st.raw_bias_age, contra_or=contra_or,
                    flow_against=flow_against)
        return eff, cap, info

    @staticmethod
    def _transition(st: InvalidationState, info: dict, bar_index: int,
                    to_stage: str, reason: str) -> None:
        st.transitions.append((bar_index, to_stage, reason))
        info["transition"] = {"bar": bar_index, "to": to_stage, "reason": reason}
