#!/usr/bin/env python3
"""claude_cas_read.py — read the ENGINEERED CAS signal from the authoritative series.

Do not recompute the imbalance locally: a local re-implementation gets the same sign but
not the same magnitude (different bid/ask-quantity source), and the validation was done on
the VM logger's series. Read THAT series so the live number and the backtested number are
the same quantity.

Signal (validated on 2026-07-28..08-06, 3 indices, logs/option_flow_shadow.jsonl):
  all day    corr(imb, forward move) ~= -0.01, sign hit 49.7%   -> nothing
  last hour  corr -0.17, sign hit 44.9%                         -> CONTRARIAN
Trade = FADE the book:  net_book_imb > 0 -> BUY PE ;  < 0 -> BUY CE.
Averaged over the WINDOW minutes ending now, matching the backtest's smoothing.
"""
from __future__ import annotations
import datetime as _dt, json, os, statistics, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
try:
    from zoneinfo import ZoneInfo; IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

VM = "ubuntu@YOUR_VM_IP"
KEY = os.path.expanduser("~/.ssh/oracle.key")
WINDOW = int(os.environ.get("CAS_WINDOW", "5"))
DEADBAND = float(os.environ.get("CAS_DEADBAND", "0.10"))


def ist_minutes(ts: str) -> int:
    h, m = int(ts[11:13]) + 5, int(ts[14:16]) + 30
    if m >= 60: m -= 60; h += 1
    return h * 60 + m


def main() -> int:
    now = _dt.datetime.now(IST)
    cutoff = now.hour * 60 + now.minute
    try:
        raw = subprocess.run(
            ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", VM,
             "tail -400 ~/institutional_trading_system_v2/logs/option_flow_shadow.jsonl"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        print(f"CAS READ FAILED: {type(e).__name__} {str(e)[:70]}"); return 1
    per = {}
    today = now.strftime("%Y-%m-%d")
    for line in raw.splitlines():
        try: d = json.loads(line)
        except Exception: continue
        if d.get("ts", "")[:10] != today or d.get("net_book_imb") is None: continue
        t = ist_minutes(d["ts"])
        if cutoff - WINDOW <= t <= cutoff:
            per.setdefault(d["symbol"], []).append(d["net_book_imb"])
    if not per:
        print(f"no imbalance rows in the {WINDOW}min window ending {now:%H:%M} IST"); return 1
    print(f"=== ENGINEERED CAS — window {WINDOW}min ending {now:%H:%M} IST (FADE the book) ===")
    ranked = []
    for sym, v in sorted(per.items()):
        m = statistics.mean(v)
        act = "BUY PE" if m >= DEADBAND else ("BUY CE" if m <= -DEADBAND else "—")
        print(f"  {sym:10} n={len(v):>2}  mean net_book_imb {m:+.4f}  "
              f"(min {min(v):+.3f} max {max(v):+.3f})  -> {act}")
        if abs(m) >= DEADBAND: ranked.append((abs(m), sym, m, act))
    if ranked:
        ranked.sort(reverse=True)
        _, sym, m, act = ranked[0]
        print(f"\n  STRONGEST: {sym} net {m:+.4f} -> {act}")
    else:
        print(f"\n  NO SIGNAL — all symbols inside the +/-{DEADBAND} deadband")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
