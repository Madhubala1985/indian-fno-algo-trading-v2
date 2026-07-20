# FILE: platform_core/options/strike_selection.py
"""
platform_core.options.strike_selection
═══════════════════════════════════════════════════════════════════════
Production-grade strike and expiry selection engine for institutional
Indian index options.

This is the terminal decision layer of the options intelligence pipeline.
It converts a directional market bias and options analytics context into
a ranked list of specific option contract recommendations, each with
quantified multi-factor reasoning.

Pipeline position:
    OIAnalyticsEngine   ─┐
    GammaExposureEngine ─┤→ StrikeSelectionEngine → StrikeSelectionResult
    GreeksEngine output  ─┤    (ranked contracts with scores + reasoning)
    IV reference         ─┘

The charter objective is to produce output of the form:
    "Buy BANKNIFTY 52500 CE Weekly | δ=+0.38 IV=18.4% Score=0.74"

Scoring dimensions (seven independent factors, weighted sum):
  1. Delta fitness       — proximity to target delta range
  2. OI support          — open interest concentration at the strike
  3. OI activity         — absolute change-in-OI (engaged strike)
  4. GEX proximity       — position relative to gamma walls and flip level
  5. IV attractiveness   — strike IV relative to ATM surface IV
  6. Liquidity quality   — bid-ask spread tightness and volume depth
  7. Max pain distance   — distance from option writers' equilibrium level

Each dimension produces a score in [0, 1].
Weights are configurable. Final score = normalised weighted average × DTE
multiplier.

Expiry selection is integrated: a DTE-based multiplier is applied to
every candidate, enabling cross-expiry ranking within a single call.

Design constraints:
  - Deterministic and replay-safe.
  - No market data fetching — pure computation from supplied inputs.
  - Degrades gracefully when optional analytics snapshots are absent.
  - JSON-serialisable state throughout.
  - No external ML library dependencies.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import fabs, isfinite

from platform_core.options.oi_analytics import OIAnalyticsSnapshot
from platform_core.options.gamma_exposure import GammaExposureSnapshot
from platform_core.options.gex_gate import _wall as _spot_side_wall

# STRIKE_GEX_SPOTSIDE (default OFF): score gex_proximity against the spot-side-
# constrained walls (call_wall_above_strike / put_wall_below_strike) instead of
# the legacy unconstrained walls, which top one dominant strike on BOTH sides in
# ~40% of recorded snapshots (07-17 report §5). A snapshot that carries the field
# with value None means "no strike on that side" and scores as wall-unknown
# (0.50) rather than falling back to the legacy wall. Pending replay A/B.
_GEX_SPOTSIDE: bool = os.environ.get("STRIKE_GEX_SPOTSIDE", "0") == "1"


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


# ── Exceptions ─────────────────────────────────────────────────────────────────

class StrikeSelectionError(RuntimeError):
    """Base exception for strike selection failures."""


class StrikeSelectionInputError(StrikeSelectionError):
    """Raised when inputs are invalid or structurally inconsistent."""


class StrikeSelectionComputationError(StrikeSelectionError):
    """Raised when selection cannot be completed safely."""


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrikeSelectionConfig:
    """
    Scoring weights and filter thresholds for the strike selection engine.

    Only weight ratios matter — absolute magnitudes are irrelevant because
    the composite score is computed as a weighted average.
    """

    # Scoring weights (non-negative)
    delta_fitness_weight: float = 1.20
    oi_support_weight: float = 0.90
    oi_activity_weight: float = 0.60
    gex_proximity_weight: float = 0.80
    iv_attractiveness_weight: float = 1.00
    liquidity_weight: float = 1.10
    max_pain_distance_weight: float = 0.50
    # Dimension 8: theta efficiency — daily theta decay as % of premium.
    # Penalises options whose time-decay cost is high relative to their
    # price.  Provides within-optimal-DTE-window differentiation (the DTE
    # multiplier is flat 1.0 for all 5-18 DTE candidates; theta efficiency
    # adds the only discrimination between, e.g., a 7-DTE and a 14-DTE
    # option inside that range).  Uses StrikeCandidate.theta, which is
    # always BSM-computed (consistent in both backtest and live paths).
    theta_efficiency_weight: float = 0.60

    # Delta targeting for directional swing trades
    # abs(delta) is used — applies equally to calls and puts
    target_delta_min: float = 0.28
    target_delta_max: float = 0.52
    delta_outer_tolerance: float = 0.15   # decay-to-zero band beyond target

    # DTE windows (calendar days to expiry)
    dte_optimal_min: int = 5
    dte_optimal_max: int = 18
    dte_acceptable_max: int = 45
    dte_min_viable: int = 1              # below this, candidate is excluded

    # IV controls
    # max acceptable IV premium over ATM IV (as a fraction, not percent)
    max_iv_premium_fraction: float = 0.40

    # Liquidity filters
    max_bid_ask_spread_pct: float = 6.0  # percent of mid price
    min_open_interest: int = 500

    # GEX: distance within which a strike is considered "near" a wall
    gex_wall_proximity_pct: float = 1.5  # percent of spot price

    # Output
    top_n: int = 5

    def __post_init__(self) -> None:
        weight_fields = {
            "delta_fitness_weight": self.delta_fitness_weight,
            "oi_support_weight": self.oi_support_weight,
            "oi_activity_weight": self.oi_activity_weight,
            "gex_proximity_weight": self.gex_proximity_weight,
            "iv_attractiveness_weight": self.iv_attractiveness_weight,
            "liquidity_weight": self.liquidity_weight,
            "max_pain_distance_weight": self.max_pain_distance_weight,
            "theta_efficiency_weight": self.theta_efficiency_weight,
        }
        for name, value in weight_fields.items():
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
        if not 0.0 < self.target_delta_min < self.target_delta_max <= 1.0:
            raise ValueError(
                "target_delta_min must satisfy 0 < min < max <= 1"
            )
        if self.delta_outer_tolerance <= 0.0:
            raise ValueError("delta_outer_tolerance must be positive")
        if not 1 <= self.dte_optimal_min <= self.dte_optimal_max <= self.dte_acceptable_max:
            raise ValueError(
                "DTE thresholds must satisfy 1 <= optimal_min <= optimal_max <= acceptable_max"
            )
        if self.dte_min_viable < 1:
            raise ValueError("dte_min_viable must be at least 1")
        if self.max_iv_premium_fraction <= 0.0:
            raise ValueError("max_iv_premium_fraction must be positive")
        if self.max_bid_ask_spread_pct <= 0.0:
            raise ValueError("max_bid_ask_spread_pct must be positive")
        if self.min_open_interest < 0:
            raise ValueError("min_open_interest must be non-negative")
        if self.top_n <= 0:
            raise ValueError("top_n must be positive")


# ── Input Model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrikeCandidate:
    """
    Normalised option contract snapshot used as input for strike selection.

    Can be constructed from:
    - A live Zerodha option chain snapshot (via OptionContractSnapshot).
    - Historical option chain data.
    - A synthetically generated chain (for backtesting and testing).

    All numeric fields must be finite. Greeks are expected to be BSM values
    produced by GreeksEngine or equivalent.
    """

    underlying_symbol: str
    expiry_date: date
    option_symbol: str
    strike_price: float
    option_type: str              # "CALL" or "PUT"
    spot_price: float
    last_traded_price: float
    open_interest: int
    change_in_open_interest: int
    volume: int
    implied_volatility: float     # annualised fraction: 0.18 = 18%
    delta: float                  # signed BSM delta
    gamma: float
    theta: float                  # per calendar day
    vega: float                   # per 1% IV move (i.e. per 0.01 IV)
    time_to_expiry_years: float
    lot_size: int
    bid_price: float | None = None
    ask_price: float | None = None

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise StrikeSelectionInputError("underlying_symbol must be non-empty")
        if not self.option_symbol.strip():
            raise StrikeSelectionInputError("option_symbol must be non-empty")
        if self.option_type.upper() not in {"CALL", "PUT"}:
            raise StrikeSelectionInputError("option_type must be CALL or PUT")
        if self.strike_price <= 0.0:
            raise StrikeSelectionInputError("strike_price must be positive")
        if self.spot_price <= 0.0:
            raise StrikeSelectionInputError("spot_price must be positive")
        if self.last_traded_price < 0.0:
            raise StrikeSelectionInputError("last_traded_price must be non-negative")
        if self.open_interest < 0:
            raise StrikeSelectionInputError("open_interest must be non-negative")
        if self.volume < 0:
            raise StrikeSelectionInputError("volume must be non-negative")
        if self.implied_volatility < 0.0:
            raise StrikeSelectionInputError("implied_volatility must be non-negative")
        if self.time_to_expiry_years < 0.0:
            raise StrikeSelectionInputError("time_to_expiry_years must be non-negative")
        if self.lot_size <= 0:
            raise StrikeSelectionInputError("lot_size must be positive")
        core_numerics = [
            self.strike_price, self.spot_price, self.last_traded_price,
            self.implied_volatility, self.delta, self.gamma, self.theta,
            self.vega, self.time_to_expiry_years,
        ]
        if not all(isfinite(v) for v in core_numerics):
            raise StrikeSelectionInputError("all numeric fields must be finite")
        if self.bid_price is not None and self.bid_price < 0.0:
            raise StrikeSelectionInputError("bid_price must be non-negative when provided")
        if self.ask_price is not None and self.ask_price < 0.0:
            raise StrikeSelectionInputError("ask_price must be non-negative when provided")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    @property
    def days_to_expiry(self) -> int:
        return max(1, round(self.time_to_expiry_years * 365.25))

    @property
    def abs_delta(self) -> float:
        return fabs(self.delta)

    @property
    def mid_price(self) -> float:
        if self.bid_price is not None and self.ask_price is not None:
            return (self.bid_price + self.ask_price) / 2.0
        return self.last_traded_price

    @property
    def bid_ask_spread_pct(self) -> float | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        mid = self.mid_price
        if mid <= 0.0:
            return None
        return (self.ask_price - self.bid_price) / mid * 100.0

    @property
    def moneyness_pct(self) -> float:
        """Positive = ITM, negative = OTM, from the option buyer's perspective."""
        if self.normalized_option_type == "CALL":
            return (self.spot_price / self.strike_price - 1.0) * 100.0
        return (self.strike_price / self.spot_price - 1.0) * 100.0

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "option_symbol": self.option_symbol,
            "strike_price": self.strike_price,
            "option_type": self.normalized_option_type,
            "spot_price": self.spot_price,
            "last_traded_price": self.last_traded_price,
            "open_interest": self.open_interest,
            "change_in_open_interest": self.change_in_open_interest,
            "volume": self.volume,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "time_to_expiry_years": self.time_to_expiry_years,
            "lot_size": self.lot_size,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "days_to_expiry": self.days_to_expiry,
            "abs_delta": self.abs_delta,
            "mid_price": self.mid_price,
            "moneyness_pct": self.moneyness_pct,
        }


# ── Score Breakdown ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrikeScoreBreakdown:
    """Per-dimension scoring breakdown for one candidate contract."""

    delta_fitness: float       # [0, 1]
    oi_support: float          # [0, 1]
    oi_activity: float         # [0, 1]
    gex_proximity: float       # [0, 1]
    iv_attractiveness: float   # [0, 1]
    liquidity: float           # [0, 1]
    max_pain_distance: float   # [0, 1]
    theta_efficiency: float    # [0, 1] — dim 8: low daily-decay/premium is better
    dte_multiplier: float      # [0, 1] — applied to weighted average
    weighted_raw: float        # weighted average of dimensions before DTE
    composite_score: float     # weighted_raw * dte_multiplier

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "delta_fitness": self.delta_fitness,
            "oi_support": self.oi_support,
            "oi_activity": self.oi_activity,
            "gex_proximity": self.gex_proximity,
            "iv_attractiveness": self.iv_attractiveness,
            "liquidity": self.liquidity,
            "max_pain_distance": self.max_pain_distance,
            "theta_efficiency": self.theta_efficiency,
            "dte_multiplier": self.dte_multiplier,
            "weighted_raw": self.weighted_raw,
            "composite_score": self.composite_score,
        }


# ── Recommendation ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrikeRecommendation:
    """
    A single ranked option contract recommendation with complete reasoning.

    The summary() method produces the one-line chartered output format:
        "[#1] Buy BANKNIFTY 52500 CE  Weekly  | δ=+0.38  IV=18.4%  Score=0.741"
    """

    rank: int
    underlying_symbol: str
    expiry_date: date
    option_symbol: str
    strike_price: float
    option_type: str           # "CALL" or "PUT"
    spot_price: float
    days_to_expiry: int
    implied_volatility: float
    delta: float
    gamma: float
    theta: float
    vega: float
    last_traded_price: float
    open_interest: int
    lot_size: int
    composite_score: float
    breakdown: StrikeScoreBreakdown
    reasoning: tuple[str, ...]
    liquidity_grade: str       # "A" / "B" / "C" / "D"
    oi_classification: str     # "HIGH" / "MEDIUM" / "LOW"
    gex_label: str             # "NEAR_CALL_WALL" / "NEAR_FLIP" / "CLEAR" / etc.
    iv_label: str              # "CHEAP" / "FAIR" / "ELEVATED" / "EXPENSIVE"
    expiry_label: str          # "WEEKLY" / "BIWEEKLY" / "MONTHLY"

    def summary(self) -> str:
        expiry_tag = (
            "Weekly" if self.days_to_expiry <= 7
            else "Biweekly" if self.days_to_expiry <= 14
            else f"{self.days_to_expiry}DTE"
        )
        return (
            f"[#{self.rank}] Buy {self.underlying_symbol} "
            f"{self.strike_price:.0f} {self.option_type}  "
            f"{expiry_tag}  |  "
            f"delta={self.delta:+.2f}  "
            f"IV={self.implied_volatility * 100:.1f}%  "
            f"LTP={self.last_traded_price:.1f}  "
            f"OI={self.open_interest:,}  "
            f"Score={self.composite_score:.3f}"
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "rank": self.rank,
            "underlying_symbol": self.underlying_symbol,
            "expiry_date": self.expiry_date.isoformat(),
            "option_symbol": self.option_symbol,
            "strike_price": self.strike_price,
            "option_type": self.option_type,
            "spot_price": self.spot_price,
            "days_to_expiry": self.days_to_expiry,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "last_traded_price": self.last_traded_price,
            "open_interest": self.open_interest,
            "lot_size": self.lot_size,
            "composite_score": self.composite_score,
            "breakdown": self.breakdown.to_dict(),
            "reasoning": list(self.reasoning),
            "liquidity_grade": self.liquidity_grade,
            "oi_classification": self.oi_classification,
            "gex_label": self.gex_label,
            "iv_label": self.iv_label,
            "expiry_label": self.expiry_label,
            "summary": self.summary(),
        }


# ── Selection Result ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StrikeSelectionResult:
    """Complete output of one strike selection pass."""

    underlying_symbol: str
    valuation_timestamp: datetime
    bias: str                    # "LONG" or "SHORT"
    spot_price: float
    candidates_evaluated: int
    candidates_passed_filters: int
    recommendations: tuple[StrikeRecommendation, ...]
    oi_context_summary: dict[str, JsonValue]
    gex_context_summary: dict[str, JsonValue]
    metadata: dict[str, JsonValue]

    def best(self) -> StrikeRecommendation | None:
        """Return the top-ranked recommendation, or None if none passed filters."""
        return self.recommendations[0] if self.recommendations else None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "valuation_timestamp": _ensure_utc(self.valuation_timestamp).isoformat(),
            "bias": self.bias,
            "spot_price": self.spot_price,
            "candidates_evaluated": self.candidates_evaluated,
            "candidates_passed_filters": self.candidates_passed_filters,
            "recommendations": [r.to_dict() for r in self.recommendations],
            "oi_context_summary": self.oi_context_summary,
            "gex_context_summary": self.gex_context_summary,
            "metadata": self.metadata,
        }


# ── Engine ─────────────────────────────────────────────────────────────────────

class StrikeSelectionEngine:
    """
    Strike and expiry selection engine for institutional index options.

    Accepts a flat list of StrikeCandidate objects (which may span multiple
    expiries) and returns a StrikeSelectionResult containing up to top_n
    ranked recommendations.

    The engine is stateless after construction; select() is safe to call
    concurrently or repeatedly without state contamination.

    Optional analytics snapshots enrich specific scoring dimensions:
    - OIAnalyticsSnapshot: enriches oi_support, oi_activity, max_pain_distance.
    - GammaExposureSnapshot: enriches gex_proximity.
    - atm_iv: enriches iv_attractiveness. If absent, estimated from chain.
    """

    def __init__(self, *, config: StrikeSelectionConfig | None = None) -> None:
        self._config = config or StrikeSelectionConfig()

    @property
    def config(self) -> StrikeSelectionConfig:
        return self._config

    def select(
        self,
        *,
        bias: str,
        candidates: list[StrikeCandidate],
        valuation_timestamp: datetime,
        oi_snapshot: OIAnalyticsSnapshot | None = None,
        gex_snapshot: GammaExposureSnapshot | None = None,
        atm_iv: float | None = None,
    ) -> StrikeSelectionResult:
        """
        Score and rank option strike candidates for the given directional bias.

        Parameters
        ----------
        bias:
            "LONG" — buy calls for bullish index move.
            "SHORT" — buy puts for bearish index move.
        candidates:
            Flat list of option contracts. May span multiple expiries.
            All must share the same underlying_symbol.
        valuation_timestamp:
            Causal timestamp for this selection pass (replay-safe).
        oi_snapshot:
            Optional OIAnalyticsEngine output for this underlying/expiry.
            Enriches OI support, activity, and max pain scoring.
        gex_snapshot:
            Optional GammaExposureEngine output. Enriches GEX scoring.
        atm_iv:
            Optional ATM implied volatility reference (annualised fraction,
            e.g. 0.18 = 18%). When absent, estimated from the candidate chain.
        """
        normalized_bias = bias.strip().upper()
        if normalized_bias not in {"LONG", "SHORT"}:
            raise StrikeSelectionInputError("bias must be LONG or SHORT")
        if not candidates:
            raise StrikeSelectionInputError("candidates list must be non-empty")

        underlying = candidates[0].underlying_symbol
        for c in candidates:
            if c.underlying_symbol != underlying:
                raise StrikeSelectionInputError(
                    "all candidates must share the same underlying_symbol"
                )

        target_option_type = "CALL" if normalized_bias == "LONG" else "PUT"
        viable = self._pre_filter(candidates, target_option_type)

        if not viable:
            return StrikeSelectionResult(
                underlying_symbol=underlying,
                valuation_timestamp=_ensure_utc(valuation_timestamp),
                bias=normalized_bias,
                spot_price=candidates[0].spot_price,
                candidates_evaluated=len(candidates),
                candidates_passed_filters=0,
                recommendations=(),
                oi_context_summary=self._oi_summary(oi_snapshot),
                gex_context_summary=self._gex_summary(gex_snapshot),
                metadata={
                    "rejection_reason": "no_viable_candidates_after_filter",
                    "target_option_type": target_option_type,
                },
            )

        chain_max_oi = max(c.open_interest for c in viable)
        chain_max_oi = max(chain_max_oi, 1)

        # atm_iv plausibility guard (2026-06-27): the supplied surface ATM IV
        # (IVSurfaceEngine interpolation) has been observed returning ~half the
        # true near-ATM value on the SENSEX/BSE chain, which corrupts
        # iv_attractiveness (it previously zeroed it). When the supplied value is
        # wildly out of line with the actual near-ATM chain IV, fall back to the
        # robust chain-based estimate (near-ATM strike's own IV). No-op when the
        # surface value is sane (within 0.5x..2x of the chain ATM IV).
        # Both the surface ATM IV and the near-ATM strike's own IV measure "ATM
        # implied vol" and should nearly match (gentle skew near ATM). A material
        # divergence means the surface interpolation is wrong, so trust the
        # directly-observed chain value. Band chosen to catch the SENSEX bug
        # (~half) while leaving normal near-ATM skew (<~30%) untouched.
        _chain_atm_iv = self._estimate_atm_iv(viable)
        if atm_iv is None:
            effective_atm_iv = _chain_atm_iv
        elif _chain_atm_iv > 0.0 and not (0.7 * _chain_atm_iv <= atm_iv <= 1.5 * _chain_atm_iv):
            effective_atm_iv = _chain_atm_iv
        else:
            effective_atm_iv = atm_iv

        scored: list[tuple[float, StrikeCandidate, StrikeScoreBreakdown, list[str]]] = []
        for candidate in viable:
            breakdown, reasoning = self._score_candidate(
                candidate=candidate,
                bias=normalized_bias,
                chain_max_oi=chain_max_oi,
                atm_iv=effective_atm_iv,
                oi_snapshot=oi_snapshot,
                gex_snapshot=gex_snapshot,
            )
            scored.append((breakdown.composite_score, candidate, breakdown, reasoning))

        scored.sort(key=lambda item: item[0], reverse=True)

        top = scored[: self._config.top_n]
        recommendations: list[StrikeRecommendation] = []
        for rank, (_, candidate, breakdown, reasoning) in enumerate(top, start=1):
            rec = self._build_recommendation(
                rank=rank,
                candidate=candidate,
                breakdown=breakdown,
                reasoning=reasoning,
                gex_snapshot=gex_snapshot,
                atm_iv=effective_atm_iv,
            )
            recommendations.append(rec)

        return StrikeSelectionResult(
            underlying_symbol=underlying,
            valuation_timestamp=_ensure_utc(valuation_timestamp),
            bias=normalized_bias,
            spot_price=candidates[0].spot_price,
            candidates_evaluated=len(candidates),
            candidates_passed_filters=len(viable),
            recommendations=tuple(recommendations),
            oi_context_summary=self._oi_summary(oi_snapshot),
            gex_context_summary=self._gex_summary(gex_snapshot),
            metadata={
                "target_option_type": target_option_type,
                "atm_iv_used": effective_atm_iv,
                "atm_iv_source": "supplied" if atm_iv is not None else "estimated_from_chain",
                "config_top_n": self._config.top_n,
            },
        )

    # ── Pre-filter ─────────────────────────────────────────────────────────────

    def _pre_filter(
        self,
        candidates: list[StrikeCandidate],
        target_option_type: str,
    ) -> list[StrikeCandidate]:
        """Discard wrong option type, expired/too-far contracts, and illiquid strikes."""
        viable: list[StrikeCandidate] = []
        for c in candidates:
            if c.normalized_option_type != target_option_type:
                continue
            dte = c.days_to_expiry
            if dte < self._config.dte_min_viable:
                continue
            if dte > self._config.dte_acceptable_max:
                continue
            if c.open_interest < self._config.min_open_interest:
                continue
            viable.append(c)
        return viable

    # ── Composite scorer ───────────────────────────────────────────────────────

    def _score_candidate(
        self,
        *,
        candidate: StrikeCandidate,
        bias: str,
        chain_max_oi: int,
        atm_iv: float,
        oi_snapshot: OIAnalyticsSnapshot | None,
        gex_snapshot: GammaExposureSnapshot | None,
    ) -> tuple[StrikeScoreBreakdown, list[str]]:
        reasoning: list[str] = []

        d1 = self._score_delta_fitness(candidate, reasoning)
        d2 = self._score_oi_support(candidate, chain_max_oi, reasoning)
        d3 = self._score_oi_activity(candidate, reasoning)
        d4 = self._score_gex_proximity(candidate, bias, gex_snapshot, reasoning)
        d5 = self._score_iv_attractiveness(candidate, atm_iv, reasoning)
        d6 = self._score_liquidity(candidate, reasoning)
        d7 = self._score_max_pain_distance(candidate, bias, oi_snapshot, reasoning)
        d8 = self._score_theta_efficiency(candidate, reasoning)
        dte_mult = self._dte_multiplier(candidate, reasoning)

        cfg = self._config
        total_weight = (
            cfg.delta_fitness_weight
            + cfg.oi_support_weight
            + cfg.oi_activity_weight
            + cfg.gex_proximity_weight
            + cfg.iv_attractiveness_weight
            + cfg.liquidity_weight
            + cfg.max_pain_distance_weight
            + cfg.theta_efficiency_weight
        )

        if total_weight <= 0.0:
            weighted_raw = 0.5
        else:
            weighted_raw = (
                cfg.delta_fitness_weight * d1
                + cfg.oi_support_weight * d2
                + cfg.oi_activity_weight * d3
                + cfg.gex_proximity_weight * d4
                + cfg.iv_attractiveness_weight * d5
                + cfg.liquidity_weight * d6
                + cfg.max_pain_distance_weight * d7
                + cfg.theta_efficiency_weight * d8
            ) / total_weight

        composite = _clip01(weighted_raw * dte_mult)

        breakdown = StrikeScoreBreakdown(
            delta_fitness=d1,
            oi_support=d2,
            oi_activity=d3,
            gex_proximity=d4,
            iv_attractiveness=d5,
            liquidity=d6,
            max_pain_distance=d7,
            theta_efficiency=d8,
            dte_multiplier=dte_mult,
            weighted_raw=weighted_raw,
            composite_score=composite,
        )
        return breakdown, reasoning

    # ── Dimension 1: Delta Fitness ─────────────────────────────────────────────

    def _score_delta_fitness(
        self,
        candidate: StrikeCandidate,
        reasoning: list[str],
    ) -> float:
        """
        Score proximity of abs(delta) to the target delta range.

        Within [target_min, target_max]    → 1.0
        Outside that range                 → linear decay over outer_tolerance
        Beyond outer_tolerance from range  → 0.0
        """
        abs_d = candidate.abs_delta
        dmin = self._config.target_delta_min
        dmax = self._config.target_delta_max
        tol = self._config.delta_outer_tolerance

        if dmin <= abs_d <= dmax:
            score = 1.0
            reasoning.append(
                f"delta={abs_d:.2f} within target [{dmin:.2f}, {dmax:.2f}] → delta_score=1.00"
            )
        elif abs_d < dmin:
            distance = dmin - abs_d
            score = _clip01(1.0 - distance / tol)
            reasoning.append(
                f"delta={abs_d:.2f} below target by {distance:.2f} → delta_score={score:.2f}"
            )
        else:
            distance = abs_d - dmax
            score = _clip01(1.0 - distance / tol)
            reasoning.append(
                f"delta={abs_d:.2f} above target by {distance:.2f} → delta_score={score:.2f}"
            )
        return score

    # ── Dimension 2: OI Support ────────────────────────────────────────────────

    def _score_oi_support(
        self,
        candidate: StrikeCandidate,
        chain_max_oi: int,
        reasoning: list[str],
    ) -> float:
        """
        Score based on open interest relative to the highest-OI strike in chain.

        High OI indicates institutional presence and price magnetism.
        """
        score = _clip01(candidate.open_interest / chain_max_oi)
        tier = "HIGH" if score >= 0.66 else "MEDIUM" if score >= 0.33 else "LOW"
        reasoning.append(
            f"OI={candidate.open_interest:,} ({tier}, {score:.2f} of chain max) → oi_support={score:.2f}"
        )
        return score

    # ── Dimension 3: OI Activity ───────────────────────────────────────────────

    def _score_oi_activity(
        self,
        candidate: StrikeCandidate,
        reasoning: list[str],
    ) -> float:
        """
        Score based on absolute change-in-OI relative to current OI.

        High recent OI change (regardless of direction) indicates the strike is
        actively engaged by market participants — it is a focus level. A 25%
        change in OI is treated as full score; above 25% is capped.

        Zero change-in-OI is treated as data unavailable (neutral 0.50), not as
        evidence of disengagement.  In the current pipeline, change_in_oi is
        always 0 because no prior-snapshot tracking exists to compute the delta.
        Returning 0.0 would penalise every candidate equally without adding
        information — inconsistent with every other unavailable-data default in
        this engine (GEX absent → 0.50, max pain absent → 0.50, OI base=0 →
        0.50, bid/ask absent → spread_score=0.50).  When real OI delta
        acquisition is added (see §30e), this branch is bypassed automatically.
        """
        if candidate.open_interest <= 0:
            reasoning.append("OI activity: no base OI → oi_activity=0.50")
            return 0.5

        if candidate.change_in_open_interest == 0:
            reasoning.append("OI activity: no change data available → oi_activity=0.50")
            return 0.5

        abs_change = fabs(candidate.change_in_open_interest)
        change_fraction = abs_change / candidate.open_interest
        score = _clip01(min(change_fraction, 0.25) / 0.25)

        direction = (
            "buildup" if candidate.change_in_open_interest > 0
            else "unwinding" if candidate.change_in_open_interest < 0
            else "flat"
        )
        reasoning.append(
            f"OI change={candidate.change_in_open_interest:+,} ({direction}, "
            f"{change_fraction * 100:.0f}% of OI) → oi_activity={score:.2f}"
        )
        return score

    # ── Dimension 4: GEX Proximity ─────────────────────────────────────────────

    def _score_gex_proximity(
        self,
        candidate: StrikeCandidate,
        bias: str,
        gex_snapshot: GammaExposureSnapshot | None,
        reasoning: list[str],
    ) -> float:
        """
        Score position relative to gamma exposure walls and the flip level.

        For LONG/CALL buyers:
          - Strikes below the call wall have room to run before dealer resistance.
          - Strikes above the gamma flip are in short-gamma territory where
            dealer hedging can amplify rather than dampen moves.

        For SHORT/PUT buyers:
          - Strikes above the put wall have room to fall.
          - Strikes below the gamma flip benefit from the same dynamic.

        Returns 0.50 when GEX snapshot is unavailable.
        """
        if gex_snapshot is None:
            reasoning.append("GEX: no snapshot — gex_proximity=0.50")
            return 0.5

        spot = candidate.spot_price
        prox_band = self._config.gex_wall_proximity_pct / 100.0 * spot
        gamma_regime = gex_snapshot.net_gamma_regime
        flip = gex_snapshot.gamma_flip_strike

        # Short-gamma environments allow explosive directional moves
        regime_score = (
            0.85 if gamma_regime == "SHORT_GAMMA"
            else 0.50 if gamma_regime == "NEUTRAL_GAMMA"
            else 0.30
        )

        if bias == "LONG":
            wall = (_spot_side_wall(gex_snapshot, "call_wall_above_strike", "call_wall_strike")
                    if _GEX_SPOTSIDE else gex_snapshot.call_wall_strike)
            if wall is None:
                wall_score = 0.50
                reasoning.append("GEX: call wall unknown")
            else:
                distance = wall - candidate.strike_price
                if distance >= prox_band:
                    wall_score = 1.0
                    reasoning.append(
                        f"GEX: {distance:.0f}pts below call wall at {wall:.0f}"
                    )
                elif distance >= 0.0:
                    wall_score = 0.50 + (distance / prox_band) * 0.50
                    reasoning.append(f"GEX: near call wall at {wall:.0f}")
                else:
                    wall_score = 0.20
                    reasoning.append(f"GEX: strike above call wall {wall:.0f} — dealer resistance")

            flip_score = 0.50
            if flip is not None:
                if spot > flip:
                    flip_score = 0.80
                    reasoning.append(f"GEX: spot above flip {flip:.0f} — short-gamma above")
                else:
                    flip_score = 0.30
                    reasoning.append(f"GEX: spot below flip {flip:.0f} — long-gamma dampens")

        else:  # SHORT → PUT buyers
            wall = (_spot_side_wall(gex_snapshot, "put_wall_below_strike", "put_wall_strike")
                    if _GEX_SPOTSIDE else gex_snapshot.put_wall_strike)
            if wall is None:
                wall_score = 0.50
                reasoning.append("GEX: put wall unknown")
            else:
                distance = candidate.strike_price - wall
                if distance >= prox_band:
                    wall_score = 1.0
                    reasoning.append(
                        f"GEX: {distance:.0f}pts above put wall at {wall:.0f}"
                    )
                elif distance >= 0.0:
                    wall_score = 0.50 + (distance / prox_band) * 0.50
                    reasoning.append(f"GEX: near put wall at {wall:.0f}")
                else:
                    wall_score = 0.20
                    reasoning.append(f"GEX: strike below put wall {wall:.0f} — dealer support")

            flip_score = 0.50
            if flip is not None:
                if spot < flip:
                    flip_score = 0.80
                    reasoning.append(f"GEX: spot below flip {flip:.0f} — short-gamma below")
                else:
                    flip_score = 0.30
                    reasoning.append(f"GEX: spot above flip {flip:.0f} — long-gamma dampens")

        score = _clip01(0.50 * wall_score + 0.30 * flip_score + 0.20 * regime_score)
        reasoning.append(f"GEX regime={gamma_regime} → gex_proximity={score:.2f}")
        return score

    # ── Dimension 5: IV Attractiveness ─────────────────────────────────────────

    def _score_iv_attractiveness(
        self,
        candidate: StrikeCandidate,
        atm_iv: float,
        reasoning: list[str],
    ) -> float:
        """
        Score how attractively the strike is priced relative to ATM IV.

        For option buyers: paying excessive IV skew premium reduces edge.
        Strikes priced at or below ATM IV receive full score.
        Score decays as IV premium exceeds max_iv_premium_fraction above ATM.
        """
        if atm_iv <= 0.0:
            reasoning.append("IV: ATM reference unavailable → iv_attractiveness=0.50")
            return 0.5

        iv_premium = (candidate.implied_volatility - atm_iv) / atm_iv
        max_prem = self._config.max_iv_premium_fraction

        if iv_premium <= 0.0:
            score = 1.0
            label = "CHEAP"
        elif iv_premium <= max_prem * 0.5:
            score = 1.0 - (iv_premium / (max_prem * 0.5)) * 0.30
            label = "FAIR"
        elif iv_premium <= max_prem:
            score = 0.70 - ((iv_premium - max_prem * 0.5) / (max_prem * 0.5)) * 0.50
            label = "ELEVATED"
        else:
            excess = iv_premium - max_prem
            score = max(0.0, 0.20 - (excess / max_prem) * 0.20)
            label = "EXPENSIVE"

        reasoning.append(
            f"IV={candidate.implied_volatility * 100:.1f}% vs ATM {atm_iv * 100:.1f}% "
            f"(premium={iv_premium * 100:+.1f}%, {label}) → iv_attractiveness={score:.2f}"
        )
        return _clip01(score)

    # ── Dimension 6: Liquidity Quality ─────────────────────────────────────────

    def _score_liquidity(
        self,
        candidate: StrikeCandidate,
        reasoning: list[str],
    ) -> float:
        """
        Score from two sub-components:
          - Bid-ask spread as % of mid price (lower = better).
          - Volume / OI participation ratio (higher = more active).

        When bid/ask is unavailable, the spread component defaults to 0.5.
        """
        spread_pct = candidate.bid_ask_spread_pct
        if spread_pct is None:
            spread_score = 0.50
            spread_label = "spread=unknown"
        else:
            spread_score = _clip01(1.0 - spread_pct / self._config.max_bid_ask_spread_pct)
            spread_label = f"spread={spread_pct:.1f}%"

        if candidate.open_interest > 0:
            vol_ratio = candidate.volume / candidate.open_interest
            vol_score = _clip01(min(vol_ratio, 0.20) / 0.20)
        else:
            vol_score = 0.0

        score = _clip01(0.60 * spread_score + 0.40 * vol_score)
        reasoning.append(
            f"Liquidity: {spread_label}  vol={candidate.volume:,} OI={candidate.open_interest:,} "
            f"→ liquidity={score:.2f}"
        )
        return score

    # ── Dimension 7: Max Pain Distance ─────────────────────────────────────────

    def _score_max_pain_distance(
        self,
        candidate: StrikeCandidate,
        bias: str,
        oi_snapshot: OIAnalyticsSnapshot | None,
        reasoning: list[str],
    ) -> float:
        """
        Score based on the strike's position relative to max pain.

        Max pain is the settlement price that minimises total pay-out by option
        writers. Price historically gravitates toward max pain near expiry,
        making it relevant for assessing whether a strike is likely to finish
        in-the-money.

        For LONG/CALL:  score increases when max_pain > strike (pin favours ITM)
        For SHORT/PUT:  score increases when max_pain < strike (pin favours ITM)

        Returns 0.50 when max pain is unavailable.
        """
        max_pain = oi_snapshot.max_pain_strike if oi_snapshot is not None else None
        if max_pain is None:
            reasoning.append("Max pain: unavailable → max_pain_distance=0.50")
            return 0.5

        spot = candidate.spot_price
        normaliser = max(spot * 0.015, 1.0)  # 1.5% of spot

        if bias == "LONG":
            diff = max_pain - candidate.strike_price
        else:
            diff = candidate.strike_price - max_pain

        score = _clip01(0.5 + diff / normaliser * 0.5)
        direction_label = "favourable" if score >= 0.5 else "unfavourable"
        reasoning.append(
            f"Max pain={max_pain:.0f} vs strike={candidate.strike_price:.0f} "
            f"({direction_label}) → max_pain_distance={score:.2f}"
        )
        return score

    # ── Dimension 8: Theta Efficiency ──────────────────────────────────────────

    def _score_theta_efficiency(
        self,
        candidate: StrikeCandidate,
        reasoning: list[str],
    ) -> float:
        """
        Score based on daily theta decay as a percentage of premium paid.

        For option buyers, lower daily decay relative to premium is better —
        it means the option retains more value while awaiting the directional
        move.  Theta efficiency provides within-optimal-DTE-window
        discrimination: the DTE multiplier returns a flat 1.0 for all
        5–18 DTE candidates, leaving no differentiation between, e.g., a
        7-DTE and a 14-DTE option.  Theta efficiency fills that gap.

        Calibration (BANKNIFTY BSM ranges):
          ≤ 3 %/day  → 1.0  (efficient: typically 14–18 DTE near ATM)
          ≥ 15 %/day → 0.0  (expensive: typically ≤5 DTE OTM)
          Linear between 3 % and 15 %.

        Both theta and last_traded_price are always present (BSM-computed
        for every candidate regardless of data source).  Returns 0.50
        (neutral) when premium is effectively zero to avoid division errors.
        """
        _THETA_EFFICIENT  = 0.03   # 3 % /day → score 1.0
        _THETA_EXPENSIVE  = 0.15   # 15 % /day → score 0.0

        if candidate.last_traded_price < 0.01:
            reasoning.append("theta efficiency: no premium data → 0.50")
            return 0.50

        daily_decay_pct = fabs(candidate.theta) / candidate.last_traded_price

        if daily_decay_pct <= _THETA_EFFICIENT:
            score = 1.0
        elif daily_decay_pct >= _THETA_EXPENSIVE:
            score = 0.0
        else:
            score = (_THETA_EXPENSIVE - daily_decay_pct) / (
                _THETA_EXPENSIVE - _THETA_EFFICIENT
            )

        reasoning.append(
            f"theta={candidate.theta:.2f} prem={candidate.last_traded_price:.2f} "
            f"decay={daily_decay_pct:.1%}/day → theta_efficiency={_clip01(score):.2f}"
        )
        return _clip01(score)

    # ── DTE Multiplier ─────────────────────────────────────────────────────────

    def _dte_multiplier(
        self,
        candidate: StrikeCandidate,
        reasoning: list[str],
    ) -> float:
        """
        DTE-based multiplier applied to the weighted average score.

        This is a separate multiplicative penalty rather than a scored
        dimension because it is an objective structural constraint:
        very short DTE = binary risk; very long DTE = excessive theta.

        Optimal window for directional swing option buying: 5–18 DTE.
        """
        dte = candidate.days_to_expiry
        opt_min = self._config.dte_optimal_min
        opt_max = self._config.dte_optimal_max

        if dte <= 2:
            mult = 0.20
            label = f"critical ({dte} DTE)"
        elif dte < opt_min:
            mult = 0.20 + (dte - 2) / max(opt_min - 2, 1) * 0.80
            label = f"short ({dte} DTE, ramping)"
        elif dte <= opt_max:
            mult = 1.0
            label = f"optimal ({dte} DTE)"
        elif dte <= 21:
            mult = 0.85
            label = f"acceptable ({dte} DTE)"
        elif dte <= 35:
            mult = 0.70
            label = f"extended ({dte} DTE)"
        else:
            mult = 0.55
            label = f"long-dated ({dte} DTE)"

        reasoning.append(f"DTE={dte} ({label}) → dte_multiplier={mult:.2f}")
        return _clip01(mult)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _estimate_atm_iv(self, candidates: list[StrikeCandidate]) -> float:
        """
        Estimate ATM IV from the option chain when no surface snapshot is given.

        Finds the candidate whose strike is closest to spot and returns its IV.
        Falls back to 0.20 (20%) if the chain is empty or IV is zero.
        """
        if not candidates:
            return 0.20
        spot = candidates[0].spot_price
        near_atm = min(candidates, key=lambda c: fabs(c.strike_price - spot))
        return near_atm.implied_volatility if near_atm.implied_volatility > 0.0 else 0.20

    def _build_recommendation(
        self,
        *,
        rank: int,
        candidate: StrikeCandidate,
        breakdown: StrikeScoreBreakdown,
        reasoning: list[str],
        gex_snapshot: GammaExposureSnapshot | None,
        atm_iv: float,
    ) -> StrikeRecommendation:
        liq = breakdown.liquidity
        liquidity_grade = (
            "A" if liq >= 0.75
            else "B" if liq >= 0.50
            else "C" if liq >= 0.25
            else "D"
        )

        oi_classification = (
            "HIGH" if breakdown.oi_support >= 0.66
            else "MEDIUM" if breakdown.oi_support >= 0.33
            else "LOW"
        )

        if gex_snapshot is None:
            gex_label = "UNKNOWN"
        else:
            spot = candidate.spot_price
            prox = self._config.gex_wall_proximity_pct / 100.0 * spot
            s = candidate.strike_price
            call_wall = gex_snapshot.call_wall_strike
            put_wall = gex_snapshot.put_wall_strike
            flip = gex_snapshot.gamma_flip_strike

            if flip is not None and fabs(s - flip) < prox:
                gex_label = "NEAR_FLIP"
            elif call_wall is not None and fabs(s - call_wall) < prox:
                gex_label = "NEAR_CALL_WALL"
            elif put_wall is not None and fabs(s - put_wall) < prox:
                gex_label = "NEAR_PUT_WALL"
            elif call_wall is not None and s > call_wall:
                gex_label = "ABOVE_CALL_WALL"
            elif put_wall is not None and s < put_wall:
                gex_label = "BELOW_PUT_WALL"
            else:
                gex_label = "CLEAR"

        if atm_iv > 0.0:
            iv_premium = (candidate.implied_volatility - atm_iv) / atm_iv
            iv_label = (
                "CHEAP" if iv_premium < 0.0
                else "FAIR" if iv_premium < 0.20
                else "ELEVATED" if iv_premium < 0.40
                else "EXPENSIVE"
            )
        else:
            iv_label = "UNKNOWN"

        dte = candidate.days_to_expiry
        expiry_label = (
            "WEEKLY" if dte <= 7
            else "BIWEEKLY" if dte <= 14
            else "MONTHLY"
        )

        return StrikeRecommendation(
            rank=rank,
            underlying_symbol=candidate.underlying_symbol,
            expiry_date=candidate.expiry_date,
            option_symbol=candidate.option_symbol,
            strike_price=candidate.strike_price,
            option_type=candidate.normalized_option_type,
            spot_price=candidate.spot_price,
            days_to_expiry=dte,
            implied_volatility=candidate.implied_volatility,
            delta=candidate.delta,
            gamma=candidate.gamma,
            theta=candidate.theta,
            vega=candidate.vega,
            last_traded_price=candidate.last_traded_price,
            open_interest=candidate.open_interest,
            lot_size=candidate.lot_size,
            composite_score=breakdown.composite_score,
            breakdown=breakdown,
            reasoning=tuple(reasoning),
            liquidity_grade=liquidity_grade,
            oi_classification=oi_classification,
            gex_label=gex_label,
            iv_label=iv_label,
            expiry_label=expiry_label,
        )

    def _oi_summary(self, snapshot: OIAnalyticsSnapshot | None) -> dict[str, JsonValue]:
        if snapshot is None:
            return {"available": False}
        return {
            "available": True,
            "max_pain_strike": snapshot.max_pain_strike,
            "put_call_ratio_oi": snapshot.put_call_ratio_open_interest,
            "total_call_oi": snapshot.total_call_open_interest,
            "total_put_oi": snapshot.total_put_open_interest,
            "highest_total_oi_strike": snapshot.highest_total_oi_strike,
        }

    def _gex_summary(self, snapshot: GammaExposureSnapshot | None) -> dict[str, JsonValue]:
        if snapshot is None:
            return {"available": False}
        return {
            "available": True,
            "gamma_flip_strike": snapshot.gamma_flip_strike,
            "call_wall_strike": snapshot.call_wall_strike,
            "put_wall_strike": snapshot.put_wall_strike,
            "net_gamma_regime": snapshot.net_gamma_regime,
            "dominant_gamma_strike": snapshot.dominant_gamma_strike,
        }


# ── Utilities ──────────────────────────────────────────────────────────────────

def _clip01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "StrikeCandidate",
    "StrikeRecommendation",
    "StrikeScoreBreakdown",
    "StrikeSelectionConfig",
    "StrikeSelectionComputationError",
    "StrikeSelectionEngine",
    "StrikeSelectionError",
    "StrikeSelectionInputError",
    "StrikeSelectionResult",
]
