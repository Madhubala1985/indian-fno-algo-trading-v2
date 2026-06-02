"""
platform_core.persistence
═══════════════════════════════════════════════════════════════════════
Additive persistence layer for trades, decisions, structure events,
and runtime events.

All writes are fire-and-forget via a background thread queue.  Failures
are silently ignored so the persistence layer never affects trading logic.

Usage (backtest):
    from platform_core.persistence.trade_database import TradeDatabase
    db = TradeDatabase("logs/trades.db")
    db.start(session_id="bt_BANKNIFTY_20260602", run_mode="backtest")
    runner = BacktestRunner(symbol="BANKNIFTY", db_writer=db)
    runner.run(csv_path="data/BN_2024.csv")
    db.flush()

Usage (live — module-level init):
    Handled internally by live_structure_pipeline.py on startup.
"""
from platform_core.persistence.trade_database import TradeDatabase

__all__ = ["TradeDatabase"]
