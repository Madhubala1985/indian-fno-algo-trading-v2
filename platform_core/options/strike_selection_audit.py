"""
platform_core.options.strike_selection_audit
═══════════════════════════════════════════════════════════════════════
Observability layer for strike selection decisions.

Logs every strike selection to:
  1. A single concise terminal line (non-blocking, low visual weight)
  2. logs/strike_selection_audit.jsonl — append-only JSONL, one record
     per selection event

This module has NO effect on trading behavior.  It never raises
exceptions into calling code; every failure is silently swallowed so
that audit errors cannot interrupt the live loop.

Integration in live_structure_pipeline.py:
    After _selection_result = _strike_engine.select(...)
    _strike_auditor.log(
        result        = _selection_result,
        candidates    = list(options_ctx.strike_candidates),
        feed_symbol   = completed_bar.symbol,
        strike_step   = _LOT_SIZE,   # used only for ATM computation
    )

Warning flags emitted in the JSONL record:
  SAME_STRIKE_AS_PREVIOUS   — winner matches previous bar's winner
  SCORE_MARGIN_LOW          — margin between rank-1 and rank-2 < 0.05
  FACTOR_DATA_MISSING       — OI=0 or bid/ask absent on winner
  OI_ZERO_OR_MISSING        — winner OI is 0
  BID_ASK_WIDE              — winner spread > 6 %
  LOT_SIZE_MISMATCH         — lot_size on candidate != expected
  SELECTOR_EFFECTIVELY_DELTA_ONLY — delta_fitness dominates (> 0.6 of
                                    score) and all other factors < 0.2
  TOP_CANDIDATES_TOO_CLOSE  — top-5 scores within 0.03 of each other
  ADJACENT_STRIKE_SCORED    — an adjacent strike (±1 step) also in top-5
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from platform_core.options.strike_selection import (
        StrikeCandidate,
        StrikeRecommendation,
        StrikeSelectionResult,
    )

_AUDIT_PATH = Path("logs/strike_selection_audit.jsonl")
_SCORE_MARGIN_THRESHOLD   = 0.05
_SPREAD_WIDE_THRESHOLD    = 6.0   # percent
_DELTA_DOMINANCE_FRACTION = 0.60  # delta_fitness / composite > this → DELTA_ONLY
_TOP_CLOSE_THRESHOLD      = 0.03


class StrikeAuditLogger:
    """
    Stateful audit logger.  Tracks previous selections per underlying for
    SAME_STRIKE_AS_PREVIOUS detection.  One instance should be shared across
    all calls within a process.

    Parameters
    ----------
    audit_path : Path or None
        JSONL output file.  Defaults to _AUDIT_PATH (live audit log).
    mode : str
        "live" (default) or "shadow".  Controls terminal prefix and the
        ``mode`` field written to JSONL records.
    """

    def __init__(self, *, audit_path: Path | None = None, mode: str = "live") -> None:
        self._previous: dict[str, float | None] = {}  # underlying → prev strike
        self._audit_path = audit_path or _AUDIT_PATH
        self._mode = mode

    def log(
        self,
        *,
        result: "StrikeSelectionResult",
        candidates: list["StrikeCandidate"],
        feed_symbol: str,
        strike_step: float = 100.0,
    ) -> None:
        """
        Log a completed strike selection.  Never raises.

        Parameters
        ----------
        result       : StrikeSelectionResult from StrikeSelectionEngine.select()
        candidates   : the same list[StrikeCandidate] passed to select()
        feed_symbol  : the WebSocket symbol (e.g. "BANKNIFTY") for display
        strike_step  : used only to identify adjacent strikes in the audit
        """
        try:
            self._log_inner(
                result=result,
                candidates=candidates,
                feed_symbol=feed_symbol,
                strike_step=strike_step,
            )
        except Exception:
            pass  # audit must never crash the live loop

    def _log_inner(
        self,
        *,
        result: "StrikeSelectionResult",
        candidates: list["StrikeCandidate"],
        feed_symbol: str,
        strike_step: float,
    ) -> None:
        best = result.best()
        if best is None:
            return

        underlying = best.underlying_symbol

        # Build candidate lookup for bid/ask/volume (not on StrikeRecommendation)
        cand_map: dict[str, "StrikeCandidate"] = {
            c.option_symbol: c for c in candidates
        }
        best_cand = cand_map.get(best.option_symbol)

        # ── Warning flags ──────────────────────────────────────────────────────
        flags: list[str] = []

        prev_strike = self._previous.get(underlying)
        if prev_strike is not None and abs(best.strike_price - prev_strike) < 1e-6:
            flags.append("SAME_STRIKE_AS_PREVIOUS")

        recs = result.recommendations
        score_margin: float | None = None
        if len(recs) >= 2:
            score_margin = recs[0].composite_score - recs[1].composite_score
            if score_margin < _SCORE_MARGIN_THRESHOLD:
                flags.append("SCORE_MARGIN_LOW")

        oi_zero = best.open_interest == 0
        if oi_zero:
            flags.append("OI_ZERO_OR_MISSING")

        no_bid_ask = best_cand is None or (
            best_cand.bid_price is None or best_cand.ask_price is None
        )
        if no_bid_ask:
            flags.append("FACTOR_DATA_MISSING")
        elif best_cand is not None:
            spread = best_cand.bid_ask_spread_pct
            if spread is not None and spread > _SPREAD_WIDE_THRESHOLD:
                flags.append("BID_ASK_WIDE")

        # Delta-only dominance
        bd = best.breakdown
        total_weight = (
            bd.delta_fitness + bd.oi_support + bd.oi_activity
            + bd.gex_proximity + bd.iv_attractiveness + bd.liquidity
            + bd.max_pain_distance + bd.theta_efficiency
        )
        if total_weight > 0 and bd.delta_fitness / total_weight > _DELTA_DOMINANCE_FRACTION:
            flags.append("SELECTOR_EFFECTIVELY_DELTA_ONLY")

        # Top-candidates too close
        if len(recs) >= 2:
            top_scores = [r.composite_score for r in recs[:5]]
            if max(top_scores) - min(top_scores) < _TOP_CLOSE_THRESHOLD:
                flags.append("TOP_CANDIDATES_TOO_CLOSE")

        # Adjacent strike also in top-5
        top_strikes = {r.strike_price for r in recs[:5]}
        adj_up   = best.strike_price + strike_step
        adj_down = best.strike_price - strike_step
        if adj_up in top_strikes or adj_down in top_strikes:
            flags.append("ADJACENT_STRIKE_SCORED")

        # ATM distance
        atm = round(result.spot_price / strike_step) * strike_step if strike_step > 0 else None
        atm_dist = (best.strike_price - atm) if atm is not None else None

        # ── Terminal line ──────────────────────────────────────────────────────
        margin_str  = f"margin={score_margin:.3f}" if score_margin is not None else "margin=n/a"
        dist_str    = (f"dist={atm_dist:+.0f}" if atm_dist is not None else "dist=n/a")
        factor_tops = self._dominant_factors(bd)
        flag_str    = f" [{','.join(flags)}]" if flags else ""
        if self._mode == "shadow":
            print(
                f"[SHADOW STRIKE] {feed_symbol} {result.bias} "
                f"selected {best.option_symbol}  "
                f"score={best.composite_score:.3f}  {margin_str}{flag_str}"
            )
        else:
            print(
                f"  [STRIKE AUDIT] {feed_symbol} {result.bias} "
                f"selected {best.option_symbol} "
                f"score={best.composite_score:.3f} {margin_str} "
                f"ATM={atm:.0f} {dist_str} "
                f"reason={factor_tops}{flag_str}"
            )

        # ── JSONL record ───────────────────────────────────────────────────────
        record = {
            "ts":               datetime.now(timezone.utc).isoformat(),
            "mode":             self._mode,
            "feed_symbol":      feed_symbol,
            "underlying":       underlying,
            "bias":             result.bias,
            "spot":             result.spot_price,
            "atm_strike":       atm,
            "expiry":           best.expiry_date.isoformat(),
            "dte":              best.days_to_expiry,
            "selected_symbol":  best.option_symbol,
            "strike":           best.strike_price,
            "option_type":      best.option_type,
            "premium":          best.last_traded_price,
            "delta":            best.delta,
            "gamma":            best.gamma,
            "theta":            best.theta,
            "iv":               best.implied_volatility,
            "oi":               best.open_interest,
            "lot_size":         best.lot_size,
            "composite_score":  best.composite_score,
            "score_margin":     score_margin,
            "atm_dist":         atm_dist,
            "rank2_score":      recs[1].composite_score if len(recs) >= 2 else None,
            "factor_scores": {
                "delta_fitness":      bd.delta_fitness,
                "oi_support":         bd.oi_support,
                "oi_activity":        bd.oi_activity,
                "gex_proximity":      bd.gex_proximity,
                "iv_attractiveness":  bd.iv_attractiveness,
                "liquidity":          bd.liquidity,
                "max_pain_distance":  bd.max_pain_distance,
                "theta_efficiency":   bd.theta_efficiency,
                "composite":          bd.composite_score,
            },
            "bid":      float(best_cand.bid_price)  if best_cand and best_cand.bid_price  is not None else None,
            "ask":      float(best_cand.ask_price)  if best_cand and best_cand.ask_price  is not None else None,
            "volume":   best_cand.volume             if best_cand else None,
            "spread_pct": (
                best_cand.bid_ask_spread_pct
                if best_cand and best_cand.bid_ask_spread_pct is not None else None
            ),
            "top5": [
                {
                    "rank":   r.rank,
                    "symbol": r.option_symbol,
                    "strike": r.strike_price,
                    "score":  r.composite_score,
                    "delta":  r.delta,
                    "iv":     r.implied_volatility,
                    "oi":     r.open_interest,
                }
                for r in recs[:5]
            ],
            "flags":         flags,
            "candidates_in": result.candidates_evaluated,
            "candidates_ok": result.candidates_passed_filters,
        }

        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        # Update previous-strike state
        self._previous[underlying] = best.strike_price

    @staticmethod
    def _dominant_factors(bd) -> str:
        """Return a '+'-joined list of the top-2 scoring factor names."""
        scores = {
            "delta":    bd.delta_fitness,
            "oi_supp":  bd.oi_support,
            "oi_act":   bd.oi_activity,
            "gex":      bd.gex_proximity,
            "iv":       bd.iv_attractiveness,
            "liq":      bd.liquidity,
            "maxpain":  bd.max_pain_distance,
            "theta":    bd.theta_efficiency,
        }
        top2 = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:2]
        return "+".join(k for k, _ in top2)


# Module-level singleton — import and use directly
_strike_auditor = StrikeAuditLogger()


def log_selection(
    *,
    result: "StrikeSelectionResult",
    candidates: list["StrikeCandidate"],
    feed_symbol: str,
    strike_step: float = 100.0,
) -> None:
    """Convenience wrapper around the module-level StrikeAuditLogger."""
    _strike_auditor.log(
        result=result,
        candidates=candidates,
        feed_symbol=feed_symbol,
        strike_step=strike_step,
    )


__all__ = ["StrikeAuditLogger", "log_selection"]
