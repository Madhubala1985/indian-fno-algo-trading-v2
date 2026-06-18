# Real-Funds Rollout Plan

Status: **Stage 1 in progress** (fill-integration engineering). Real orders remain
disarmed; `real_armed` requires 9 interlocks + explicit "arm real orders" from the user.

## Context (why this exists)

Wiring audit (2026-06-18) found: the live paper decision path is fully wired and
coherent, BUT the **real-order plumbing places orders without integrating the fill
back into the position**:

- `ExecutionRouter` routes every entry/exit through Paper / KiteDryRun / KiteLive
  adapters, with `real_armed` fail-closed behind 9 interlocks. ✅
- BUT `route_entry()` / `route_exit()` return values are **discarded** (bare calls,
  live_structure_pipeline.py:3623 / 2403 / 2954). The position is opened by the
  **paper** trader (`OptionPaperTrader`) at the *intended* premium.
- `KiteLiveBrokerAdapter` correctly returns `filled=False, status="PLACED"` (order_id
  is not a fill) — but **nothing polls `get_order_trades()`** for the real fill.
- `ReconciliationEngine` exists but is **not wired** into the live pipeline.

Consequence: `real_armed` today would place real Kite orders while internal
position/PnL/stops run on paper-assumed fills → divergence from broker truth. Unsafe.

## Design invariants (non-negotiable — "cover every corner")

1. **Paper mode behavior is byte-for-byte unchanged.** All new fill logic is gated to
   real modes; paper still opens instant synthetic fills at LTP.
2. **Fail-closed.** If a real fill cannot be confirmed, NEVER assume a fill. Treat the
   position as uncertain → halt + alert, never silently proceed.
3. **Broker truth wins.** Reconciliation overrides local assumptions; mismatch ⇒ halt.
4. **Every new branch is tested** (unit + a dry-run integration pass) before it counts
   as done.

## Stages

- **Stage 0 — Prove the edge on paper.** Next paper session(s) with SENSEX-primary
  (0aef273) + chop filter (51096a7). Proceed only after 2-3 consecutive net-positive
  sessions with NIFTY churn gone.
- **Stage 1 — Build the fill-integration layer (current).** See checklist below.
- **Stage 2 — Shadow validation `real_dry_run`.** Several live sessions; verify intents
  are correct against live market. No real money.
- **Stage 3 — (user prefers to skip) tiny `real_armed` 1-lot SENSEX, supervised.**
  Recommended as a final smoke test even if brief; build quality must be high enough
  that this is uneventful.
- **Stage 4 — Scale gradually.** Raise lots/capital, add NIFTY (very-strong only), only
  after clean reconciliation across multiple sessions.

## Stage 1 checklist

1. [x] Capture `route_entry`/`route_exit` results (stop discarding them). — 81ff60d / 49c7d2b
2. [x] Fill-confirmation loop (`confirm_fill`): FILLED / PARTIAL / REJECTED / TIMEOUT,
       bounded retries, cancel-on-timeout. — 359b643 (8 unit tests)
3. [x] Open the position at the ACTUAL average fill price (real modes). — 81ff60d
4. [x] Order state machine handled at entry (no-fill ⇒ no position, fail-closed). — 81ff60d
5. [x] Exit symmetry: confirm exit fills; unconfirmed real exit ⇒ trading_halted +
       CRITICAL (broker may still hold). EOD close symmetric. — 49c7d2b
6. [x] Wire `ReconciliationEngine`: startup + every RECONCILE_EVERY_BARS bars;
       HIGH/CRITICAL break ⇒ trading_halted + CRITICAL RECONCILE_BREAK. — 1507cc7
7. [x] Pre-trade margin check via `get_available_margin()` (REAL_MARGIN_BLOCK). — 1507cc7
8. [x] Order type — DECISION: keep MARKET orders for both entry and exit. For a
       stop-driven strategy, guaranteed fills matter more than price control: a
       LIMIT that doesn't fill on a stop leaves you in a losing position. Strikes
       are already liquidity/OI-gated, so MARKET fills are reasonable; the ACTUAL
       fill price is recorded via confirm_fill. A LIMIT collar remains a future
       option if real_dry_run data shows entry slippage is a problem.

Also wired in 1507cc7: the ExecutionRouter now receives the Kite client
(adapter.kite) — previously constructed without it, so real_armed could not have
placed orders or queried positions/margin.

**Stage 1 status: COMPLETE + integration-tested.** All new logic is real_armed-only;
paper/dry-run unchanged by construction. The full real_armed chain (interlocks →
place_order → confirm actual fill → reject fail-closed → reconcile mismatch →
margin) is now exercised end-to-end with a MOCK Kite client in
tests/test_real_armed_integration.py (7 tests, no real money). Total: ~30 tests pass.

CODE READINESS: the real-money pipeline is built and tested. Arming is gated by
scripts/real_funds_readiness_check.py (git clean, EXECUTION_MODE=real_armed, all 9
interlocks, capital limits, token, no orphans, dry-run + EOD tested, order tables).

NOT-YET-MET before actually arming (these are DATA / PROCESS, not code):
  - Stage 0: net-positive PAPER sessions over a real sample (currently ~break-even
    over 2 conflicting days — far too small).
  - Stage 2: several real_dry_run live sessions (validate intents vs live market).
  - Explicit user "arm real orders" + the 9 interlocks set. NEVER armed autonomously.

## Real-money interlocks (the arming wall — Stage 3+)

9 env vars via `_check_real_interlocks()`:
`REAL_TRADING_ENABLED=true`, `REAL_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY_RISK`,
`BROKER_LIVE_ORDERS_ALLOWED=true`, `MAX_REAL_CAPITAL`, `MAX_REAL_ORDER_VALUE`,
`MAX_REAL_DAILY_LOSS`, `REAL_ALLOWED_UNDERLYINGS`, `REAL_ALLOWED_EXCHANGES`,
`REAL_MAX_LOTS_PER_ORDER`. Start SENSEX-only / BFO / 1 lot / tiny caps.

## Operational guardrails

- Kite token generated in the 08:30-09:10 IST window (pre-dawn tokens get invalidated).
- Run via supervised runner (preflight token-validity gate).
- Never arm `real_armed` without explicit user "arm real orders".
