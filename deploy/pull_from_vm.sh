#!/usr/bin/env bash
# Run on YOUR LAPTOP. Pulls the model's recorded corpus off the VM so Claude can
# replay / factor-eval / walk-forward it locally. Usage: ./pull_from_vm.sh user@vm_ip
set -euo pipefail
HOST="${1:?usage: pull_from_vm.sh user@vm_ip}"
RROOT="institutional_trading_system_v2"
LROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "[pull] logs (trades.db, shadows, live logs) + option_history + index CSVs from $HOST"
rsync -avz --progress "$HOST:$RROOT/logs/"                 "$LROOT/logs/"
rsync -avz --progress "$HOST:$RROOT/data/option_history/"  "$LROOT/data/option_history/"
rsync -avz --progress --include='*_1m_*.csv' --exclude='*' "$HOST:$RROOT/data/" "$LROOT/data/"
echo "[pull] done — corpus is local; Claude can now replay/evaluate/build wirings on it."
