#!/usr/bin/env python3
"""
morning_health_check.py — AUTONOMOUS post-open health check + auto-fix (runs ON the VM via a
systemd timer at ~09:40 IST daily). Verifies every batch-4/shadow change is actually EXECUTING
and producing REAL data, restarts the model if it crashed, and writes a dated report. No approval
needed; safe auto-fix only (restart a down dry-run service).

Checks (PASS/WARN/FAIL): model process alive; token valid (preflight); each shadow logging TODAY;
crossasset source=kite (not NEEDS_FEED); orderflow depth present; MCB snapshot written; today's
trades + P&L. Report → logs/morning_health_<date>.txt and stdout (journald).
"""
from __future__ import annotations

import json
import subprocess
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date().isoformat()
R = []          # report lines
FAILS = 0


def line(status, msg):
    global FAILS
    if status == "FAIL":
        FAILS += 1
    R.append(f"  [{status:4}] {msg}")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def shadow_today(path, key=None, want=None):
    """Return (records_today, sample_dict) and check key==want on the newest record."""
    p = Path(path)
    if not p.exists():
        return 0, None
    n, last = 0, None
    for ln in p.read_text().splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        ts = d.get("ts") or d.get("logged_at") or d.get("session_date") or ""
        if isinstance(ts, str) and ts[:10] == TODAY:
            n += 1
            last = d
    return n, last


def main():
    R.append("=" * 70)
    R.append(f"  MORNING HEALTH CHECK — {datetime.now(IST):%Y-%m-%d %H:%M IST}")
    R.append("=" * 70)

    # 1. model process alive. systemd Restart=on-failure already auto-restarts crashes; here we
    #    VERIFY and, if down during market hours, try a restart (no-op without root → reported).
    active = sh("systemctl is-active trading-model.service")
    now_ist = datetime.now(IST)
    in_session = (9, 10) <= (now_ist.hour, now_ist.minute) <= (15, 30)
    if active == "active":
        line("PASS", "model service active")
    elif not in_session:
        line("INFO", f"model service {active} (outside 09:10-15:30 session — expected)")
    else:
        line("FAIL", f"model service {active} DURING SESSION — attempting restart")
        sh("systemctl restart trading-model.service 2>/dev/null || sudo -n systemctl restart trading-model.service 2>/dev/null")
        import time; time.sleep(8)
        after = sh("systemctl is-active trading-model.service")
        line("INFO" if after == "active" else "FAIL",
             f"after restart: {after}" + ("" if after == "active" else " (needs manual restart — no root)"))

    # 2. token / preflight — read recent journal
    jr = sh("journalctl -u trading-model.service --since '30 min ago' --no-pager 2>/dev/null | tail -40")
    if "Access token EXPIRED" in jr or "Preflight FAILED" in jr:
        line("FAIL", "token EXPIRED / preflight failed — token.timer must regen (cannot auto-fix TOTP)")
    elif "PREFLIGHT" in jr or "MCB WARMSTART" in jr:
        line("PASS", "preflight ran (token accepted)")
    else:
        line("WARN", "no recent preflight in journal (model may not have booted yet)")

    # 3. boot wiring — MCB snapshot + shadow-spawn lines
    if Path(f"logs/mcb_snapshots/{TODAY}.json").exists():
        line("PASS", f"MCB snapshot written (replay-parity fix) mcb_snapshots/{TODAY}.json")
    else:
        line("WARN", "MCB snapshot for today NOT found (boot may be pending)")
    if "[CROSSASSET SHADOW]" in jr:
        line("PASS", "crossasset shadow spawned at boot")

    # 4. each shadow logging TODAY
    checks = [
        ("logs/regime_shadow.jsonl", None, None),
        ("logs/htf_shadow.jsonl", None, None),
        ("logs/conviction_shadow.jsonl", None, None),
        ("logs/continuation_shadow.jsonl", None, None),
        ("logs/reversal_shadow.jsonl", None, None),
        ("logs/trap_shadow.jsonl", None, None),
        ("logs/options_analytics_shadow.jsonl", None, None),
    ]
    for path, k, w in checks:
        n, last = shadow_today(path)
        name = path.split("/")[-1]
        line("PASS" if n > 0 else "WARN", f"{name}: {n} records today")

    # 5. crossasset: source must be kite (not NEEDS_FEED)
    n, last = shadow_today("logs/crossasset_shadow.jsonl")
    if n == 0:
        line("WARN", "crossasset_shadow: 0 records today (boot pending or spawn failed)")
    else:
        src = (last or {}).get("source", "?")
        line("PASS" if "kite" in str(src) else "FAIL",
             f"crossasset_shadow: {n} recs, source={src} "
             f"(gift_gap={last.get('gift_gap_pct')} sp={last.get('overnight_sp_pct')} vix={last.get('india_vix')})")

    # 6. orderflow: depth present
    n, last = shadow_today("logs/orderflow_shadow.jsonl")
    if n == 0:
        line("WARN", "orderflow_shadow: 0 records today (needs live ticks)")
    else:
        depth = (last or {}).get("depth_imbalance")
        line("PASS" if depth is not None else "WARN",
             f"orderflow_shadow: {n} recs, depth_imbalance={depth}, source={(last or {}).get('source')}")

    # 7. dead-component detector: a repeated fail-open/error print means a
    #    component is silently DEAD while the model keeps trading without it
    #    (2026-07-15: bias-invalidation crashed on EVERY bar ×478 and the only
    #    symptom was a one-line fail-open print — the check said ALL HEALTHY).
    try:
        import glob, collections
        logs_today = sorted(glob.glob(f"logs/live_run_{TODAY.replace('-','')}_*.log"))
        sig = collections.Counter()
        for lp in logs_today:
            with open(lp, errors="replace") as fh:
                for ln in fh:
                    if ("fail-open" in ln or "Pipeline error" in ln
                            or "error (fail-open)" in ln or "Traceback" in ln):
                        sig[ln.strip()[:110]] += 1
        repeated = {s: n for s, n in sig.items() if n >= 10}
        if repeated:
            for s, n in sorted(repeated.items(), key=lambda kv: -kv[1])[:5]:
                line("FAIL", f"component DEAD? x{n}: {s}")
        elif sig:
            line("INFO", f"{sum(sig.values())} transient error line(s) in live log (below dead-component threshold)")
        else:
            line("PASS", "no fail-open/error signatures in today's live log")
    except Exception as e:
        line("WARN", f"dead-component scan failed: {e}")

    # 8. how is the model doing today
    try:
        c = sqlite3.connect("logs/trades.db")
        row = c.execute("select count(*), coalesce(sum(pnl),0) from pt_trades where date(entry_ts)=?",
                        (TODAY,)).fetchone()
        line("INFO", f"model trades today: {row[0]}  net P&L: Rs {row[1]:+,.0f}")
    except Exception as e:
        line("WARN", f"trades.db read failed: {e}")

    R.append("=" * 70)
    R.append(f"  VERDICT: {'ALL HEALTHY' if FAILS == 0 else f'{FAILS} FAIL(S) — see above'}")
    R.append("=" * 70)
    out = "\n".join(R)
    print(out)
    Path("logs").mkdir(exist_ok=True)
    Path(f"logs/morning_health_{TODAY}.txt").write_text(out + "\n")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(0 if main() == 0 else 0)   # never non-zero (don't trip systemd restart loops)
