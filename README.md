# Indian F&O Algo Trading Platform: v2

**An autonomous intraday index-options trading system for NSE/BSE (Nifty, BankNifty, Sensex) that ran
every trading day on a cloud VM against live Zerodha market data and real option prices, in a
fail-closed "real dry-run" mode that computes and logs every order without placing it.**

~92,000 lines of Python · 212 commits · async/event-driven · Zerodha Kite Connect · Oracle Cloud deployment

> **Status:** the infrastructure is production-grade and ready for real orders behind 9 safety interlocks.
> It was **deliberately never armed with real money**, because the system's own validation showed that the
> edge was not yet proven out-of-sample. Reaching that decision with data is the point of the project.

---

## Highlights
- **Live, autonomous operation:** systemd services/timers on an Oracle Cloud ARM VM start the model before market open, auto-generate the daily Kite access token (TOTP), run the session, and shut down cleanly.
- **Full live pipeline:** WebSocket ticks → validation → multi-timeframe bars → causal market structure → options context (OI, GEX, IV surface, dealer hedging) → bias engine → entry/exit decisions → execution router.
- **Three execution modes:** `paper` (simulated), `real_dry_run` (live data, real premiums, no orders), `real_armed` (real orders, gated behind 9 interlocks plus explicit operator confirmation).
- **Fail-closed safety engineering:** feed-stall watchdog with forced reconnect, broker-truth reconciliation design, unconfirmed fills treated as uncertain (halt + alert), per-symbol guards, force-close tooling that refuses to run in armed mode.
- **Shadow-first research discipline:** new signals are logged in shadow, measured against real trade outcomes across days, and only then promoted. Many ideas were measured and rejected (documented in the commit log).

## Architecture (`platform_core/`)
| Domain | Responsibility |
|---|---|
| `core/` | Enums, frozen Pydantic event envelopes, protocols, settings |
| `orchestration/` | Dependency-ordered async orchestrator, replay-safe event bus |
| `data/` + `data/live/` | Kite WebSocket adapter, bar builders, live structure pipeline, Redis/TimescaleDB persistence, replay |
| `analytics/structure/` | Causal swings, BOS, CHOCH, FVG quality scoring, sweep validation, session context, previous-day levels, opening range |
| `options/` | Greeks, IV and IV-surface engines, OI analytics, gamma exposure (GEX), dealer hedging, premium behaviour, strike selection |
| `intelligence/` | Multi-timeframe MarketContextBuilder (weighted 1d/4h/1h/15m/1m bias fusion), HTF bias engine |
| `adaptive/` | Regime engine, adaptive policy, market memory, reflection engine |
| `risk/` | Tiered risk fractions, conviction lot ceilings, liquidity gate, circuit breakers |
| `execution/` | Execution router with Paper / KiteDryRun / KiteLive adapters, order manager, reconciliation |
| `paper_trading/` · `backtesting/` · `persistence/` | Option paper trader, backtest runner, runtime DB |

Further reading: [`docs/HOW_THE_MODEL_WORKS.md`](docs/HOW_THE_MODEL_WORKS.md) ·
[`docs/PROJECT_HANDOFF_2026-07-11.md`](docs/PROJECT_HANDOFF_2026-07-11.md) ·
[`REAL_FUNDS_ROLLOUT.md`](REAL_FUNDS_ROLLOUT.md) · [`deploy/ORACLE_SETUP.md`](deploy/ORACLE_SETUP.md)

## Results (honest, cost-adjusted)
**Historical backtest (BankNifty 1-minute, in-sample):** 437 trades, 49.2% win rate. Treated as a
development benchmark only, not as evidence of an edge.

**Live real-market dry run (66 trades, through 2026-07-11):**
| Bucket | Trades | Win rate | Net P&L |
|---|---|---|---|
| All SHORT trades | 14 | 57% | **+₹50,289** |
| All LONG trades | 52 | 34% | **−₹35,904** |
| **Total** | 66 | 39% | **+₹14,385** |

The headline is positive, but the system's own analysis showed it came from a single day (+₹44,102 on
2026-06-29). Without that day the book was −₹29,718. The diagnosis traced the losses to a multi-timeframe
bias engine that "froze" LONG and flipped too slowly, plus a degenerate confidence score. Later causal
replays (for example 11 SENSEX days, net −₹172) confirmed **no robust out-of-sample edge yet**, so real
funds stayed disarmed by design.

**What this shows:** the system measured its own performance honestly, found the exact failure mode
(direction asymmetry and bias staleness), and refused to risk capital on an unproven edge.

## Skills demonstrated
- Low-latency, event-driven systems in async Python, built for live market hours
- Options analytics: Greeks, IV surfaces, gamma exposure, dealer positioning, open interest
- Broker integration (Zerodha Kite Connect): WebSocket, orders, historical data, TOTP token automation
- Cloud deployment and operations: Oracle Cloud VM, systemd services/timers, cron, log and disk management
- Safety-critical engineering: interlocks, fail-closed design, reconciliation, watchdogs
- Quant research hygiene: shadow testing, causal replay, cost modelling, out-of-sample gating

## Repository layout
```
platform_core/   production trading system (see architecture table)
scripts/         analysis, causal replay, backtests, dashboards, diagnostics, live tools
deploy/          Oracle Cloud setup, systemd units, TOTP token refresh, VM sync scripts
docs/            model explanation, handoff, rollout and arming runbooks
forex/           exploratory gold/forex shadow tooling
tests/           unit tests
configs/         templates only (no credentials committed)
```

## Running
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
cp deploy/secrets.env.example deploy/secrets.env   # your own Kite Connect credentials
python scripts/run_supervised_live_session.py --execution-mode paper
```
See [`deploy/README.md`](deploy/README.md) for the full always-on VM deployment. Large market-data
recordings and logs are not included.

## Part of a three-project series
| Project | Market | Focus |
|---|---|---|
| [XAU/USD Gold Quant Research](https://github.com/Madhubala1985/xau-gold-quant-research) | Spot gold, MT5 | Edge discovery and statistical falsification |
| [Indian F&O Algo Trading v1](https://github.com/Madhubala1985/indian-fno-algo-trading-v1) | NSE BankNifty | First platform: event-driven architecture, paper trading |
| **Indian F&O Algo Trading v2** (this repo) | NSE/BSE index options | Live real-data dry-run on a cloud VM, options analytics, fail-closed execution |

---
*Built with AI-pair-programming (Claude Code). For research and education only. Not financial advice.
No real-money orders were placed.*
