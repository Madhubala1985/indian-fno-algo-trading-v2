# Real-Armed Runbook — first armed week (target: week of 2026-07-06)

The decision path is identical across paper / real_dry_run / real_armed (only the
order router differs). What arming changes is the blast radius — so this runbook is
about the operational chain, not the model.

## Go / no-go checklist (all must be green before arming)

| # | Check | How | Status 2026-07-05 |
|---|-------|-----|-------------------|
| 1 | Readiness gate 11/11 | `PYTHONPATH=. python scripts/real_funds_readiness_check.py` (with interlocks sourced) | 7/11 — remaining 4 are arm-time env + git-clean |
| 2 | Cap interlocks ENFORCED | `validate_real_order_caps` + pipeline rupee-cap gate (added 2026-07-05; were decorative before) | DONE, 9 unit cases + 59-test suite pass |
| 3 | Real-fund risk profile | Supervisor pins real_armed to 2% risk (net-validated level; 4% is paper-only) | DONE |
| 4 | Fresh OOS validation days | Mon: backfill 07-04 chains + index, dry-run Mon (+Tue if Mon is red), batch must be NET-positive incl. new days | PENDING — the actual gate |
| 5 | Capital funded & matched | Broker account funded; MAX_REAL_CAPITAL = funded amount; paper state equity basis sane vs it | PENDING (user) |
| 6 | Token fresh | Generate 08:30–09:10 IST the same morning (pre-dawn tokens get invalidated) | Daily |
| 7 | Operator present | First armed sessions are supervised end-to-end; dashboard open | PENDING (user) |

## Arm procedure (each armed morning)

1. 08:30–09:10 IST: `python scripts/generate_kite_session.py`
2. `set -a; source <private interlocks env>; set +a` (from
   `configs/real_armed_interlocks.env.example`; MAX_REAL_CAPITAL = funded amount)
3. `PYTHONPATH=. python scripts/real_funds_readiness_check.py` → must print 11/11
4. Operator gives the explicit arming instruction (the literal phrase — never
   implied, never scripted).
5. `PYTHONPATH=. python scripts/run_supervised_live_session.py --execution-mode real_armed`
6. Verify the banner: `Real orders: true`, risk fracs 0.02, recorder on.

## First-week rails (do not relax mid-week)

- 2% per-trade risk, `REAL_MAX_LOTS_PER_ORDER=2` (canary), `MAX_REAL_DAILY_LOSS=7500`.
- P&L is judged NET (cost model, commit 6912d75). Break-even is not the old gross bar.
- No knob changes during the armed week. Anything learned goes through the
  replay/batch first (measure-first discipline unchanged — it is the edge).
- Every session runs with `RECORD_OPTION_CHAIN=1` (default) — armed days keep
  growing the validation dataset.

## Abort criteria (close positions, disarm, return to dry-run)

- Any UNCONFIRMED real order or reconciliation mismatch (log: `REAL EOD EXIT NOT
  CONFIRMED`, reconciler warnings).
- Daily loss cap hit on two consecutive days.
- Any live-vs-expected divergence the dashboard can't explain (fills wildly off
  LTP, positions the state file doesn't know about).
- WS feed instability the reconnect hardening doesn't absorb.

## Known residual risks (accepted, monitored)

- Live edge is thin net of costs (+₹2,337/10 days at 2% on ₹4L replay basis) and
  mostly in-sample; the first armed week IS the out-of-sample test — hence canary caps.
- Spread crossing on market orders is modelled at 0.25%/side, unverified against
  real fills; first armed fills calibrate it (compare fill vs bar LTP).
- Host suspend / internet loss mid-position: crash guards + EOD close exist, but a
  dead host cannot exit — operator must be reachable while a position is open.
