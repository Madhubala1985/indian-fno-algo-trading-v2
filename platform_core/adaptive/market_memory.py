from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class MarketMemoryRecord:

    symbol: str

    direction: str

    confidence: float

    events: tuple[str, ...]

    entry_price: float

    exit_price: float | None

    pnl: float | None

    timestamp: datetime

    outcome: str | None


class MarketMemory:

    def __init__(self) -> None:

        self._records = []

    def remember_trade(
        self,
        *,
        record: MarketMemoryRecord,
    ) -> None:

        self._records.append(record)

    @property
    def records(self):

        return tuple(self._records)

    def successful_patterns(self):

        return [
            r
            for r in self._records
            if (
                r.outcome == "WIN"
            )
        ]

    def failed_patterns(self):

        return [
            r
            for r in self._records
            if (
                r.outcome == "LOSS"
            )
        ]
