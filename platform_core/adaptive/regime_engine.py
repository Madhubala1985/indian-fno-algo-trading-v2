from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RegimeState:

    volatility_regime: str

    directional_regime: str

    confidence_multiplier: float


class RegimeEngine:

    def classify(
        self,
        *,
        bar_range: float,
        average_range: float,
    ) -> RegimeState:

        high_volatility = (
            bar_range
            > average_range * 1.5
        )

        low_volatility = (
            bar_range
            < average_range * 0.7
        )

        if high_volatility:

            return RegimeState(

                volatility_regime=(
                    "HIGH_VOLATILITY"
                ),

                directional_regime=(
                    "EXPANSION"
                ),

                confidence_multiplier=1.25,
            )

        if low_volatility:

            return RegimeState(

                volatility_regime=(
                    "LOW_VOLATILITY"
                ),

                directional_regime=(
                    "COMPRESSION"
                ),

                confidence_multiplier=0.75,
            )

        return RegimeState(

            volatility_regime="NORMAL",

            directional_regime="BALANCED",

            confidence_multiplier=1.0,
        )
