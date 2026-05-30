
from __future__ import annotations

from dataclasses import dataclass

from platform_core.paper_trading.paper_broker import (
    PaperTrade,
)


@dataclass(slots=True)
class PerformanceSummary:

    total_trades: int

    winning_trades: int

    losing_trades: int

    total_pnl: float

    win_rate: float

    average_pnl: float


class PerformanceTracker:

    def calculate_summary(
        self,
        *,
        trades: list[PaperTrade],
    ) -> PerformanceSummary:

        total_trades = len(trades)

        winning_trades = sum(
            1
            for trade in trades
            if trade.pnl > 0
        )

        losing_trades = sum(
            1
            for trade in trades
            if trade.pnl <= 0
        )

        total_pnl = sum(
            trade.pnl
            for trade in trades
        )

        win_rate = (
            winning_trades / total_trades
            if total_trades > 0
            else 0.0
        )

        average_pnl = (
            total_pnl / total_trades
            if total_trades > 0
            else 0.0
        )

        return PerformanceSummary(
            total_trades=total_trades,

            winning_trades=winning_trades,

            losing_trades=losing_trades,

            total_pnl=total_pnl,

            win_rate=win_rate,

            average_pnl=average_pnl,
        )

