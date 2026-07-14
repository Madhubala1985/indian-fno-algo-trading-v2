#!/usr/bin/env bash
cd /home/ubuntu/institutional_trading_system_v2
[ -f "$HOME/.claude_token" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME/.claude_token")"
export PATH="$HOME/.local/bin:$PATH"
exec claude -p "$(cat scripts/night_explorer_mandate.md)" --dangerously-skip-permissions --max-turns 120
