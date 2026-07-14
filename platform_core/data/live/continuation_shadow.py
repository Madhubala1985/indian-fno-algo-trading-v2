"""
continuation_shadow.py — SHADOW wiring of the ContinuationClassifier (logs only).

The classifier needs a 22-field ContinuationObservation that nothing else in the
live path assembles. This module builds that observation each bar from data that
IS available at the option-context build site — price/structure features from the
recent 1m bar history, options-flow features from the already-built Dealer / OI /
Premium snapshots, and IV change from the tracked ATM IV — then runs the
classifier and appends its continuation_probability + confidence to
``logs/continuation_shadow.jsonl``. Never affects the trade path.

Shared feature math lives in shadow_feature_kit (reused by the reversal shadow).

Honesty about inputs (grows a MEANINGFUL corpus, not a null one — the htf-null
lesson): ~20 of 22 features are REAL. Two are neutral-defaulted in v1 because they
are produced elsewhere — ``trap_probability`` and ``reversal_probability`` default
to 0.5. Every record logs ``defaulted`` listing which fields were not real.

Observational only; ``observe()`` never raises.
"""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

from platform_core.ml.continuation_classifier import (
    ContinuationClassifier,
    ContinuationObservation,
)
from platform_core.data.live.shadow_feature_kit import (
    clip01, num, compute_price_structure_features, extract_option_flow,
)


def build_continuation_observation(
    *, underlying, timestamp, exec_bias, bars, or_high=None, or_low=None,
    dealer=None, premium=None, oi=None, atm_iv=None, prev_atm_iv=None,
    trap_probability=0.5, reversal_probability=0.5,
    regime_label=None, regime_confidence=None,
):
    """Assemble a ContinuationObservation. Returns (obs, defaulted) or (None, reason)."""
    b = str(exec_bias).upper()
    if b not in ("LONG", "SHORT"):
        return None, "no_direction"
    direction = "UP" if b == "LONG" else "DOWN"
    feats, dflt_p = compute_price_structure_features(bars, direction, or_high, or_low)
    if feats is None:
        return None, dflt_p
    flow, dflt_o = extract_option_flow(dealer, premium, oi, atm_iv, prev_atm_iv)
    defaulted = ["trap_probability", "reversal_probability"] + dflt_p + dflt_o
    try:
        obs = ContinuationObservation(
            underlying_symbol=str(underlying),
            timestamp=timestamp,
            active_direction=direction,
            trap_probability=clip01(trap_probability),
            reversal_probability=clip01(reversal_probability),
            regime_label=regime_label,
            regime_confidence=(None if regime_confidence is None else clip01(regime_confidence)),
            **feats, **flow,
        )
        return obs, defaulted
    except Exception as exc:
        return None, f"build_error:{type(exc).__name__}"


class ContinuationShadow:
    """Runs ContinuationClassifier per bar in shadow — logging only, never trades."""

    def __init__(self, *, out_path: str = "logs/continuation_shadow.jsonl") -> None:
        self._out = Path(out_path)
        self._clf: dict[str, ContinuationClassifier] = {}
        self._prev_iv: dict[str, float] = {}
        self.records = 0
        self.nonnull = 0

    def observe(self, *, underlying, timestamp, exec_bias, bars,
                or_high=None, or_low=None, dealer=None, premium=None, oi=None,
                atm_iv=None, trap_probability=0.5, reversal_probability=0.5,
                regime_label=None, regime_confidence=None) -> None:
        """Build observation, classify, log. NEVER raises."""
        try:
            u = str(underlying)
            obs, info = build_continuation_observation(
                underlying=u, timestamp=timestamp, exec_bias=exec_bias, bars=bars,
                or_high=or_high, or_low=or_low, dealer=dealer, premium=premium, oi=oi,
                atm_iv=atm_iv, prev_atm_iv=self._prev_iv.get(u),
                trap_probability=trap_probability, reversal_probability=reversal_probability,
                regime_label=regime_label, regime_confidence=regime_confidence,
            )
            if atm_iv is not None:
                self._prev_iv[u] = num(atm_iv)
            if obs is None:
                return
            clf = self._clf.get(u)
            if clf is None:
                clf = ContinuationClassifier()
                self._clf[u] = clf
            result = clf.classify(observation=obs, valuation_timestamp=timestamp)
            rec = {
                "ts": self._ts(timestamp),
                "underlying": u,
                "direction": obs.normalized_direction,
                "continuation_prob": round(result.continuation_probability, 4),
                "reversal_prob": round(result.reversal_probability, 4),
                "confidence": round(result.confidence, 4),
                "trend_score": round(obs.trend_score, 5),
                "trend_efficiency": round(obs.trend_efficiency, 3),
                "breakout_hold_pct": round(obs.breakout_hold_pct, 3),
                "pullback_shallow_pct": round(obs.pullback_shallow_pct, 3),
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
