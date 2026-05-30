from __future__ import annotations

from dataclasses import dataclass

from platform_core.analytics.structure.models import (
    StructureEventType,
    StructureSide,
)

from platform_core.adaptive.regime_engine import (
    RegimeState,
)

from platform_core.intelligence.market_context import (
    MarketContext,
)


@dataclass(slots=True)
class TradeSignal:

    symbol: str

    direction: str

    confidence: float

    reason: str

    action: str

    regime: str


class SignalEngine:

    def generate_signals(
        self,
        *,
        symbol: str,
        events,
        regime: RegimeState,
        market_context: MarketContext,
    ) -> list[TradeSignal]:

        signals = []

        bullish_bos = False
        bearish_bos = False

        bullish_choch = False
        bearish_choch = False

        bullish_fvg = False
        bearish_fvg = False

        for event in events:

            if (
                event.event_type
                == StructureEventType.BOS
            ):

                if (
                    event.side
                    == StructureSide.BULLISH
                ):
                    bullish_bos = True

                elif (
                    event.side
                    == StructureSide.BEARISH
                ):
                    bearish_bos = True

            elif (
                event.event_type
                == StructureEventType.CHOCH
            ):

                if (
                    event.side
                    == StructureSide.BULLISH
                ):
                    bullish_choch = True

                elif (
                    event.side
                    == StructureSide.BEARISH
                ):
                    bearish_choch = True

            elif (
                event.event_type
                == StructureEventType.FVG_CREATED
            ):

                if (
                    event.side
                    == StructureSide.BULLISH
                ):
                    bullish_fvg = True

                elif (
                    event.side
                    == StructureSide.BEARISH
                ):
                    bearish_fvg = True

        base_confidence = (
            0.75
            * regime.confidence_multiplier
        )

        if not market_context.is_aligned:

            return []

        if (
            market_context.confidence_score
            < 0.50
        ):

            return []

        if (
            (
                bullish_bos
                or bullish_fvg
            )
            and (
                market_context
                .liquidity_direction
                == "BULLISH"
            )
        ):

            signals.append(
                TradeSignal(
                    symbol=symbol,
                    direction="LONG",
                    confidence=base_confidence,
                    reason=(
                        "Bullish structure "
                        "confluence detected"
                    ),
                    action="OPEN",
                    regime=(
                        regime.volatility_regime
                    ),
                )
            )

        if (
            (
                bearish_bos
                or bearish_choch
            )
            and (
                market_context
                .liquidity_direction
                == "BEARISH"
            )
        ):

            signals.append(
                TradeSignal(
                    symbol=symbol,
                    direction="SHORT",
                    confidence=base_confidence,
                    reason=(
                        "Bearish structure "
                        "confluence detected"
                    ),
                    action="OPEN",
                    regime=(
                        regime.volatility_regime
                    ),
                )
            )

        if (
            bearish_bos
            or bearish_choch
        ):

            signals.append(
                TradeSignal(
                    symbol=symbol,
                    direction="LONG",
                    confidence=0.80,
                    reason=(
                        "Bullish structure "
                        "failed"
                    ),
                    action="CLOSE",
                    regime=(
                        regime.volatility_regime
                    ),
                )
            )

        if (
            bullish_bos
            or bullish_choch
        ):

            signals.append(
                TradeSignal(
                    symbol=symbol,
                    direction="SHORT",
                    confidence=0.80,
                    reason=(
                        "Bearish structure "
                        "failed"
                    ),
                    action="CLOSE",
                    regime=(
                        regime.volatility_regime
                    ),
                )
            )

        return signals