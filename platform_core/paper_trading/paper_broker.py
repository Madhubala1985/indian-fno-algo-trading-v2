from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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

        print(
            f"\n[PAPER] OPENED "
            f"{direction} "
            f"{symbol} "
            f"@ {entry_price}"
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

        print(
            f"\n[PAPER] CLOSED "
            f"{symbol} "
            f"PnL={pnl:.2f}"
        )

    def has_open_position(
        self,
        *,
        symbol: str,
    ) -> bool:

        return symbol in self.positions