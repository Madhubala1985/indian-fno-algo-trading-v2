# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Institutional-grade automated trading platform targeting Indian Index Derivatives (BankNifty, Nifty, FinNifty) on NSE/BSE via Zerodha Kite Connect. Python 3.12, async-first, event-driven architecture.

## Common Commands

```bash
# Run a single-symbol backtest
python scripts/run_backtest.py

# Run batch backtest over all CSVs in data/
python scripts/run_batch_backtest.py

# Validate core imports
python scripts/system_smoke_test.py

# Boot test (orchestrator lifecycle)
python scripts/runtime_boot_test.py

# Generate/refresh Zerodha Kite access token (interactive browser login)
python scripts/generate_kite_session.py

# Fetch BankNifty historical data
python scripts/fetch_banknifty_history.py

# Live Kite connectivity test
python scripts/live_kite_test.py
```

## Architecture

All production code lives under `platform_core/`. The package is organized by domain:

**Foundation (`core/`)** — The zero-business-logic layer shared by all domains. `enums.py` defines all `StrEnum`/`IntFlag` types; `events.py` defines `EventEnvelope` (the only cross-service data contract, built with Pydantic, frozen, JSON-safe); `interfaces.py` defines all `Protocol`/ABC contracts (`SupportsLifecycle`, `EventBus`, `StorageWriter`, etc.); `config.py` holds Pydantic-Settings-based runtime configuration.

**Orchestration (`orchestration/`)** — `MainOrchestrator` handles dependency-aware async service start/stop with topological sort. Services register via `register_service(name, service, dependencies=(...))` and implement `start()`/`stop()`/`health_check()`. Shutdown is always reverse-dependency order. The `AsyncEventBus` inside enforces non-decreasing timestamps for replay safety and buffers an audit log.

**Data (`data/`)** — Ingestion pipeline: `websocket_ingestion.py` → `validation_pipeline.py` → `bar_builder.py` / `multi_timeframe_builder.py`. Persistence backends: `redis_cache.py` (hot layer), `timescaledb_interface.py` + `postgresql_interface.py` (cold layer). `replay_engine.py` and `backtesting/historical_replay.py` drive deterministic replay from CSV or stored ticks.

**Analytics (`analytics/structure/`)** — Market structure analysis (causal, no lookahead). Pipeline: `SwingDetector` → `LiquiditySweepDetector` + `BOSDetector` + `CHOCHDetector` + `FVGEngine` + `SupportResistanceEngine`, all coordinated by `StructureEngine`. All engines accept a finalized `StructureBar` and emit `StructureEvent` objects; no future bar data is read.

**Options (`options/`)** — `GreeksEngine`, `IVEngine`, `IVSurfaceEngine`, `OIAnalytics`, `GammaExposure`, `DealerHedging`, `PremiumBehavior`. Feed from option chain snapshots via `data/option_chain_ingestion.py`.

**Intelligence & Adaptive (`intelligence/`, `adaptive/`)** — `HTFBiasEngine` establishes higher-timeframe directional bias; `MarketContext`/`ContextBuilder` aggregate structure + options context for signal generation. `RegimeEngine` classifies volatility/directional regimes; `AdaptivePolicyEngine` + `MarketMemory` + `ReflectionEngine` provide feedback-loop adaptation.

**ML (`ml/`)** — `FeaturePipeline` builds feature vectors from structure events + regime + options data. Classifiers (`RegimeClassifier`, `ContinuationClassifier`, `ReversalClassifier`, `TrapProbabilityModel`) gate `ConfidenceEngine` output.

**Signals (`signals/`)** — `SignalEngine.generate_signals()` consumes `StructureEvent` lists + `RegimeState` + `MarketContext` to emit `TradeSignal` dataclasses.

**Risk (`risk/`)** — `PositionSizingEngine` applies per-trade risk fraction, volatility adjustment, confidence scaling, drawdown haircuts, and lot-size rounding. Layered safety: `DrawdownProtection` → `PortfolioHeat` → `ExposureControl` → `KillSwitches` → `EmergencyShutdown`.

**Execution (`execution/`)** — `ZerodhaIntegration` wraps Kite Connect REST + WebSocket (gracefully degrades if `pykiteconnect` is absent). `OrderManager` owns order lifecycle. `ReconciliationEngine` reconciles broker state vs. local state. `LatencyMonitor` enforces microsecond-level SLAs.

**Paper Trading (`paper_trading/`)** — `PaperBroker` + `PerformanceTracker` + `TradeLogger` for simulated execution during backtesting.

**Backtesting (`backtesting/`)** — `BacktestRunner` wires all engines for a single symbol/CSV. `BatchBacktester` loops over a directory. `HistoricalReplayFeed` drives bar-by-bar replay.

**Testing (`testing/`)** — Framework modules: `SimulationFramework`, `StressTesting`, `MonteCarloFramework`, `WalkforwardFramework`, `LatencySimulation`, `SlippageSimulation`. Actual test files go in `tests/`.

## Key Conventions

**Enums** — All domain enums use `@unique StrEnum` for safe JSON/Redis/PostgreSQL serialization. Never use string literals where an enum value exists. `TickQualityFlag` and `SafeStateFlag` are `IntFlag` for bitwise composition.

**Events** — Always use `build_event_envelope()` / `build_event_metadata()` from `core/events.py`. Set `replay=True` on events replayed from history. `EventEnvelope.topic_name()` returns `"<domain>.<event_type>"` for routing. `clone_event_envelope()` preserves `correlation_id` and chains `causation_id`.

**Services** — Every long-lived service inherits `BaseService` (ABC) and must implement `start()`, `stop()`, `healthcheck()`. Register with `MainOrchestrator` before calling `orchestrator.start()`.

**No lookahead** — Structure analytics must only consume the current finalized bar plus previously emitted events. Never index forward into bar arrays.

**Credentials** — `configs/kite_config.json` stores live Zerodha credentials (`api_key`, `api_secret`, `access_token`). The access token expires daily; regenerate with `scripts/generate_kite_session.py`. Do not commit changes to this file.

**Data format** — Historical CSV files in `data/` are OHLCV with a timestamp column. `BacktestRunner` defaults to `symbol="BANKNIFTY"`, `timeframe="MIN_1"`. `BatchBacktester.run_directory(directory="data", symbol="BANKNIFTY")` processes all CSVs in the directory.
