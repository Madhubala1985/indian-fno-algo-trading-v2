from dataclasses import dataclass


@dataclass(slots=True)
class MarketContext:

    daily_bias: str

    h4_bias: str

    h1_bias: str

    m15_bias: str

    liquidity_direction: str

    volatility_regime: str

    confidence_score: float

    is_aligned: bool
