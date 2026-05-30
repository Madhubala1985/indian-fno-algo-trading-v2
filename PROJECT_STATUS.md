# PROJECT_STATUS.md
> Authoritative handoff document for future Claude sessions and future development.
> Reflects repository state as of 2026-05-30 on branch `paper-trading`.

---

## 1. Project Objective

Institutional-grade automated trading system targeting Indian Index Derivatives (BankNifty, Nifty, FinNifty) on NSE via Zerodha Kite Connect.

- **Target instruments:** BankNifty weekly options (primary), Nifty, FinNifty
- **Broker integration:** Zerodha Kite Connect (REST + WebSocket)
- **Execution mode:** Live paper trading — no real orders placed
- **Virtual capital model:** ₹1,00,000 starting equity, tiered risk fractions
- **Runtime language:** Python 3.12, async-first, event-driven

---

## 2. Current Architecture

### MarketContextBuilder
**File:** `platform_core/intelligence/market_context_builder.py`

Multi-timeframe directional bias engine. Maintains one `RegimeClassifier` per timeframe. Bars are fed via `update(bar, timeframe)`; `build()` produces a `MarketContext`.

Supported timeframes: `1m`, `15m`, `1h`, `4h`, `1d`

Timeframe weights (bias fusion):
| Timeframe | Weight | Role |
|---|---|---|
| `1d` | 0.35 | Macro bias |
| `4h` | 0.25 | Institutional trend |
| `1h` | 0.20 | Market structure |
| `15m` | weighted | Execution zone |
| `1m` | minimal | Trigger confirmation |

Bias thresholds: directional weighted score > threshold → LONG/SHORT; otherwise NEUTRAL.
Outputs: `MarketContext.bias` (StrEnum), `.confidence` [0,1], `.trend_score`, `.momentum_score`, `.volatility_regime`, `.liquidity_regime`, `.timeframes_with_history`.

`min_history_bars=24` (default). A timeframe does not appear in `timeframes_with_history` until it has seen 24+ bars. The live pipeline persists 30 bars per timeframe for warm-start recovery.

---

### SessionContextEngine
**File:** `platform_core/analytics/structure/session_context.py`

Classifies a bar timestamp into its NSE session phase (IST):

| Phase | Time | allow_entry | confidence_multiplier |
|---|---|---|---|
| PRE_OPEN | 09:00–09:14 | No | 0.0 |
| OPENING | 09:15–09:29 | No | 0.0 |
| MAIN | 09:30–14:29 | Yes | 1.0 |
| PRE_CLOSE | 14:30–14:59 | Yes | 0.65 |
| CLOSING | 15:00–15:29 | No | 0.0 |
| CLOSED | outside | No | 0.0 |

**Status: Active in both `BacktestRunner` and live pipeline (`live_structure_pipeline.py`).**

---

### PreviousDayLevelsEngine
**File:** `platform_core/analytics/structure/previous_day_levels.py`

Carries PDH (prior day high) and PDL (prior day low) forward from completed daily bars. Applies a `0.45×` confidence penalty when spot approaches an opposing level (within 0.3% proximity). Penalises: LONG near PDH (resistance), SHORT near PDL (support). Confirms with full multiplier when spot has broken through.

Updated by `DailyBarAccumulator` output. In the live pipeline, `_daily_acc` (a module-level `DailyBarAccumulator`) feeds `_pdl_engine` whenever the session date rolls over.

**Status: Active in both `BacktestRunner` and live pipeline (`live_structure_pipeline.py`).**

---

### OpeningRangeEngine
**File:** `platform_core/analytics/structure/opening_range.py`

Tracks the NSE 09:15–09:30 opening range per session. Finalised at 09:30.

Confidence adjustments:
- Spot inside OR → no entry (multiplier=0.0)
- LONG above OR → no penalty
- SHORT below OR → no penalty
- LONG below OR → 0.40× (contradiction)
- SHORT above OR → 0.40× (contradiction)

**Status: Active in both `BacktestRunner` and live pipeline (`live_structure_pipeline.py`).**

---

### FVG Quality Scorer
**File:** `platform_core/analytics/structure/fvg_scoring.py`

Six-factor FVG quality score in [0,1]:

| Factor | Weight | Description |
|---|---|---|
| Displacement strength | 0.25 | Middle-bar body ratio + gap ratio |
| Session quality | 0.15 | IST session phase at FVG creation time |
| Sweep origin | 0.20 | Aligned confirmed sweep recency + quality |
| HTF alignment | 0.20 | FVG direction vs current market bias |
| Freshness | 0.10 | 1 − mitigation_ratio |
| Volume participation | 0.10 | Middle-bar volume vs left bar |

MTF confluence bonus [0.0, 0.15] additive when lower-TF FVG sits inside an active higher-TF FVG of the same direction. In the live pipeline, dedicated `_fvg_15m` and `_fvg_1h` `FVGEngine` instances provide the HTF active gaps.

Block threshold: `total < 0.40` → hard block. Scores 0.30–0.70 reduce confidence proportionally.

**Status: Active in both `BacktestRunner` and live pipeline (`live_structure_pipeline.py`).**

---

### LiquiditySweepValidationEngine
**File:** `platform_core/analytics/structure/sweep_validation.py`

Validates sequential institutional setup: sweep → BOS/CHOCH → displacement FVG.

| Conditions met | is_valid | setup_score |
|---|---|---|
| 3/3 | True | 1.00 |
| 2/3 | True | 0.65 |
| ≤1/3 | False | 0.00 |

Warm-up bypass: first 60 bars bypass validation (structure not yet built). Max age window: 50 bars for BOS/displacement, 30 minutes for sweeps.

**Status: Wired into backtest as observability-only. The line that multiplies `setup_val.setup_score` into `effective_confidence` is explicitly commented out** (`# effective_confidence *= setup_val.setup_score  # disabled: sweep rarely fires`). Not in the live pipeline.

---

### OptionsContextBuilder
**File:** `platform_core/options/options_context/` (package)
**Entry point:** `OptionsContextBuilder.build(snapshots, spot_price, lot_size, valuation_timestamp)`

Runs five sub-engines over `OptionChainSnapshot` inputs:
- `OIAnalyticsSnapshot` — open interest distribution, max pain, PCR
- `GammaExposureSnapshot` — gamma walls, flip level, gamma regime
- `IVSurface` — ATM IV, skew, term structure
- `DealerHedgingSnapshot` — dealer delta and hedging flows
- `PremiumBehaviorSnapshot` — put/call premium ratios

Outputs a single `OptionsContext` containing `strike_candidates`, `atm_iv`, `pcr`, `max_pain`, `gamma_regime`, `oi_snapshot`, `gex_snapshot`.

When `compute_missing_greeks=True`, missing greeks are filled via BSM.

---

### StrikeSelectionEngine
**File:** `platform_core/options/strike_selection.py`

Seven-factor ranked contract selection:

| Factor | Description |
|---|---|
| Delta fitness | Proximity to target delta range (default 0.28–0.52) |
| OI support | OI concentration at the strike |
| OI activity | Absolute change-in-OI |
| GEX proximity | Position relative to gamma walls and flip level |
| IV attractiveness | Strike IV vs ATM IV surface |
| Liquidity quality | Bid-ask spread tightness and volume depth |
| Max pain distance | Distance from option writers' equilibrium level |

DTE multiplier applied for cross-expiry ranking. Returns `StrikeSelectionResult` with `top_n` ranked `StrikeRecommendation` objects. `best()` returns the top-scored recommendation.

**Configured in live pipeline:** `top_n=3`

---

### Expiry Selection
**File:** `platform_core/data/synthetic_chain_builder.py` — `ExpiryProvider`

Computes nearest weekly and biweekly expiry dates from any bar date:

| Underlying | Weekly expiry weekday |
|---|---|
| NIFTY | Thursday |
| BANKNIFTY | Wednesday |
| FINNIFTY | Tuesday |
| SENSEX | Friday (BSE) |

Used by both the live pipeline (via `KiteAdapter.fetch_option_chain`) and backtest (`SyntheticOptionChainBuilder`).

---

### PositionSizer
**File:** `platform_core/risk/position_sizing.py`

Eight-factor deterministic lot sizing:
1. Base risk budget = `max_risk_fraction_per_trade × equity`
2. Confidence scale [0.25, 1.25]
3. Volatility ratio scale (ATM IV vs reference IV)
4. Liquidity haircut
5. Drawdown haircut
6. Correlation haircut
7. Portfolio heat cap
8. Cash availability cap

Returns `PositionSizingResult` with `recommended_lots`, `tradable`, `rejection_reason`, full `PositionSizingBreakdown`.

**Live pipeline uses four sizer instances with different risk fractions** (see §5 below).

---

### OptionPaperTrader
**File:** `platform_core/backtesting/backtest_runner.py` — `OptionPaperTrader` class

Tracks one open synthetic option position. BSM mark-to-market via `GreeksEngine` on every `evaluate_and_close()` call.

Exit conditions (checked in order):
1. `force_reason` (structure exit or external)
2. DTE < `min_dte` (default 2)
3. Current premium ≤ `entry × stop_loss_pct` (default 0.50)
4. Current premium ≥ `entry × take_profit_pct` (default 2.00)

Accepts variable `lots` parameter at `open()`. On close, emits an `OptionTrade` with full `pnl`, `exit_reason`, `bars_held`. `as_paper_trade()` converts to `PaperTrade` for `TradeLogger`.

---

### TradeLogger
**File:** `platform_core/paper_trading/trade_logger.py`

Appends completed `PaperTrade` records to `logs/paper_trades.csv`. CSV fields: `symbol, direction, entry_price, exit_price, quantity, pnl`.

---

### PerformanceTracker
**File:** `platform_core/paper_trading/performance_tracker.py`

Computes a summary from a list of `PaperTrade` objects: `total_trades`, `winning_trades`, `losing_trades`, `win_rate`, `total_pnl`, `average_pnl`.

---

## 3. End-to-End Live Flow

```
Kite WebSocket (KiteAdapter)
  │  on_ticks(raw_ticks)
  ▼
LiveTickMapper
  │  raw dict → MarketTick
  ▼
BarBuilder
  │  tick → completed LiveBar (1-min)
  ▼
LiveStructureAdapter
  │  LiveBar → StructureBar
  ├──► MultiTimeframeBuilder → MTF bars (5m / 15m / 1h / 4h)
  │
  ▼ [BANKNIFTY only]
SessionContextEngine.classify(bar.start_timestamp) → _session_ctx
OpeningRangeEngine.update_bar(bar)
DailyBarAccumulator.update(bar) → on date roll → PreviousDayLevelsEngine.update_daily_bar()
  │
MarketContextBuilder.update(bar, timeframe)  [1m + MTF bars]
  │  MTF bars also fed to _fvg_15m / _fvg_1h FVGEngine instances
  │  saves bar_history every 15 bars → logs/paper_trading_state.json
  ▼
MarketContextBuilder.build() → MarketContext (bias, confidence)
  │
  ├─ [bias != NEUTRAL and kite_session_valid]
  │     KiteAdapter.fetch_option_chain()  [Kite REST]
  │       → OptionsContextBuilder.build()
  │       → StrikeSelectionEngine.select()
  │       → best StrikeRecommendation
  │
  ▼
[BANKNIFTY: exit evaluation]
OptionPaperTrader.evaluate_and_close()
  │  checks stop loss / take profit / DTE / structure exit
  │  on close: equity update, _daily_pnl, _consecutive_losses
  │  → TradeLogger.log_trade()
  │  → PerformanceTracker.calculate_summary()
  │  → _save_state()
  │
  ▼
[Capital protection]
_try_resume_trading()   ← session reset at each new date
_check_capital_limits() ← hard halt evaluation
_check_soft_halt()      ← soft halt evaluation

  ▼
[Effective confidence composition]
_effective_conf = MCB.confidence
                × session_ctx.confidence_multiplier   ← SessionContextEngine
                × pdl_mult                            ← PreviousDayLevelsEngine
                × or_mult                             ← OpeningRangeEngine
                × fvg_quality_for_entry               ← FVGQualityScorer
  │
  │  [FILTER] printed when any multiplier reduces confidence
  │
  ▼
[Entry gate — all conditions must be true]
  strike_rec is not None
  and not has_open_position()
  and not trading_halted
  and session_ctx.allow_entry            ← blocks OPENING / PRE_OPEN / CLOSING
  and not fvg_quality_reason             ← blocks total < 0.40
  and _effective_conf >= tier_min_conf   ← tier-dependent threshold

  ▼
_size_live_position()   [tier-appropriate PositionSizer]
  │
  ▼
OptionPaperTrader.open()
  │
  ▼
_save_state()   ← state persisted after every trade event
  │
  ▼
[Structure analysis — all symbols]
SwingDetector → StructureEngine (BOS / CHOCH / FVG / sweep)
→ RegimeEngine → AdaptivePolicyEngine → SignalEngine
→ PaperBroker (legacy spot positions, not options)
→ MarketMemory → ReflectionEngine
```

**Note:** `LiquiditySweepValidationEngine` remains backtest-only (observability mode, `setup_score` multiply explicitly commented out). All other filters are active in both paths.

---

## 4. Backtest Architecture

### Synthetic Option Chain
**File:** `platform_core/data/synthetic_chain_builder.py`

When historical option chain CSV data is unavailable, `SyntheticOptionChainBuilder` generates a BSM-consistent `OptionChainSnapshot` from index bar data:

- **Strikes:** spot ± N steps around ATM (configurable)
- **IV:** ATM IV estimated from trailing realized volatility (past `vol_lookback_bars=20`). Put skew applied via configurable slope.
- **Greeks:** Real BSM outputs from `GreeksEngine`
- **OI:** Synthetic Gaussian distribution peaked at ATM
- **LTP:** BSM theoretical price
- **Spread:** Synthetic 1% of LTP each side

Replay-safe: only uses past bars for vol estimation. No future data accessed.

### Why Synthetic Chains Exist in Backtests

Historical Kite option chain data is not archived by default. The `data/option_chains/` directory does not currently exist. All backtest runs therefore use synthetic chains exclusively. This is the expected state until `scripts/record_option_chains.py` is run during live sessions to record real chains.

### Live Option-Chain Path

In the live pipeline: `KiteAdapter.fetch_option_chain()` calls `kite.instruments("NFO")` (cached 5 minutes), filters contracts matching the underlying + expiry + ± N strikes, then calls `kite.quote()` for live LTP and OI. Greeks absent from Kite quotes are filled by `OptionsContextBuilder` via BSM (`compute_missing_greeks=True`).

### Known Limitations of Synthetic Chains

1. **OI is synthetic** — Gaussian distribution, not real market OI. Max pain, PCR, and GEX from synthetic chains are approximations.
2. **IV is backward-looking realized vol** — does not capture intraday IV moves, earnings premium, or surface skew accurately.
3. **LTP is BSM theoretical** — no bid-ask spread modelling beyond synthetic 1% spread. Slippage is not captured.
4. **Strike selection scoring is degraded** — OI support, OI activity, and GEX factors score on synthetic data, producing lower-fidelity recommendations than real chains.
5. **Backtests cannot validate real fill quality** — synthetic chains assume exact fill at LTP.

---

## 5. Risk & Capital Management

### Capital Model
- Starting virtual equity: ₹1,00,000
- Equity tracked in memory and persisted to `logs/paper_trading_state.json`
- Peak equity tracked for drawdown calculation

### Position Sizing — Tiered Risk Fractions

| Tier | Condition | Risk Fraction | Max Lots | Min Confidence |
|---|---|---|---|---|
| Soft (0) | Daily loss ≥ 2% | 0.7% | 1 | 0.65 |
| Tier 1 | Drawdown ≥ 5% | 1.0% | 1 | 0.55 |
| Tier 2 | Default | 2.0% | 3 | 0.40 |
| Tier 3 | Equity > initial×1.03 AND drawdown < 2.5% AND conf ≥ 0.65 | 3.5% | 5 | 0.35 |

Portfolio heat cap: 10% of equity maximum in open premium.

### Soft Halt
- **Trigger:** Daily loss ≥ 2% of session opening equity
- **Effect:** Forces Tier 0 sizer (0.7% risk, max 1 lot, min conf 0.65). Trading continues in defensive mode.
- **Reset:** Automatically clears at next session boundary.

### Hard Halt
Three independent triggers (priority order):

| Trigger | Threshold | Recovery |
|---|---|---|
| Account drawdown | ≥ 15% of peak equity | Manual restart required |
| Daily loss | ≥ 4% of session opening equity | Auto-resumes next session |
| Consecutive losses | ≥ 3 | Auto-resumes next session |

When hard-halted: no new entries. Open position exit evaluation continues.

### Trade-Level Stops
- Stop loss: exit when current premium ≤ entry × 0.50
- Take profit: exit when current premium ≥ entry × 2.00
- DTE minimum: exit when days to expiry < 2
- Structure exit: CHoCH or BOS against position direction triggers immediate close

### Daily Controls
- `_daily_pnl` accumulates from session open equity
- `_session_opening_equity` resets each new trading date
- Daily loss fraction = `max(0, -daily_pnl) / session_opening_equity`

### Drawdown Controls
- `_peak_equity` tracks running high-water mark
- Current drawdown = `(peak_equity - equity) / peak_equity`
- At 15% drawdown: hard halt, manual restart required

---

## 6. Operational Reliability

### State Persistence
**File:** `logs/paper_trading_state.json`

Written atomically via `.tmp` → `.replace()` (POSIX atomic) on every trade event and every 15 BANKNIFTY 1-min bars. State includes:

- `equity`, `peak_equity`, `daily_pnl`, `consecutive_losses`
- `trading_halted`, `halt_reason`, `soft_halted`, `soft_halt_reason`
- `session_date`, `session_opening_equity`, `last_atm_iv`

Restored at process startup via `_load_state()`. Parse is all-or-nothing: partial/corrupt file leaves state at defaults rather than partially restoring.

### Open Position Persistence
`open_position` key in the state file stores full `OptionPosition` fields. On restore, `OptionPaperTrader._position` is reconstructed and `_open_premium_cost` is re-derived from `entry_premium × lot_size × lots`.

### MarketContextBuilder Warm-Start Recovery
`bar_history` key in the state file stores up to 30 bars per timeframe (`_MCB_HISTORY_BARS=30`), serialised as dicts via `_bar_to_dict()`. On restore:

1. Each timeframe's bars are deserialized via `_dict_to_bar()`.
2. `_market_ctx_builder.update(bar, timeframe)` is called for every bar in chronological order.
3. Final `build()` is called and logged with resulting bias, confidence, and ready timeframes.

A corrupt `bar_history` does not block account state restore — it is caught independently. 30 bars > `min_history_bars=24`, so a full warm-start crosses the classifier's history threshold.

Warm-start write interval: every 15 BANKNIFTY 1-min bars (`_MCB_SAVE_INTERVAL=15`).

### Kite Session Validation
`KiteAdapter.check_session()` is called once at startup. If the token is invalid, the options pipeline is disabled and a clear remediation message is printed. The WebSocket tick stream and structure analysis continue regardless.

Per-bar token expiry detection: any 401/403 error from `fetch_option_chain()` sets `_kite_session_valid=False` and disables the options pipeline for the remainder of the session.

### Token Expiry Handling
Zerodha access tokens expire daily. Remediation: `python scripts/generate_kite_session.py` (interactive browser login) writes a new token to `configs/kite_config.json`. The live pipeline must then be restarted.

### WebSocket Automatic Reconnect
`KiteAdapter` implements bounded exponential backoff reconnection:

- Maximum attempts: 5 (`_MAX_RECONNECTS`)
- Delays: 2 → 4 → 8 → 16 → 32 s (capped at 60 s)
- On successful reconnect: resubscribes to all `_instrument_tokens`, resets attempt counter
- On max exceeded: prints actionable message, stops reconnecting
- `disconnect()` sets `_stopping=True` which suppresses all reconnect scheduling

---

## 7. Important Repository Entry Points

| File | Purpose |
|---|---|
| `platform_core/data/live/live_structure_pipeline.py` | Main live process — all runtime logic |
| `platform_core/backtesting/backtest_runner.py` | Full backtest with all filters active |
| `platform_core/intelligence/market_context_builder.py` | Bias/confidence engine |
| `platform_core/options/strike_selection.py` | 7-factor strike scoring |
| `platform_core/risk/position_sizing.py` | 8-factor position sizing |
| `platform_core/data/live/kite_adapter.py` | Kite REST + WebSocket adapter |
| `platform_core/analytics/structure/fvg_scoring.py` | FVG quality scorer |
| `platform_core/analytics/structure/sweep_validation.py` | Sequential setup validator |
| `platform_core/analytics/structure/session_context.py` | NSE session phase classifier |
| `platform_core/analytics/structure/previous_day_levels.py` | PDH/PDL confidence adjuster |
| `platform_core/analytics/structure/opening_range.py` | Opening range entry filter |
| `platform_core/data/synthetic_chain_builder.py` | BSM chain generator + ExpiryProvider |
| `platform_core/data/historical/option_chain_feed.py` | Historical chain CSV reader |
| `platform_core/data/live/instrument_registry.py` | Kite token → symbol map |
| `scripts/validate_live_pipeline.py` | 10-step pre-run validation script |
| `scripts/generate_kite_session.py` | Interactive token regeneration |
| `scripts/run_backtest.py` | Single-symbol backtest launcher |
| `scripts/run_batch_backtest.py` | Batch backtest over data/ directory |
| `configs/kite_config.json` | Kite credentials (do not commit changes) |

---

## 8. Git Workflow

### Current Branch
`paper-trading`

### Recent Commits

| Hash | Message |
|---|---|
| `eff6a84` | Align live pipeline with backtest filtering logic ← **`paper-trading-ready` tag** |
| `34d7388` | Add bounded automatic WebSocket reconnection to KiteAdapter |
| `b32aea5` | Persist open positions to state file for crash recovery |
| `3a48882` | Add end-to-end live pipeline validation script |
| `0931b02` | Add state persistence and Kite session handling for operational readiness |
| `e43737f` | Wire live paper trading with tiered capital model and soft/hard halt protection |
| `8b7059e` | Initial institutional trading system checkpoint |

### Recommended Workflow

1. All development happens on `paper-trading` or feature branches off it.
2. `main` is the stable merge target — open PRs against `main`.
3. Before any live session: run `scripts/validate_live_pipeline.py` to verify the full pipeline end-to-end.
4. Do not commit `configs/kite_config.json` changes — access tokens are ephemeral.
5. After confirming live paper trading works, merge `paper-trading` → `main`.

---

## 9. Backtest Findings

### Data Available
`data/banknifty_1m_2025-06-01_2026-05-29.csv` — 91,934 rows of BANKNIFTY 1-minute OHLCV, June 2025 through May 2026. This is the primary backtest dataset.

### Architecture Lessons That Shaped the Current Design

**MarketContextBuilder replaced hardcoded bias:**
Prior iterations used `bias="LONG"` / `bias="SHORT"` literals. The MCB replaced this with genuine multi-timeframe regime classification. The daily bar accumulator (DailyBarAccumulator in BacktestRunner) was added specifically to feed the 1d slot (weight 0.35), the largest weight in the bias fusion.

**Session filtering is high-signal:**
BacktestRunner logs `session_blocks`, `pdl_blocks`, `or_blocks`, `fvg_blocks` separately. The OPENING phase filter (09:15–09:30) and PRE_OPEN filter block structurally noisy entries. PDH/PDL and OR filters are applied as confidence multipliers rather than hard blocks, preserving entries that have broken through levels.

**FVG quality scoring replaced gap_ratio proxy:**
The single-dimensional `confidence_score=gap.gap_ratio` was replaced with the six-factor `FVGQualityScorer`. The sweep origin factor was subsequently upgraded from binary (had_sweep → 1.0/0.30) to a directional + recency + quality composite using actual `LiquiditySweep` objects.

**LiquiditySweepValidationEngine is observability-only:**
The sequential validation (sweep → BOS → displacement → FVG) was implemented but its `setup_score` multiply into `effective_confidence` is explicitly disabled in BacktestRunner (line 795 comment: `# disabled: sweep rarely fires`). The validator runs and logs blocks, but does not gate entries. This reflects observed infrequency of confirmed sweep events in the test data.

**StrikeSelectionValidator provides quantitative audit:**
Every backtest produces a counterfactual report comparing the selected contract's P&L against all alternatives at the same expiry (adjacent strikes and different option types). This answers: "Was the selected strike better than ATM? Better than adjacent strikes? Was the expiry choice correct?"

**PositionSizer was integrated after initial fixed-lots testing:**
Initial backtests used `lots=1` fixed. The PositionSizer was integrated after observing that confidence and volatility information was available to inform dynamic sizing. The tiered risk model (T1/T2/T3 + Soft) in the live pipeline extends this with account-state-dependent tier selection.

**Synthetic chains are the dominant limitation:**
Backtest P&L figures cannot be fully trusted because OI, PCR, max pain, and GEX are derived from synthetic Gaussian OI rather than real market data. Strike selection scores are valid in relative terms (within a backtest run) but not absolute calibrations.

---

## 10. Current Status

### Completed

- Full market structure analytics pipeline (SwingDetector, BOS, CHoCH, FVG, LiquiditySweep, SR)
- MarketContextBuilder with multi-timeframe RegimeClassifier
- OptionsContextBuilder (OI, GEX, IV, dealer, premium)
- StrikeSelectionEngine (7-factor scoring, expiry selection)
- PositionSizer (8-factor, tiered risk in live pipeline)
- OptionPaperTrader with BSM mark-to-market
- Tiered capital model (Soft/T1/T2/T3) with soft halt and hard halt
- Daily P&L tracking and session reset
- State persistence with atomic writes
- Open position persistence and recovery
- MarketContextBuilder warm-start (bar history persisted and replayed on restart)
- Kite session validation at startup
- Per-bar token expiry detection and options pipeline disable
- WebSocket automatic reconnect with bounded exponential backoff
- TradeLogger and PerformanceTracker
- End-to-end validation script (`scripts/validate_live_pipeline.py`, 10 steps)
- StrikeSelectionValidator (counterfactual P&L audit in backtests)
- SessionContextEngine, PreviousDayLevelsEngine, OpeningRangeEngine, FVGQualityScorer wired into live pipeline (commit `eff6a84`)
- LiquiditySweepValidationEngine (BacktestRunner observability-only)
- HistoricalOptionChainFeed (reads real recorded chains in backtest when available)
- DailyBarAccumulator (feeds 1d slot of MCB in backtest; also feeds PDH/PDL in live pipeline)
- SyntheticOptionChainBuilder with ExpiryProvider
- 91,934 bars BANKNIFTY 1m historical data (June 2025–May 2026)

### Operationally Validated

- All 10 steps of `scripts/validate_live_pipeline.py` pass in offline (synthetic) mode
- MCB warm-start: 30-bar rehydration crosses `min_history_bars=24` threshold, resulting in a non-NEUTRAL bias
- State persistence round-trip: write → simulate restart → restore passes fidelity checks
- WebSocket reconnect logic: code path validated; live reconnect requires market hours

### Awaiting Live-Market Validation

- Full live market-hours run with real Kite tick stream
- Option chain retrieval from Kite REST during trading hours
- OptionsContextBuilder with real OI and real LTP
- StrikeSelectionEngine recommendations against real chains
- OptionPaperTrader open/close cycle with real market premiums
- WebSocket reconnect behaviour under real network conditions
- MCB warm-start with real bars across a session restart

---

## 11. Remaining Blockers Before Live Paper Trading

**One genuine operational blocker:**

1. **Daily Kite access token renewal.** Zerodha tokens expire at the end of each trading day. Before each session: `python scripts/generate_kite_session.py`. The system detects an invalid token at startup and disables the options pipeline with a printed instruction, but the token must be renewed manually before starting.

There are no remaining code-level gaps between the backtest and live entry logic. `SessionContextEngine`, `PreviousDayLevelsEngine`, `OpeningRangeEngine`, and `FVGQualityScorer` are now active in the live pipeline.

---

## 12. Remaining Blockers Before Real-Money Trading

The following components exist in the codebase but are not wired into the live pipeline. All must be integrated, tested, and validated before any real order is placed.

**Order placement:**
- `ZerodhaIntegration` / `ZerodhaRestClient` — async REST client for real order placement. Exists at `platform_core/execution/zerodha_integration.py`. Not connected to the live pipeline.
- `OrderManager` — order lifecycle management at `platform_core/execution/order_manager.py`. Not connected.
- Real entry requires replacing `OptionPaperTrader.open()` call with a real order submission path.

**Broker-state reconciliation:**
- `ReconciliationEngine` at `platform_core/execution/reconciliation_engine.py` — compares internal order records against broker state. Not wired.
- Before real money: every session start must reconcile local state against Kite's actual position/order book. Any discrepancy must block new entries until resolved.

**Live execution safeguards:**
- `LatencyMonitor` (`platform_core/execution/latency_monitor.py`) — not active.
- `FailsafeExits` (`platform_core/execution/failsafe_exits.py`) — not active.
- `KillSwitches` / `EmergencyShutdown` (`platform_core/risk/`) — not wired into live pipeline.
- Position limit enforcement at the broker level (separate from paper lot caps).

**Real-money operational requirements:**
- SEBI/NSE margin requirements differ from paper capital model. ₹1,00,000 may be insufficient for real lot sizes.
- Real fills include brokerage, STT, NSE charges, GST. P&L model must account for these.
- Slippage on market orders for index options can be significant at open/close. The paper model assumes fill at LTP.
- Options pipeline currently operates on a single open position at a time. Real account may require multi-leg or portfolio management.
- `scripts/record_option_chains.py` should be run during live sessions to build a real historical chain dataset for future backtests.

---

## 13. Explicit Design Constraints

Future sessions must respect the following constraints. The components listed below are complete, tested, and consistent with the rest of the system. Do not replace them.

**Do NOT replace:**
- `MarketContextBuilder` — the multi-timeframe bias engine is the decision spine of the entire system. Changing it invalidates all backtest context.
- `StrikeSelectionEngine` — seven-factor scorer is calibrated to the options pipeline's data model. Do not substitute a different scorer.
- `PositionSizer` — eight-factor engine with tiered config. The live pipeline's four sizer instances are intentionally parameterised separately.
- `OptionsContextBuilder` — the analytics package (OI, GEX, IV, dealer, premium) is the canonical option-chain intelligence layer.

**Do NOT create:**
- Parallel persistence systems alongside `_save_state()` / `_load_state()`. State is single-source-of-truth in `logs/paper_trading_state.json`.
- Parallel Kite adapters. `KiteAdapter` is the single REST+WebSocket integration point.
- New architecture without clear justification. Three similar lines of code are better than a premature abstraction.

**Prefer extending existing components:**
- Real option chain recording: `scripts/record_option_chains.py` already exists; run it, then set `chain_data_dir` in backtest config.
- Additional risk filters: extend the existing `_check_capital_limits()` / `_check_soft_halt()` pattern rather than adding new global state variables.
- `LiquiditySweepValidationEngine`: re-enable `setup_val.setup_score` multiply in both `BacktestRunner` and the live pipeline once sweep event frequency is validated on real data.

---

## 14. Next Action When Market Opens

Execute in this exact sequence:

1. **Generate Kite token.**
   ```
   python scripts/generate_kite_session.py
   ```
   Complete the browser login. Confirm `configs/kite_config.json` is updated with the new access token.

2. **Validate the full live pipeline** (takes ~30 seconds, no real orders).
   ```
   python scripts/validate_live_pipeline.py
   ```
   All 10 steps must pass. Step 1 (auth), Step 4 (live chain retrieval), and Step 6 (strike recommendations) require a valid token and open market.

3. **Start the live paper trading process.**
   ```
   python platform_core/data/live/live_structure_pipeline.py
   ```
   Observe startup logs:
   - `[STATE] Restored: ...` or `[STATE] No state file — starting fresh`
   - `[MCB] rehydrated: ...` (if restarting with existing state)
   - `[KITE SESSION] Token valid — options pipeline enabled`
   - `LIVE STRUCTURE PIPELINE ACTIVE`

4. **Observe bar-level recommendations.** Per BANKNIFTY bar, watch for:
   - `[OPTIONS] bias=LONG conf=0.XX` — MCB output
   - `[FILTER] bar=N  conf=0.XX→eff=0.XX  ...reason...` — when a filter reduces confidence
   - `[STRIKE] BANKNIFTY XXXXX CE  δ=0.XX  score=0.XX  iv=XX.X%` — strike selection
   - `[TIER2] lots=N  cost=₹XX,XXX  equity=₹1,00,XXX  conf=0.XX→eff=0.XX` — paper entry

5. **Observe paper trade lifecycle.**
   - Entry print: `[ENTRY] LONG BANKNIFTY XXXXX CE  expiry=YYYY-MM-DD  lots=N  prem=XXX`
   - Exit prints: `STOP_LOSS`, `TAKE_PROFIT`, `DTE_MINIMUM`, or `STRUCTURE_EXIT`
   - Performance print after each close: `[PERF] trades=N  win=XX.X%  P&L=₹±XXX  equity=₹1,00,XXX`

6. **Verify persistence and reconnect behaviour.**
   - After first trade event, confirm `logs/paper_trading_state.json` exists and contains the position.
   - Optionally kill and restart the process. Confirm `[STATE] Restored` and `[MCB] rehydrated` appear at startup.
   - If the WebSocket drops, confirm `[KITE] Reconnect 1/5 scheduled in 2 s` appears and the stream recovers.

7. **Record findings.**
   - Note real vs synthetic chain quality difference in strike recommendations.
   - Note whether bias/confidence levels are sensible relative to price action.
   - Note how often `[FILTER]` lines appear and which filter fires most (session / PDL / OR / FVG).
   - Note effective confidence vs raw confidence at entries — large gaps indicate active filtering.
   - These observations inform the next development priority.

---

## 15. Future Development Priority Order

After confirming the live pipeline runs correctly during market hours:

**Priority 1 — Record real option chains**
Run `scripts/record_option_chains.py` during live sessions to build `data/option_chains/BANKNIFTY/` CSVs. Once available, re-run backtests with `chain_data_dir="data/option_chains"` to get accurate OI, PCR, max pain, and GEX signal quality. Directly improves `StrikeSelectionEngine` calibration.

**Priority 2 — Enable LiquiditySweepValidationEngine**
The `setup_val.setup_score` multiply is commented out in `BacktestRunner` due to infrequent confirmed sweep events. Analyse what fraction of trades would be blocked on the real dataset. If the frequency is sufficient, re-enable it in both `BacktestRunner` and the live pipeline — it represents the most institutionally grounded entry gate.

**Priority 3 — Real execution path**
Wire `ZerodhaIntegration` and `OrderManager` into the live pipeline. Replace `OptionPaperTrader.open()` with a real order submission. This is the pre-requisite for real-money operation and requires all risk safeguards to be active first.

**Priority 4 — Reconciliation integration**
Wire `ReconciliationEngine` to run at session start. Compare `_option_trader._position` against Kite's actual positions API. Discrepancies must block new entries.

**Priority 5 — LatencyMonitor and KillSwitches**
Wire `LatencyMonitor` and `KillSwitches` / `EmergencyShutdown` into the live pipeline. These are operational safety layers required before real-money trading.

**Priority 6 — Daily bar warm-start**
The live pipeline has a `DailyBarAccumulator` (`_daily_acc`) feeding the PDH/PDL engine, but the `1d` MCB slot is not seeded from it on process restart. The largest MCB weight (0.35) is always cold at startup until a full day accumulates. A daily bar seed from the prior session's closing bar would improve MCB quality immediately after restart.
