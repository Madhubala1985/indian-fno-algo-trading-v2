
from __future__ import annotations

from pathlib import Path

from platform_core.backtesting.backtest_runner import (
    BacktestRunner,
)


class BatchBacktester:

    def run_directory(
        self,
        *,
        directory: str,
        symbol: str,
    ) -> None:

        csv_files = sorted(
            Path(directory).glob("*.csv")
        )

        if not csv_files:

            print(
                "\nNo CSV files found.\n"
            )

            return

        print(
            "\nBATCH BACKTEST STARTED\n"
        )

        for csv_file in csv_files:

            print(
                f"\nRUNNING: {csv_file.name}\n"
            )

            runner = BacktestRunner(
                symbol=symbol
            )

            runner.run(
                csv_path=str(csv_file)
            )

        print(
            "\nBATCH BACKTEST COMPLETED\n"
        )

