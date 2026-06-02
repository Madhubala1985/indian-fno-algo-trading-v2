
from __future__ import annotations

from pathlib import Path

from platform_core.backtesting.backtest_runner import (
    BacktestRunner,
)
from platform_core.paper_trading.terminal_display import (
    print_batch_header,
    print_batch_file,
    print_batch_footer,
    print_warning_block,
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
            print_warning_block(
                "NO CSV FILES FOUND",
                reason=f"directory={directory}",
            )
            return

        print_batch_header(count=len(csv_files))

        for idx, csv_file in enumerate(csv_files, start=1):

            print_batch_file(
                name=csv_file.name,
                index=idx,
                total=len(csv_files),
            )

            runner = BacktestRunner(
                symbol=symbol
            )

            runner.run(
                csv_path=str(csv_file)
            )

        print_batch_footer()

