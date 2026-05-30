
from platform_core.backtesting.batch_backtester import (
    BatchBacktester,
)


backtester = BatchBacktester()

backtester.run_directory(
    directory="data",
    symbol="BANKNIFTY",
)

