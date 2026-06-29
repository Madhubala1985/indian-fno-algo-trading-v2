# Conviction-Driven Position Sizing — Design (implement AFTER conviction is proven)

Status: **DESIGN ONLY.** Do not implement until the conviction shadow
(`conviction_shadow.py`, harness `analyze_conviction_shadow.py`) proves —
out-of-sample — that higher conviction actually wins more. Wiring unproven
conviction into live sizing would amplify a coin-flip, not an edge.

## 1. The core principle (read first)

**Position size is a MULTIPLIER on the per-trade outcome. It does not create
edge — it amplifies whatever edge (or lack of it) already exists.**

- If a setup has positive expectancy (win_rate·avg_win > loss_rate·avg_loss),
  sizing up increases expected profit — and variance (drawdowns).
- If a setup has NO edge, sizing up only increases expected LOSS and variance.

So "size more on good setups" is intelligent **only if we can tell good setups
from average ones in advance.** That discriminator is the conviction score — and
it must be *validated* before it drives size. This is the entire safety case.

## 2. The two-sided safety thesis (the central concern)

The user's worry — "more lots = more profit AND more loss; make sure the model
doesn't just book bigger losses" — is exactly right and drives the design:

1. **Size up only on PROVEN edge.** Conviction may drive size only after the
   shadow shows HIGH-conviction trades beat LOW out-of-sample. Until then, sizing
   stays on the current fixed fractions.
2. **Asymmetric response — quick to shrink, slow to grow.** Drawdown haircuts and
   the consecutive-loss / daily-loss halts shrink size *immediately* on losses;
   conviction may only grow size when BOTH conviction is high AND conditions are
   favorable. Losing faster-down than up is the protection against a streak.
3. **All downside controls stay UNSCALED by conviction.** Conviction scales the
   risk budget UP toward a cap; it never weakens the per-trade stop, heat cap,
   drawdown haircut, daily-loss halt, consec-loss halt, or chop brake. Those are
   the risk-of-ruin guards and remain fixed.
4. **A conviction FLOOR.** Below a minimum conviction, the setup is SKIPPED (size
   0), not merely small — a weak setup is a no-trade, not a small bet.

Net effect: bigger size is *earned* by proven-good setups; a losing streak at any
size is bounded by the unscaled halts (e.g. hard-halt at 15% daily, account DD
21%, 3 consecutive losses) so amplified losses cannot run away.

## 3. The mechanism

Replace the 3 discrete lot ceilings (arbitrary 1/10/15) with a **continuous
deployment fraction driven by conviction**, then derive lots:

```
# conviction c ∈ [0,1] from the (validated) conviction score
if c < CONV_FLOOR:            # e.g. 0.35 — weak setup
    lots = 0                   # skip, do not trade
else:
    # map conviction to a deployment fraction of capital (premium outlay)
    deploy_frac = DEPLOY_MIN + (DEPLOY_MAX - DEPLOY_MIN) * norm(c)
    #   norm(c) = (c - CONV_FLOOR) / (1 - CONV_FLOOR)   ∈ [0,1]
    #   DEPLOY_MIN ~ 0.08 (8% of capital), DEPLOY_MAX ~ 0.30 (= the heat cap)
    budget_premium = equity * deploy_frac * haircuts   # dd/heat/liquidity/cash
    raw_lots = floor( budget_premium / (premium * lot_size) )
    lots = min(raw_lots, liquidity_cap, hard_lot_safety_cap)
```

Key properties:
- **Auto-scales with capital** (deploy_frac is a % of equity) — no magic lot numbers.
- **Continuous** — smoothly more on stronger conviction, not 3 steps.
- **Floored** — weak setups skipped.
- **Capped** — by the heat cap (DEPLOY_MAX), a liquidity cap (below), and a final
  absolute safety cap.

### Why this is Kelly-shaped (the rigorous justification)
Fractional-Kelly says optimal stake ∝ edge. If conviction correlates with
realized win-rate (the thing we must validate), then deploy_frac ∝ conviction is
a fractional-Kelly approximation: bigger edge → bigger stake, no edge → floor →
skip. We deliberately stay well BELOW full Kelly (DEPLOY_MAX = heat cap 30%, and
real_armed far lower) because full Kelly is too aggressive and assumes perfectly
known probabilities, which we do not have.

## 4. Liquidity-aware cap (replaces the arbitrary lot ceilings)

The old 10/15 lot ceilings crudely stood in for "don't size beyond what the
market absorbs." Replace with an explicit cap from the strike's depth:

```
liquidity_cap = floor( LIQ_FRAC * min(strike_open_interest, strike_volume) / lot_size )
#   LIQ_FRAC ~ 0.01–0.02 (never take more than ~1–2% of the strike's OI/volume)
```

This scales the cap with *actual* market depth instead of a fixed number — large
accounts can size up only where liquidity genuinely supports it.

## 5. What stays exactly as-is (the safety stack — never conviction-scaled)

- Per-trade 30% stop (and its intrabar cap).
- Portfolio heat cap (30% paper / 10% real) — also the DEPLOY_MAX.
- Drawdown haircut (size ∝ remaining room to account-DD).
- Daily soft/hard halt (14% / 15% paper), account-DD halt (21%), consec-loss (3).
- Chop re-entry brake.
- real_armed keeps the conservative fractions/caps; conviction sizing is
  paper/dry-run first and promoted to real_armed only after separate validation.

## 6. Validation gates (must pass BEFORE this drives live size)

1. **Conviction separates winners.** `analyze_conviction_shadow.py` shows HIGH
   tertile win-rate / avg-PnL clearly > LOW, out-of-sample, on a meaningful trade
   count (target: ≥100 trades spanning >1 volatility regime).
2. **Monotonicity.** Win-rate rises monotonically across conviction buckets (not
   a fluke of one bucket).
3. **Calibration of the map.** DEPLOY_MIN/MAX and CONV_FLOOR chosen so realized
   risk-per-trade stays within the existing fractions — NOT tuned to maximize the
   backtest (overfitting). Prefer round, conservative numbers.
4. **Shadow the new sizer first.** Compute the would-be conviction-lots alongside
   the current lots for several sessions; confirm it isn't wildly larger and that
   its larger sizes land on the winners — before it ever places size.

## 7. Migration path

1. (now) Conviction shadow live, gathering data.
2. Run dry-runs → `analyze_conviction_shadow.py` until gates §6.1–6.2 pass.
3. Build the sizer in SHADOW: log conviction-lots vs current-lots per decision
   (no effect). Measure §6.4.
4. Promote to paper/dry-run live sizing (env-gated, e.g. PAPER_CONV_SIZING=1).
5. Monitor several sessions on the dashboard (uP&L, drawdown, halt frequency).
6. Only then consider real_armed, with conservative DEPLOY_MAX and a separate
   readiness review.

## 8. Failure modes to watch (and why each is contained)

- **Conviction doesn't separate winners** → gates §6 fail → never promoted. (The
  whole design is conditional on this.)
- **Bigger size, worse drawdowns** → unscaled daily/DD/consec halts + heat cap
  bound the loss; the asymmetric shrink-fast logic de-risks into a streak.
- **Overfit map parameters** → §6.3 forbids tuning to the backtest; use round
  conservative values, validate out-of-sample.
- **Illiquid fills at size** → liquidity cap (§4) ties size to real depth.
- **Regime shift** (the edge decays) → ongoing shadow measurement + the halts;
  re-validate periodically.
```
