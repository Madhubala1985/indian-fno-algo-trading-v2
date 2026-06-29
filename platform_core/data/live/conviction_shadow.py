"""
conviction_shadow.py — transparent, graded setup-conviction score (SHADOW).

Replaces the saturated confidence signals (MCB always 1.00; the ML
ConfidenceEngine clips to 0.99 by default and needs the unwired options-Greeks
classifier stack) with a simple, auditable conviction score built ONLY from real
inputs already flowing through the pipeline at the entry decision.

By construction the weights sum to 1.0 and every component is clipped to [0,1],
so the output is a convex combination in [0,1] that CANNOT saturate — it grades.
This is Phase-2 of docs/intelligence_wiring_plan.md, done shadow-first: it logs a
conviction per decision to logs/conviction_shadow.jsonl and never touches the
trade path. The harness measures whether higher conviction actually wins more
before it is ever wired into sizing.

Components (all real, already computed):
  composite  0.35  strike actionability (overall quality of the chosen strike)
  margin     0.20  score_margin / MARGIN_FULL — how clearly this is the best strike
  liquidity  0.20  strike liquidity (tradeability)
  vol        0.15  realized_vol_ratio (IV/reference) — more expected movement is
                   better for option buying; the best days were higher-vol
  fvg        0.10  FVG setup quality
"""
from __future__ import annotations

import json
import os
from datetime import timezone
from pathlib import Path


def _clip01(x: float) -> float:
    try:
        x = float(x)
    except Exception:
        return 0.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class ConvictionScorer:
    """Pure, transparent conviction in [0,1] from real decision inputs."""

    W_COMPOSITE = float(os.environ.get("PAPER_CONV_W_COMPOSITE", "0.35") or 0.35)
    W_MARGIN = float(os.environ.get("PAPER_CONV_W_MARGIN", "0.20") or 0.20)
    W_LIQUIDITY = float(os.environ.get("PAPER_CONV_W_LIQUIDITY", "0.20") or 0.20)
    W_VOL = float(os.environ.get("PAPER_CONV_W_VOL", "0.15") or 0.15)
    W_FVG = float(os.environ.get("PAPER_CONV_W_FVG", "0.10") or 0.10)
    MARGIN_FULL = float(os.environ.get("PAPER_CONV_MARGIN_FULL", "0.10") or 0.10)
    VOL_CAP = 1.5  # realized_vol_ratio at/above this = full marks

    def score(
        self,
        *,
        composite: float,
        score_margin: float,
        liquidity: float,
        vol_ratio: float,
        fvg_quality: float,
    ) -> tuple[float, dict]:
        c = _clip01(composite)
        m = _clip01((score_margin or 0.0) / self.MARGIN_FULL)
        liq = _clip01(liquidity)
        v = _clip01(min(max(vol_ratio, 0.0), self.VOL_CAP) / self.VOL_CAP)
        f = _clip01(fvg_quality)
        conv = (
            self.W_COMPOSITE * c
            + self.W_MARGIN * m
            + self.W_LIQUIDITY * liq
            + self.W_VOL * v
            + self.W_FVG * f
        )
        return _clip01(conv), {
            "composite": round(c, 3), "margin": round(m, 3), "liquidity": round(liq, 3),
            "vol": round(v, 3), "fvg": round(f, 3),
        }


class ConvictionShadow:
    """Logs a transparent conviction per decision — never affects trades."""

    def __init__(self, *, out_path: str = "logs/conviction_shadow.jsonl") -> None:
        self._scorer = ConvictionScorer()
        self._out = Path(out_path)
        self.records = 0

    def observe(
        self,
        *,
        underlying: str,
        timestamp,
        composite: float,
        score_margin: float,
        liquidity: float,
        vol_ratio: float,
        fvg_quality: float,
        mcb_conf: float | None = None,
        effective_conf: float | None = None,
        bias: str | None = None,
    ) -> None:
        """Record the conviction for one entry decision. NEVER raises."""
        try:
            conv, parts = self._scorer.score(
                composite=composite, score_margin=score_margin, liquidity=liquidity,
                vol_ratio=vol_ratio, fvg_quality=fvg_quality,
            )
            try:
                ts = timestamp.astimezone(timezone.utc).isoformat()
            except Exception:
                ts = str(timestamp)
            rec = {
                "ts": ts, "underlying": underlying, "bias": bias,
                "conviction": round(conv, 3), "parts": parts,
                "mcb_conf": mcb_conf, "effective_conf": effective_conf,
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            pass  # shadow must never affect live trading
