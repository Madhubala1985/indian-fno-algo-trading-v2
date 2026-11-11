# Oracle Cloud (Mumbai, free ARM) — step-by-step deploy

Beginner walkthrough. You run some commands on **YOUR LAPTOP (WSL)** and some
**ON THE VM (after SSH)**. Each block says which. Replace ALL-CAPS placeholders.

Recommended VM image: **Ubuntu 24.04** (ships Python 3.12). Shape: Ampere A1 (ARM, always-free).
The model only makes OUTBOUND connections to Zerodha, so you do NOT need to open any
inbound ports — only SSH (port 22), which Oracle opens by default. No firewall edits needed.

---
## STEP 0 — Get your Zerodha secrets ready (do this first)
You need FIVE values:
1. `api_key`, `api_secret` — from https://kite.trade (Kite Connect app). Set the app's
   **redirect URL** to anything, e.g. `https://127.0.0.1` (the script never needs it to load).
2. `user_id` — your Zerodha login ID (e.g. AB1234).
3. `password` — your Zerodha login password.
4. `totp_secret` — Zerodha console → Settings → enable **external/authenticator TOTP**.
   When it shows the QR, click "can't scan / show key" — that **base32 string** is your
   `totp_secret` (NOT a 6-digit code). Save it.

Keep these five in a note; you'll paste them into the VM in Step 4.

---
## STEP 1 — (LAPTOP) find your VM IP + SSH key, then connect
- In the Oracle console: Compute → Instances → your instance → copy the **Public IP address**.
- Find the private key Oracle gave you when you created the instance (a `.key` file, often in
  `~/Downloads/`). Move it somewhere safe and lock permissions:

```bash
mkdir -p ~/.ssh && mv ~/Downloads/ssh-key-*.key ~/.ssh/oracle.key
chmod 600 ~/.ssh/oracle.key
```
- Connect (Ubuntu images use the `ubuntu` user):
```bash
ssh -i ~/.ssh/oracle.key ubuntu@YOUR_VM_IP
```
If it logs in, you're on the VM. Type `exit` to come back to the laptop for Step 2.

---
## STEP 2 — (LAPTOP) copy the code to the VM
From your laptop (not SSH'd in), push the project up (skips venv/caches/big CSVs — those
are rebuilt on the VM):
```bash
rsync -avz --exclude venv --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'data/*.csv' --exclude 'logs/*.log' \
  -e "ssh -i ~/.ssh/oracle.key" \
  ~/nse-market-intelligence-v2/ \
  ubuntu@YOUR_VM_IP:/home/ubuntu/institutional_trading_system_v2/
```

---
## STEP 3 — (VM) run the bootstrap
SSH back in, then:
```bash
ssh -i ~/.ssh/oracle.key ubuntu@YOUR_VM_IP           # from laptop
cd /home/ubuntu/institutional_trading_system_v2
sudo apt-get update -y && sudo apt-get install -y build-essential python3.12-dev
bash deploy/setup.sh
```
`setup.sh` sets the VM clock to IST, builds the Python env, installs deps, and installs the
timers. If it complains Python 3.12 is missing, your image isn't 24.04 — tell me and I'll
give the 3.11+ fallback.

---
## STEP 4 — (VM) enter your secrets
```bash
sudo nano /etc/trading/secrets.env
```
Fill in the five values from Step 0 (leave `KITE_CONFIG` as-is). Save: `Ctrl+O`, `Enter`,
`Ctrl+X`. Then lock it:
```bash
sudo chmod 600 /etc/trading/secrets.env
```

---
## STEP 5 — (VM) test the token, then a manual model run
```bash
# 1) token refresh (must succeed — proves your 5 secrets are right):
sudo systemctl start trading-token.service
journalctl -u trading-token.service -n 20 --no-pager      # expect: [TOKEN] OK ...

# 2) one manual model run (Ctrl+C to stop after you see it connect):
cd /home/ubuntu/institutional_trading_system_v2
git update-index --skip-worktree configs/kite_config.json  # stop git tracking the daily token
set -a; source /etc/trading/secrets.env; set +a
PYTHONPATH=. PYTHONUNBUFFERED=1 venv/bin/python scripts/run_supervised_live_session.py --execution-mode real_dry_run
```
Look for `Token valid`, `WebSocket connected`, `LIVE STRUCTURE PIPELINE ACTIVE`. Then `Ctrl+C`.

---
## STEP 6 — (VM) turn on the automation
```bash
sudo systemctl enable --now trading-token.timer trading-model.timer trading-model-stop.timer
systemctl list-timers 'trading-*'      # confirm next-run times (IST)
```
That's it. From now on, every trading day (Mon–Fri IST): **08:45** token refresh → **09:10**
model starts → runs & records all session → **15:35** stops. Crashes auto-restart in 5s.

Watch it live any time:
```bash
journalctl -u trading-model.service -f
```

---
## STEP 7 — (LAPTOP) pull the data back for analysis (whenever you want me to look)
```bash
cd ~/nse-market-intelligence-v2
# edit deploy/pull_from_vm.sh once if your key path differs, then:
rsync -avz -e "ssh -i ~/.ssh/oracle.key" \
  ubuntu@YOUR_VM_IP:/home/ubuntu/institutional_trading_system_v2/logs/ logs/
rsync -avz -e "ssh -i ~/.ssh/oracle.key" \
  ubuntu@YOUR_VM_IP:/home/ubuntu/institutional_trading_system_v2/data/option_history/ data/option_history/
```
Then open Claude Code — I'll have the full recorded session to evaluate, replay, and build
the next wiring on.

---
## Updating the model later (after I validate a new wiring)
On the laptop, re-run the Step-2 rsync. On the VM: `sudo systemctl restart trading-model`
(or it picks it up at the next 09:10 start). Nothing else changes.
