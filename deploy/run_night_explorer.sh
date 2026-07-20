#!/usr/bin/env bash
cd /home/ubuntu/institutional_trading_system_v2
[ -f "$HOME/.claude_token" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME/.claude_token")"
export PATH="$HOME/.local/bin:$PATH"
# Model (operator directive 2026-07-20): Opus 4.8, switched off Fable 5. The 07-20
# 19:30 launch died instantly on "You've reached your Fable 5 limit" and produced no
# report — that limit is MODEL-SCOPED, so Opus 4.8 draws on a separate pool. It is the
# most capable Opus-tier model at half Fable's token cost ($5/$25 vs $10/$50).
# Supersedes the 2026-07-15 "prefers the top model" pin.
# flock replaces the crontab pgrep guards (2026-07-17): `pgrep -f 'claude -p'` matched the
# cron job's own sh -c cmdline, so the guarded launches NEVER fired. The lock is held for
# claude's whole lifetime and vanishes with the process — no self-match, no stale state.
# nice/ionice (2026-07-17): research must never starve sshd or the live pipeline —
# the Thu 07-16 wedge happened while an agent run was the only workload.
exec flock -n /home/ubuntu/.night_explorer.lock nice -n 10 ionice -c3 claude -p "$(cat scripts/night_explorer_mandate.md)" --model claude-opus-4-8 --dangerously-skip-permissions --max-turns 120
