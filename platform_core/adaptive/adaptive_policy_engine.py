from __future__ import annotations

from collections import defaultdict


class AdaptivePolicyEngine:

    def __init__(self) -> None:

        self._pattern_scores = (
            defaultdict(
                lambda: 0.75
            )
        )

    def pattern_key(
        self,
        *,
        events: tuple[str, ...],
        regime: str,
    ) -> str:

        ordered_events = sorted(events)

        return (
            "|".join(ordered_events)
            + f"|{regime}"
        )

    def confidence(
        self,
        *,
        events: tuple[str, ...],
        regime: str,
    ) -> float:

        key = self.pattern_key(
            events=events,
            regime=regime,
        )

        return self._pattern_scores[key]

    def reinforce(
        self,
        *,
        events: tuple[str, ...],
        regime: str,
        pnl: float,
    ) -> None:

        key = self.pattern_key(
            events=events,
            regime=regime,
        )

        current = (
            self._pattern_scores[key]
        )

        if pnl > 0:

            current += 0.05

        else:

            current -= 0.05

        current = max(
            0.10,
            min(current, 1.0),
        )

        self._pattern_scores[key] = (
            current
        )

        print(
            "\n[POLICY ENGINE UPDATED]\n"
        )

        print(
            f"Pattern={key}"
        )

        print(
            f"New Confidence={current}"
        )

