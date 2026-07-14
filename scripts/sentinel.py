#!/usr/bin/env python3
"""
sentinel.py — resident self-healing agent trigger (runs ON the VM, cron every 5 min during
market hours). When the model breaks or Fable's checks flag a problem, it invokes a headless
Claude Code agent that diagnoses from the logs, fixes the code, redeploys, and restarts the
service — the model runs uninterrupted with no human in the loop.

Triggers: (a) trading-model.service failed/inactive during session hours; (b) fresh Python
Traceback / CRITICAL in the journal; (c) FAIL lines in today's morning-health report.

Guardrails (non-negotiable):
  * git snapshot BEFORE the agent runs; the agent commits its fix → everything revertible.
  * scope-locked mandate: minimal fix-to-green; NEVER touch EXECUTION_MODE / arming /
    configs/kite_config.json / safety rails; dry-run stays dry-run.
  * rate limits: ≤3 agent runs/day, 20-min cooldown, same failure-signature ≤2 attempts →
    then ESCALATE (loud report, stop retrying) so a fix-loop can't thrash the session.
  * every action appended to logs/sentinel_reports.md.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path("/home/ubuntu/institutional_trading_system_v2")
STATE = ROOT / "logs/sentinel_state.json"
REPORT = ROOT / "logs/sentinel_reports.md"
MAX_RUNS_PER_DAY = 3
COOLDOWN_MIN = 20
MAX_SAME_SIGNATURE = 2


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def load_state():
    try:
        s = json.loads(STATE.read_text())
        if s.get("day") != datetime.now(IST).date().isoformat():
            raise ValueError
        return s
    except Exception:
        return {"day": datetime.now(IST).date().isoformat(), "runs": 0,
                "last_run_epoch": 0, "signatures": {}}


def detect():
    """Return (signature, context) if a problem exists, else (None, None)."""
    now = datetime.now(IST)
    in_session = (9, 12) <= (now.hour, now.minute) <= (15, 28)
    active = sh("systemctl is-active trading-model.service")
    problems = []
    if in_session and active != "active":
        problems.append(f"SERVICE_{active.upper()}")
    j = sh("journalctl -u trading-model.service --since '6 min ago' --no-pager 2>/dev/null | tail -120")
    if "Traceback (most recent call last)" in j:
        # signature = last exception line
        exc = [l for l in j.splitlines() if "Error" in l or "Exception" in l]
        problems.append("TRACEBACK_" + (exc[-1].strip()[:60] if exc else "unknown"))
    health = ROOT / f"logs/morning_health_{now.date().isoformat()}.txt"
    if health.exists() and "[FAIL" in health.read_text():
        fails = [l.strip() for l in health.read_text().splitlines() if "[FAIL" in l]
        problems.append("HEALTH_" + fails[0][:60])
    if not problems:
        return None, None
    sig = hashlib.md5("|".join(sorted(problems)).encode()).hexdigest()[:10]
    ctx = (f"PROBLEMS: {problems}\n\nSERVICE: {active}\n\nJOURNAL (last 120):\n{j}\n\n"
           f"HEALTH REPORT:\n{health.read_text() if health.exists() else '(none)'}")
    return sig, ctx


MANDATE = """You are the resident self-healing agent for a LIVE dry-run trading model
(no real money — EXECUTION_MODE=real_dry_run). It is broken RIGHT NOW during market hours.
Your single job: restore it to running with the MINIMAL safe fix, fast.

Context file with the failure: {ctx_path}
Repo: /home/ubuntu/institutional_trading_system_v2 (you are cd'd there; it is a git repo).

DO: diagnose from the journal/traceback; find the offending code; apply the smallest fix;
`venv/bin/python -m py_compile` every file you touch; `git add -A && git commit -m "sentinel: <fix>"`;
then `sudo systemctl reset-failed trading-model.service; sudo systemctl restart trading-model.service`;
wait 45s; verify `systemctl is-active` is active AND the journal shows the pipeline booting
without the same traceback. Append what you did + why to logs/sentinel_reports.md.

FORBIDDEN (hard rules): changing EXECUTION_MODE or any arming/real-order path; touching
configs/kite_config.json or secrets; weakening safety rails (max-loss cap, halts, margin);
deleting/truncating logs or state; pip installing new packages; refactors beyond the fix.
If the failure is external (expired token, exchange outage, no network) — do NOT hack around
it; write the diagnosis to logs/sentinel_reports.md and stop."""


def invoke_agent(ctx: str):
    ts = datetime.now(IST).strftime("%Y%m%d_%H%M")
    ctx_path = ROOT / f"logs/sentinel_ctx_{ts}.txt"
    ctx_path.write_text(ctx)
    sh("cd %s && git add -A && git commit -m 'sentinel: pre-fix snapshot' --quiet" % ROOT, 30)
    prompt = MANDATE.format(ctx_path=ctx_path)
    out = sh(f"cd {ROOT} && [ -f $HOME/.claude_token ] && export CLAUDE_CODE_OAUTH_TOKEN=$(cat $HOME/.claude_token); "
             f"PATH=$HOME/.local/bin:$PATH claude -p {json.dumps(prompt)} "
             # Fable 5 pinned (operator directive 2026-07-15): live-fault
             # diagnosis/fixes are exactly the work that warrants the top model.
             f"--model claude-fable-5 "
             f"--dangerously-skip-permissions --max-turns 50 2>&1 | tail -30", timeout=1200)
    with REPORT.open("a") as fh:
        fh.write(f"\n## sentinel run {ts}\n```\n{out[-3000:]}\n```\n"
                 f"post-run service: {sh('systemctl is-active trading-model.service')}\n")
    return out


def main():
    sig, ctx = detect()
    if sig is None:
        return 0
    s = load_state()
    now_ep = datetime.now(timezone.utc).timestamp()
    if s["runs"] >= MAX_RUNS_PER_DAY or (now_ep - s["last_run_epoch"]) < COOLDOWN_MIN * 60:
        return 0
    if s["signatures"].get(sig, 0) >= MAX_SAME_SIGNATURE:
        with REPORT.open("a") as fh:
            fh.write(f"\n## ESCALATION {datetime.now(IST)}: signature {sig} failed "
                     f"{MAX_SAME_SIGNATURE}x — sentinel stopped retrying. Human review needed.\n")
        return 0
    s["runs"] += 1
    s["last_run_epoch"] = now_ep
    s["signatures"][sig] = s["signatures"].get(sig, 0) + 1
    STATE.write_text(json.dumps(s))
    invoke_agent(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
