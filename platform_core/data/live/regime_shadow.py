"""
regime_shadow.py — Phase-1 SHADOW wiring of RegimeEngine (no effect on trades).

Per the intelligence-wiring plan (docs/intelligence_wiring_plan.md): wire each
engine in shadow mode FIRST — compute it and log what it WOULD do, but never let
it touch the live trade path — then measure on out-of-sample data before any
promotion to the live sizer/gate.

This module classifies the volatility regime for each completed bar using the
existing RegimeEngine and appends one JSONL record per bar to
``logs/regime_shadow.jsonl``. The measurement harness joins these records to
executed-trade outcomes (by timestamp + underlying) to test whether regime-based
selectivity/sizing would have helped — BEFORE it ever affects a real decision.

Causal / no-lookahead: the average range is computed over PRIOR bars only (the
current bar's range is appended AFTER the average is taken). Every public call is
wrapped so a shadow failure can never disturb the trade path.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import timezone
from pathlib import Path

from platform_core.adaptive.regime_engine import RegimeEngine


class RegimeShadow:
    """Observational regime classifier — logging only, never affects trades."""

    def __init__(self, *, window: int = 20, out_path: str = "logs/regime_shadow.jsonl") -> None:
        self._engine = RegimeEngine()
        self._window = max(2, window)
        self._ranges: dict[str, deque] = {}
        self._out = Path(out_path)
        self.records = 0

    def observe(
        self,
        *,
        underlying: str,
        timestamp,
        high: float,
        low: float,
        close: float,
        bias: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Record the shadow regime for one completed bar. NEVER raises."""
        try:
            rng = max(float(high) - float(low), 0.0)
            buf = self._ranges.setdefault(underlying, deque(maxlen=self._window))
            # PRIOR-bars average (no lookahead): take the mean before appending.
            avg = (sum(buf) / len(buf)) if buf else rng
            buf.append(rng)
            if avg <= 0.0:
                return
            regime = self._engine.classify(bar_range=rng, average_range=avg)
            try:
                ts = timestamp.astimezone(timezone.utc).isoformat()
            except Exception:
                ts = str(timestamp)
            rec = {
                "ts": ts,
                "underlying": underlying,
                "close": round(float(close), 2),
                "bar_range": round(rng, 2),
                "avg_range": round(avg, 2),
                "vol_regime": regime.volatility_regime,
                "dir_regime": regime.directional_regime,
                "conf_mult": regime.confidence_multiplier,
                "bias": bias,
                "confidence": confidence,
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            # Shadow path must NEVER affect live trading. Swallow everything.
            pass
