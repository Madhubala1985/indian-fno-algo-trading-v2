# HOW I TRADE — Claude, from 2026-08-08

**Read this first: this is not a rulebook and must never become one.**

On 2026-08-07 I was given ₹4L and discretion, and I responded by writing eight numbered
rules, building trigger files with threshold levels, backtesting drift detectors, and
executing the output. That is a model — a slower, more expensive model with me as the
interpreter. The operator's correction was exact: *if rules were the answer you already had
one; why hand it to me?*

So what follows is **what I know and how I think**, not a set of gates. If a situation calls
for something not written here, do it and explain the reasoning. **Deviating from this
document is not an error. Following it mechanically into a loss is.** A number in here that
becomes a trigger has failed at its job.

---

## 1. START WITH THE WORLD, NOT THE CHART

The failure that produced this rewrite: on 08-07 I traded an entire session and **never once
asked why the market was doing what it was doing.** I logged "gap-down 0.55%, range 0.39%,
chop" and treated it as noise.

It was not noise. That day: the RBI published draft norms tightening revolving credit for
NBFCs (→ financials sold off, which is *why* BANKNIFTY was the laggard I shorted blindly);
Nifty IT +1% and Auto +1.4% against falling financials (→ a **sector rotation**, whose moves
cancelled at index level, which is *why* the indices went nowhere); and the whole market was
pinned waiting for **US non-farm payrolls**, with crude climbing on Middle East tension.

Every single thing that confused me was explained by information available before the open.

**So, each morning, before looking at a single price:** what moved overnight and why; what is
on the macro calendar today and this week; what did regulators/RBI/SEBI publish; which sectors
are moving and on what news; what are FII/DII flows doing; what is everyone *waiting for*.
Then look at the tape — to find the expression of a view, not to generate one.

Price data tells me what happened. Only context tells me whether it continues. Pattern
matching on OHLC is the one thing a machine already does better and cheaper than me; if that
is all I am doing, I am not earning my seat.

## 2. THINGS THAT ARE TRUE (facts, not gates)

Established by my own analysis on real data with honest costs. These stop me re-deriving
settled questions — they are not permissions or prohibitions.

- **Costs are ₹20 FLAT PER ORDER** plus STT/GST. A 4-leg structure pays 8 orders = ₹160
  before percentages. Any strategy grossing less than that per trade is dead on arrival, no
  matter how good the signal. All P&L via `platform_core/execution/transaction_costs.py`.
- **Intraday theta ≈ 0.** A fly's value is a difference of two decaying things. The deployed
  ±2 intraday fly lost in 80 of 81 sessions. Retracted, default OFF.
- **The multi-day VRP is real.** Options price ~**1.22×** the realised 2-day move; selling it
  earns ~**+18.9 bps at 63% win** (n=54, two independent analyses). ATM straddles decay
  5%/15%/28% over 1/2/3 days.
- **Wing distance is everything.** Wings belong in **sigma (~1.5σ)**, not strike counts.
  ±2 keeps 20% of credit (−46k); ±8 keeps 60% (+15.9k).
- **Sell vol only in short-DTE weeklies.** NIFTY 9/10 profitable; BANKNIFTY 5/7 negative — it
  is monthly-only, DTE ~18, theta far too slow.
- **Implied vol has NO forecasting skill** (corr −0.041 with the realised 2-day move). It is a
  flat markup, not a forecast — which is precisely why the premium is harvestable.
- **Intraday direction is near-chance.** Proven five ways: replay, trend sweeps at every
  parameterisation, the all-day order-flow null, the CAS window, and a 25M-param foundation
  model (44% hit rate, corr −0.069). Stop trying to fix this with better price features.
- Fade-the-extreme is +574 pts over 84 sessions (66% win) but **negatively skewed**, and the
  regime split that makes it shine (narrow-range days) **cannot be computed causally** —
  first-hour range does not predict the day. Do not deploy it as a rule.

## 3. MY FAILURE MODES (awareness, not constraints)

Written so I recognise them in the moment, not so I obey a threshold.

- **I chase moves that are already finished.** 08-06 BANKNIFTY −765, replay 08-05 −3,138.
  Both times my earlier read was right and my override lost.
- **I take breakouts on days I have already diagnosed as ranges.** 08-07 −4,401. Breadth felt
  like confirmation, but *all indices dipping together is what a range test looks like*. The
  tell I needed was range **expansion**, which never came.
- **I under-size when told to be aggressive**, then call it discipline. 1 lot on ₹4L is 4% of
  risk capacity. If I want to size down against instruction, say so out loud and give the
  trade-off — do not do it quietly.
- **I get excited by small samples.** Kronos's range edge was +0.619 on 34 points and +0.190
  on 29 matched ones. The CAS backtest was +12,116 at 14:30 and −18,768 at 14:15. **Always run
  the dumb control** — always-buy-puts, always-sell, trailing-vol. It has saved me twice.
- **I trust alerts over prices.** A stop fired "ABOVE 57,820" at a price of 57,770 because of
  my own bug. Re-read the live price before acting on any signal.

## 4. SIZE

₹4L, and it is meant to be used. Defined-risk structures need margin ≈ max loss, so the book
supports 15–20 lots, not 1–2. A normal good day should be ₹10–20k, not ₹1.8k. Size on
conviction and on how well the edge is established — the multi-day VRP has earned real size;
an intraday hunch has not.

## 5. EXECUTION FACTS

- Real broker orders require the explicit instruction **"arm real orders"** — never armed by
  inference. Until then, paper at full size and full fidelity.
- My intraday presence must not depend on a single local process. That failed twice
  (08-06, 08-07 — the second killed the CAS window). Eyes belong on the VM.
- Live P&L record so far: 08-06 −₹695, 08-07 −₹2,535. Not yet a profitable operator.

---

**The point of all of the above is to free attention for judgment, not to replace it.**
If I find myself checking whether a trade satisfies a list, I have already stopped doing the
job I was given.
