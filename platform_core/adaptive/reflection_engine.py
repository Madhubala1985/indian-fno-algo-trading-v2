from __future__ import annotations

from dataclasses import dataclass

from platform_core.adaptive.market_memory import (
    MarketMemoryRecord,
)


@dataclass(slots=True)
class ReflectionResult:

    outcome: str

    insight: str

    adaptation: str


class ReflectionEngine:

    def reflect(
        self,
        *,
        memory: MarketMemoryRecord,
    ) -> ReflectionResult:

        if (
            memory.pnl is not None
            and memory.pnl > 0
        ):

            return ReflectionResult(

                outcome="WIN",

                insight=(
                    "Structure alignment "
                    "was favorable."
                ),

                adaptation=(
                    "Increase confidence "
                    "for similar event "
                    "combinations."
                ),
            )

        return ReflectionResult(

            outcome="LOSS",

            insight=(
                "Structure failed "
                "or regime changed."
            ),

            adaptation=(
                "Reduce confidence "
                "during similar "
                "conditions."
            ),
        )
