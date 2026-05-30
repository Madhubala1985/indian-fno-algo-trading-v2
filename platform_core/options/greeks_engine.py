# FILE: platform_core/options/greeks_engine.py

"""
platform_core.options.greeks_engine
═══════════════════════════════════════════════════════════════════════
Production-grade Black-Scholes-Merton Greeks engine for Indian index options.

The engine is deterministic, JSON-serializable, replay-safe, and suitable for
incremental real-time usage within an event-driven options analytics pipeline.
It supports European call and put contracts with continuous carry inputs and
returns stable price and Greeks outputs with explicit scaling conventions.
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


class GreeksEngineError(RuntimeError):
    """Base exception for Greeks engine failures."""


class GreeksInputError(GreeksEngineError):
    """Raised when input values are invalid or numerically unsafe."""


class GreeksComputationError(GreeksEngineError):
    """Raised when stable Greeks computation cannot be completed."""


@dataclass(frozen=True, slots=True)
class GreeksEngineConfig:
    """Configuration controlling numerical safeguards and output scaling."""

    minimum_time_to_expiry_years: float = 1.0e-8
    minimum_volatility: float = 1.0e-6
    maximum_volatility: float = 10.0
    vega_scale_per_1_vol: bool = False
    rho_scale_per_1_rate: bool = False
    theta_scale_per_day: bool = True
    calendar_days_per_year: int = 365

    def __post_init__(self) -> None:

        if self.minimum_time_to_expiry_years <= 0:
            raise ValueError(
                "minimum_time_to_expiry_years must be positive"
            )

        if self.minimum_volatility <= 0:
            raise ValueError(
                "minimum_volatility must be positive"
            )

        if (
            self.maximum_volatility
            <= self.minimum_volatility
        ):
            raise ValueError(
                "maximum_volatility must exceed minimum_volatility"
            )

        if self.calendar_days_per_year <= 0:
            raise ValueError(
                "calendar_days_per_year must be positive"
            )


@dataclass(frozen=True, slots=True)
class OptionGreeksInput:
    """Serializable BSM input for a single option contract snapshot."""

    instrument_id: str
    underlying_symbol: str
    option_symbol: str
    option_type: str
    spot_price: float
    strike_price: float
    time_to_expiry_years: float
    implied_volatility: float
    risk_free_rate: float
    dividend_yield: float = 0.0
    valuation_timestamp: datetime | None = None
    expiry_timestamp: datetime | None = None
    lot_size: int | None = None

    def __post_init__(self) -> None:

        option_type_normalized = self.option_type.upper()

        if option_type_normalized not in {
            "CALL",
            "PUT",
        }:
            raise ValueError(
                "option_type must be CALL or PUT"
            )

        if not self.instrument_id.strip():
            raise ValueError(
                "instrument_id must be non-empty"
            )

        if not self.underlying_symbol.strip():
            raise ValueError(
                "underlying_symbol must be non-empty"
            )

        if not self.option_symbol.strip():
            raise ValueError(
                "option_symbol must be non-empty"
            )

        if self.spot_price <= 0:
            raise ValueError(
                "spot_price must be positive"
            )

        if self.strike_price <= 0:
            raise ValueError(
                "strike_price must be positive"
            )

        if self.time_to_expiry_years < 0:
            raise ValueError(
                "time_to_expiry_years must be non-negative"
            )

        if self.implied_volatility < 0:
            raise ValueError(
                "implied_volatility must be non-negative"
            )

        if (
            self.lot_size is not None
            and self.lot_size <= 0
        ):
            raise ValueError(
                "lot_size must be positive when provided"
            )

    @property
    def normalized_option_type(self) -> str:
        return self.option_type.upper()

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "instrument_id": self.instrument_id,
            "underlying_symbol": self.underlying_symbol,
            "option_symbol": self.option_symbol,
            "option_type": self.normalized_option_type,
            "spot_price": self.spot_price,
            "strike_price": self.strike_price,
            "time_to_expiry_years": (
                self.time_to_expiry_years
            ),
            "implied_volatility": (
                self.implied_volatility
            ),
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "valuation_timestamp": (
                self.valuation_timestamp.isoformat()
                if self.valuation_timestamp
                else None
            ),
            "expiry_timestamp": (
                self.expiry_timestamp.isoformat()
                if self.expiry_timestamp
                else None
            ),
            "lot_size": self.lot_size,
        }


@dataclass(frozen=True, slots=True)
class OptionGreeksResult:
    """Serializable option price and Greeks result."""

    instrument_id: str
    underlying_symbol: str
    option_symbol: str
    option_type: str
    valuation_timestamp: datetime | None
    expiry_timestamp: datetime | None
    spot_price: float
    strike_price: float
    time_to_expiry_years: float
    implied_volatility: float
    risk_free_rate: float
    dividend_yield: float
    forward_price: float
    discount_factor: float
    carry_discount_factor: float
    d1: float
    d2: float
    option_price: float
    intrinsic_value: float
    extrinsic_value: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    vomma: float
    vanna: float
    charm: float
    delta_notional: float | None
    gamma_notional: float | None
    vega_notional: float | None
    theta_notional: float | None
    rho_notional: float | None
    lot_size: int | None
    metadata: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "instrument_id": self.instrument_id,
            "underlying_symbol": self.underlying_symbol,
            "option_symbol": self.option_symbol,
            "option_type": self.option_type,
            "valuation_timestamp": (
                self.valuation_timestamp.isoformat()
                if self.valuation_timestamp
                else None
            ),
            "expiry_timestamp": (
                self.expiry_timestamp.isoformat()
                if self.expiry_timestamp
                else None
            ),
            "spot_price": self.spot_price,
            "strike_price": self.strike_price,
            "time_to_expiry_years": (
                self.time_to_expiry_years
            ),
            "implied_volatility": (
                self.implied_volatility
            ),
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "forward_price": self.forward_price,
            "discount_factor": self.discount_factor,
            "carry_discount_factor": (
                self.carry_discount_factor
            ),
            "d1": self.d1,
            "d2": self.d2,
            "option_price": self.option_price,
            "intrinsic_value": self.intrinsic_value,
            "extrinsic_value": self.extrinsic_value,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega": self.vega,
            "theta": self.theta,
            "rho": self.rho,
            "vomma": self.vomma,
            "vanna": self.vanna,
            "charm": self.charm,
            "delta_notional": self.delta_notional,
            "gamma_notional": self.gamma_notional,
            "vega_notional": self.vega_notional,
            "theta_notional": self.theta_notional,
            "rho_notional": self.rho_notional,
            "lot_size": self.lot_size,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class GreeksEngineState:
    """Serializable engine state for replay-safe diagnostics."""

    last_valuation_timestamp: datetime | None
    last_option_symbol: str | None
    processed_count: int

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "last_valuation_timestamp": (
                self.last_valuation_timestamp.isoformat()
                if self.last_valuation_timestamp
                else None
            ),
            "last_option_symbol": (
                self.last_option_symbol
            ),
            "processed_count": self.processed_count,
        }


class GreeksEngine:
    """Deterministic BSM price and Greeks engine."""

    def __init__(
        self,
        *,
        config: GreeksEngineConfig | None = None,
    ) -> None:

        self._config = (
            config
            if config is not None
            else GreeksEngineConfig()
        )

        self._last_valuation_timestamp = None
        self._last_option_symbol = None
        self._processed_count = 0

    @property
    def state(self) -> GreeksEngineState:

        return GreeksEngineState(
            last_valuation_timestamp=(
                self._last_valuation_timestamp
            ),
            last_option_symbol=(
                self._last_option_symbol
            ),
            processed_count=self._processed_count,
        )

    def reset(self) -> None:

        self._last_valuation_timestamp = None
        self._last_option_symbol = None
        self._processed_count = 0

    def compute(
        self,
        *,
        option_input: OptionGreeksInput,
    ) -> OptionGreeksResult:

        self._validate_input(option_input)

        sigma = self._bounded_volatility(
            option_input.implied_volatility
        )

        t = self._bounded_time(
            option_input.time_to_expiry_years
        )

        s = option_input.spot_price
        k = option_input.strike_price
        r = option_input.risk_free_rate
        q = option_input.dividend_yield

        sqrt_t = sqrt(t)

        sigma_sqrt_t = sigma * sqrt_t

        discount_factor = exp(-r * t)

        carry_discount_factor = exp(-q * t)

        forward_price = s * exp(
            (r - q) * t
        )

        try:

            d1 = (
                (
                    log(s / k)
                    + (
                        r
                        - q
                        + 0.5 * sigma * sigma
                    )
                    * t
                )
                / sigma_sqrt_t
            )

            d2 = d1 - sigma_sqrt_t

        except ValueError as exc:

            raise GreeksComputationError(
                "failed to compute d1/d2"
            ) from exc

        nd1 = self._normal_cdf(d1)
        nd2 = self._normal_cdf(d2)

        npd1 = self._normal_pdf(d1)

        option_type = (
            option_input.normalized_option_type
        )

        if option_type == "CALL":

            option_price = (
                s
                * carry_discount_factor
                * nd1
            ) - (
                k
                * discount_factor
                * nd2
            )

            delta = (
                carry_discount_factor
                * nd1
            )

            theta_annual = (
                -(
                    s
                    * carry_discount_factor
                    * npd1
                    * sigma
                )
                / (2.0 * sqrt_t)
                - r
                * k
                * discount_factor
                * nd2
                + q
                * s
                * carry_discount_factor
                * nd1
            )

            rho = (
                k
                * t
                * discount_factor
                * nd2
            )

            intrinsic_value = max(
                0.0,
                s - k,
            )

        else:

            option_price = (
                k
                * discount_factor
                * self._normal_cdf(-d2)
            ) - (
                s
                * carry_discount_factor
                * self._normal_cdf(-d1)
            )

            delta = (
                carry_discount_factor
                * (nd1 - 1.0)
            )

            theta_annual = (
                -(
                    s
                    * carry_discount_factor
                    * npd1
                    * sigma
                )
                / (2.0 * sqrt_t)
                + r
                * k
                * discount_factor
                * self._normal_cdf(-d2)
                - q
                * s
                * carry_discount_factor
                * self._normal_cdf(-d1)
            )

            rho = (
                -k
                * t
                * discount_factor
                * self._normal_cdf(-d2)
            )

            intrinsic_value = max(
                0.0,
                k - s,
            )

        gamma = (
            carry_discount_factor
            * npd1
            / (
                s
                * sigma_sqrt_t
            )
        )

        vega = (
            s
            * carry_discount_factor
            * npd1
            * sqrt_t
        )

        vomma = (
            vega
            * d1
            * d2
            / sigma
        )

        vanna = (
            -carry_discount_factor
            * npd1
            * d2
            / sigma
        )

        theta = (
            theta_annual
            / float(
                self._config.calendar_days_per_year
            )
            if self._config.theta_scale_per_day
            else theta_annual
        )

        if not self._config.vega_scale_per_1_vol:

            vega /= 100.0
            vomma /= 10000.0
            vanna /= 100.0

        if not self._config.rho_scale_per_1_rate:

            rho /= 100.0

        option_price = max(
            0.0,
            option_price,
        )

        extrinsic_value = max(
            0.0,
            option_price - intrinsic_value,
        )

        (
            delta_notional,
            gamma_notional,
            vega_notional,
            theta_notional,
            rho_notional,
        ) = self._compute_notionals(
            spot_price=s,
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            rho=rho,
            lot_size=option_input.lot_size,
        )

        result = OptionGreeksResult(
            instrument_id=option_input.instrument_id,
            underlying_symbol=(
                option_input.underlying_symbol
            ),
            option_symbol=(
                option_input.option_symbol
            ),
            option_type=option_type,
            valuation_timestamp=(
                _ensure_utc(
                    option_input.valuation_timestamp
                )
                if option_input.valuation_timestamp
                else None
            ),
            expiry_timestamp=(
                _ensure_utc(
                    option_input.expiry_timestamp
                )
                if option_input.expiry_timestamp
                else None
            ),
            spot_price=s,
            strike_price=k,
            time_to_expiry_years=t,
            implied_volatility=sigma,
            risk_free_rate=r,
            dividend_yield=q,
            forward_price=forward_price,
            discount_factor=discount_factor,
            carry_discount_factor=(
                carry_discount_factor
            ),
            d1=d1,
            d2=d2,
            option_price=option_price,
            intrinsic_value=intrinsic_value,
            extrinsic_value=extrinsic_value,
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            rho=rho,
            vomma=vomma,
            vanna=vanna,
            charm=0.0,
            delta_notional=delta_notional,
            gamma_notional=gamma_notional,
            vega_notional=vega_notional,
            theta_notional=theta_notional,
            rho_notional=rho_notional,
            lot_size=option_input.lot_size,
            metadata={
                "model": (
                    "black_scholes_merton"
                )
            },
        )

        self._processed_count += 1

        self._last_valuation_timestamp = (
            result.valuation_timestamp
        )

        self._last_option_symbol = (
            result.option_symbol
        )

        return result

    def compute_many(
        self,
        *,
        option_inputs: list[
            OptionGreeksInput
        ],
    ) -> tuple[OptionGreeksResult, ...]:

        return tuple(
            self.compute(
                option_input=option_input
            )
            for option_input in option_inputs
        )

    def _validate_input(
        self,
        option_input: OptionGreeksInput,
    ) -> None:

        scalar_values = [
            option_input.spot_price,
            option_input.strike_price,
            option_input.time_to_expiry_years,
            option_input.implied_volatility,
            option_input.risk_free_rate,
            option_input.dividend_yield,
        ]

        if not all(
            isfinite(value)
            for value in scalar_values
        ):
            raise GreeksInputError(
                "all numeric input values must be finite"
            )

    def _bounded_time(
        self,
        value: float,
    ) -> float:

        return max(
            value,
            self._config.minimum_time_to_expiry_years,
        )

    def _bounded_volatility(
        self,
        value: float,
    ) -> float:

        bounded = max(
            value,
            self._config.minimum_volatility,
        )

        bounded = min(
            bounded,
            self._config.maximum_volatility,
        )

        return bounded

    def _compute_notionals(
        self,
        *,
        spot_price: float,
        delta: float,
        gamma: float,
        vega: float,
        theta: float,
        rho: float,
        lot_size: int | None,
    ) -> tuple[
        float | None,
        float | None,
        float | None,
        float | None,
        float | None,
    ]:

        if lot_size is None:
            return (
                None,
                None,
                None,
                None,
                None,
            )

        lot_multiplier = float(lot_size)

        return (
            delta
            * spot_price
            * lot_multiplier,

            gamma
            * spot_price
            * spot_price
            * lot_multiplier,

            vega * lot_multiplier,

            theta * lot_multiplier,

            rho * lot_multiplier,
        )

    def _normal_cdf(
        self,
        value: float,
    ) -> float:

        return 0.5 * (
            1.0
            + erf(
                value / sqrt(2.0)
            )
        )

    def _normal_pdf(
        self,
        value: float,
    ) -> float:

        return (
            exp(
                -0.5
                * value
                * value
            )
            / sqrt(2.0 * pi)
        )


def _ensure_utc(
    value: datetime,
) -> datetime:

    if value.tzinfo is None:

        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


__all__ = [
    "GreeksComputationError",
    "GreeksEngine",
    "GreeksEngineConfig",
    "GreeksEngineError",
    "GreeksEngineState",
    "GreeksInputError",
    "OptionGreeksInput",
    "OptionGreeksResult",
]