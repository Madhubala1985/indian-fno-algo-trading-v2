"""
trap_shadow.py — Phase-1 SHADOW wiring of TrapProbabilityModel (no trade effect).

Per docs/intelligence_wiring_plan.md: wire each engine in shadow mode FIRST —
compute it and log what it WOULD say, never touching the live trade path — then
measure on out-of-sample data before any promotion.

This scores whether the CURRENT opening-range break attempt is a TRAP, and it is
DIRECTION-AGNOSTIC by design: the attempted direction is inferred from the actual
market break (UP if the window's high broke above the OR, DOWN if its low broke
below), NOT from the model's (possibly stale) MCB bias. So it never assumes a
side — it evaluates "is this break, whichever way the market went, real or a
trap?". One JSONL record per break-attempt bar → logs/trap_shadow.jsonl. The
measurement harness joins these to executed-trade outcomes to test whether the
2026-07-08 lead (a trap_prob>0.8 tail-filter caught the egregious open-drive
traps) holds out-of-sample BEFORE it ever affects a real decision.

Causal / no-lookahead: features use only the current + prior bars in the rolling
window. Every public call is wrapped so a shadow failure can never disturb the
trade path.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import timezone
from pathlib import Path

from platform_core.ml.trap_probability_model import TrapProbabilityModel, TrapObservation


class TrapShadow:
    """Observational trap-probability scorer — logging only, never affects trades."""

    def __init__(self, *, window: int = 8, out_path: str = "logs/trap_shadow.jsonl") -> None:
        self._window = max(3, window)
        self._bars: dict[str, deque] = {}
        self._models: dict[str, TrapProbabilityModel] = {}   # stateful, one per underlying
        self._out = Path(out_path)
        self.records = 0

    def observe(
        self,
        *,
        underlying: str,
        timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        or_low: float | None,
        or_high: float | None,
    ) -> None:
        """Score the current OR break attempt (UP or DOWN) for trap-ness. NEVER raises."""
        try:
            buf = self._bars.setdefault(underlying, deque(maxlen=self._window + 1))
            buf.append((float(open_), float(high), float(low), float(close), float(volume)))
            if or_low is None or or_high is None or len(buf) < 3:
                return
            win = list(buf)
            hi_ext = max(b[1] for b in win)
            lo_ext = min(b[2] for b in win)
            up_break = hi_ext - float(or_high)
            down_break = float(or_low) - lo_ext
            if up_break <= 0.0 and down_break <= 0.0:
                return  # no break attempt this window — nothing to evaluate
            direction = "UP" if up_break >= down_break else "DOWN"
            fe = self._features(win, float(or_low), float(or_high), direction)
            model = self._models.setdefault(underlying, TrapProbabilityModel())
            obs = TrapObservation(
                underlying_symbol=underlying, timestamp=timestamp,
                attempted_direction=direction, put_call_ratio=1.0,
                atm_implied_volatility=0.12, implied_volatility_change=0.0,
                net_gamma_exposure=0.0, net_delta_exposure=0.0, net_vanna_exposure=0.0,
                net_charm_exposure=0.0, total_premium_oi_notional=0.0,
                net_time_decay_per_day=0.0, **fe,
            )
            prob = model.predict(observation=obs).trap_probability
            rec = {
                "ts": timestamp.astimezone(timezone.utc).isoformat(),
                "underlying": underlying,
                "attempted_direction": direction,
                "trap_probability": round(prob, 4),
                "breakout_extent_pct": round(fe["breakout_extent_pct"], 4),
                "reclaim_distance_pct": round(fe["reclaim_distance_pct"], 2),
                "close_back_inside_range": fe["close_back_inside_range"],
                "wick_rejection_pct": round(fe["wick_rejection_pct"], 2),
                "spot": round(float(close), 2),
                "or_low": round(float(or_low), 2),
                "or_high": round(float(or_high), 2),
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            pass  # shadow must NEVER disturb the trade path

    @staticmethod
    def _features(win, or_lo: float, or_hi: float, direction: str) -> dict:
        cur = win[-1]
        c = cur[3]
        ref = or_hi if direction == "UP" else or_lo
        ext = max(b[1] for b in win) if direction == "UP" else min(b[2] for b in win)
        span = abs(ext - ref) or 1e-9
        reclaim = max(0.0, (ext - c) if direction == "UP" else (c - ext)) / span * 100.0
        inside = or_lo <= c <= or_hi
        eb = max(win, key=lambda b: b[1]) if direction == "UP" else min(win, key=lambda b: b[2])
        rng = (eb[1] - eb[2]) or 1e-9
        wick = (eb[1] - max(eb[0], eb[3])) if direction == "UP" else (min(eb[0], eb[3]) - eb[2])
        vols = [b[4] for b in win]
        vr = (cur[4] / (sum(vols) / len(vols))) if sum(vols) > 0 else 1.0
        moves = [win[i][3] - win[i - 1][3] for i in range(1, len(win))]
        persist = sum(1 for m in moves if (m > 0) == (direction == "UP")) / max(len(moves), 1)
        net = abs(win[-1][3] - win[0][3])
        path = sum(abs(m) for m in moves) or 1e-9
        clv = (cur[3] - cur[2]) / ((cur[1] - cur[2]) or 1e-9)
        return dict(
            reference_level=ref, breakout_extent_pct=abs(ext - ref) / ref * 100.0,
            reclaim_distance_pct=reclaim, close_back_inside_range=inside,
            wick_rejection_pct=max(0.0, wick) / rng * 100.0, volume_ratio=vr,
            directional_persistence=persist, trend_efficiency=net / path,
            realized_volatility_ratio=1.0, range_compression=0.5, close_location_value=clv,
        )


__all__ = ["TrapShadow"]
