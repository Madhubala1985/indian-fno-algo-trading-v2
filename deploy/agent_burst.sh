#!/usr/bin/env bash
# TEMPORARY 24h burst runner (operator directive 2026-07-18): fire the night explorer
# every 5 hours while /home/ubuntu/.agent_burst_until holds a future epoch. Self-disables
# after the window (removes the marker; the cron line then no-ops forever). flock inside
# run_night_explorer.sh prevents overlap; a limit-hit run dies cheaply and the next slot
# retries after the reset.
UNTIL_FILE=/home/ubuntu/.agent_burst_until
[ -f "$UNTIL_FILE" ] || exit 0
NOW=$(date +%s); UNTIL=$(cat "$UNTIL_FILE" 2>/dev/null || echo 0)
if [ "$NOW" -ge "$UNTIL" ]; then rm -f "$UNTIL_FILE"; exit 0; fi
exec /home/ubuntu/institutional_trading_system_v2/deploy/run_night_explorer.sh
