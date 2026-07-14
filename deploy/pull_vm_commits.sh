#!/usr/bin/env bash
# Pull the VM night-agent's code changes back to the laptop so they can be
# committed HERE too (the two repos share files via rsync, not git history).
# Shows the VM's recent commits so the laptop commit can reference the hashes.
# Usage: ./deploy/pull_vm_commits.sh   (then review `git status` / `git diff`
# and commit on the laptop referencing the VM commit ids it prints)
set -euo pipefail
HOST="${1:-ubuntu@YOUR_VM_IP}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oracle.key}"
RSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"
RROOT="institutional_trading_system_v2"
LROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[pull-commits] VM commits (newest first):"
$RSH "$HOST" "cd $RROOT && git log --oneline -15"

echo "[pull-commits] syncing code paths back (platform_core, scripts, docs, deploy)…"
for p in platform_core scripts docs deploy; do
  rsync -az -e "$RSH" \
    --exclude='__pycache__' --exclude='*.pyc' \
    "$HOST:$RROOT/$p/" "$LROOT/$p/"
done
echo "[pull-commits] done. Review with: git status && git diff"
echo "[pull-commits] then commit on the laptop referencing the VM hashes above."
