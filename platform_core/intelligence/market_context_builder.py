# FILE: platform_core/intelligence/market_context_builder.py
"""
platform_core.intelligence.market_context_builder
═══════════════════════════════════════════════════════════════════════
Multi-timeframe market context builder for the options decision engine.

Replaces the hardcoded bias="LONG" / bias="SHORT" pattern with a
genuine MarketContext derived from bar history across multiple timeframes.

Existing components reused (no rewriting):
  - ml/regime_classifier.py : RegimeClassifier, FinalizedBar
      Per-timeframe regime and direction classification using trend
      efficiency, realized volatility, range compression, and directional
      persistence over a rolling bar window.
  - data/bar_builder.py : LiveBar
      Existing bar type throughout the system. Converted to FinalizedBar
      internally.

The builder maintains one RegimeClassifier per timeframe. Bars are fed
via update(); MarketContext is produced via build().

Supported timeframes: "1m", "15m", "1h", "4h", "1d"

Bias fusion algorithm:
  For each timeframe with sufficient history:
    - Extract DirectionState (BULLISH/BEARISH/NEUTRAL) and confidence
      from RegimeClassification.
    - Apply timeframe weight (daily > 4h > 1h > 15m > 1m).
    - Compute weighted directional score in [-1, +1].

  If score > LONG_THRESHOLD  → LONG
  If score < SHORT_THRESHOLD → SHORT
  Else                       → NEUTRAL

  Confidence = min(|score| / threshold, 1.0) × regime quality

Output directly feeds StrikeSelectionEngine.select(bias=context.bias):
  MarketBias is a StrEnum → context.bias == "LONG" is True.

Charter timeframe hierarchy preserved:
  Daily  → Macro bias
  H4     → Institutional trend
  H1     → Market structure
  M15    → Execution zone
  M1     → Trigger confirmation weight only (minimal weight)
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import unique
from math import fabs
from statistics import fmean
from typing import Deque

from platform_core.data.bar_builder import LiveBar
from platform_core.ml.regime_classifier import (
    RegimeClassifier,
    RegimeClassifierConfig,
    RegimeClassification,
    FinalizedBar,
    DirectionState,
    VolatilityState,
    MarketRegime,
)


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

# ── Supported timeframes ───────────────────────────────────────────────────────

SUPPORTED_TIMEFRAMES = frozenset({"1m", "15m", "1h", "4h", "1d"})

# Timeframe weights (must sum to 1.0; daily carries most weight)
_TF_WEIGHTS: dict[str, float] = {
    "1d":  0.35,
    "4h":  0.25,
    "1h":  0.20,
    "15m": 0.15,
    "1m":  0.05,
}

# Directional score thresholds
_LONG_THRESHOLD:  float = 0.18   # net weighted bullish score to confirm LONG
_SHORT_THRESHOLD: float = -0.18  # net weighted bearish score to confirm SHORT

# ── Graded confidence (de-saturation) ────────────────────────────────────────
# The legacy confidence = clip(|trend_score| / 0.18) PEGS at 1.0 for ANY confirmed
# signal (|trend_score| >= threshold), discarding the strength signal — measured
# 100% of trades saturate at conf=1.0 while trend_score spans 0.18..0.9 AND
# separates winners (0.565) from losers (0.383). Graded mode maps the confirmed
# range [threshold, 1.0] -> [floor, 1.0] so confidence-scaled sizing can put more
# on strong setups and less on marginal ones. Bias itself is unaffected (it keys on
# trend_score, not confidence). OFF by default (MCB_GRADED_CONFIDENCE=1 enables) so
# live is unchanged until proven on the batch. Floor 0.5 stays above the live entry
# min_confidence gates (Tier2 0.40), so the trade SET does not change — only sizing.
_GRADED_CONFIDENCE: bool = str(os.environ.get("MCB_GRADED_CONFIDENCE", "")).strip().lower() \
    in ("1", "true", "yes", "on")
_CONF_FLOOR: float = float(os.environ.get("MCB_CONF_FLOOR", "0.5") or 0.5)


# ── Public types ───────────────────────────────────────────────────────────────

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


@unique
class MarketBias(StrEnum):
    """Directional bias for StrikeSelectionEngine.select(bias=...)."""
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class TimeframeAnalysis:
    """Per-timeframe regime classification summary."""

    timeframe: str
    bars_seen: int
    history_ready: bool

    regime: str             # MarketRegime.value
    direction: str          # DirectionState.value  BULLISH/BEARISH/NEUTRAL
    volatility: str         # VolatilityState.value LOW/NORMAL/HIGH
    trend_score: float      # [-1, +1]: positive=bullish, negative=bearish
    trend_efficiency: float # [0, 1]: how directional the recent price path is
    confidence: float       # [0, 1]: classifier confidence in this classification

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "timeframe": self.timeframe,
            "bars_seen": self.bars_seen,
            "history_ready": self.history_ready,
            "regime": self.regime,
            "direction": self.direction,
            "volatility": self.volatility,
            "trend_score": self.trend_score,
            "trend_efficiency": self.trend_efficiency,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class MarketContext:
    """
    Rich multi-timeframe market context for the options decision engine.

    bias is a MarketBias StrEnum — it IS a str, so:
        strike_engine.select(bias=context.bias, ...)
    works without conversion.

    confidence is in [0, 1]. Values below 0.3 indicate weak directional
    conviction and should generally result in reduced position sizing.
    """

    underlying_symbol: str
    valuation_timestamp: datetime

    bias: MarketBias     # LONG / SHORT / NEUTRAL
    confidence: float    # [0, 1]

    # Per-timeframe analysis (None when no bars have been fed for that TF)
    daily:  TimeframeAnalysis | None
    tf_4h:  TimeframeAnalysis | None
    tf_1h:  TimeframeAnalysis | None
    tf_15m: TimeframeAnalysis | None
    tf_1m:  TimeframeAnalysis | None

    # Aggregate scores
    trend_score: float      # [-1, +1] weighted directional score across TFs
    momentum_score: float   # [0, 1] agreement across timeframes
    regime_score: float     # [0, 1] average regime classification confidence

    # Regime summary (from primary timeframe — 4h/1h when available)
    volatility_regime: str    # "HIGH_VOLATILITY" / "NORMAL" / "LOW_VOLATILITY"
    liquidity_regime: str     # "HIGH_LIQUIDITY" / "NORMAL" / "LOW_LIQUIDITY"

    timeframes_with_history: tuple[str, ...]
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "valuation_timestamp": _ensure_utc(self.valuation_timestamp).isoformat(),
            "bias": self.bias.value,
            "confidence": self.confidence,
            "trend_score": self.trend_score,
            "momentum_score": self.momentum_score,
            "regime_score": self.regime_score,
            "volatility_regime": self.volatility_regime,
            "liquidity_regime": self.liquidity_regime,
            "timeframes_with_history": list(self.timeframes_with_history),
            "daily": self.daily.to_dict() if self.daily else None,
            "tf_4h": self.tf_4h.to_dict() if self.tf_4h else None,
            "tf_1h": self.tf_1h.to_dict() if self.tf_1h else None,
            "tf_15m": self.tf_15m.to_dict() if self.tf_15m else None,
            "tf_1m": self.tf_1m.to_dict() if self.tf_1m else None,
            "metadata": self.metadata,
        }


# ── Builder ────────────────────────────────────────────────────────────────────

class MarketContextBuilder:
    """
    Stateful multi-timeframe market context builder.

    Maintains one RegimeClassifier per supported timeframe and updates it
    incrementally as bars arrive. Call build() at any time to get the
    current MarketContext.

    Usage (backtest):
        builder = MarketContextBuilder(underlying_symbol="BANKNIFTY")
        for bar in bars_1m:
            builder.update(bar=bar, timeframe="1m")
        for bar in bars_15m:
            builder.update(bar=bar, timeframe="15m")
        context = builder.build()
        result = strike_engine.select(
            bias=context.bias,          # StrEnum → directly usable as str
            candidates=...,
            ...
        )

    Usage (live):
        builder.update(bar=current_1m_bar, timeframe="1m")
        # Repeat for each aggregated TF bar when available
        context = builder.build()
    """

    def __init__(
        self,
        *,
        underlying_symbol: str,
        min_history_bars: int = 24,
    ) -> None:
        if not underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        self._underlying = underlying_symbol
        self._min_history = min_history_bars

        # One RegimeClassifier per timeframe
        rc_config = RegimeClassifierConfig(min_history_bars=min_history_bars)
        self._classifiers: dict[str, RegimeClassifier] = {
            tf: RegimeClassifier(config=rc_config)
            for tf in SUPPORTED_TIMEFRAMES
        }

        # Latest classification per timeframe (None until first update)
        self._latest: dict[str, RegimeClassification] = {}

        # Bar counts per timeframe
        self._bar_counts: dict[str, int] = {tf: 0 for tf in SUPPORTED_TIMEFRAMES}

        # Recent volume deque for liquidity estimation (from 1m bars)
        self._recent_volumes: Deque[float] = deque(maxlen=60)

        # Bars the classifier rejected (stale/duplicate timestamp, validation),
        # per timeframe. A silently-frozen classifier looks identical to a live
        # one from the outside — this counter is the only tell.
        self._rejected_counts: dict[str, int] = {tf: 0 for tf in SUPPORTED_TIMEFRAMES}

    def update(self, *, bar: LiveBar, timeframe: str) -> bool:
        """
        Feed a finalized bar into the appropriate timeframe classifier.

        Returns True when the bar was accepted by the classifier, False when
        it was rejected (unsupported timeframe, stale/non-increasing timestamp,
        or validation failure).

        Parameters
        ----------
        bar:
            A LiveBar from bar_builder.py (or HistoricalReplayFeed).
        timeframe:
            One of: "1m", "15m", "1h", "4h", "1d".
        """
        tf = _normalise_tf(timeframe)
        if tf not in SUPPORTED_TIMEFRAMES:
            return False  # silently ignore unsupported timeframes

        finalized = _live_to_finalized(bar, self._underlying)
        accepted = True
        try:
            classification = self._classifiers[tf].update(bar=finalized)
            self._latest[tf] = classification
            self._bar_counts[tf] = self._bar_counts.get(tf, 0) + 1
        except Exception:
            # stale timestamp or validation failure — skip, but count it
            self._rejected_counts[tf] = self._rejected_counts.get(tf, 0) + 1
            accepted = False

        if tf == "1m" and bar.volume >= 0.0:
            self._recent_volumes.append(bar.volume)
        return accepted

    def reset(self, timeframe: str | None = None) -> None:
        """Reset one or all timeframe classifiers."""
        tfs = [_normalise_tf(timeframe)] if timeframe else list(SUPPORTED_TIMEFRAMES)
        for tf in tfs:
            if tf in self._classifiers:
                self._classifiers[tf].reset()
                self._latest.pop(tf, None)
                self._bar_counts[tf] = 0
                self._rejected_counts[tf] = 0

    def build(
        self,
        *,
        valuation_timestamp: datetime | None = None,
    ) -> MarketContext:
        """
        Compute a MarketContext from the current bar history.

        Returns NEUTRAL with confidence=0.0 when no timeframe has
        sufficient history.
        """
        effective_ts = _ensure_utc(valuation_timestamp) if valuation_timestamp else _utc_now()

        # ── Collect per-timeframe analysis ─────────────────────────────────────
        tf_map: dict[str, TimeframeAnalysis] = {}
        for tf in SUPPORTED_TIMEFRAMES:
            clf = self._latest.get(tf)
            if clf is None:
                continue
            tf_map[tf] = TimeframeAnalysis(
                timeframe=tf,
                bars_seen=self._bar_counts.get(tf, 0),
                history_ready=clf.metadata.get("history_ready", False),
                regime=clf.regime.value,
                direction=clf.direction_state.value,
                volatility=clf.volatility_state.value,
                trend_score=clf.features.trend_score,
                trend_efficiency=clf.features.trend_efficiency,
                confidence=clf.confidence,
            )

        # ── Compute weighted directional score ─────────────────────────────────
        trend_score, confidence, regime_score, momentum_score = _fuse_directions(tf_map)

        # ── Determine bias ─────────────────────────────────────────────────────
        if trend_score > _LONG_THRESHOLD:
            bias = MarketBias.LONG
        elif trend_score < _SHORT_THRESHOLD:
            bias = MarketBias.SHORT
        else:
            bias = MarketBias.NEUTRAL

        # ── Volatility regime (primary TF: 4h > 1h > 15m > 1m > 1d) ──────────
        vol_regime = _derive_volatility_regime(tf_map)

        # ── Liquidity regime (from recent 1m volume) ───────────────────────────
        liq_regime = _derive_liquidity_regime(self._recent_volumes)

        # ── Select per-TF analysis objects for OptionsContext ─────────────────
        tfs_with_history = tuple(
            tf for tf, a in tf_map.items() if a.history_ready
        )

        return MarketContext(
            underlying_symbol=self._underlying,
            valuation_timestamp=effective_ts,
            bias=bias,
            confidence=confidence,
            daily=tf_map.get("1d"),
            tf_4h=tf_map.get("4h"),
            tf_1h=tf_map.get("1h"),
            tf_15m=tf_map.get("15m"),
            tf_1m=tf_map.get("1m"),
            trend_score=trend_score,
            momentum_score=momentum_score,
            regime_score=regime_score,
            volatility_regime=vol_regime,
            liquidity_regime=liq_regime,
            timeframes_with_history=tfs_with_history,
            metadata={
                "underlying": self._underlying,
                "bar_counts": {tf: self._bar_counts.get(tf, 0) for tf in SUPPORTED_TIMEFRAMES},
                "bar_rejections": {tf: n for tf, n in self._rejected_counts.items() if n},
                "timeframes_active": len(tf_map),
                "timeframes_with_history": len(tfs_with_history),
                "long_threshold": _LONG_THRESHOLD,
                "short_threshold": _SHORT_THRESHOLD,
            },
        )


# ── Private functions ──────────────────────────────────────────────────────────

def _normalise_tf(timeframe: str) -> str:
    """Normalise timeframe strings to canonical form."""
    mapping = {
        "MIN_1": "1m", "1min": "1m", "m1": "1m",
        "MIN_15": "15m", "15min": "15m", "m15": "15m",
        "HOUR_1": "1h", "60m": "1h", "h1": "1h",
        "HOUR_4": "4h", "240m": "4h", "h4": "4h",
        "DAY_1": "1d", "daily": "1d", "d1": "1d",
    }
    return mapping.get(timeframe, timeframe)


def _live_to_finalized(bar: LiveBar, underlying_symbol: str) -> FinalizedBar:
    """Convert LiveBar → FinalizedBar for RegimeClassifier."""
    return FinalizedBar(
        underlying_symbol=underlying_symbol,
        timestamp=bar.start_timestamp,
        open_price=bar.open_price,
        high_price=bar.high_price,
        low_price=bar.low_price,
        close_price=bar.close_price,
        volume=bar.volume,
    )


def _fuse_directions(
    tf_map: dict[str, TimeframeAnalysis]
) -> tuple[float, float, float, float]:
    """
    Fuse per-timeframe direction + confidence into aggregate scores.

    Returns: (trend_score, confidence, regime_score, momentum_score)
      trend_score   — weighted directional score in [-1, +1]
      confidence    — scaled [0, 1] from trend_score / threshold
      regime_score  — average classifier confidence across ready TFs
      momentum_score — agreement fraction across TFs (0=split, 1=unanimous)
    """
    weighted_bull = 0.0
    weighted_bear = 0.0
    weighted_neutral = 0.0
    regime_confidences: list[float] = []
    direction_signs: list[int] = []

    for tf, analysis in tf_map.items():
        if not analysis.history_ready:
            continue  # insufficient history — skip this TF's vote
        w = _TF_WEIGHTS.get(tf, 0.05)
        adj_w = w * max(analysis.confidence, 0.05)  # floor weight at 5% of base

        if analysis.direction == DirectionState.BULLISH.value:
            weighted_bull += adj_w
            direction_signs.append(1)
        elif analysis.direction == DirectionState.BEARISH.value:
            weighted_bear += adj_w
            direction_signs.append(-1)
        else:
            weighted_neutral += adj_w
            direction_signs.append(0)

        regime_confidences.append(analysis.confidence)

    total = weighted_bull + weighted_bear + weighted_neutral
    if total <= 0.0:
        return 0.0, 0.0, 0.0, 0.0

    trend_score = (weighted_bull - weighted_bear) / total

    # Confidence scaled to threshold
    max_threshold = max(_LONG_THRESHOLD, fabs(_SHORT_THRESHOLD))
    _at = fabs(trend_score)
    if _GRADED_CONFIDENCE:
        # Grade the confirmed range [threshold, 1.0] -> [floor, 1.0]; below threshold
        # (NEUTRAL, not traded) ramp 0 -> floor so it stays monotonic.
        if _at >= max_threshold:
            confidence = _clip01(
                _CONF_FLOOR + (_at - max_threshold) / (1.0 - max_threshold) * (1.0 - _CONF_FLOOR)
            )
        else:
            confidence = _clip01(_at / max_threshold * _CONF_FLOOR)
    else:
        confidence = _clip01(_at / max_threshold)   # legacy: saturates at threshold

    # Regime score — average classifier confidence
    regime_score = fmean(regime_confidences) if regime_confidences else 0.0

    # Momentum — fraction of non-neutral TFs that agree on direction
    non_neutral = [s for s in direction_signs if s != 0]
    if not non_neutral:
        momentum_score = 0.0
    else:
        dominant_sign = 1 if sum(non_neutral) >= 0 else -1
        agreements = sum(1 for s in non_neutral if s == dominant_sign)
        momentum_score = _clip01(agreements / len(non_neutral))

    return trend_score, confidence, regime_score, momentum_score


def _derive_volatility_regime(tf_map: dict[str, TimeframeAnalysis]) -> str:
    """Derive volatility regime from the most informative ready timeframe."""
    priority = ["4h", "1h", "15m", "1m", "1d"]
    for tf in priority:
        a = tf_map.get(tf)
        if a is not None and a.history_ready:
            if a.volatility == VolatilityState.HIGH.value:
                return "HIGH_VOLATILITY"
            if a.volatility == VolatilityState.LOW.value:
                return "LOW_VOLATILITY"
            return "NORMAL"
    return "NORMAL"


def _derive_liquidity_regime(recent_volumes: deque) -> str:
    """Estimate liquidity from recent 1m bar volume relative to its own median."""
    vols = list(recent_volumes)
    if len(vols) < 10:
        return "NORMAL"
    median_vol = sorted(vols)[len(vols) // 2]
    recent_avg = fmean(vols[-5:]) if len(vols) >= 5 else vols[-1]
    if median_vol <= 0.0:
        return "NORMAL"
    ratio = recent_avg / median_vol
    if ratio >= 1.5:
        return "HIGH_LIQUIDITY"
    if ratio <= 0.5:
        return "LOW_LIQUIDITY"
    return "NORMAL"


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "MarketBias",
    "MarketContext",
    "MarketContextBuilder",
    "TimeframeAnalysis",
]
