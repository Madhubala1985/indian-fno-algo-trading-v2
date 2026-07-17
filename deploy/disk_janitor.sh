#!/usr/bin/env bash
# disk_janitor.sh — recurring safe cleanup on the VM (cron 07:00 IST daily).
# PROTECTED, never touched: logs/trades.db*, logs/*state*.json, logs/*.jsonl shadows,
# logs/night_explorer_*.md reports, mcb_snapshots/, data/ (option_history, source, CSVs),
# configs/. Only derived/regenerable artifacts are removed; old live logs are COMPRESSED
# (kept), not deleted, and only their gzipped form expires after 60 days.
set -uo pipefail
cd /home/ubuntu/institutional_trading_system_v2 || exit 1

# 1. compress full-session live logs older than 3 days (10x smaller, still readable)
find logs -maxdepth 1 -name "live_run_*.log" -mtime +3 -exec gzip -f {} \; 2>/dev/null
# 2. expire compressed live logs after 60 days
find logs -maxdepth 1 -name "live_run_*.log.gz" -mtime +60 -delete 2>/dev/null
# 3. caches and bytecode (always regenerable)
rm -rf /home/ubuntu/.cache/pip 2>/dev/null
find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
# 4. cap the systemd journal
sudo journalctl --vacuum-size=200M >/dev/null 2>&1
# 5. disk-pressure alert the morning health check can surface
USE=$(df --output=pcent / | tail -1 | tr -dc "0-9")
if [ "${USE:-0}" -ge 85 ]; then
    echo "$(date -Is) DISK_PRESSURE ${USE}%" >> logs/disk_alerts.log
fi
echo "$(date -Is) janitor done, disk ${USE:-?}%"
