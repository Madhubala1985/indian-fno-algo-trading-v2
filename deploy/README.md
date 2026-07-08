# Cloud deployment — hands-off market-hours run + research loop

Solves the real root cause of the 2026-07-08 loss: a laptop can't reliably be
awake/online at IST market open (05:45 CEST). A tiny always-on VM runs the model
every full session; a data loopback keeps the research/improvement loop alive.

## Architecture (prod vs research — and how they stay connected)

```
  CLOUD VM  (production: RUNS + RECORDS)          LAPTOP + Claude (research: ANALYSES + BUILDS)
  ─────────────────────────────────────          ────────────────────────────────────────────
  08:45 IST  token refresh (headless TOTP)   git push  ┌─────────────────────────────┐
  09:10 IST  model starts (auto)          <──────────  │ build/validate wirings,     │
  ~all day   records: trades.db, shadows,             │ replay, factor-eval,        │
             option_history/, live logs               │ walk-forward on the corpus  │
  15:35 IST  model stops (auto)              ────────> └─────────────────────────────┘
  crash      auto-restart (systemd)         pull_from_vm.sh (rsync the day's corpus back)
```

- **VM = production.** Runs reliably, **records everything** (chain recorder, `trades.db`,
  regime/htf/conviction/trap shadows). The more it runs, the bigger the corpus — which is
  exactly what the "make it superintelligent" work is bottlenecked on (the edge is data-bound).
- **Laptop + Claude = research.** All the deep analysis I do — `surgical_replay`,
  `walkforward_regime_validation.py`, factor-eval, trap-OOS, building/testing new wirings —
  runs on **recorded data files, not the live process.** So nothing about my analysis power is
  lost by going remote; I just need the *files*.
- **The two bridges:** `git` carries validated code changes VM←→laptop; `pull_from_vm.sh`
  carries the recorded corpus VM→laptop so I can see exactly how the live model behaved and
  keep improving it.

## Setup (once)
1. Provision a small Ubuntu 24.04 VM (Oracle Cloud always-free ARM is ideal/₹0; or a $5 droplet).
2. `git clone` this repo to `/home/ubuntu/institutional_trading_system_v2`.
3. Get your **TOTP secret**: Zerodha console → enable external TOTP → it shows a base32 seed
   (the string behind the QR). That seed goes in `KITE_TOTP_SECRET` — not a 6-digit code.
4. `bash deploy/setup.sh` → sets IST tz, venv, installs deps + systemd timers.
5. Edit `/etc/trading/secrets.env` with real credentials (`chmod 600`), then
   `sudo systemctl start trading-token.service` and check `journalctl -u trading-token -n20`.

## Daily flow (fully hands-off)
`08:45 IST` token refreshes → `09:10` model starts → runs/records all session →
`15:35` stops. Crash → auto-restart in 5s (and the OR-seed fix means a mid-session
restart rebuilds the opening range from Kite, so the chop guard stays active).

## The research/improvement loop (how I keep making it smarter)
1. On your laptop: `bash deploy/pull_from_vm.sh ubuntu@<vm_ip>` — pulls logs + `trades.db`
   + `option_history/` + index CSVs.
2. Open Claude Code. I now have the full recorded session and can:
   - evaluate the day's trades and whether the live fixes behaved (OR seed fired, halt, etc.);
   - re-run the shadow evaluations (regime walk-forward, trap OOS) on the **grown** corpus;
   - build/test the next wiring (ReversalClassifier, etc.) on real data.
3. Validated code changes → `git commit` → on the VM `git pull && sudo systemctl restart trading-model` (or it picks them up at next 09:10 start).

**So remote does NOT blind me — it feeds me.** Every reliable full session grows the exact
corpus my analysis needs; the pull brings it back; the promotion triggers
(walk-forward stable across ≥3 held-out days, trap tail at ~100+ samples) fire on that
growing evidence. Reliability (VM) and intelligence (research loop) reinforce each other.

## Monitor
- Live: `journalctl -u trading-model.service -f`
- Timers: `systemctl list-timers 'trading-*'`
- Token health: `journalctl -u trading-token.service -n 20`

## Security
Credentials live only in `/etc/trading/secrets.env` (0600, never committed). Lock the VM:
SSH-key-only, no password auth, firewall to your IP. `configs/kite_config.json` holds the
daily access_token and is **git-tracked** — do NOT commit its token changes; run
`git update-index --skip-worktree configs/kite_config.json` on each host so git ignores the
daily edits. A real `deploy/secrets.env` must never be committed (it's git-ignored below).
