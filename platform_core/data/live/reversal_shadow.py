"""
reversal_shadow.py — SHADOW wiring of the ReversalClassifier (logs only).

Near-twin of continuation_shadow. The ReversalObservation is the continuation
field-set MINUS reversal_probability, breakout_hold_pct and pullback_shallow_pct.
It reuses the shared shadow_feature_kit computations, runs the classifier, and
appends reversal_probability + confidence per bar to logs/reversal_shadow.jsonl.
Never affects the trade path.

Only ``trap_probability`` is neutral-defaulted (0.5) in v1 — produced elsewhere;
so ~18 of 19 features are REAL. Every record logs ``defaulted``.

Observational only; ``observe()`` never raises.
"""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

from platform_core.ml.reversal_classifier import (
    ReversalClassifier,
    ReversalObservation,
)
from platform_core.data.live.shadow_feature_kit import (
    clip01, num, compute_price_structure_features, extract_option_flow,
)

# continuation-only fields not present on ReversalObservation
_DROP = ("breakout_hold_pct", "pullback_shallow_pct")


def build_reversal_observation(
    *, underlying, timestamp, exec_bias, bars, or_high=None, or_low=None,
    dealer=None, premium=None, oi=None, atm_iv=None, prev_atm_iv=None,
    trap_probability=0.5, regime_label=None, regime_confidence=None,
):
    """Assemble a ReversalObservation. Returns (obs, defaulted) or (None, reason)."""
    b = str(exec_bias).upper()
    if b not in ("LONG", "SHORT"):
        return None, "no_direction"
    direction = "UP" if b == "LONG" else "DOWN"
    feats, dflt_p = compute_price_structure_features(bars, direction, or_high, or_low)
    if feats is None:
        return None, dflt_p
    rev_feats = {k: v for k, v in feats.items() if k not in _DROP}
    flow, dflt_o = extract_option_flow(dealer, premium, oi, atm_iv, prev_atm_iv)
    # breakout_hold_pct is not a reversal field, so its default flag is irrelevant here
    defaulted = ["trap_probability"] + [x for x in dflt_p if x not in _DROP] + dflt_o
    try:
        obs = ReversalObservation(
            underlying_symbol=str(underlying),
            timestamp=timestamp,
            active_direction=direction,
            trap_probability=clip01(trap_probability),
            regime_label=regime_label,
            regime_confidence=(None if regime_confidence is None else clip01(regime_confidence)),
            **rev_feats, **flow,
        )
        return obs, defaulted
    except Exception as exc:
        return None, f"build_error:{type(exc).__name__}"


class ReversalShadow:
    """Runs ReversalClassifier per bar in shadow — logging only, never trades."""

    def __init__(self, *, out_path: str = "logs/reversal_shadow.jsonl") -> None:
        self._out = Path(out_path)
        self._clf: dict[str, ReversalClassifier] = {}
        self._prev_iv: dict[str, float] = {}
        self.records = 0
        self.nonnull = 0

    def observe(self, *, underlying, timestamp, exec_bias, bars,
                or_high=None, or_low=None, dealer=None, premium=None, oi=None,
                atm_iv=None, trap_probability=0.5,
                regime_label=None, regime_confidence=None) -> None:
        """Build observation, classify, log. NEVER raises."""
        try:
            u = str(underlying)
            obs, info = build_reversal_observation(
                underlying=u, timestamp=timestamp, exec_bias=exec_bias, bars=bars,
                or_high=or_high, or_low=or_low, dealer=dealer, premium=premium, oi=oi,
                atm_iv=atm_iv, prev_atm_iv=self._prev_iv.get(u),
                trap_probability=trap_probability,
                regime_label=regime_label, regime_confidence=regime_confidence,
            )
            if atm_iv is not None:
                self._prev_iv[u] = num(atm_iv)
            if obs is None:
                return
            clf = self._clf.get(u)
            if clf is None:
                clf = ReversalClassifier()
                self._clf[u] = clf
            result = clf.classify(observation=obs, valuation_timestamp=timestamp)
            rec = {
                "ts": self._ts(timestamp),
                "underlying": u,
                "direction": obs.normalized_direction,
                "reversal_prob": round(result.reversal_probability, 4),
                "continuation_prob": round(result.continuation_probability, 4),
                "confidence": round(result.confidence, 4),
                "trend_score": round(obs.trend_score, 5),
                "trend_efficiency": round(obs.trend_efficiency, 3),
                "put_call_ratio": round(obs.put_call_ratio, 3),
                "regime": regime_label,
                "defaulted": info if isinstance(info, list) else [],
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
            self.nonnull += 1
        except Exception:
            pass

    @staticmethod
    def _ts(timestamp) -> str:
        try:
            return timestamp.astimezone(timezone.utc).isoformat()
        except Exception:
            return str(timestamp)
