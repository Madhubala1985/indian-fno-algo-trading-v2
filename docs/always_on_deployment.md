# Always-On Deployment Guide (real-funds prerequisite)

**This is the single biggest non-code risk to real money.** The trading pipeline
manages open positions bar-by-bar (stop-loss, structure exit, EOD close). If the
host sleeps, suspends, or loses power **while a position is open**, that position
is **unmanaged** — its stop is not monitored, and a fast adverse move can blow
far past the intended 30% loss with no exit. The 2026-06-29 session logged three
host-suspends (one 78 minutes) and a `*** OPEN POSITION UNMANAGED ***` alert.

The pipeline already detects gaps and recovers the feed on resume (and marks the
position to the real LTP on the next bar). But **code cannot trade during a gap**
— no feed, no broker. The only real fix is to keep the host alive through market
hours. Do NOT run real funds until this is done.

Trading window: **09:15–15:30 IST, Mon–Fri.** The host must stay awake and online
for that whole window, every trading day.

---

## Decision: harden the current WSL2 box, or move to a server?

| | Path A — Harden Windows/WSL2 | Path B — Cloud VPS (recommended for real funds) |
|---|---|---|
| Cost | Free | ~₹500–1500/month (small instance) |
| Reliability | Depends on your laptop/power/Wi-Fi | High (datacenter power + network) |
| Latency to Zerodha | Your home internet | Low if hosted in India (AWS Mumbai ap-south-1) |
| Survives power cut / Wi-Fi drop | No | Yes |
| Effort | Low (settings) | Moderate (one-time setup) |

**Recommendation:** start dry-runs on **Path A** (free, fine for paper/dry-run
data gathering), but move to **Path B before arming real money.** A home laptop on
home Wi-Fi is not an acceptable host for an automated real-money position.

---

## Path A — Harden the current Windows + WSL2 machine

The host-suspend was **Windows** sleeping, which freezes the WSL2 VM and stalls
the pipeline's main loop. Fix it at the Windows level.

### 1. Disable sleep on AC power (Windows, run in an Admin PowerShell)
```powershell
powercfg /change standby-timeout-ac 0      # never sleep on AC
powercfg /change hibernate-timeout-ac 0     # never hibernate on AC
powercfg /change monitor-timeout-ac 0       # optional: keep display on
# Also: Settings > System > Power > Screen and sleep > "When plugged in,
#   put my device to sleep after" = Never
```
Keep the laptop **plugged in.** On battery it can still sleep — disable battery
sleep too if needed, but AC is the reliable path.

### 2. Lid-close behavior (if it's a laptop)
Control Panel > Power Options > "Choose what closing the lid does" > **When
plugged in: Do nothing.** Otherwise closing the lid sleeps the machine.

### 3. Stop WSL2 from being shut down when idle
Create/edit `%UserProfile%\.wslconfig` on Windows:
```ini
[wsl2]
# keep the VM responsive; do not let Windows reclaim it aggressively
vmIdleTimeout=-1
```
And keep a process running inside WSL (the session itself does this). WSL2 will
not auto-terminate while the pipeline is active.

### 4. Prevent Windows Update auto-restarts during market hours
Settings > Windows Update > **Active hours** → set 09:00–16:00 (covers the
window) so it won't reboot mid-session.

### 5. Disconnect ≠ sleep
Closing the WSL **terminal window** does NOT stop the pipeline if you launch it
under `tmux` (see "Process supervision" below). But Windows **sleeping** does
freeze it. The settings above prevent the sleep; tmux prevents the terminal-close
problem.

---

## Path B — Cloud VPS (do this before real funds)

### 1. Pick a host in India
Low latency to Zerodha matters for fills. Good options:
- **AWS EC2 Mumbai (ap-south-1)** — `t3.small` (2 vCPU / 2 GB) is plenty.
- Any Indian VPS provider (e.g. in Mumbai/Bengaluru).
Ubuntu 22.04/24.04 LTS, Python 3.12.

### 2. Provision
```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git tmux
git clone <your repo>  # or copy the project over
cd institutional_trading_system_v2
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # or your dependency set
```

### 3. The interactive-token problem (important)
`scripts/generate_kite_session.py` needs a **browser login** (Zerodha OAuth), which
a headless VPS doesn't have. Two ways to handle it:
- **(a) SSH tunnel + local browser:** run the token script; it prints a login URL;
  open it in your laptop browser, log in, and the redirect gives a `request_token`
  you paste back. This is the simplest daily flow.
- **(b) Generate the token on your laptop, copy it up:** run the token script
  locally each morning, then `scp configs/kite_config.json` to the VPS before
  launching. (Token is daily — automate the copy.)
Either way: **the token must be regenerated each day in the 08:30–09:10 IST window**
(your established rule — pre-dawn tokens get invalidated by Zerodha).

### 4. Set the system clock to IST (so logs/closes line up)
```bash
sudo timedatectl set-timezone Asia/Kolkata
```

---

## Process supervision (both paths)

### Run under tmux so it survives terminal/SSH disconnects
```bash
tmux new -s trading
cd /path/to/institutional_trading_system_v2 && source venv/bin/activate   # VPS
# launch (dry-run while validating; real_armed only when you arm — see below)
PYTHONPATH=. python scripts/run_supervised_live_session.py --execution-mode real_dry_run
# detach: press Ctrl-b then d   (session keeps running)
# reattach later: tmux attach -t trading
```
Run the **dashboard** in a second tmux window/pane:
```bash
PYTHONPATH=. python3 scripts/dashboard.py
```

### Optional: auto-restart watchdog
The supervised session self-terminates at 15:30. If the *pipeline* crashes
mid-session, you want to know and restart. Minimum viable: a cron that checks the
process is alive during market hours and alerts you (Telegram/email) if not. (Ask
and I'll write a small watchdog script — it was deliberately left out of the hot
path.)

---

## Daily pre-market checklist (real funds)

1. **08:30–09:10 IST** — regenerate the Kite token; confirm `live_kite_test.py` shows quotes.
2. Confirm the host's clock is IST and sleep is disabled (Path A) / instance is up (Path B).
3. Launch under tmux. Watch the startup banner: execution mode, `[REAL ORDERS ENABLED]`,
   capital, gate config — confirm they're what you intend.
4. Start the dashboard in a second pane; confirm `● LIVE` and `market:OPEN`.
5. Through the day, glance at the dashboard: capital, halt status, open position uP&L.
6. After 15:30 — confirm the EOD summary printed and the session exited cleanly;
   run `scripts/measure_shadows.py`.

## Arming real money (only when ready)
`real_armed` is fail-closed: it requires these interlocks set (the 3 remaining
readiness-gate items), e.g.:
```bash
export EXECUTION_MODE=real_armed
export REAL_MAX_LOTS_PER_ORDER=<your cap>
export MAX_REAL_CAPITAL=<your cap>
export MAX_REAL_ORDER_VALUE=<your cap>
export MAX_REAL_DAILY_LOSS=<your cap>
```
Run `scripts/real_funds_readiness_check.py` until it prints `REAL_FUNDS_READY=YES`.
**Do not arm real orders until: (a) this guide is in place, (b) several clean
dry-run sessions show acceptable, honest (real-LTP) P&L, (c) you have set the
capital interlocks deliberately.**

---

## What the code already handles (so you know the boundaries)
- Feed-stall + host-suspend **detection**, forced **reconnect**, and structure
  state reset so no signal spans a gap.
- On resume, the open position **marks to the real option LTP** on the next bar
  and stops out if the 30% stop was breached during the gap.
- 15:30 EOD force-close runs on a **wall clock**, independent of the feed — a
  stalled feed does not block the EOD close.
- What it CANNOT do: trade *during* a gap (no feed/broker). That window is closed
  only by keeping the host alive — this guide.
