# How the model works, what's real, and what's still just watching

Plain-language reference. Written 2026-07-12. No hype — where something is unproven it says so.

## 1. What the model actually does, step by step

Every minute during the session, one bar completes and this pipeline runs (all in one live process):

1. **Ticks → bars.** The Kite websocket streams index ticks (price, volume, 5-level depth).
   `BarBuilder` turns them into 1-minute bars for NIFTY / SENSEX / BANKNIFTY.
2. **Bars → market structure.** Each finished bar feeds the structure engine: swings, then
   BOS (break of structure), CHOCH (change of character), FVG (fair-value gaps),
   support/resistance. This is pure price-pattern reading, no lookahead.
3. **Structure + higher timeframes → a directional BIAS.** The `MarketContextBuilder` (MCB)
   combines 1m/15m/1h/4h/1d reads into one bias: LONG, SHORT, or NEUTRAL, with a confidence.
   **The 4h + 1d timeframes carry ~60% of the weight** — this is why the bias can be "sticky"
   and slow to flip (the known weakness).
4. **Bias → option choice.** If the bias is directional, the options layer builds a chain
   snapshot, and `StrikeSelectionEngine` picks a near-ATM strike (delta ~0.5) of the right
   type — a CALL to express LONG, a PUT to express SHORT. It always BUYS the option.
5. **Sizing.** `PositionSizer` computes lots from % of capital, then a chain of hard rails
   clamps it: per-symbol cap, exchange freeze qty, premium cap, correlated-heat cap, and now
   the **per-trade max-loss cap** (batch-4). Whatever survives is the position size.
6. **Entry gates (can veto the trade).** Opening-range filter, FVG-quality floor, session
   phase, and the **alignment gate** (block a LONG when higher-timeframe structure disagrees)
   and **NIFTY-long gate**. Any veto sets effective confidence to 0 → no trade.
7. **Order.** In `real_dry_run` it logs the trade to `trades.db` and marks it to real option
   LTP (no real money). In `real_armed` it would place the order (never enabled).
8. **Exits.** While a position is open: a 30% stop, a 200% take-profit, a structure-based exit
   (an opposing BOS/CHOCH), a breakeven ratchet (once +8%, never let it become a loss), and
   the EOD close. Whichever fires first closes the trade.

That's the whole loop. Everything else is either **safety rails** (rails in step 5, halts,
crash guards) or **shadows** (step 9 below — observers that only write log files).

## 2. What is REAL vs what is still being watched

- **REAL and affecting trades (validated on live `trades.db`, not on the replay):** the entry
  gates, the sizing rails, the max-loss cap, the cost model, the stop/ratchet/structure exits.
  These are either arithmetic (the max-loss cap provably shrinks a position) or measured on the
  66 real trades (the LONG bleed the alignment gate targets is −₹43,458 ex-best-day over 49
  trades — robust across days, not one lucky day).
- **SHADOW (writes a log, changes nothing):** cross-asset premarket, order-flow, regime, HTF,
  trap, conviction, options-analytics. These exist to gather out-of-sample evidence so we can
  decide — later, on real data — whether they'd help. None of them touches a trade.

## 3. Why the replay being wrong did NOT corrupt the live model

The `SurgicalReplay` (offline "what-if" tool) disagrees with the live model 10 out of 11 days —
it even flipped the +₹44k day into a loss. That was a real problem for **offline studies**.
But the live model never used the replay: its trades come from the live pipeline above and are
recorded in `trades.db`. So the fix was simple and total — **judge everything on `trades.db`,
never on the replay.** Every batch-4 finding was computed that way.

The structural replay fix (snapshot the true startup state, replay from it) is **forward-only**:
it can make *future* replays faithful, but it cannot retroactively fix the historical days
(there are no snapshots for them). Until snapshots accumulate on real trading days, the replay
parity stays 1/11 and the replay is not trusted for direction/P&L.

## 4. How we know it's improving, not going dumb

Three guardrails, by design:
1. **Nothing wires without proof on real trades.** Conviction-floor, GEX-veto, straddles, and
   the "SHORT is an edge" idea were all REJECTED after measurement. Saying "no" is the point.
2. **ex-best-day on every number.** A bucket carried by one day is called noise, out loud, so a
   one-day fluke can't masquerade as an edge.
3. **The edge-health monitor** watches our own per-bucket expectancy and flags decay — it's the
   smoke alarm for "the model is going dumb."

If we ever can't validate a thing on real trades, it does not get wired. Full stop.
