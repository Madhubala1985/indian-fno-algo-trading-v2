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

1. [ ] Capture `route_entry`/`route_exit` results (stop discarding them).
2. [ ] Fill-confirmation loop: poll `get_order_trades(order_id)` → FILLED / PARTIAL /
       REJECTED / TIMEOUT, with bounded retries.
3. [ ] Open the position at the ACTUAL average fill price (real modes), not intended.
4. [ ] Order state machine: PLACED→FILLED/PARTIAL/REJECTED/CANCELLED + each handled.
5. [ ] Exit symmetry: confirm exit fills (an unfilled exit = still in the market).
6. [ ] Wire `ReconciliationEngine`: startup + periodic; halt on mismatch.
7. [ ] Pre-trade margin check via `get_available_margin()`.
8. [ ] Order type / slippage policy for option entries (market vs limit + cap).

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
