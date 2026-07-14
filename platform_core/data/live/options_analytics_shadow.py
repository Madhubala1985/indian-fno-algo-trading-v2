"""
options_analytics_shadow.py — SHADOW wiring of the 4 options-analytics engines
(GEX / OI / DealerHedging / PremiumBehavior), logging only, no trade effect.

These four engines are ALREADY computed every bar inside OptionsContext
(via OptionsContextBuilder → EngineRunner) but their results are discarded —
nothing gates on them. This module records, per option-context build, a compact
JSONL summary of each engine's signal to ``logs/options_analytics_shadow.jsonl``
so the research loop can join them to executed-trade outcomes and test whether
any of them separates winners from losers BEFORE any of them gates a real trade.

Observational only. ``observe()`` never raises; a shadow failure can never
disturb the live trade path. A per-engine non-null counter is tracked so we can
detect a silently-broken feed (the htf_shadow null-logging lesson) instead of
growing a corpus of nulls.
"""
from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path


def _f(v):
    """Coerce to float or None (JSON-safe, never raises)."""
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _pct_dist(spot, level):
    """Signed distance from spot to a strike level, as a fraction of spot."""
    try:
        if spot and level is not None and spot > 0:
            return (float(level) - float(spot)) / float(spot)
    except Exception:
        pass
    return None


class OptionsAnalyticsShadow:
    """Logs GEX/OI/Dealer/Premium engine summaries per bar — never affects trades."""

    def __init__(self, *, out_path: str = "logs/options_analytics_shadow.jsonl") -> None:
        self._out = Path(out_path)
        self.records = 0
        # per-engine non-null counters — surfaced in the shadow summary so a
        # silently-empty feed is caught early rather than growing null records.
        self.nonnull = {"gex": 0, "oi": 0, "dealer": 0, "premium": 0}

    def observe(self, *, underlying: str, timestamp, spot=None,
                exec_bias=None, options_ctx=None) -> None:
        """Record the 4 engine snapshots for one option-context build. NEVER raises."""
        try:
            gex = getattr(options_ctx, "gex_snapshot", None)
            oi = getattr(options_ctx, "oi_snapshot", None)
            dlr = getattr(options_ctx, "dealer_hedging_snapshot", None)
            prem = getattr(options_ctx, "premium_behavior_snapshot", None)

            spot_f = _f(spot)
            rec = {
                "ts": self._ts(timestamp),
                "underlying": underlying,
                "spot": spot_f,
                "exec_bias": str(exec_bias) if exec_bias is not None else None,
            }

            # ── GEX (dealer gamma walls / regime) ──────────────────────────────
            if gex is not None:
                flip = _f(getattr(gex, "gamma_flip_strike", None))
                call_wall = _f(getattr(gex, "call_wall_strike", None))
                put_wall = _f(getattr(gex, "put_wall_strike", None))
                rec.update(
                    gex_total_gamma=_f(getattr(gex, "total_gamma_exposure", None)),
                    gex_call_gamma=_f(getattr(gex, "call_gamma_exposure", None)),
                    gex_put_gamma=_f(getattr(gex, "put_gamma_exposure", None)),
                    gex_regime=getattr(gex, "net_gamma_regime", None),
                    gex_flip_strike=flip,
                    gex_call_wall=call_wall,
                    gex_put_wall=put_wall,
                    gex_dominant_strike=_f(getattr(gex, "dominant_gamma_strike", None)),
                    # derived: where is spot relative to the flip + the walls
                    gex_spot_vs_flip=(None if (flip is None or spot_f is None)
                                      else ("ABOVE" if spot_f >= flip else "BELOW")),
                    gex_dist_call_wall=_pct_dist(spot_f, call_wall),
                    gex_dist_put_wall=_pct_dist(spot_f, put_wall),
                )
                self.nonnull["gex"] += 1

            # ── OI (put/call ratios + build-up) ────────────────────────────────
            if oi is not None:
                rec.update(
                    oi_pcr=_f(getattr(oi, "put_call_ratio_open_interest", None)),
                    oi_pcr_change=_f(getattr(oi, "put_call_ratio_change_in_open_interest", None)),
                    oi_pcr_volume=_f(getattr(oi, "put_call_ratio_volume", None)),
                    oi_total_call=_f(getattr(oi, "total_call_open_interest", None)),
                    oi_total_put=_f(getattr(oi, "total_put_open_interest", None)),
                    oi_call_chg=_f(getattr(oi, "total_call_change_in_open_interest", None)),
                    oi_put_chg=_f(getattr(oi, "total_put_change_in_open_interest", None)),
                )
                self.nonnull["oi"] += 1

            # ── Dealer hedging (net Greeks the dealer must hedge) ──────────────
            if dlr is not None:
                rec.update(
                    dlr_net_delta=_f(getattr(dlr, "net_delta_exposure", None)),
                    dlr_net_gamma=_f(getattr(dlr, "net_gamma_exposure", None)),
                    dlr_net_vanna=_f(getattr(dlr, "net_vanna_exposure", None)),
                    dlr_net_charm=_f(getattr(dlr, "net_charm_exposure", None)),
                    dlr_flip_strike=_f(getattr(dlr, "gamma_flip_strike", None)),
                )
                self.nonnull["dealer"] += 1

            # ── Premium behaviour (extrinsic value distribution) ───────────────
            if prem is not None:
                rec.update(
                    prem_oi_notional=_f(getattr(prem, "total_premium_oi_notional", None)),
                    prem_intrinsic=_f(getattr(prem, "total_intrinsic_value", None)),
                    prem_extrinsic=_f(getattr(prem, "total_extrinsic_value", None)),
                    prem_call_extr_share=_f(getattr(prem, "call_extrinsic_share", None)),
                    prem_put_extr_share=_f(getattr(prem, "put_extrinsic_share", None)),
                    prem_atm_extr_share=_f(getattr(prem, "atm_extrinsic_share", None)),
                )
                self.nonnull["premium"] += 1

            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            # SHADOW path must NEVER affect live trading.
            pass

    @staticmethod
    def _ts(timestamp) -> str:
        try:
            return timestamp.astimezone(timezone.utc).isoformat()
        except Exception:
            return str(timestamp)
