import MetaTrader5 as mt5
mt5.initialize(timeout=30000)
ti=mt5.terminal_info()
print("ALGO_TRADING =", ti.trade_allowed, "->", "ENABLED - ready" if ti.trade_allowed else "STILL OFF (toolbar button / Ctrl+E)")
mt5.shutdown()
