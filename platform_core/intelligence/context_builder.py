from platform_core.intelligence.market_context import (
    MarketContext,
)


class ContextBuilder:

    def build(
        self,
        *,
        daily_bias: str,
        h4_bias: str,
        h1_bias: str,
        m15_bias: str,
        volatility_regime: str,
    ) -> MarketContext:

        bullish = sum(
            [
                daily_bias == "BULLISH",
                h4_bias == "BULLISH",
                h1_bias == "BULLISH",
                m15_bias == "BULLISH",
            ]
        )

        bearish = sum(
            [
                daily_bias == "BEARISH",
                h4_bias == "BEARISH",
                h1_bias == "BEARISH",
                m15_bias == "BEARISH",
            ]
        )

        confidence = (
            max(
                bullish,
                bearish,
            )
            / 4
        )

        aligned = (
            bullish >= 3
            or bearish >= 3
        )

        return MarketContext(
            daily_bias=daily_bias,
            h4_bias=h4_bias,
            h1_bias=h1_bias,
            m15_bias=m15_bias,
            liquidity_direction=(
                "BULLISH"
                if bullish >= bearish
                else "BEARISH"
            ),
            volatility_regime=(
                volatility_regime
            ),
            confidence_score=confidence,
            is_aligned=aligned,
        )
