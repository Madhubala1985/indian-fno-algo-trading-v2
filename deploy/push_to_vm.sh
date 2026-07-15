#!/usr/bin/env bash
# Run on YOUR LAPTOP. Pushes CODE ONLY to the VM — never the VM's live state, token, or corpus.
# Ongoing code deploy (NOT first-time setup). Safe to run between sessions; a running process
# picks up changes only on its next restart (systemd timer).
#
# Usage: ./push_to_vm.sh [user@vm_ip]   (default ubuntu@YOUR_VM_IP)
#        DRY=1 ./push_to_vm.sh ...      → rsync dry-run, shows what WOULD transfer, changes nothing
set -euo pipefail
HOST="${1:-ubuntu@YOUR_VM_IP}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oracle.key}"
RSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"
RROOT="/home/ubuntu/institutional_trading_system_v2"
LROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRYFLAG=""; [ "${DRY:-0}" = "1" ] && DRYFLAG="-n"

# EXPLICIT include list — only code. NEVER push: logs/ (VM's trades.db/state/shadows/snapshots),
# data/ (VM's recorded chains), configs/kite_config.json (VM's daily token — pushing a stale one
# BREAKS auth). No --delete: we never remove files the VM has that we don't.
PATHS=(platform_core scripts requirements.txt deploy validation_reports docs CLAUDE.md)

# Pre-push gate: undefined-name scan of the live path. A NameError-class bug
# (2026-07-15: bias-invalidation referenced a variable assigned 400 lines
# later → dead on every bar, fail-open) must never reach the VM again.
if python3 -m pyflakes --version >/dev/null 2>&1; then
  _UNDEF=$(python3 -m pyflakes "$LROOT"/platform_core/data/live/*.py "$LROOT"/platform_core/intelligence/*.py 2>/dev/null | grep -c "undefined name" || true)
  if [ "${_UNDEF:-0}" -gt 0 ]; then
    echo "[push] ABORT: $_UNDEF undefined-name error(s) in live path — fix before deploying:"
    python3 -m pyflakes "$LROOT"/platform_core/data/live/*.py "$LROOT"/platform_core/intelligence/*.py 2>/dev/null | grep "undefined name"
    exit 1
  fi
  echo "[push] pyflakes gate: no undefined names in live path"
else
  echo "[push] (pyflakes not installed — skipping undefined-name gate)"
fi

echo "[push] ${DRY:+DRY-RUN }code → $HOST:$RROOT"
echo "[push] paths: ${PATHS[*]}"
echo "[push] EXCLUDING: logs/, data/, configs/kite_config.json, venv, __pycache__, *.pyc, .git"
for p in "${PATHS[@]}"; do
  [ -e "$LROOT/$p" ] || { echo "  (skip missing $p)"; continue; }
  rsync -avz $DRYFLAG --relative \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
    --exclude 'configs/kite_config.json' \
    -e "$RSH" "$LROOT/./$p" "$HOST:$RROOT/"
done
echo "[push] ${DRY:+DRY-RUN }done. Changes take effect on the model's NEXT restart."
