#!/usr/bin/env python3
"""
dashboard.py — clean, read-only live monitoring overview for the terminal.

Run in a SECOND terminal alongside the live session. It never touches the trade
path — it only reads logs/paper_trading_state.json, logs/trades.db, the latest
logs/live_run_*.log, and the regime/HTF shadow files, then renders a compact
at-a-glance panel that refreshes in place. No external dependencies.

Usage:
  PYTHONPATH=. python3 scripts/dashboard.py            # refresh every 5s
  PYTHONPATH=. python3 scripts/dashboard.py --interval 3
  PYTHONPATH=. python3 scripts/dashboard.py --once     # print once and exit
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
LOGS = Path("logs")
STATE = LOGS / "paper_trading_state.json"
DB = LOGS / "trades.db"
REGIME = LOGS / "regime_shadow.jsonl"

# ── tiny ANSI helpers (no deps) ──────────────────────────────────────────────
R = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; RED = "\033[31m"; YEL = "\033[33m"; CYAN = "\033[36m"; GREY = "\033[90m"
W = 64  # inner width


def money(x: float) -> str:
    return f"₹{x:,.0f}"


def signed(x: float) -> str:
    c = GREEN if x > 0 else RED if x < 0 else GREY
    return f"{c}{'+' if x >= 0 else '-'}₹{abs(x):,.0f}{R}"


def _vis_len(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def row(s: str = "") -> str:
    pad = W - _vis_len(s)
    return f"║ {s}{' ' * max(pad, 0)} ║"


def rule(ch: str = "─") -> str:
    return f"╟{ch * (W + 2)}╢"


def session_live() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "live_structure_pipeline.py"],
                             capture_output=True, text=True, timeout=3)
        return bool(out.stdout.strip())
    except Exception:
        return False


def market_open(now_ist: datetime) -> bool:
    if now_ist.weekday() >= 5:
        return False
    mins = now_ist.hour * 60 + now_ist.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def latest_log() -> Path | None:
    files = [Path(p) for p in glob.glob(str(LOGS / "live_run_*.log"))]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def tail(path: Path, n: int = 400) -> list[str]:
    try:
        return path.read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def today_trades() -> dict:
    """Per-underlying {n, wins, pnl} for today's closed trades."""
    out: dict[str, list] = {}
    if not DB.exists():
        return out
    today = datetime.now(IST).date().isoformat()
    try:
        c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
        for r in c.execute(
            "select underlying, pnl from pt_trades "
            "where state='CLOSED' and substr(entry_ts,1,10)=?", (today,)
        ):
            a = out.setdefault(r["underlying"], [0, 0, 0.0])
            a[0] += 1; a[1] += 1 if r["pnl"] > 0 else 0; a[2] += r["pnl"]
    except Exception:
        pass
    return out


def last_regime() -> dict:
    out: dict[str, str] = {}
    if not REGIME.exists():
        return out
    try:
        for line in REGIME.read_text().splitlines()[-200:]:
            d = json.loads(line)
            out[d["underlying"]] = d.get("vol_regime", "?")
    except Exception:
        pass
    return out


def parse_log(lines: list[str]) -> dict:
    """Latest bias per underlying + last few notable activity lines."""
    bias: dict[str, str] = {}
    activity: list[str] = []
    for ln in lines:
        m = re.search(r"EXECUTION_MCB_CONTEXT\s+(\w+)\s+bias=(\w+)", ln)
        if m:
            bias[m.group(1)] = m.group(2)
        if re.search(r"TRADE LOGGED|▼ SHORT|▲ LONG|ENTRY CHECK BLOCKED|MARKET CLOSE|HARD-HALT|SOFT.?HALT|HOST SUSPEND|FEED STALL", ln):
            t = ln.strip()
            if t:
                activity.append(t[:W])
    return {"bias": bias, "activity": activity[-5:]}


def render() -> str:
    now = datetime.now(IST)
    st = load_state()
    live = session_live()
    mopen = market_open(now)
    lg = latest_log()
    log_info = parse_log(tail(lg)) if lg else {"bias": {}, "activity": []}
    trades = today_trades()
    regimes = last_regime()

    eq = float(st.get("equity", 0) or 0)
    peak = float(st.get("peak_equity", eq) or eq)
    sopen = float(st.get("session_opening_equity", eq) or eq)
    dpnl = float(st.get("daily_pnl", 0) or 0)
    dd = (peak - eq) / peak if peak > 0 else 0.0
    dpct = (dpnl / sopen * 100) if sopen > 0 else 0.0
    halted = st.get("trading_halted", False)
    soft = st.get("soft_halted", False)
    consec = st.get("consecutive_losses", 0)
    ivby = st.get("last_atm_iv_by_underlying", {}) or {}
    opens = st.get("open_positions", {}) or {}

    status = (f"{GREEN}● LIVE{R}" if live else f"{GREY}○ IDLE{R}")
    mkt = (f"{GREEN}OPEN{R}" if mopen else f"{YEL}CLOSED{R}")

    L = []
    L.append("╔" + "═" * (W + 2) + "╗")
    L.append(row(f"{BOLD}LIVE TRADING DASHBOARD{R}   {status}   market:{mkt}"))
    L.append(row(f"{DIM}{now:%Y-%m-%d %H:%M:%S} IST   session={st.get('session_date','-')}{R}"))
    L.append(rule())
    # capital
    L.append(row(f"{BOLD}CAPITAL{R}"))
    L.append(row(f"  Equity   {BOLD}{money(eq)}{R}      Peak  {money(peak)}"))
    ddc = RED if dd >= 0.10 else YEL if dd >= 0.05 else GREEN
    L.append(row(f"  Daily    {signed(dpnl)} ({dpct:+.1f}%)    Drawdown {ddc}{dd*100:.1f}%{R}"))
    if halted:
        hs = f"{RED}HARD-HALTED{R} ({st.get('halt_reason','')[:28]})"
    elif soft:
        hs = f"{YEL}SOFT-HALT{R} ({st.get('soft_halt_reason','')[:30]})"
    else:
        hs = f"{GREEN}OK{R}"
    L.append(row(f"  Status   {hs}   consec-loss:{consec}"))
    L.append(rule())
    # open positions
    L.append(row(f"{BOLD}OPEN POSITIONS{R}"))
    any_open = False
    for sym, p in opens.items():
        if not p:
            continue
        any_open = True
        L.append(row(f"  {CYAN}{sym}{R} {p.get('contract_symbol','?')}  "
                     f"x{p.get('lots','?')}  entry {money(float(p.get('entry_premium',0)))}  "
                     f"{p.get('direction','')}"))
    if not any_open:
        L.append(row(f"  {GREY}(flat — no open positions){R}"))
    L.append(rule())
    # today's trades
    L.append(row(f"{BOLD}TODAY'S TRADES{R}"))
    if trades:
        for sym, (n, w, p) in sorted(trades.items()):
            wr = f"{w}W/{n-w}L"
            L.append(row(f"  {CYAN}{sym:9}{R} {n} trades  {wr:8}  {signed(p)}"))
    else:
        L.append(row(f"  {GREY}(no closed trades today){R}"))
    L.append(rule())
    # per-symbol context
    L.append(row(f"{BOLD}PER-SYMBOL{R}"))
    syms = sorted(set(list(log_info['bias'].keys()) + list(regimes.keys()) + ['SENSEX', 'NIFTY']))
    for sym in syms:
        b = log_info['bias'].get(sym, '-')
        bc = GREEN if b == 'LONG' else RED if b == 'SHORT' else GREY
        reg = regimes.get(sym, '-')
        ivv = ivby.get(sym)
        ivs = f"  IV {float(ivv)*100:.1f}%" if ivv else ""
        L.append(row(f"  {sym:9} bias {bc}{b:8}{R} regime {reg:15}{ivs}"))
    L.append(rule())
    # activity
    L.append(row(f"{BOLD}LAST ACTIVITY{R}  {DIM}{lg.name if lg else '-'}{R}"))
    if log_info['activity']:
        for a in log_info['activity']:
            L.append(row(f"  {GREY}{a[:W-3]}{R}"))
    else:
        L.append(row(f"  {GREY}(no recent activity){R}"))
    L.append("╚" + "═" * (W + 2) + "╝")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only live trading dashboard")
    ap.add_argument("--interval", type=float, default=5.0, help="refresh seconds (default 5)")
    ap.add_argument("--once", action="store_true", help="print once and exit")
    args = ap.parse_args()
    if args.once:
        print(render())
        return 0
    try:
        while True:
            print("\033[H\033[2J", end="")  # home + clear
            print(render(), flush=True)
            time.sleep(max(args.interval, 1.0))
    except KeyboardInterrupt:
        print("\n[dashboard stopped]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
