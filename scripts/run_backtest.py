
from platform_core.backtesting.backtest_runner import (
    BacktestRunner,
)


runner = BacktestRunner(
    symbol="BANKNIFTY"
)

runner.run(
    csv_path="data/banknifty_real.csv"
)

