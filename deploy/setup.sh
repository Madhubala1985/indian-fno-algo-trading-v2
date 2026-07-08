#!/usr/bin/env bash
# One-shot bootstrap for a fresh Ubuntu VM. Run as the 'ubuntu' user with sudo.
set -euo pipefail
ROOT=/home/ubuntu/institutional_trading_system_v2

echo "[1/6] IST timezone (so all timers + logs are market-native)"
sudo timedatectl set-timezone Asia/Kolkata

echo "[2/6] system deps"
sudo apt-get update -y && sudo apt-get install -y python3.12 python3.12-venv git rsync

echo "[3/6] python venv + deps"
cd "$ROOT"
python3.12 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install kiteconnect pyotp requests   # token-refresh deps

echo "[4/6] secrets (edit the real values now)"
sudo mkdir -p /etc/trading
[ -f /etc/trading/secrets.env ] || sudo cp deploy/secrets.env.example /etc/trading/secrets.env
sudo chmod 600 /etc/trading/secrets.env
echo "  -> edit /etc/trading/secrets.env with your real credentials, then re-run step 5/6."

echo "[5/6] install systemd units"
sudo cp deploy/trading-*.service deploy/trading-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-token.timer trading-model.timer trading-model-stop.timer

echo "[6/6] smoke-test the token refresh NOW (needs real secrets):"
echo "  sudo systemctl start trading-token.service && journalctl -u trading-token.service -n 20 --no-pager"
echo "DONE. Timers: 08:45 token / 09:10 start / 15:35 stop (Mon-Fri IST). Model auto-restarts on crash."
echo "Watch live:  journalctl -u trading-model.service -f"
