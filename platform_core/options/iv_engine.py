# FILE: platform_core/options/iv_engine.py
"""
platform_core.options.iv_engine
═══════════════════════════════════════════════════════════════════════
Production-grade implied volatility engine for Indian index options.

This module inverts Black-Scholes-Merton option prices into implied volatility
using deterministic, replay-safe root-finding with explicit no-arbitrage bounds,
Newton refinement, and bisection fallback. It is designed for incremental,
real-time event-driven options analytics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from math import erf
from math import exp
from math import isfinite
from math import log
from math import pi
from math import sqrt


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class IVEngineError(RuntimeError):
    """Base exception for IV engine failures."""


class IVInputError(IVEngineError):
    """Raised when market price or model inputs are invalid."""


class IVComputationError(IVEngineError):
    """Raised when implied volatility cannot be solved stably."""


@dataclass(frozen=True, slots=True)
class IVEngineConfig:
    """Configuration for numerical inversion and output behavior."""

    minimum_time_to_expiry_years: float = 1.0e-8
    minimum_volatility: float = 1.0e-6
    initial_volatility: float = 0.2
    maximum_volatility: float = 8.0
    absolute_price_tolerance: float = 1.0e-8
    absolute_vol_tolerance: float = 1.0e-8
    max_newton_iterations: int = 12
    max_bisection_iterations: int = 200
    minimum_vega_for_newton: float = 1.0e-10

    def __post_init__(self) -> None:
        if self.minimum_time_to_expiry_years <= 0:
            raise ValueError("minimum_time_to_expiry_years must be positive")
        if self.minimum_volatility <= 0:
            raise ValueError("minimum_volatility must be positive")
        if not (self.minimum_volatility <= self.initial_volatility <= self.maximum_volatility):
            raise ValueError("initial_volatility must lie within min/max volatility bounds")
        if self.maximum_volatility <= self.minimum_volatility:
            raise ValueError("maximum_volatility must exceed minimum_volatility")
        if self.absolute_price_tolerance <= 0:
            raise ValueError("absolute_price_tolerance must be positive")
        if self.absolute_vol_tolerance <= 0:
            raise ValueError("absolute_vol_tolerance must be positive")
        if self.max_newton_iterations <= 0:
            raise ValueError("max_newton_iterations must be positive")
        if self.max_bisection_iterations <= 0:
            raise ValueError("max_bisection_iterations must be positive")
        if self.minimum_vega_for_newton <= 0:
            raise ValueError("minimum_vega_for_newton must be positive")


@dataclass(frozen=True, slots=True)
class IVInput:
    """Serializable option snapshot used for IV inversion."""

    instrument_id: str
    underlying_symbol: str
    option_symbol: str
    option_type: str
    market_price: float
    spot_price: float
    strike_price: float
    time_to_expiry_years: float
    risk_free_rate: float
    dividend_yield: float = 0.0
    valuation_timestamp: datetime | None = None
    expiry_timestamp: datetime | None = None
    lot_size: int | None = None

    def __post_init__(self) -> None:
        option_type_normalized = self.option_type.upper()
        if option_type_normalized not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if not self.option_symbol.strip():
            raise ValueError("option_symbol must be non-empty")
        if self.market_price < 0:
            raise ValueError("market_price must be non-negative")
        if self.spot_price <= 0:
            raise ValueError("spot_price must be positive")
        if self.strike_price <= 0:
            raise ValueError("strike_price must be positive")
        if self.time_to_expiry_years < 0:
            raise ValueError("time_to_expiry_years must be non-negative")
        if self.lot_size is not None and self.lot_size <= 0:
            raise ValueError("lot_size must be positive when provided")

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "underlying_symbol": self.underlying_symbol,
            "option_symbol": self.option_symbol,
            "option_type": self.normalized_option_type,
            "market_price": self.market_price,
            "spot_price": self.spot_price,
            "strike_price": self.strike_price,
            "time_to_expiry_years": self.time_to_expiry_years,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "expiry_timestamp": self.expiry_timestamp.isoformat() if self.expiry_timestamp else None,
            "lot_size": self.lot_size,
        }


@dataclass(frozen=True, slots=True)
class IVResult:
    """Serializable IV inversion result."""

    instrument_id: str
    underlying_symbol: str
    option_symbol: str
    option_type: str
    valuation_timestamp: datetime | None
    expiry_timestamp: datetime | None
    market_price: float
    model_price: float
    pricing_error: float
    implied_volatility: float
    spot_price: float
    strike_price: float
    time_to_expiry_years: float
    risk_free_rate: float
    dividend_yield: float
    forward_price: float
    discount_factor: float
    carry_discount_factor: float
    d1: float
    d2: float
    vega: float
    lot_size: int | None
    used_fallback_bisection: bool
    iteration_count: int
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "underlying_symbol": self.underlying_symbol,
            "option_symbol": self.option_symbol,
            "option_type": self.option_type,
            "valuation_timestamp": self.valuation_timestamp.isoformat() if self.valuation_timestamp else None,
            "expiry_timestamp": self.expiry_timestamp.isoformat() if self.expiry_timestamp else None,
            "market_price": self.market_price,
            "model_price": self.model_price,
            "pricing_error": self.pricing_error,
            "implied_volatility": self.implied_volatility,
            "spot_price": self.spot_price,
            "strike_price": self.strike_price,
            "time_to_expiry_years": self.time_to_expiry_years,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "forward_price": self.forward_price,
            "discount_factor": self.discount_factor,
            "carry_discount_factor": self.carry_discount_factor,
            "d1": self.d1,
            "d2": self.d2,
            "vega": self.vega,
            "lot_size": self.lot_size,
            "used_fallback_bisection": self.used_fallback_bisection,
            "iteration_count": self.iteration_count,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class IVEngineState:
    """Serializable engine state."""

    last_option_symbol: str | None
    last_valuation_timestamp: datetime | None
    processed_count: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "last_option_symbol": self.last_option_symbol,
            "last_valuation_timestamp": self.last_valuation_timestamp.isoformat() if self.last_valuation_timestamp else None,
            "processed_count": self.processed_count,
        }


class IVEngine:
    """Deterministic BSM implied volatility inversion engine."""

    def __init__(self, *, config: IVEngineConfig | None = None) -> None:
        self._config = config or IVEngineConfig()
        self._last_option_symbol: str | None = None
        self._last_valuation_timestamp: datetime | None = None
        self._processed_count = 0

    @property
    def state(self) -> IVEngineState:
        return IVEngineState(
            last_option_symbol=self._last_option_symbol,
            last_valuation_timestamp=self._last_valuation_timestamp,
            processed_count=self._processed_count,
        )

    def reset(self) -> None:
        self._last_option_symbol = None
        self._last_valuation_timestamp = None
        self._processed_count = 0

    def compute(self, *, option_input: IVInput) -> IVResult:
        self._validate_input(option_input)
        t = max(option_input.time_to_expiry_years, self._config.minimum_time_to_expiry_years)
        intrinsic_floor, upper_bound = self._no_arbitrage_bounds(option_input=option_input, t=t)
        if option_input.market_price < intrinsic_floor - self._config.absolute_price_tolerance:
            raise IVInputError("market_price violates lower no-arbitrage bound")
        if option_input.market_price > upper_bound + self._config.absolute_price_tolerance:
            raise IVInputError("market_price violates upper no-arbitrage bound")
        sigma, used_fallback_bisection, iteration_count = self._solve_implied_volatility(option_input=option_input, t=t)
        model_price, d1, d2, vega, discount_factor, carry_discount_factor, forward_price = self._price_and_vega(
            option_type=option_input.normalized_option_type,
            market_spot=option_input.spot_price,
            strike=option_input.strike_price,
            time_to_expiry_years=t,
            risk_free_rate=option_input.risk_free_rate,
            dividend_yield=option_input.dividend_yield,
            sigma=sigma,
        )
        result = IVResult(
            instrument_id=option_input.instrument_id,
            underlying_symbol=option_input.underlying_symbol,
            option_symbol=option_input.option_symbol,
            option_type=option_input.normalized_option_type,
            valuation_timestamp=_ensure_utc(option_input.valuation_timestamp) if option_input.valuation_timestamp else None,
            expiry_timestamp=_ensure_utc(option_input.expiry_timestamp) if option_input.expiry_timestamp else None,
            market_price=option_input.market_price,
            model_price=model_price,
            pricing_error=option_input.market_price - model_price,
            implied_volatility=sigma,
            spot_price=option_input.spot_price,
            strike_price=option_input.strike_price,
            time_to_expiry_years=t,
            risk_free_rate=option_input.risk_free_rate,
            dividend_yield=option_input.dividend_yield,
            forward_price=forward_price,
            discount_factor=discount_factor,
            carry_discount_factor=carry_discount_factor,
            d1=d1,
            d2=d2,
            vega=vega,
            lot_size=option_input.lot_size,
            used_fallback_bisection=used_fallback_bisection,
            iteration_count=iteration_count,
            metadata={
                "model": "black_scholes_merton",
                "solver": "newton_then_bisection" if used_fallback_bisection else "newton",
                "absolute_price_tolerance": self._config.absolute_price_tolerance,
                "absolute_vol_tolerance": self._config.absolute_vol_tolerance,
            },
        )
        self._processed_count += 1
        self._last_option_symbol = result.option_symbol
        self._last_valuation_timestamp = result.valuation_timestamp
        return result

    def compute_many(self, *, option_inputs: list[IVInput]) -> tuple[IVResult, ...]:
        return tuple(self.compute(option_input=option_input) for option_input in option_inputs)

    def _solve_implied_volatility(self, *, option_input: IVInput, t: float) -> tuple[float, bool, int]:
        sigma = self._config.initial_volatility
        for iteration in range(1, self._config.max_newton_iterations + 1):
            model_price, _d1, _d2, vega, _df, _cdf, _fwd = self._price_and_vega(
                option_type=option_input.normalized_option_type,
                market_spot=option_input.spot_price,
                strike=option_input.strike_price,
                time_to_expiry_years=t,
                risk_free_rate=option_input.risk_free_rate,
                dividend_yield=option_input.dividend_yield,
                sigma=sigma,
            )
            error = model_price - option_input.market_price
            if abs(error) <= self._config.absolute_price_tolerance:
                return sigma, False, iteration
            if abs(vega) < self._config.minimum_vega_for_newton:
                break
            candidate = sigma - (error / vega)
            if not isfinite(candidate):
                break
            if candidate < self._config.minimum_volatility or candidate > self._config.maximum_volatility:
                break
            if abs(candidate - sigma) <= self._config.absolute_vol_tolerance:
                return candidate, False, iteration
            sigma = candidate
        sigma_bisect, iteration_count = self._bisection_solve(option_input=option_input, t=t)
        return sigma_bisect, True, iteration_count

    def _bisection_solve(self, *, option_input: IVInput, t: float) -> tuple[float, int]:
        low = self._config.minimum_volatility
        high = self._config.maximum_volatility
        low_price, *_ = self._price_and_vega(
            option_type=option_input.normalized_option_type,
            market_spot=option_input.spot_price,
            strike=option_input.strike_price,
            time_to_expiry_years=t,
            risk_free_rate=option_input.risk_free_rate,
            dividend_yield=option_input.dividend_yield,
            sigma=low,
        )
        high_price, *_ = self._price_and_vega(
            option_type=option_input.normalized_option_type,
            market_spot=option_input.spot_price,
            strike=option_input.strike_price,
            time_to_expiry_years=t,
            risk_free_rate=option_input.risk_free_rate,
            dividend_yield=option_input.dividend_yield,
            sigma=high,
        )
        if option_input.market_price < low_price - self._config.absolute_price_tolerance:
            return low, 0
        if option_input.market_price > high_price + self._config.absolute_price_tolerance:
            raise IVComputationError("market price lies above solvable maximum-volatility model price")
        for iteration in range(1, self._config.max_bisection_iterations + 1):
            mid = 0.5 * (low + high)
            mid_price, *_ = self._price_and_vega(
                option_type=option_input.normalized_option_type,
                market_spot=option_input.spot_price,
                strike=option_input.strike_price,
                time_to_expiry_years=t,
                risk_free_rate=option_input.risk_free_rate,
                dividend_yield=option_input.dividend_yield,
                sigma=mid,
            )
            error = mid_price - option_input.market_price
            if abs(error) <= self._config.absolute_price_tolerance:
                return mid, iteration
            if (mid - low) <= self._config.absolute_vol_tolerance:
                return mid, iteration
            if error > 0.0:
                high = mid
            else:
                low = mid
        raise IVComputationError("bisection solver failed to converge within configured iteration limit")

    def _no_arbitrage_bounds(self, *, option_input: IVInput, t: float) -> tuple[float, float]:
        s = option_input.spot_price
        k = option_input.strike_price
        r = option_input.risk_free_rate
        q = option_input.dividend_yield
        discounted_strike = k * exp(-r * t)
        discounted_spot = s * exp(-q * t)
        if option_input.normalized_option_type == "CALL":
            return max(0.0, discounted_spot - discounted_strike), discounted_spot
        return max(0.0, discounted_strike - discounted_spot), discounted_strike

    def _price_and_vega(
        self,
        *,
        option_type: str,
        market_spot: float,
        strike: float,
        time_to_expiry_years: float,
        risk_free_rate: float,
        dividend_yield: float,
        sigma: float,
    ) -> tuple[float, float, float, float, float, float, float]:
        sqrt_t = sqrt(time_to_expiry_years)
        sigma_sqrt_t = sigma * sqrt_t
        if sigma_sqrt_t <= 0.0:
            raise IVComputationError("sigma_sqrt_t must be positive for pricing")
        d1 = (log(market_spot / strike) + (risk_free_rate - dividend_yield + 0.5 * sigma * sigma) * time_to_expiry_years) / sigma_sqrt_t
        d2 = d1 - sigma_sqrt_t
        discount_factor = exp(-risk_free_rate * time_to_expiry_years)
        carry_discount_factor = exp(-dividend_yield * time_to_expiry_years)
        forward_price = market_spot * exp((risk_free_rate - dividend_yield) * time_to_expiry_years)
        if option_type == "CALL":
            model_price = market_spot * carry_discount_factor * self._normal_cdf(d1) - strike * discount_factor * self._normal_cdf(d2)
        else:
            model_price = strike * discount_factor * self._normal_cdf(-d2) - market_spot * carry_discount_factor * self._normal_cdf(-d1)
        vega = market_spot * carry_discount_factor * self._normal_pdf(d1) * sqrt_t
        return model_price, d1, d2, vega, discount_factor, carry_discount_factor, forward_price

    def _validate_input(self, option_input: IVInput) -> None:
        values = [
            option_input.market_price,
            option_input.spot_price,
            option_input.strike_price,
            option_input.time_to_expiry_years,
            option_input.risk_free_rate,
            option_input.dividend_yield,
        ]
        if not all(isfinite(value) for value in values):
            raise IVInputError("all numeric input values must be finite")

    def _normal_cdf(self, value: float) -> float:
        return 0.5 * (1.0 + erf(value / sqrt(2.0)))

    def _normal_pdf(self, value: float) -> float:
        return exp(-0.5 * value * value) / sqrt(2.0 * pi)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "IVComputationError",
    "IVEngine",
    "IVEngineConfig",
    "IVEngineError",
    "IVEngineState",
    "IVInput",
    "IVInputError",
    "IVResult",
]
