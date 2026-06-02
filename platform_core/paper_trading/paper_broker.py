from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from platform_core.paper_trading.terminal_display import (
    print_broker_open,
    print_broker_close,
)


@dataclass(slots=True)
class PaperPosition:

    symbol: str

    direction: str

    entry_price: float

    quantity: int

    entry_timestamp: datetime


@dataclass(slots=True)
class PaperTrade:

    symbol: str

    direction: str

    entry_price: float

    exit_price: float

    quantity: int

    pnl: float


class PaperBroker:

    def __init__(self) -> None:

        self.positions = {}

        self.completed_trades = []

    def open_position(
        self,
        *,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: int,
    ) -> None:

        if symbol in self.positions:
            return

        self.positions[symbol] = (
            PaperPosition(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                entry_timestamp=datetime.utcnow(),
            )
        )

        print_broker_open(
            direction=direction,
            symbol=symbol,
            price=entry_price,
            quantity=quantity,
        )

    def close_position(
        self,
        *,
        symbol: str,
        exit_price: float,
    ) -> None:

        position = self.positions.get(
            symbol
        )

        if position is None:
            return

        pnl = (
            (
                exit_price
                - position.entry_price
            )
            * position.quantity
        )

        if (
            position.direction
            == "SHORT"
        ):

            pnl *= -1

        trade = PaperTrade(
            symbol=position.symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl=pnl,
        )

        self.completed_trades.append(
            trade
        )

        del self.positions[symbol]

        print_broker_close(
            symbol=symbol,
            pnl=pnl,
            entry_price=position.entry_price,
            exit_price=exit_price,
        )

    def has_open_position(
        self,
        *,
        symbol: str,
    ) -> bool:

        return symbol in self.positions