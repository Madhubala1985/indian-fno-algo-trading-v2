# INSTITUTIONAL TRADING SYSTEM v2 — COMPLETE PROJECT HANDOFF
_Last updated: 2026-07-11. Self-contained: hand this whole file to any LLM to continue research._

---

## 0. HOW TO USE THIS DOCUMENT

You are picking up an automated options-trading system. This document is the single
source of truth for **what it is, how it runs, what has been measured, what is
already decided, and what to research next.** Read sections 5, 8, 9, 11 and 15
before proposing ANY change — most "obvious" ideas here have already been tested
and rejected with data. The governing rule of this project is **shadow-first,
out-of-sample-gated: never wire an unvalidated signal into the live trade path;
log it first, measure it against real trade outcomes across multiple days, then
promote.** Breaking that rule is how money gets lost.

---

## 1. WHAT THE SYSTEM IS

- **Goal:** automated intraday trading of Indian index options (BankNifty, Nifty,
  Sensex) on NSE/BSE via **Zerodha Kite Connect**. It BUYS ATM options (calls for
  a LONG bias, puts for SHORT) — it is a long-premium directional strategy, not a
  spread/premium-selling strategy.
- **Stack:** Python 3.12, async/event-driven. All production code under
  `platform_core/`. Deterministic + human-tuned (see §6 "No autonomous learning").
- **Capital:** REAL account = ₹4,00,000. Capital model COMPOUNDS (each session
  rolls the equity basis forward from saved state).
- **Execution modes:** `paper` (sim), `real_dry_run` (full live pipeline, real
  market data + real option prices, **but places NO real orders** — logs what it
  WOULD do), `real_armed` (places real orders — **NOT currently used**).
- **Current status:** runs fully autonomously every weekday in **`real_dry_run`**
  on a cloud VM. **It has NOT been armed with real money.** See §5 for why.

---

## 2. ARCHITECTURE (`platform_core/` by domain)

- **`core/`** — zero-business-logic foundation. `enums.py` (all `StrEnum`/`IntFlag`),
  `events.py` (`EventEnvelope`, the only cross-service contract, Pydantic/frozen),
  `interfaces.py` (Protocols/ABCs), `config.py` (Pydantic-Settings).
- **`orchestration/`** — `MainOrchestrator` (dependency-ordered async start/stop),
  `AsyncEventBus` (non-decreasing timestamps for replay safety).
- **`data/`** — ingestion: websocket → validation → bar builder / multi-timeframe
  builder. Persistence: redis (hot), timescaledb/postgres (cold). Replay engines.
  **`data/live/` is where the live trading pipeline lives (see §3).**
- **`analytics/structure/`** — causal market-structure (NO lookahead): SwingDetector
  → LiquiditySweep + BOS + CHOCH + FVG + Support/Resistance, coordinated by
  `StructureEngine`. Emits `StructureEvent`s from finalized bars only.
- **`options/`** — GreeksEngine, IVEngine, IVSurfaceEngine, OIAnalytics,
  GammaExposure (GEX), DealerHedging, PremiumBehavior, StrikeSelection. Fed from
  option-chain snapshots. **`options/options_context/` builds `OptionsContext`,
  which internally runs OI/GEX/Dealer/Premium every bar via `EngineRunner`.**
- **`intelligence/`** — `MarketContextBuilder` (the MCB — authoritative bias engine),
  `HTFBiasEngine` (auxiliary higher-timeframe bias), `market_context.py`
  (the FULL context dataclass with `daily/h4/h1/m15` biases + `is_aligned`),
  `bias_detector.py`, `context_builder.py`.
- **`adaptive/`** — `RegimeEngine` (vol/directional regime classifier),
  `AdaptivePolicyEngine`, `MarketMemory`, `ReflectionEngine`. **The learning trio
  (Adaptive/Memory/Reflection) is DELIBERATELY UNWIRED — see §6.**
- **`ml/`** — `FeaturePipeline`, `RegimeClassifier`, `ContinuationClassifier`,
  `ReversalClassifier`, `TrapProbabilityModel`, `ConfidenceEngine`. These are
  **deterministic heuristic scorers, not trained models.**
- **`signals/`, `risk/`, `execution/`, `paper_trading/`, `backtesting/`, `testing/`**
  — signal generation; position sizing + layered safety (drawdown → heat →
  exposure → kill switches → emergency shutdown); Kite REST/WS wrapper +
  OrderManager + ReconciliationEngine; paper broker; backtest runners; sim
  frameworks.

**Two `MarketContext` classes exist and are easy to confuse:**
1. `intelligence/market_context_builder.py::MarketContext` — the **MCB execution
   context**. Has `.bias` (LONG/SHORT/NEUTRAL) and `.confidence`. This drives trades.
2. `intelligence/market_context.py::MarketContext` — the **full HTF context**. Has
   `daily_bias/h4_bias/h1_bias/m15_bias/liquidity_direction/volatility_regime/
   confidence_score/is_aligned`. Used for the alignment signal.
Reading the wrong one caused the HTF-shadow null bug (fixed 2026-07-10).

---

## 3. THE LIVE TRADE LIFECYCLE (end-to-end, `platform_core/data/live/live_structure_pipeline.py`)

This ~4700-line file is the heart of the system. Per completed 1-minute bar, per
underlying:

1. **Bar completes** → structure adapter converts it; multi-timeframe builder
   updates 15m/1h/4h/1d bars; `LATEST_HTF_BARS` updated.
2. **MCB update** → `market_ctx_builder.update()` for each timeframe; then
   `market_ctx = market_ctx_builder.build()` → gives `bias` + `confidence`.
   **CRITICAL PROPERTY: `market_ctx.confidence` is SATURATED at ~1.0 at every
   entry decision** (it varies bar-to-bar during warmup but is always 1.0 when a
   trade is actually evaluated). This is the root of several problems (§5).
3. **Full HTF context** → `bias_detector.detect_multi_timeframe_bias()` +
   `htf_bias_engine.build_context()` → `market_context` (the full one) with
   `is_aligned`. Printed as `[DIAG HTF — not used for option execution]`.
   `is_aligned` is stashed to `_current_state.last_is_aligned` for the alignment gate.
4. **Options context (enabled targets only)** → `_opt_ctx_builder.build()` →
   `OptionsContext` containing `oi_snapshot / gex_snapshot / dealer_hedging_snapshot
   / premium_behavior_snapshot / strike_candidates`. **These 4 analytics engines
   are computed here and (except GEX prototype) their results are discarded** —
   now shadow-logged.
5. **Strike selection** → `_strike_engine.select()` picks the ATM-ish strike
   (delta ≈ 0.5) with a `score_margin` (rank-1 minus rank-2 score gap).
6. **Entry filter (confidence multiplier chain)** → `_effective_conf` starts at
   `market_ctx.confidence` and is multiplied by: PDL mult, OR mult (chop guard),
   FVG-quality mult, **alignment gate** (×0 if LONG & not aligned), **NIFTY-long
   gate** (×0 if NIFTY & LONG). A `conf→0` blocks the entry.
7. **Entry gate stack** → hard gates (contradiction cooldown, warm-start, OI,
   risk, heat, position limit, halt) + soft gates (`score_margin ≥ floor`,
   confluence). `INSUFFICIENT_STRIKE_QUALITY` blocks when `score_margin < 0.05`.
8. **Sizing** → tier-based sizer → `recommended_lots`; then re-entry haircut
   (×0.5 on 2nd+ same-direction entry) and **regime de-size** (×0.5 in BALANCED
   regime); then %-of-capital rails (premium cap, heat, freeze-qty, correlated).
9. **Order** → paper/dry-run marks to REAL Kite option LTP (a past bug used
   Black-Scholes model prices → fictional P&L; fixed). Records to `logs/trades.db`.
10. **Position management** → breakeven ratchet (+8% peak arms a breakeven floor),
    structure-based exit (BOS/CHOCH), stop-loss, EOD force-close at 15:30 IST.

---

## 4. DEPLOYMENT & OPERATIONS

- **Cloud VM:** Oracle Cloud (Mumbai/ARM), IP **`YOUR_VM_IP`**, user `ubuntu`,
  SSH key `~/.ssh/oracle.key`. Repo at `~/institutional_trading_system_v2`.
- **NO git remote** on either laptop or VM. **Code is synced via `rsync`**, not git.
  - Push code to VM: `rsync -az -e "ssh -i ~/.ssh/oracle.key" platform_core scripts ubuntu@YOUR_VM_IP:institutional_trading_system_v2/`
    (laptop has a `pushvm` bash function for this).
  - Pull the recorded corpus back: `deploy/pull_from_vm.sh ubuntu@YOUR_VM_IP`
    (BUG: the committed script omits `-e "ssh -i ~/.ssh/oracle.key"` — pass it).
- **systemd timers (IST, Mon-Fri, all `enabled`):**
  - `08:45` `trading-token.timer` → headless-TOTP Kite token refresh
    (`deploy/kite_token_totp.py`). The whole day depends on this succeeding.
  - `09:10` `trading-model.timer` → starts the model
    (`scripts/run_supervised_live_session.py --execution-mode real_dry_run`).
  - `15:35` `trading-model-stop.timer` → stops it. Crash → auto-restart 5s.
- **Monitoring (from laptop):**
  - `watchvm` (bash fn) = `ssh -i ~/.ssh/oracle.key ubuntu@YOUR_VM_IP 'journalctl -u trading-model.service -f'`
  - Human log: `tail -f ~/institutional_trading_system_v2/logs/current_live_log.txt`
- **Recorded corpus (grows every session — this is the research fuel):**
  `logs/trades.db` (SQLite), `logs/*_shadow.jsonl` (8 streams, see §9),
  `logs/strike_selection_audit.jsonl`, `data/option_history/*.jsonl` (real option
  chains recorded per day — lets you reprice any strike at any time).
- **Research loop:** VM records → `pull_from_vm.sh` → analyze locally (replay,
  factor-eval, walk-forward) → validated code changes → `pushvm` → picked up at
  next 09:10. Remote does not blind research; it feeds it.

**trades.db key table `pt_trades`** columns: `entry_ts, exit_ts, run_mode,
underlying, contract_symbol, strike, option_type (CALL/PUT), direction (LONG/SHORT),
lots, lot_size, entry_premium, exit_premium, pnl (NET of costs), bars_held,
exit_reason, state, conf_raw, expiry_date, ...`. NOTE: `option_type` is `CALL/PUT`
but `option_history` uses `CE/PE` — map when joining.

---

## 5. ⭐ THE CENTRAL FINDING — CURRENT EDGE STATE (read this twice)

As of 2026-07-11, over **66 dry-run trades** (cost-adjusted NET P&L):

- **Cumulative: +₹14,385, 39% win rate.** BUT this is **entirely ONE day** —
  2026-06-29, where **3 SHORT trades made +₹44,102.** **Ex that one day the book
  is −₹29,718.** Last 10-12 sessions: only ~2-3 green. A multi-week drawdown.

- **THE decisive pattern — direction asymmetry:**
  | Bucket | Trades | Win rate | Net |
  |---|---|---|---|
  | SENSEX SHORT | 9 | 66% | **+₹36,611** |
  | NIFTY SHORT | 5 | 40% | +₹13,677 |
  | SENSEX LONG | 25 | 40% | −₹14,137 |
  | **NIFTY LONG** | 27 | 29% | **−₹21,767** |
  - **SHORT total: +₹50,289 (57% WR). LONG total: −₹35,904 (34% WR).**
  - The system is **a profitable short-trader trapped inside a compulsive
    long-trader.** It takes LONG 52× vs SHORT 14× — it over-trades the LOSING
    direction. Root cause: the **MCB bias engine freezes LONG (conf 1.0)** and is
    slow to flip (higher timeframes 4h/1d outvote intraday reversals) — the
    "bias-staleness" problem. Every recent red day is a losing LONG.

- **Exit asymmetry:** STRUCTURE_EXIT (59 trades) nets **+₹34,343**; **3 STOP_LOSS
  trades net −₹22,596** (one was −₹15,972 in 4 bars — a large LONG hit by a fast
  adverse move on 06-18). A per-trade max-loss cap would bound this tail
  (identified, NOT yet wired — see §11).

- **Confidence is degenerate:** MCB `confidence = 1.00` at all decision points, so
  it carries zero discriminating information — which is WHY confidence-scaled
  sizing does nothing and "conviction" is anti-predictive.

**Consequence for arming:** the infrastructure is ready but the **edge is not
demonstrated** (positive P&L is one lucky day). **DO NOT ARM REAL FUNDS** until the
book is robustly positive out-of-sample (see §12).

---

## 6. GOVERNING METHODOLOGY & PHILOSOPHY (do not violate)

- **Shadow-first, OOS-gated.** New signals are logged (shadow) alongside live
  decisions, joined to real trade outcomes, and only promoted after they separate
  winners from losers across **≥3 held-out days** (classifiers: ~100+ samples).
  Every promotion path is env-knobbed and reversible.
- **Measure before believing.** Plausible ideas repeatedly died OOS here (see §11).
  In-sample edges are assumed overfit until walk-forward proves otherwise. A single
  good day (like 06-29) is NOT evidence.
- **No autonomous learning.** The model does NOT learn from its losses. The
  `adaptive/` learning trio (AdaptivePolicy/MarketMemory/Reflection) is deliberately
  unwired. Adaptation is human-in-the-loop: research → validate → wire.
- **The edge is DATA-bound, not cleverness-bound.** More reliable sessions = bigger
  corpus = the actual bottleneck. This is why the VM (reliability) and the shadow
  streams (coverage) matter more than new features.
- **Never make untested hot-path changes.** A lock held across the live `on_ticks`
  WS callback once deadlocked a live session (reverted). Shadow/gate code is always
  wrapped in try/except and can never disturb trading.
- **Costs:** all P&L is NET of transaction costs (a past "cost-blindness" bug
  reported gross; fixed). Quote NET only.
- **No lookahead** in structure analytics — current finalized bar + prior events only.

---

## 7. HOW A DECISION IS GATED (the gate stack, in order)

Bias (MCB) → strike selected → `_effective_conf` chain [PDL · OR-chop-guard ·
FVG-quality · **alignment gate** · **NIFTY-long gate**] → hard gates [contradiction
cooldown, warm-start ≥20 bars, OI liquidity, risk, heat, position limit (max 2),
halt] → soft gates [`score_margin ≥ 0.05`, confluence] → SENSEX-priority routing
(NIFTY defers when SENSEX has a position + must clear a very-strong bar) →
open-drive window (lifts session/warmup blocks only 09:15–09:35 IST if seeded) →
sizing [tier sizer → re-entry haircut → **regime de-size** → %-capital rails] →
order. **The gate stack IS the edge** — raw OR-breaks have no edge by themselves.

---

## 8. GATES & ENV KNOBS (current live config — all reversible)

Set in code defaults or the supervisor env. `real_dry_run` runs with these ON:

| Knob | Default | Effect |
|---|---|---|
| `LONG_ALIGNMENT_GATE` | 1 (ON) | Block LONG entry when `is_aligned=False` (HTF structure contradicts). Targets the −₹36k long bleed. **UNVALIDATED — live experiment since 2026-07-10.** |
| `NIFTY_LONG_GATE` | 1 (ON) | Block NIFTY LONG entries (worst bucket −₹21.8k). NIFTY shorts + SENSEX untouched. UNVALIDATED. |
| `REGIME_DESIZE_MULT` | 0.5 | Halve lots when directional regime == BALANCED (lost 5/7 days recently). |
| `SENSEX_PRIMARY_ROUTING` | 1 | NIFTY enters only when SENSEX flat AND `_effective_conf ≥ NIFTY_VERY_STRONG_CONF`. |
| `NIFTY_VERY_STRONG_CONF` | 0.95 | The NIFTY quality bar (weak in practice — conf saturates at 1.0). |
| `PAPER_OPEN_DRIVE_WINDOW` | 1 | Lift session/warmup blocks 09:15–09:35 IST (only if warm-start seeded). |
| `PAPER_REENTRY_RISK_HAIRCUT` | 0.5 | 2nd+ same-direction session entry sizes at ×0.5. |
| `PAPER_CONV_LIVE` | **0 (OFF — keep off)** | Would feed graded conviction into sizing/floor/NIFTY-routing. **REJECTED: conviction is ANTI-predictive, so this sizes up losers.** |
| `GEX_REGIME_GATE` | 0 (OFF — keep off) | GEX wall veto. **MEASURED HARMFUL** (+31,938 → −1,082); the model wins by breaking THROUGH walls. |
| `STRIKE_MARGIN_PRE_DTE` | 0 (OFF) | Pre-DTE margin fix. A/B'd → worse. Keep off. |

Shadow toggles (`PAPER_*_SHADOW`, all default 1) are logs-only, see §9.
Arm-readiness check: `python scripts/arm_readiness_check.py` (offline).

---

## 9. SHADOW SIGNALS (8 live streams — logs-only, the research corpus)

All in `platform_core/data/live/`, all write `logs/<name>.jsonl`, all wrapped so
they never affect trades. Shared feature math for the two classifiers lives in
`shadow_feature_kit.py`.

1. **htf_shadow** — HTF alignment (`is_aligned`, per-timeframe biases, agree/against
   count). Was logging nulls until fixed 2026-07-10 (read the wrong context object).
   Now records real values. **This feeds the alignment gate — the #1 lead.**
2. **regime_shadow** — vol/directional regime (COMPRESSION/BALANCED/EXPANSION,
   LOW/NORMAL/HIGH vol) + confidence multiplier. Also exposes `.last_regime` for the
   regime de-size gate.
3. **trap_shadow** — TrapProbabilityModel score on each OR-break attempt
   (direction-agnostic). Weak `>0.8` tail-filter OOS.
4. **conviction_shadow** — transparent graded conviction (non-saturating, unlike MCB
   conf). **Verified ANTI-predictive** (winners 0.565 vs losers 0.695).
5. **strike_selection_shadow** / audit — strike scoring, `score_margin`, DTE
   multiplier.
6. **options_analytics_shadow** — GEX (gamma regime, walls, flip), OI (put/call
   ratios), Dealer (net delta/gamma/vanna), Premium (extrinsic distribution). These
   4 engines were computed-then-discarded; now logged (2026-07-10).
7. **continuation_shadow** — ContinuationClassifier probability that a move
   continues. ~20 of 22 features real (trap/reversal prob defaulted). Validated
   responsive (uptrend 0.94/conf 0.74 vs chop 0.84/conf 0.46). **This is the
   real-vs-fake-momentum signal that gates strike-rotation ideas.**
8. **reversal_shadow** — ReversalClassifier probability a move reverses. Inverse of
   continuation (chop 0.77 > trend 0.70). Built 2026-07-10.

**How to use them:** join a shadow's value at a trade's `entry_ts` (nearest ts,
same underlying, ≤~90s) to that trade's `pnl`; check if the signal separates
winners from losers, consistently across days. This is the promotion test.

---

## 10. ⭐ RESEARCH LEDGER — WHAT IS ALREADY DECIDED (do NOT re-litigate)

**ACCEPTED / WIRED (with evidence):**
- OR chop-guard (buffer + extension-chase limit + same-contract 2-loss block +
  same-direction 3-entry cap): 34.5%→66.7% WR, +₹12,810. **The core protection.**
- Breakeven ratchet (+8% peak arms a floor; winners peak ≥10.4%, losers ≤5.9%).
- Re-entry risk haircut (×0.5 on 2nd+ same-dir entry): −1,354 → +317 same trade set.
- Open-drive window (09:15–09:35 IST, triple-interlocked): blocked-set forensics
  72% WR / +₹17,972 — IN-SAMPLE; OOS judging ongoing.
- OR seed on late start (fixes silent chop-guard-off bug, root cause of a −₹2,893).
- Nearest-expiry selection with DTE floor.
- Real-LTP marking for paper/dry-run exits (was Black-Scholes → fictional P&L).
- Cost model everywhere (NET P&L).
- Alignment gate + NIFTY-long gate + regime de-size (2026-07-10, UNVALIDATED tests).

**REJECTED (measured, do not re-propose without NEW data):**
- **OTM strike selection** ("lose little / win big"): ATM +₹13k vs OTM+1 −₹28k;
  OTM wins 9× less because intraday moves are too small to reach OTM. ATM (δ≈0.5)
  is correct.
- **Conviction floor / conviction-based sizing (`PAPER_CONV_LIVE`)**: conviction is
  anti-predictive → sizes up losers. Keep OFF.
- **GEX wall veto**: kills the edge (+31,938 → −1,082). Model wins THROUGH walls.
- **Trailing stop**: repeatedly inert / negative. Rejected multiple times.
- **Strike-rotation-after-loss**: +20% but entirely one day (07-06), hurts trend
  days. Needs a validated real-momentum signal (continuation_shadow) first.
- **Pre-DTE margin fix**: A/B on 16 days → 2 divergent both worse. Crushed margin
  is an accidental (useful) chop brake.
- **8% risk profile**: measured NET-NEGATIVE −₹13,286 vs 2% +₹317.
- **Shorting counter-bias OR-breaks**: worst subset. MCB stickiness is protective.

**OPEN / UNVALIDATED (in flight):**
- The 3 gates wired 2026-07-10 (alignment, NIFTY-long, regime de-size) — awaiting
  their first OOS sessions. **Next task: join gate-fires + `is_aligned` to LONG
  outcomes to confirm they kill losers, not winners.**

---

## 11. OPEN RESEARCH QUESTIONS / NEXT STEPS (prioritized)

1. **VALIDATE the 3 new gates (highest priority).** After each session: does
   blocking misaligned longs + NIFTY longs + de-sizing BALANCED move the book
   positive ex-outlier? Does the alignment gate block losers (good) or winners
   (revert instantly via knob)? This is the direct path to arm-readiness.
2. **Per-trade max-loss cap** (identified, NOT wired — user deferred). A hard
   rupee/% cap so no single trade can lose ₹16k. Pure safety rail, arming-relevant.
3. **Continuation/reversal evaluation.** Once corpus grows: do high-continuation /
   low-reversal breakouts actually follow through? If yes → gates momentum-conditional
   strike rotation and a "trade the real breakout, skip the fake" filter.
4. **Fix the confidence saturation the RIGHT way.** MCB conf=1.0 carries no info.
   The fix must use PREDICTIVE signals (alignment, regime) — NOT conviction
   (anti-predictive). Partially done via the gates; consider a proper composite.
5. **Time-of-day filter.** 11:00 IST is the worst hour (22% WR, −₹9.4k); 09:00 also
   red; 10:00 & 13-14:00 carry the book. Measure before wiring.
6. **Expiry-day handling.** Long options bleed theta on stalled expiry-day moves.
   Consider skipping expiry day or a theta-aware adjustment. (DTE analysis is
   confounded with direction/date — measure carefully.)
7. **The short-side edge.** SHORT works (+₹50k). Why does the model short so rarely
   (14×)? Can it be encouraged to flip short faster when structure reverses —
   WITHOUT hacking the MCB bias engine (risky)? This is the bias-staleness frontier.
8. **Exit giveback.** Historically exit capture ≈ 0.36 (winners give back a lot).
   Structure exits net +₹34k though, so this is lower priority than direction.

---

## 12. ARMING REAL FUNDS — CRITERIA & BLOCKERS

- **Infra readiness: DONE.** `real_armed` path fully wired + test-passing; kill
  switches, reconciliation, cap interlocks enforced. Readiness gate 11/11. These
  are PLUMBING checks — they do NOT check profitability.
- **Edge readiness: NOT DONE.** `scripts/arm_readiness_check.py` is the gate:
  needs (a) ≥10 recent trading days, (b) ≥55% green, (c) net positive EX-best-day
  (robustness), (d) LONG win rate ≥45%. **Currently FAILS 3/4** (green 25%,
  ex-best −₹26,879, LONG WR 34%).
- **Hard rule:** the system is NEVER armed without the operator typing the literal
  phrase **"arm real orders"**. "Full power of attorney to run live" means
  `real_dry_run` ONLY. Parity directive: armed would run the EXACT same profile as
  dry-run (no canary/derating).
- **Path to arm:** validate the gates over ~1-2 weeks of dry-run → confirm the
  book is robustly positive ex-outlier and the LONG bleed is fixed → arm-check
  passes → THEN, and only then, consider arming.

---

## 13. KEY FILES MAP

- `platform_core/data/live/live_structure_pipeline.py` — **the live pipeline (start here)**.
- `platform_core/data/live/*_shadow.py` + `shadow_feature_kit.py` — the 8 shadows.
- `platform_core/intelligence/market_context_builder.py` (MCB, bias) &
  `market_context.py` (full HTF context, `is_aligned`).
- `platform_core/adaptive/regime_engine.py` — regime classifier.
- `platform_core/ml/{continuation,reversal}_classifier.py` — the classifiers.
- `platform_core/options/options_context/` — `OptionsContext` + `EngineRunner`.
- `scripts/run_supervised_live_session.py` — the launcher (builds env, runs pipeline).
- `scripts/arm_readiness_check.py` — the EDGE arm-gate.
- `scripts/replay_day.py` / `platform_core/backtesting/surgical_replay.py` — offline
  replay on real recorded chains (NOTE: surgical_replay is a standalone
  re-implementation — it does NOT exercise the live pipeline's shadow path).
- `deploy/` — systemd units, `kite_token_totp.py`, `pull_from_vm.sh`, `setup.sh`,
  `ORACLE_SETUP.md`, `README.md`.
- `logs/trades.db`, `logs/*_shadow.jsonl`, `data/option_history/` — the corpus.
- `CLAUDE.md` — repo conventions.

---

## 14. CRITICAL DO-NOTs

- **DO NOT arm real funds** — edge unproven; and never without the literal
  "arm real orders" phrase.
- **DO NOT enable `PAPER_CONV_LIVE` or `GEX_REGIME_GATE`** — both measured harmful.
- **DO NOT re-propose** OTM strikes, trailing stops, 8% risk, or shorting
  counter-bias breaks without genuinely new evidence — all rejected with data.
- **DO NOT hold a lock across `on_ticks`** or make unwrapped hot-path changes.
- **DO NOT trust a single good day** as edge (06-29 is the cautionary tale).
- **DO NOT commit `configs/kite_config.json` token changes** (daily-rotating;
  `git update-index --skip-worktree` it). Real credentials live only on the VM in
  `/etc/trading/secrets.env` (0600).
- **DO NOT assume git deploys to the VM** — there is no remote; use `rsync`.

---

## 15. GLOSSARY

- **MCB** — MarketContextBuilder; the authoritative bias engine (LONG/SHORT/NEUTRAL
  + confidence). Its confidence saturates at 1.0 at decision time.
- **is_aligned** — whether the MCB exec bias agrees with the HTF structure biases
  (daily/h4/h1/m15). False = bias contradicts structure = the losing longs.
- **OR** — opening range; the chop guard blocks entries inside it + buffer.
- **score_margin** — rank-1 minus rank-2 strike score; a strike-quality gate
  (`< 0.05` blocks). Compressed near expiry by the DTE multiplier.
- **DTE** — days to (option) expiry.
- **Bias-staleness** — MCB freezes LONG because 4h/1d outvote intraday reversals;
  the core structural leak (THE edge frontier).
- **Shadow** — a signal logged alongside live decisions, never affecting trades,
  to test predictive power before promotion.
- **real_dry_run** — full live pipeline, real data/prices, no real orders.

---
_End of handoff. The single most important next action: validate whether the
alignment + NIFTY-long gates (wired 2026-07-10, currently ON in dry-run) actually
convert the −₹36k long bleed toward breakeven over the coming sessions — that is
the gate to arm-readiness._
