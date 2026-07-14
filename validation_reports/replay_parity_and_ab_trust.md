# Replay Parity & A/B-Trust Ledger

**Standing check.** Run `PYTHONPATH=.:scripts python3 scripts/replay_parity_test.py` to refresh
`validation_reports/replay_parity_ledger.json`. Any conclusion drawn from `SurgicalReplay`
must first confirm its days are **FAITHFUL** in that ledger. As of 2026-07-11 the corpus-wide
score is **1/11 days faithful** — so the replay is currently **not** a trustworthy P&L or
direction oracle, and replay-only A/B verdicts inherit that risk.

## 1. Verdict

| metric | value |
|---|---|
| Parity (count ∧ directions ∧ P&L-sign match) | **1 / 11 days** |
| Sign flips | 5 days |
| Replay took 0 trades where live traded | 4 days |
| Monster day (06-29 SENSEX) | **inverted** |
| Aggregate live vs replay P&L | +₹13,294 vs +₹14,402 (coincidentally close; day-level uncorrelated) |

The near-equal aggregate is a **coincidence of cancellation**, not agreement — day by day the two
disagree on direction, count, and sign. Aggregate P&L parity is meaningless here.

## 2. Root cause — MCB warm-start state divergence (structural, not a bug)

06-29 SENSEX forensic:
- **LIVE:** MCB `bias=SHORT conf=1.00` from `live_bars=15` onward (847 "short" refs, 0 "long" in
  the run log); 3 afternoon PUT entries → **+₹44,102**, riding the US–Iran/Hormuz selloff.
- **REPLAY:** `bias=LONG`, 1 entry at the 09:15 open, out in 9 min → **−₹2,348**.

Why the bias inverts:
- The MCB bias is **60% higher-timeframe** (`1d`=0.35, `4h`=0.25 weight). The **live** pipeline
  seeds those from **persisted intraday state accumulated across prior live sessions**
  (`[MCB WARMSTART] … state has N 15m bars`). `SurgicalReplay.warm_start()` can only reconstruct
  warm-start from `data/*.csv`, which is **not the same state**.
- Different higher-TF seed → different starting bias → the session's own bars don't overturn it →
  the whole day's **direction** differs. This is the same mechanism as the known bias-staleness
  finding (4h/1d out-vote the intraday reversal).

**Fixability: structural.** The replay cannot match live direction unless the live MCB state is
**snapshotted daily** and the replay is started from that snapshot. Until then, treat
`SurgicalReplay` as an **exit/gate mechanics tester at best, never a direction/bias/P&L oracle.**

**Recommended fix (not yet wired — hot-path-adjacent, needs go-ahead):** persist a per-session MCB
warm-start snapshot (the exact `bar_history` + higher-TF state used at boot) so replays can
`--warmstart-from-state <snapshot>` and become faithful going forward. This only helps *future*
sessions; historical replay parity is unrecoverable.

## 3. A/B-Trust Ledger — which conclusions are at risk

Because 10/11 overlap days are **divergent**, any verdict whose evidence came from `SurgicalReplay`
(or the older replay/backtest harness) is **SUSPECT** and needs re-validation against **live**
trades (`logs/trades.db pt_trades`) or real live sessions. Verdicts independently confirmed on live
data **stand**.

### SUSPECT — replay/backtest-derived, re-validate on live
| conclusion | memory | why at risk |
|---|---|---|
| Conviction ~0.60 floor edge | project_conviction_floor_edge | "full-day replay"; already flip-flopped once |
| Strike-rotation-after-loss +20% | project_strike_rotation_trailing | replay days; entry-set-dependent |
| Pre-DTE margin fix REJECT | project_sensex_iv_attr_blocker | "A/B'd on 16 replay days" |
| Trailing-stop REJECT | project_strike_rotation_trailing, breakeven_ratchet | replay/backtest; exit knob on divergent trade set |
| OTM-strike skew REJECT | project_otm_skew_rejected | 25 trades, replay-priced |
| 8% vs 2% profile net-negative | project_session_20260706 | replay/forensics |
| Factor/regime separation | project_factor_eval | "vs 30 trades" replay |
| TrapProbability OOS tail-filter | project_ml_layer_wiring | replay corpus |
| Open-drive window edge | project_session_20260706 | forensics; dry-run OOS was the intended judge |
| Straddle reframe (this session) | project_straddle_reframe_study | already corrected via live re-anchor |

### TRUST — validated on live sessions / live trade logs
| conclusion | memory |
|---|---|
| Cost model (NET everywhere) | project_cost_blindness |
| OR late-start seed fix | project_or_late_start_seed |
| Breakeven ratchet (real winner/loser peaks) | project_breakeven_ratchet |
| Halt config (real days) | project_halt_config |
| MCB gap fix | project_mcb_gap_fix |
| SENSEX priority routing (live cumulative) | project_sensex_priority_routing |
| Re-entry risk haircut (wired, live) | project_session_20260706 |
| Crash resilience / feed hardening | project_crash_resilience |

**Caveat on "SUSPECT":** it means *unproven*, not *wrong*. Relative A/B deltas can survive if the
knob's effect is common-mode across both arms. The correct next step is per-knob re-validation:
re-run each A/B through the replay, then check (via the ledger) whether the days that DRIVE its
delta are faithful — if the edge lives on divergent days, the verdict is void.

## 4. How to use this going forward
1. Refresh the ledger (`scripts/replay_parity_test.py`).
2. For any replay A/B, intersect its driving days with the ledger's FAITHFUL set.
3. If the edge depends on non-faithful days → **do not promote**; re-measure on live.
4. Fastest structural cure: daily MCB-state snapshots + `--warmstart-from-state` replay
   (proposed, unwired).
