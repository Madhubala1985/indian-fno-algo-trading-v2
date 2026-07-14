# Improvement Batches — 2026-07-15 (night-agent mandate + repo record)

Operator directive: results must show up LIVE, not in replay. Every change is
env-reversible, fully tested, and committed on the VM; the laptop pulls the
commits back via `deploy/pull_vm_commits.sh` so both repos stay confusion-free.
Execution order = the order below (live impact first). Hard deadline for any
code that affects the live session: **08:30 IST — anything unverified by then
gets git-reverted, not "probably fine"**. The 09:10 boot must be clean.

## FROZEN TONIGHT (do not touch — consequence control)
- Execution mode / arming / kite_config / secrets.
- The rail-widening experiment values (option stop 45%, per-trade cap 3%,
  daily floor 10%, sleeve rails): the A/B is IN FLIGHT; changing mid-test
  destroys the measurement.
- `BIAS_INVAL_*` thresholds: the engine's first live day must run the exact
  validated parameters (15/15 pre-registered checks). Grade first, tune later.
- Safety rails as a class (loss caps, halts, stops, margin, freeze-qty).

## BATCH 1 — Weighted constituent-flow engine v2 (LIVE impact tomorrow)
Today's flow = top-3 stocks per index, EQUAL-weighted (≈31–55% of index
weight). Upgrade `crossasset_shadow.py`:
- Top-8 constituents per index with their real index weights (document the
  weights + as-of date in the code; static weights are fine, they drift
  slowly). NIFTY & SENSEX baskets differ (SENSEX has no free-float small
  names like some NIFTY tails); BANKNIFTY is banks-only.
- Weighted flow: `Σ(wᵢ·moveᵢ)/Σwᵢ` → becomes `avg_flow_pct` (all existing
  consumers upgrade automatically: adaptive override, NIFTY gate, TURN
  detector, bias-invalidation flow input). Keep the equal-weighted top-3
  value as `avg_flow_pct_eq` in the log for continuity.
- Add `breadth` = weighted fraction of constituents agreeing with the sign —
  a 1-stock rally is not participation. Log it; do NOT gate on it yet.
- Momentum (`mom`) recomputed on the weighted series.
- EFFICIENCY: fetch all ~24 LTPs in ONE `kite.ltp([...])` batch call per
  poll, not per-symbol loops. Verify the poll stays non-blocking.
- `CROSSASSET_WEIGHTED=0` reverts to the legacy basket. Unit test the math.

## BATCH 2 — Replay parity for the BiasInvalidationEngine (known gap)
`surgical_replay.py` does not model the engine → replay diverges from live as
of today. Integrate it: same evidence inputs (replay OR, replay BOS/CHOCH,
recorded flow from `logs/crossasset_shadow.jsonl` when the day has it).
Re-run 2026-07-13 and 2026-07-08 (NIFTY+SENSEX) and report the P&L delta vs
the engine-less replays (expected: the flip trades appear). REPLAY_BIAS_INVAL=0
knob for A/B.

## BATCH 3 — Grading harnesses (so tomorrow night has verdicts, not vibes)
- `scripts/grade_bias_invalidation.py`: every NEUTRALIZE/FLIP transition in
  `logs/bias_invalidation.jsonl` → what the market did next (15/30/60m), what
  trades resulted, was the flip right/timely/whipsawed.
- TURN-entry grader for `futures_sleeve.jsonl` tag=turn entries.
- Append to the night-explorer mandate: run `orderflow_scorecard.py` +
  `grade_bias_invalidation.py` every session with trades.
- Edge-health: persist per-roll multipliers to track the multi-roll
  persistence its own promotion bar requires.

## BATCH 4 — Research (pre-registered, report PASS/FAIL with n + ex-best-day)
- Strike-level writer-OI walls v2 (mining finding B refinement): walls +
  OI velocity + move-size weighting on the recorded per-minute chains.
- Gap-day playbook: across the 45d corpus — overnight gap size/direction vs
  trend-day probability and OR-break follow-through (the 07-13 pattern,
  formalized into a pre-open input candidate).
- Theta-hold study: holding-window P&L net of theta drag from recorded
  chains (the ₹20.6k theta toll — when does holding stop paying?).

## BATCH 5 — Verify + commit discipline (by 08:30 IST, non-negotiable)
- Battery per batch BEFORE its commit: py_compile everything touched +
  scripts/test_bias_invalidation.py + test_per_trade_loss_cap.py +
  test_gate_profiles.py + test_supervised_live_session.py +
  test_live_decision_safety.py + system_smoke_test.py.
- One commit per batch, clear message, working tree CLEAN at the end.
- Confirm timers (`systemctl list-timers`) still show 09:10 model boot.
- Write `logs/night_batch_20260715_report.md`: per batch — done/partial/
  reverted, evidence, and what tomorrow's session should show if it worked.

## Sync-back (laptop side, next session)
`deploy/pull_vm_commits.sh` rsyncs the VM's code changes back and shows the
VM git log; the laptop then commits them here referencing the VM hashes.
