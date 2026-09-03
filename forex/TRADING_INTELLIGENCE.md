# The Decision Framework — what I actually do, and the proof

Written 2026-09-02. Every rule below traces to a measured failure, not an opinion.
Purpose: when a real edge appears, the edge->money machinery is already built and calibrated.

---

## PART 1 — THE PROOF: my search process does NOT generalise

I walked my own methodology forward. Train 12 months, search 126 configs
(hour-window x lookback x hold x momentum/reversion), pick the best **that passes all my
declared gates**, then trade it blind for the next 3 months. Roll. Never look at test data.

| out-of-sample period | config chosen on train | train net | **TEST net** |
|---|---|---|---|
| 2023-11 -> 2024-04 | MOM h14 lb8 hold24 | +620 | **-463** |
| 2024-04 -> 2024-08 | REV h7 lb4 hold16 | +345 | **-319** |
| 2024-08 -> 2025-01 | MOM h9 lb12 hold24 | +780 | **-496** |
| 2025-01 -> 2025-05 | MOM h7 lb8 hold8 | +218 | +304 |
| 2025-05 -> 2025-09 | MOM h7 lb12 hold24 | +1,891 | **-224** |
| 2025-09 -> 2026-02 | MOM h12 lb8 hold16 | +1,683 | **-353** |
| 2026-02 -> 2026-06 | MOM h13 lb8 hold24 | +4,038 | +694 |

**TOTAL OUT-OF-SAMPLE: -$858 per 0.01 lot. 2 of 7 windows positive.**

Every training window looked good — they all cleared net>0, ex-best-day>0 and both-halves>0.
And the winning config **changes almost every window** (h14->h7->h9->h7->h7->h12->h13, MOM and REV
alternating). A real effect gets picked consistently. Noise gets re-picked at random.

### => RULE 1: parameter search over price history may NOT generate a hypothesis.
Passing gates on historical data is **not** evidence. My gates are necessary, not sufficient.
Search is permitted only to *size* a hypothesis that already has a stated mechanism.

### => RULE 2: a hypothesis needs a MECHANISM stated BEFORE the data is touched.
Every single thing that ever worked in this project was **mechanical**, not statistical:
multi-day short vol worked because theta is arithmetic. Every statistical pattern found by
search — CAS flow, dispersion, max-pain pin, the gold Friday cell — died.

---

## PART 2 — WHAT AN EDGE MUST CLEAR (the gates, in order of what they catch)

1. **Friction gate.** Round-trip cost as a share of the average move at the intended horizon.
   Above ~30% the idea is dead regardless of signal quality. (EURUSD 5m = 116%. Gold 60m = 5.8%.)
2. **Zero-cost bound.** Would it make money at *literally zero* transaction cost? If the gross
   edge is the same order as the toll, no execution improvement can save it.
3. **Ex-best-day.** Remove the single best day. Still positive?
4. **Half-split.** Both halves of the sample positive?
5. **Year-by-year.** *Every* year positive. This is the gate that caught the gold Friday cell
   (t=+4.12, tail-robust, and 89% of it was 2026 alone).
6. **Walk-forward.** Chosen on train only, traded blind on test. **This is the only gate that
   cannot be fooled by searching**, and it is the one my methodology just failed.

---

## PART 3 — EDGE -> MONEY (the machinery, calibrated on $3,000)

Measured inputs: round-trip cost **$0.39** per 0.01 lot (verified with a real order), mean 4h
gold move **~$9**. Sizing is **quarter-Kelly**, 250 trades/year, 4,000 simulations.

| edge | full Kelly | used (1/4 K) | median end | p10 end | median maxDD |
|---|---|---|---|---|---|
| 50% win, 1:1 | **0%** | **0%** | $3,000 | $3,000 | 0% |
| 52% win, 1:1 | **0%** | **0%** | $3,000 | $3,000 | 0% |
| 55% win, 1:1 | 5.9% | 1.5% | $3,629 | $2,724 | 17% |
| 58% win, 1:1 | 12.2% | 3.0% | $6,331 | $3,526 | 27% |
| **45% win, 2:1 payoff** | 15.7% | 3.9% | **$37,911** | $11,196 | 36% |

**Three things this proves:**

- **A 50-52% "edge" sizes to ZERO.** After the $0.39 toll it has no positive expectancy. The
  machinery refusing to trade is the machinery working.
- **Payoff ratio beats win rate, massively.** 45% at 2:1 turns $3,000 into a median $37,911;
  55% at 1:1 makes $3,629. We have been chasing the wrong variable — and recall the no-signal
  null already hands out a 71% win rate for free while losing money.
- **Quarter-Kelly is not timidity, it is survival.** Same 55% edge at FULL Kelly: median $4,752
  but p10 $1,504 and a **10% chance of losing half the account**. Quarter-Kelly: p10 $2,724,
  ruin 0%.

At a genuine 55% edge the correct size is **$44 risked per trade = 0.05 lots** — five times the
minimum, not fifty. Sizing is derived from the edge, never chosen.

---

## PART 4 — THE STANDING CONSTRAINTS

- **Instrument** admitted only by the friction table. Gold yes; FX majors arithmetically never.
- **One position at a time.** No averaging down, ever.
- **Size = quarter-Kelly of the *measured* edge.** No edge measured -> size is zero.
- **Fixed 0.01 lot for the first 30 live trades** regardless of what Kelly says — the edge
  estimate is not trustworthy until it has survived forward.
- **Daily loss limit 2%; hard stop at -10% equity.** Not advisory; enforced in code.
- **Kill condition declared before starting**, and honoured without reparameterising.
- **Never trust a scheduled job's exit code** — verify the output file's mtime advanced.
  (Three separate silent-failure incidents in this project, most recently the battery bug.)

## PART 5 — WHAT WOULD ACTUALLY BE WORTH TESTING NEXT

Not another price-pattern search — Part 1 proves that is negative-expectancy. Only hypotheses
with a mechanism you can state first:

- **Mechanical decay** — the one family that has ever paid here. Gold options / expiry effects.
- **Structural flow with a known agent** — someone forced to trade at a known time
  (index rebalancing, options expiry pinning, month-end FX fixing).
- **Asymmetric payoff structures** — per Part 3, a 45%/2:1 profile beats a 58%/1:1 profile.
  We have never once optimised for payoff ratio; we have only ever optimised for win rate.
