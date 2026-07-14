"""
shadow_feature_kit.py — shared causal feature computation for the ML-classifier
shadows (continuation + reversal). Both classifiers consume the same price/
structure + options-flow features; keeping the math in ONE place avoids the
maintenance hazard of two subtly-diverging copies.

Pure functions, no side effects, never raise on bad input (return safe defaults /
None). Every helper defensively clips to the validator ranges the classifier
observations require, so building an observation from these never trips a
ValueError.
"""
from __future__ import annotations

from math import isfinite


def clip(v, lo, hi):
    try:
        v = float(v)
        if not isfinite(v):
            return lo
        return lo if v < lo else hi if v > hi else v
    except Exception:
        return lo


def clip01(v):
    return clip(v, 0.0, 1.0)


def num(v, default=0.0):
    try:
        v = float(v)
        return v if isfinite(v) else default
    except Exception:
        return default


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def compute_price_structure_features(bars, direction, or_high=None, or_low=None):
    """Return (features_dict, defaulted_list) or (None, reason).

    direction is 'UP' or 'DOWN'. features_dict keys: trend_score, trend_efficiency,
    directional_persistence, realized_volatility_ratio, range_compression,
    close_location_value, volume_ratio, breakout_hold_pct, pullback_shallow_pct.
    """
    if direction not in ("UP", "DOWN"):
        return None, "no_direction"
    if not bars or len(bars) < 6:
        return None, "insufficient_bars"
    w = bars[-20:] if len(bars) >= 20 else list(bars)
    closes = [num(x.get("close")) for x in w]
    highs = [num(x.get("high")) for x in w]
    lows = [num(x.get("low")) for x in w]
    vols = [max(0.0, num(x.get("volume"))) for x in w]
    if any(c <= 0 for c in closes):
        return None, "bad_closes"

    net = closes[-1] - closes[0]
    trend_score = net / closes[0]
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    path = sum(abs(d) for d in diffs)
    trend_efficiency = clip01(abs(net) / path) if path > 0 else 0.0
    if abs(net) < 1e-12 or not diffs:
        directional_persistence = 0.5
    else:
        nd_up = net > 0
        agree = sum(1 for d in diffs if d != 0 and (d > 0) == nd_up)
        directional_persistence = clip01(agree / len(diffs))
    rets = [diffs[i] / closes[i] for i in range(len(diffs))] if diffs else [0.0]
    base = _std(rets)
    recent = _std(rets[-5:]) if len(rets) >= 5 else base
    realized_volatility_ratio = clip(recent / base, 0.0, 10.0) if base > 0 else 1.0
    kk = min(5, len(w))
    range_compression = clip(
        sum((highs[-i] - lows[-i]) / closes[-i] for i in range(1, kk + 1)) / kk, 0.0, 1.0
    )
    wl, wh = min(lows), max(highs)
    close_location_value = clip(2.0 * (closes[-1] - wl) / (wh - wl) - 1.0, -1.0, 1.0) if wh > wl else 0.0
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    volume_ratio = clip(vols[-1] / avg_vol, 0.0, 10.0) if avg_vol > 0 else 1.0

    defaulted = []
    K = min(10, len(w))
    lvl = or_high if direction == "UP" else or_low
    if lvl is not None and num(lvl) > 0:
        lvl = num(lvl)
        held = sum(1 for c in closes[-K:] if (c > lvl if direction == "UP" else c < lvl))
        breakout_hold_pct = clip01(held / K)
    else:
        breakout_hold_pct = 0.5
        defaulted.append("breakout_hold_pct")
    if avg_vol <= 0:
        defaulted.append("volume_ratio")

    rng = max(wh - wl, 1e-9)
    if direction == "UP":
        ext = closes[0]; dd = 0.0
        for c in closes:
            ext = max(ext, c); dd = max(dd, ext - c)
    else:
        ext = closes[0]; dd = 0.0
        for c in closes:
            ext = min(ext, c); dd = max(dd, c - ext)
    pullback_shallow_pct = clip01(1.0 - dd / rng)

    feats = {
        "trend_score": num(trend_score),
        "trend_efficiency": trend_efficiency,
        "directional_persistence": directional_persistence,
        "realized_volatility_ratio": realized_volatility_ratio,
        "range_compression": range_compression,
        "close_location_value": close_location_value,
        "volume_ratio": volume_ratio,
        "breakout_hold_pct": breakout_hold_pct,
        "pullback_shallow_pct": pullback_shallow_pct,
    }
    return feats, defaulted


def extract_option_flow(dealer=None, premium=None, oi=None, atm_iv=None, prev_atm_iv=None):
    """Return (flow_dict, defaulted_list). Raw snapshot values — the classifiers
    apply direction internally."""
    defaulted = []
    if dealer is None:
        defaulted += ["net_gamma_exposure", "net_delta_exposure", "net_vanna_exposure", "net_charm_exposure"]
    if premium is None:
        defaulted += ["total_premium_oi_notional", "total_extrinsic_value", "net_time_decay_per_day"]
    if oi is None:
        defaulted += ["put_call_ratio"]
    if prev_atm_iv is None:
        defaulted += ["implied_volatility_change"]
    pcr = num(getattr(oi, "put_call_ratio_open_interest", None), 1.0)
    flow = {
        "net_gamma_exposure": num(getattr(dealer, "net_gamma_exposure", 0.0)),
        "net_delta_exposure": num(getattr(dealer, "net_delta_exposure", 0.0)),
        "net_vanna_exposure": num(getattr(dealer, "net_vanna_exposure", 0.0)),
        "net_charm_exposure": num(getattr(dealer, "net_charm_exposure", 0.0)),
        "total_premium_oi_notional": max(0.0, num(getattr(premium, "total_premium_oi_notional", 0.0))),
        "total_extrinsic_value": max(0.0, num(getattr(premium, "total_extrinsic_value", 0.0))),
        "net_time_decay_per_day": num(getattr(premium, "net_time_decay_per_day", 0.0)),
        "put_call_ratio": pcr if pcr > 0 else 1.0,
        "implied_volatility_change": (num(atm_iv) - num(prev_atm_iv)) if prev_atm_iv is not None else 0.0,
    }
    return flow, defaulted
