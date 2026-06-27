# Intelligence Wiring Plan (review → implement 2026-06-27)

Goal: move SENSEX toward "best-day quality, most of the time" by wiring the
disconnected intelligence — **without overfitting** and **without recreating the
over-gating mess** we just spent a session removing (iv_attr / score_margin /
oi_support all silently killed SENSEX).

## Guiding principle (read first)

The best days were **selective**, not high-volume: a few high-conviction trades
in a favorable regime (06-05: ~18% IV, dominant peak-OI strikes, strong
structure, +₹45,741). So the target is **fewer-but-better trades + size up true
conviction** — achieved by *skipping bad conditions* and *calibrating size to
real conviction*, NOT by adding more vetoes. Every wiring must be judged against:
"does this make us more selective / better-calibrated, or just add another gate?"

## The overfitting threat model (what would make this a mess)

1. **Tiny sample.** We have ~40 executed trades across a handful of days. Tuning
   anything to make *those exact days* look great = fitting noise. This is the
   #1 risk and the reason implementation is gated on more data (see Phase 0).
2. **Too many correlated signals.** Regime + HTF + continuation often key off the
   same trend → they are NOT independent confirmation; stacking them creates
   false confidence and fragility.
3. **In-sample optimization.** Designing a rule on the same data you score it on
   always looks good and usually fails live.
4. **Gate proliferation.** Each added veto narrows the tradeable set; 9 at once
   risks back-to-zero trades and an untestable black box (exactly the bug class
   we just fixed).
5. **Learning on a small/biased history** (adaptive layer) → spurious patterns,
   drift, non-reproducibility.

### Defenses (non-negotiable for every step)
- **Shadow-first.** Wire each engine in shadow mode (compute + log what it WOULD
  do; do not touch live trades). The codebase already has this infra
  (`_run_shadow_option_intelligence`, shadow auditor, shadow targets).
- **Out-of-sample / walk-forward.** Use the existing `WalkforwardFramework`.
  Design/tune on an early window; validate on a held-out later window never used
  for tuning. Reserve the most recent sessions as a final untouched holdout.
- **Pre-register the success metric** for each engine BEFORE looking at results
  (avoid p-hacking). Metric must be on out-of-sample trades.
- **Bias toward fewer, simpler, robust signals.** Prefer one good regime filter
  over five fine-tuned gates. Drop anything that doesn't clearly earn its place.
- **One engine at a time.** Never promote two in the same measurement window —
  you won't know which did what.
- **Keep it deterministic** (see Exclusions): no learning/drift until everything
  else is proven and there's a large dataset.

## Prerequisite — Phase 0 (must clear before ANY promotion)

- **Gather data.** Run the new aggressive-sizing config in `real_dry_run` for
  enough sessions to have a statistically meaningful sample (target: several
  weeks / 100+ decisions per symbol, spanning >1 volatility regime). 40 trades
  is too few to validate anything.
- **Build the measurement harness:** a script that replays a session's decisions
  with an engine's shadow output attached, and reports — on OUT-OF-SAMPLE trades
  — win-rate / P&L / drawdown WITH vs WITHOUT the engine's filter or size change.
- **Define the holdout split** (e.g., tune on weeks 1–N, validate on N+1…), fixed
  up front and never peeked at during design.

## Per-engine plan, consequences, and success metric

### 1. RegimeEngine / RegimeClassifier → selectivity  *(highest value)*
- **What:** classify volatility/directional regime per bar; only allow full-size
  trading in regimes resembling the best days; throttle or skip in chop.
- **Wire:** shadow — log "would-skip / would-allow" per decision. Then promote as
  a *size scaler* first (reduce size in poor regime), only later as a hard skip.
- **Consequence / risk:** could over-filter → too few trades (the gate bug
  again). Regime labels can lag at turns. Mitigate: start as a soft size-scaler,
  not a veto; validate it skips the LOSING conditions, not the winning ones.
- **Success metric (OOS):** higher avg P&L/trade and lower drawdown with regime
  filter on, with trade count not collapsing (>~60% of trades retained).

### 2. ConfidenceEngine → real conviction signal  *(fixes a known defect)*
- **What:** replace/augment the MCB confidence that drives sizing. Today MCB conf
  saturates at 1.0 on ~2/3 of trades, so our widened conviction-sizing (1.5×)
  barely differentiates. A graded confidence makes "best setups = more lots"
  actually work.
- **Wire:** shadow — log ConfidenceEngine score next to MCB conf and the trade
  outcome. Check the score actually *separates* winners from losers before it
  touches sizing.
- **Consequence / risk:** if mis-calibrated it sizes the wrong trades bigger —
  directly amplifies losses. Must validate calibration (do higher-confidence
  trades actually win more?) before promoting to the sizer.
- **Success metric (OOS):** monotonic relationship between confidence bucket and
  realized win-rate; sizing-by-confidence beats flat sizing on P&L.

### 3. HTF bias → entry alignment
- **What:** use higher-timeframe bias (already computed, currently `[DIAG]`) to
  filter or down-weight trades fighting the HTF.
- **Wire:** shadow — log HTF-aligned vs counter-HTF per trade + outcome.
- **Consequence / risk:** blocks counter-trend/mean-reversion setups that may be
  winners; HTF lag. Mitigate: down-weight (size) rather than hard-veto first.
- **Success metric (OOS):** counter-HTF trades demonstrably underperform → then
  filtering them helps.

### 4. Continuation / Reversal / Trap classifiers → entry-quality (rule-based)
- **What:** heuristic entry-quality scores; trap model flags likely fakeouts.
- **Wire:** shadow ONLY for a long while — these are *additional gates*, highest
  over-blocking risk. Promote at most ONE, and only if it clearly removes losers.
- **Consequence / risk:** gate proliferation → over-filtering; correlated with
  regime/HTF (double-counting). Keep at most one; check independence.
- **Success metric (OOS):** the trades it would block are net-negative AND it
  doesn't block a meaningful share of winners.

### 5. Options analytics (OIAnalytics / DealerHedging / PremiumBehavior / IVSurface)
- **What:** refine strike choice and timing (GEX already wired as a wall-veto).
- **Wire:** shadow as strike-score inputs; lowest priority, marginal vs effort.
- **Consequence / risk:** complexity for small edge; IV-surface needs the atm_iv
  bug fixed first (see open item). Defer until 1–3 are proven.
- **Success metric (OOS):** better strike-selection rank correlation with realized
  P&L vs current composite score.

## Exclusions (deliberate — not in this plan)

- **Adaptive / learning layer** (MarketMemory, ReflectionEngine,
  AdaptivePolicyEngine): ~290 lines of skeleton, and wiring it makes the model
  **learn and drift**, reversing the deliberate "deterministic + human-tuned, no
  autonomous learning" design. With a ~40-trade history it would overfit
  immediately. **Do not wire** until (a) everything above is proven, (b) there is
  a large multi-regime dataset, and (c) you explicitly decide to accept a
  learning (non-deterministic) system. Revisit as a separate project.

## Sequencing

- **Phase 0:** data + harness + holdout split. (Gated on dry-run sessions.)
- **Phase 1:** shadow-wire RegimeEngine (#1) and ConfidenceEngine (#2). Measure.
- **Phase 2:** promote whichever passes OOS (regime as soft size-scaler first).
- **Phase 3:** shadow-wire HTF (#3); measure; promote if it earns it.
- **Phase 4:** consider ONE of the entry-quality classifiers (#4); options
  analytics (#5) last.
- **Stop criterion:** if added complexity isn't improving OOS metrics, STOP —
  selectivity + good sizing beats a pile of gates.

## Open item that blocks part of this
- **atm_iv root-cause bug** (reads ~half true value for SENSEX). It no longer
  blocks trades (iv_attr demoted) but corrupts any IV-based intelligence
  (IVSurface, iv_attractiveness scoring). Fix before Phase 4/5.
