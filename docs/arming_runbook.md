# Real-Armed Runbook — first armed week (target: week of 2026-07-06)

The decision path is identical across paper / real_dry_run / real_armed (only the
order router differs). What arming changes is the blast radius — so this runbook is
about the operational chain, not the model.

## Go / no-go checklist (all must be green before arming)

| # | Check | How | Status 2026-07-05 |
|---|-------|-----|-------------------|
| 1 | Readiness gate 11/11 | `PYTHONPATH=. python scripts/real_funds_readiness_check.py` (with interlocks sourced) | 7/11 — remaining 4 are arm-time env + git-clean |
| 2 | Cap interlocks ENFORCED | `validate_real_order_caps` + pipeline rupee-cap gate (added 2026-07-05; were decorative before) | DONE, 9 unit cases + 59-test suite pass |
| 3 | Profile parity | real_armed runs the EXACT paper/dry-run profile (8% risk, %-of-capital, same gates incl. SENSEX margin 0.015) — parity directive 2026-07-05; interlocks are backstops only | DONE |
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
5. **First armed day only**: launch with `--capital <funded amount>` (e.g.
   `--capital 400000`). The paper state's compounded equity (₹427,997) includes
   void pre-06-30 BSM-fiction gains and must NOT become the real sizing basis;
   the explicit capital archives the paper state (preserved in archive/) and
   starts the real ledger at the actual funded amount. From day 2 onward launch
   WITHOUT `--capital` — compounding then rolls the REAL ledger forward.
   `PYTHONPATH=. python scripts/run_supervised_live_session.py --execution-mode real_armed --capital 400000`
6. Verify the banner: `Real orders: true`, risk fracs 0.08 (same as dry-run),
   recorder on, `starting_capital` = the funded amount.

## First-week rails (do not relax mid-week)

- Sizing/gates are IDENTICAL to the validated dry-run profile — what was observed
  live is what trades armed. The interlocks (`MAX_REAL_DAILY_LOSS` = the 15% daily
  halt in rupees, `MAX_REAL_ORDER_VALUE` = the 35% premium cap in rupees
  (₹1.25–1.5L deployment, 2026-07-06 directive; a single 30% stop on a full
  position ≈ ₹42k ≈ 10.5% of capital — two stops ≈ the daily halt),
  `REAL_MAX_LOTS_PER_ORDER` = freeze-qty scale) are backstops against bugs and
  must never bind on a normal trade — if one fires, treat it as an incident.
- Expected worst normal day at this profile ≈ the 15% daily halt (₹60k on ₹4L).
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

- Live edge is thin net of costs (+₹2,337/10 days at 2% replay basis; the armed 8%
  profile scales both wins AND losses ~4x) and mostly in-sample; the first armed
  week IS the out-of-sample test, at full size by explicit user decision (parity
  directive 2026-07-05).
- Spread crossing on market orders is modelled at 0.25%/side, unverified against
  real fills; first armed fills calibrate it (compare fill vs bar LTP).
- Host suspend / internet loss mid-position: crash guards + EOD close exist, but a
  dead host cannot exit — operator must be reachable while a position is open.
