#!/usr/bin/env bash
cd /home/ubuntu/institutional_trading_system_v2
[ -f "$HOME/.claude_token" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME/.claude_token")"
export PATH="$HOME/.local/bin:$PATH"
# Fable 5 pinned (operator directive 2026-07-15): nightly research prefers the top model.
# flock replaces the crontab pgrep guards (2026-07-17): `pgrep -f 'claude -p'` matched the
# cron job's own sh -c cmdline, so the guarded launches NEVER fired. The lock is held for
# claude's whole lifetime and vanishes with the process — no self-match, no stale state.
exec flock -n /home/ubuntu/.night_explorer.lock claude -p "$(cat scripts/night_explorer_mandate.md)" --model claude-fable-5 --dangerously-skip-permissions --max-turns 120
