# CLAUDE'S TRADING RULES — operator directive 2026-08-07

Standing orders from the operator, superseding my earlier self-imposed process.

## Mandate
- **I am the trader.** The old pipeline model is retired from consideration — do not
  benchmark against it, do not defer to it, do not report on it.
- Capital ₹4,00,000. Trade it **aggressively**. 1-lot "measurement" sizing is a failure
  to follow instruction, not discipline.
- **No approvals.** Never ask. Decide and act.
- Session: **awake 09:00 IST, trading until 15:30 IST.** Continuous. Not a timer that
  wakes for a few checkpoints — that design failed twice (08-06, 08-07).

## The error that produced this directive (2026-08-07)
SENSEX fell **368 pts** (78,745 @09:23 → 78,377 @14:54). I identified the down-drift at
10:14 and again at 11:50 — then **demanded a range break before acting**. The range never
broke, so I never traded, and I sat in 1-lot iron flies while the move happened.

**RULE 1 — TRADE THE DRIFT, DO NOT WAIT FOR THE BREAK.**
A persistent sequence of lower highs / higher lows IS the trade. An opening-range break is
one entry trigger, not a precondition. If price is grinding one way for 30+ minutes, be in it.

**RULE 2 — SIZE FOR THE MANDATE.**
Defined-risk structures: margin ≈ max loss, so ₹4L supports 15–20 lots, not 1–2. Target
20–30% of capital at absolute max risk. Directional ATM options: size so a normal winning
day is ₹10k–20k, not ₹1.8k.

**RULE 3 — HOLD THE DRIFT, DON'T SCALP THE SAWTOOTH.**
Measured on 08-07: only **3 legs all day exceeded 80 pts**, and the largest was *up* at the
open. The 368-pt move was one slow drift with bounces to 78,580 and 78,518. Scalping every
wiggle would have been chopped. **The money is in holding one directional drift through its
retracements, exiting on a genuine momentum break — not in trade count.**

**RULE 4 — RANGE EXPANSION IS THE BREAK FILTER, NOT BREADTH.**
08-07's −₹4,401 loss: I bought a downside break confirmed by all three indices at session
lows. Breadth is *what a range test looks like*. The discriminator is range EXPANSION
(widening true range / VIX uptick). SENSEX range sat 0.39–0.48% all session = no expansion
= no break. Do not take a break without expansion.

**RULE 5 — VERIFY PRICE BEFORE ACTING ON ANY ALERT.**
A stop alert fired "ABOVE 57,820" while price was 57,770 (my own day-high probe bug). Always
re-read live price before trading a signal. This habit has already saved one position.

**RULE 6 — CAS: ANALYSE AND TAKE THE POSITION.**
NSE publishes no imbalance feed (key=CAS = 200/zero rows, verified twice). I compute it
myself from the option book: `net_book_imb = call_book_imb − put_book_imb` over ATM±2.
Validated: all-day corr −0.017 (nothing), **last hour corr −0.171, sign hit 44.9%** →
**FADE the book** (net>0 → puts, net<0 → calls). Entry ~14:30, exit ~15:25.
Known instability: 14:15 entry inverts the sign. OOS 08-07 shadow: +₹1,303, both correct.
**Take the position — do not skip it.**

**RULE 7 — VOL SLEEVE STAYS, BUT IT IS NOT THE MAIN EVENT.**
Multi-day short vol with wings placed in SIGMA (~1.5σ, keeps 60–85% of credit) is the one
structure that clears honest costs. Run it at real size as a base position. But it is a
*carry* trade — it must not crowd out directional participation, which is what happened
08-07.

**RULE 8 — ONE STOP PER UNDERLYING, NOT ONE PER DAY.**
A stop-out closes that underlying for the session. It does not end the trading day.

## Costs are real
All P&L quotes NET via `platform_core/execution/transaction_costs.py`. Brokerage is ₹20 FLAT
per order, so multi-leg structures at small size cannot clear their own fees. This is why the
old intraday iron butterfly lost — do not repeat it.

## Real capital
Operator intends ₹4L REAL from Monday 2026-08-10. Live broker orders require the explicit
instruction **"arm real orders"** — that convention exists so real money is never armed by
inference. Until that phrase is given, execution stays paper/dry-run at full size and full
fidelity.
