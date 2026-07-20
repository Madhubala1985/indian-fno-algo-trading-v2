#!/usr/bin/env bash
# smart_agent_launcher.sh — reset-aware, token-frugal agent launcher (2026-07-19).
# Cron calls this every 10 min during the burst window. It launches the night explorer
# ONLY when all of these hold:
#   1. burst window marker (~/.agent_burst_until) still valid (self-disables after);
#   2. no agent already running (flock probe on the run lock);
#   3. the last Claude "session limit resets H:MMam/pm" time has passed;
#   4. a 1-turn cheap probe call succeeds (catches the nearly-empty-window case that
#      burned 2-5 min of context-loading tokens and died with nothing to show).
# Every decision is logged to logs/smart_launcher.log so runs are never a mystery.
ROOT=/home/ubuntu/institutional_trading_system_v2
LOG=$ROOT/logs/night_explorer_cron.log
SLOG=$ROOT/logs/smart_launcher.log
UNTIL_FILE=/home/ubuntu/.agent_burst_until
LOCK=/home/ubuntu/.night_explorer.lock
note() { echo "$(date -Is) $*" >> "$SLOG"; }

[ -f "$UNTIL_FILE" ] || exit 0
NOW=$(date +%s)
if [ "$NOW" -ge "$(cat "$UNTIL_FILE" 2>/dev/null || echo 0)" ]; then
    rm -f "$UNTIL_FILE"; note "burst window expired — launcher disabled"; exit 0
fi
# agent already running? (grabbing the free lock for an instant is harmless)
if ! flock -n "$LOCK" true 2>/dev/null; then exit 0; fi

# 3. reset-time gate: only consult it when the log currently ENDS in a limit death.
# Two distinct limit shapes exist and BOTH must match here (2026-07-20): the generic
# "You've hit your session limit · resets H:MMpm" AND the model-scoped "You've reached
# your <Model> limit" (which carries NO reset time). The original pattern matched only
# the former, so the 07-20 Fable-5 death would have slipped the gate and relaunched
# straight into the wall had the burst window still been open.
if tail -c 300 "$LOG" 2>/dev/null | grep -qE "session limit|reached your .* limit"; then
    # Read the reset time from the SAME tail window that matched, not the whole
    # file: a model-scoped death carries no reset time, and a whole-file grep
    # would silently reuse an unrelated timestamp from hours earlier.
    RESET=$(tail -c 300 "$LOG" 2>/dev/null \
            | grep -oE "resets [0-9]{1,2}:[0-9]{2}(am|pm)" | tail -1 | sed 's/resets //')
    if [ -n "$RESET" ]; then
        R=$(date -d "today $RESET" +%s 2>/dev/null || echo 0)
        MSG_TS=$(stat -c %Y "$LOG")
        # a reset clock-time earlier than the message itself means "tomorrow"
        [ "$R" -le "$MSG_TS" ] && R=$((R + 86400))
        if [ "$NOW" -lt "$R" ]; then
            note "limit active, reset at $RESET — waiting ($(( (R - NOW) / 60 )) min)"; exit 0
        fi
    fi
fi

# 4. cheap probe (1 turn, haiku, ~zero budget) before committing the big run
[ -f "$HOME/.claude_token" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME/.claude_token")"
export PATH="$HOME/.local/bin:$PATH"
P=$(cd "$ROOT" && timeout 90 claude -p "Reply with exactly: OK" \
      --model claude-haiku-4-5-20251001 --max-turns 1 2>&1 | tail -1)
case "$P" in
    *"session limit"*) note "probe blocked: $P — not launching"; exit 0 ;;
    *OK*) note "probe OK — launching agent" ;;
    *) note "probe unexpected ('$P') — launching anyway (fail-open)" ;;
esac
exec "$ROOT/deploy/run_night_explorer.sh"
