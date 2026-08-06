# Claude live paper-trading plan — Friday 2026-08-07

Written 00:40 IST from the 11-day causal replay block (net −₹172). Rules below are the ones
that block earned, not preferences.

## Carry-in
- No open paper positions. 08-06 closed net −₹695 (SENSEX fly +70, BANKNIFTY chase −765).
- SENSEX weekly expiry is **2026-08-13** → Friday is DTE 6. NIFTY/BANKNIFTY per their cycle.

## THE ONE RULE THAT MATTERS TODAY
**Trade the open, then stop.** My mid-session overrides of my own correct stand-aside are
0-for-2 for −₹3,903 (replay 08-05 −3,138, live 08-06 −765). If I call stand-aside at 09:35,
that call stands for the session unless a *new* structural event appears (a clean range break
with follow-through) — not merely "it made a new high".

## Decision at 09:35 IST — exactly one of three
1. **Open drift present** (net move ≥0.3% with the path making persistent higher-lows or
   lower-highs, still moving at 09:35): buy the ATM option on that side, 1 lot.
   - Exit on **momentum break** — ~30min of no new extreme while off the peak — not on a clock.
   - Hard stop at the pre-stated invalidation level. This bucket went 4/5, +₹3,481.
2. **Drive done and now pinning** (move finished before 09:35, then flat): stand aside on
   direction. Only sell vol if **DTE ≤ 3** — see the fee rule below.
3. **Flat/chop** (range <0.3%, no drift): **stand aside.** 07-29/07-30 confirm this is correct
   and free.

## Fee rule (arithmetic, not opinion)
An intraday 4-leg iron fly at 1 lot costs ~₹226 round-trip and my intraday flies grossed only
₹143–271. **Do not open an intraday fly at 1 lot.** A fly is only worth it if held to close
(the validated form) at DTE ≤ 3 where the credit is actually harvested. Friday is DTE 6 for
SENSEX → **no fly today** unless I hold it into next week, which I won't intraday.
→ Practically: today is a direction-or-nothing day.

## Do NOT
- Chase a move that is already done (08-06 BANKNIFTY, replay 07-27/08-03 style pins).
- Buy premium into a VIX-12 flat tape.
- Re-enter after a stop. One directional loss = done for the day.
- Sell a naked straddle. Ever. (07-08, −13,090.)

## Symmetric trigger
If I arm a conditional level, arm it **both ways**. On 07-31 I armed only a downside break off a
failed-breakout narrative and was blind to the persistent up-drift that actually ran all day.

## Credit discipline
No background loop. ScheduleWakeup clamps at 1 hour, so it cannot bridge overnight to the open —
**the operator starts me at ~09:10 IST**, and from there I self-pace to at most:
09:35 open read → 11:00 manage → 13:30 manage → 15:20 close-out/grade.
Log every call to `data/claude_trades/claude_trades.jsonl`; grade the day at the close.
