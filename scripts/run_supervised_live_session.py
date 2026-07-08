"""
run_supervised_live_session.py
──────────────────────────────────────────────────────────────────────
One-command supervised live session runner.

What this does:
  1. Preflight checks (access token, MCB warm-start CSV age, disk space)
  2. Archives previous session logs
  3. Starts the live pipeline as a subprocess with configured env vars
  4. Waits for the process to end (market close or manual kill)
  5. Runs post-market validation (lifecycle checks, issue register update,
     reflection generation)

Flags:
  --run-oi-diagnostics   Also run diagnose_oi_support.py during startup
                         preflight (useful when oi_support blocks are
                         being investigated).  Market hours only.
  --profile PROFILE      Override PAPER_GATE_PROFILE (default: from env)
  --capital AMOUNT       Override PAPER_STARTING_CAPITAL (default: from env)
  --dry-run              Print the plan but do not start the pipeline.

Safety:
  - Never places real orders (paper broker throughout)
  - Does not modify configs/kite_config.json
  - Does not commit data CSVs or logs

Usage:
  PYTHONPATH=. python3 scripts/run_supervised_live_session.py
  PYTHONPATH=. python3 scripts/run_supervised_live_session.py --run-oi-diagnostics
  PYTHONPATH=. python3 scripts/run_supervised_live_session.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
ARCHIVE  = LOGS_DIR / "archive"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_core.execution.gate_profiles import validate_profile_mode
from platform_core.utils.live_log_resolver import (
    resolve_current_live_log,
    write_current_live_log,
)

_IST      = timezone(timedelta(hours=5, minutes=30))
_MIN_CSV_BYTES   = 50_000
# Warm-start CSV must contain the LAST COMPLETED trading day (age=1 pre-open is
# the freshest possible). Was 3, which let a 2-day-old CSV pass on 2026-07-03 —
# the 4h/1d timeframes then missed the previous session entirely. age > 1 →
# auto-refresh from Kite (which publishes the prior day's minutes by morning).
_STALE_THRESHOLD = 1     # trading days

# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(_IST)


def _heading(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n  {title}\n{bar}")


def _ok(msg: str) -> None:
    print(f"  [OK ] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ── MCB warm-start freshness check ──────────────────────────────────────────

def _csv_end_date_key(p: Path) -> str:
    try:
        return p.stem.split("_")[-1]
    except Exception:
        return ""


def _find_warmstart_csv(prefix: str) -> Path | None:
    candidates = [
        p for p in DATA_DIR.glob(f"{prefix}_1m_*.csv")
        if p.stat().st_size >= _MIN_CSV_BYTES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (_csv_end_date_key(p), p.stat().st_size))


def _csv_last_ts(p: Path) -> datetime | None:
    import csv as _csv
    try:
        last: datetime | None = None
        with p.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                ts_raw = row.get("timestamp") or row.get("date") or ""
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    last = ts
                except Exception:
                    pass
        return last
    except Exception:
        return None


def _trading_days_since(last_date, today) -> int:
    from datetime import date, timedelta as td
    count = 0
    d = last_date + td(days=1)
    while d <= today:
        if d.weekday() < 5:
            count += 1
        d += td(days=1)
    return count


# ── Access token validity ────────────────────────────────────────────────────

def _validate_access_token(api_key: str, token: str) -> tuple[str, str]:
    """Verify the access token against Kite with a cheap authenticated call.

    Kite access tokens expire daily, so a token that is merely *present* in
    kite_config.json may already be dead.  An expired/invalid token makes the
    live WebSocket connect silently produce no ticks — a confusing failure that
    looks like "the market is quiet".  Fail closed here instead.

    Returns (status, detail) where status is:
      "valid"   — profile() succeeded; the token authenticates
      "expired" — broker rejected the token (TokenException) — must regenerate
      "unknown" — network/other error; cannot confirm — do not block on this
    """
    try:
        from kiteconnect import KiteConnect
        from kiteconnect.exceptions import TokenException
    except Exception as exc:  # pykiteconnect absent
        return "unknown", f"kiteconnect not importable ({exc}) — skipping live check"
    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)
        profile = kite.profile()
        who = profile.get("user_name") or profile.get("user_id") or "?"
        return "valid", f"token authenticates (user={who})"
    except TokenException as exc:
        return "expired", (
            f"broker rejected token ({exc}) — regenerate with "
            f"scripts/generate_kite_session.py"
        )
    except Exception as exc:
        return "unknown", f"could not verify token ({exc}) — proceeding without confirmation"


# ── Preflight ───────────────────────────────────────────────────────────────

def _auto_warmstart_refresh_enabled() -> bool:
    """Auto-refresh is ON by default; disable with SUPERVISOR_AUTO_WARMSTART_REFRESH=0.
    Auto-refresh guarantees warm-start data is never stale at session start, so the
    ~4h NEUTRAL blind-start (found 2026-07-01) can never return operationally."""
    return str(os.environ.get("SUPERVISOR_AUTO_WARMSTART_REFRESH", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def _run_warmstart_refresh(days: int = 30) -> bool:
    """Run scripts/refresh_mcb_warmstart_data.py as a subprocess (needs a VALID
    token — call only after the token check passes). Returns True on success.
    Time-bounded so a hung fetch can't block the session indefinitely."""
    script = ROOT / "scripts" / "refresh_mcb_warmstart_data.py"
    if not script.exists():
        _warn(f"warm-start refresh script not found: {script.name}")
        return False
    _info(f"Auto-refreshing warm-start data ({days} trading days, all symbols)…")
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--days", str(days), "--overwrite"],
            cwd=str(ROOT), timeout=600,
        )
    except subprocess.TimeoutExpired:
        _warn("warm-start refresh timed out (>600s) — proceeding with existing data")
        return False
    except Exception as exc:
        _warn(f"warm-start refresh failed to launch: {exc}")
        return False
    if result.returncode == 0:
        _ok("warm-start data refreshed")
        return True
    _warn(f"warm-start refresh returned non-zero ({result.returncode}) — check output above")
    return False


def run_preflight(run_oi_diagnostics: bool) -> bool:
    _heading("PREFLIGHT CHECKS")
    ok = True
    token_valid = False

    # 1. Access token — present AND valid (tokens expire daily)
    cfg_path = ROOT / "configs" / "kite_config.json"
    if not cfg_path.exists():
        _fail("configs/kite_config.json not found")
        ok = False
    else:
        try:
            cfg = json.loads(cfg_path.read_text())
            token = cfg.get("access_token", "")
            api_key = cfg.get("api_key", "")
            if token:
                _ok(f"Access token present (length={len(token)})")
                status, detail = _validate_access_token(api_key, token)
                if status == "valid":
                    token_valid = True
                    _ok(f"Access token valid — {detail}")
                elif status == "expired":
                    _fail(f"Access token EXPIRED — {detail}")
                    ok = False
                else:
                    _warn(f"Access token unverified — {detail}")
            else:
                _fail("No access_token in kite_config.json — run scripts/generate_kite_session.py")
                ok = False
        except Exception as exc:
            _fail(f"Could not read kite_config.json: {exc}")
            ok = False

    # 2. MCB warm-start CSV age — AUTO-REFRESH when stale/missing (never start blind).
    _PREFIX_MAP = {"BANKNIFTY": "banknifty", "NIFTY": "nifty", "SENSEX": "sensex"}
    today_ist = _now_ist().date()

    def _stale_symbols() -> list[str]:
        out = []
        for sym, prefix in _PREFIX_MAP.items():
            csv_path = _find_warmstart_csv(prefix)
            if csv_path is None:
                out.append(f"{sym} (missing)")
                continue
            last_ts = _csv_last_ts(csv_path)
            if last_ts is None:
                out.append(f"{sym} (unreadable)")
                continue
            if _trading_days_since(last_ts.date(), today_ist) > _STALE_THRESHOLD:
                out.append(f"{sym} ({_trading_days_since(last_ts.date(), today_ist)}d)")
        return out

    stale = _stale_symbols()
    if stale:
        if token_valid and _auto_warmstart_refresh_enabled():
            _warn(f"warm-start data stale/missing: {', '.join(stale)} — auto-refreshing")
            _run_warmstart_refresh()
            stale = _stale_symbols()   # re-check after refresh
            if stale:
                _warn(f"still stale/missing after refresh: {', '.join(stale)} "
                      f"— MCB may start with reduced context")
        elif not token_valid:
            _warn(f"warm-start data stale/missing: {', '.join(stale)} — cannot auto-refresh "
                  f"(token not valid); regenerate token, or run refresh manually")
        else:
            _warn(f"warm-start data stale/missing: {', '.join(stale)} — auto-refresh disabled "
                  f"(SUPERVISOR_AUTO_WARMSTART_REFRESH=0); run scripts/refresh_mcb_warmstart_data.py")
    # Report per-symbol freshness (post-refresh)
    for sym, prefix in _PREFIX_MAP.items():
        csv_path = _find_warmstart_csv(prefix)
        if csv_path is None:
            _warn(f"{sym}: no warm-start CSV in data/ — MCB will start cold")
            continue
        last_ts = _csv_last_ts(csv_path)
        age = _trading_days_since(last_ts.date(), today_ist) if last_ts else -1
        if age < 0:
            _warn(f"{sym}: could not read last timestamp from {csv_path.name}")
        elif age > _STALE_THRESHOLD:
            _warn(f"{sym}: warm-start CSV {age}d old ({csv_path.name}) — still stale")
        else:
            _ok(f"{sym}: warm-start CSV ok  file={csv_path.name}  age={age}d")

    # 3. Disk space
    try:
        usage = shutil.disk_usage(str(ROOT))
        free_gb = usage.free / 1e9
        if free_gb < 1.0:
            _warn(f"Disk space low: {free_gb:.2f} GB free")
        else:
            _ok(f"Disk space: {free_gb:.1f} GB free")
    except Exception:
        _info("Could not check disk space")

    # 4. Pipeline file exists (do NOT import — it has startup side effects)
    pipeline_path = ROOT / "platform_core" / "data" / "live" / "live_structure_pipeline.py"
    if pipeline_path.exists():
        _ok(f"Pipeline script found: {pipeline_path.name}")
    else:
        _fail(f"Pipeline script not found: {pipeline_path}")
        ok = False

    # 5. Optional OI diagnostics
    if run_oi_diagnostics:
        _heading("OI DIAGNOSTICS (preflight)")
        script = ROOT / "scripts" / "run_live_oi_diagnostics.sh"
        if script.exists():
            result = subprocess.run(["bash", str(script)], cwd=str(ROOT))
            if result.returncode != 0:
                _warn("OI diagnostics returned non-zero — check output above")
        else:
            _warn("scripts/run_live_oi_diagnostics.sh not found — skipping")

    return ok


# ── Paper state capital resolution ──────────────────────────────────────────

def resolve_state_capital(
    requested_capital: float,
    state_path: Path | None = None,
    archive_dir: Path | None = None,
) -> tuple[str, str]:
    """
    Resolve a conflict between the requested starting capital and the
    capital basis of an existing logs/paper_trading_state.json.

    Returns (action, detail) where action is one of:
      "fresh"    — no state file; pipeline starts at requested capital
      "match"    — state capital equals requested capital; state continues
      "archived" — capitals conflict, no open positions; state file moved to
                   logs/archive/paper_trading_state_<ts>.json (explicit reset)
      "refuse"   — capitals conflict AND state holds open positions; the
                   session must NOT start

    A mismatched state is NEVER silently continued.
    """
    state_path  = state_path  or (LOGS_DIR / "paper_trading_state.json")
    archive_dir = archive_dir or ARCHIVE
    if not state_path.exists():
        return "fresh", f"no state file — starting fresh at ₹{requested_capital:,.0f}"
    try:
        s = json.loads(state_path.read_text())
    except Exception as exc:
        return "refuse", f"state file unreadable ({exc}) — resolve manually"

    raw_cap = s.get("starting_capital")
    state_capital = float(raw_cap) if raw_cap is not None else float(s.get("peak_equity", 0.0))
    cap_src = "state" if raw_cap is not None else "inferred_from_peak"
    if abs(state_capital - requested_capital) <= 0.5:
        return "match", (
            f"state capital ₹{state_capital:,.0f} matches requested "
            f"₹{requested_capital:,.0f} — continuing"
        )

    has_open = bool(s.get("open_position")) or any(
        v for v in (s.get("open_positions") or {}).values()
    )
    if has_open:
        return "refuse", (
            f"state capital ₹{state_capital:,.0f} ({cap_src}) conflicts with "
            f"requested ₹{requested_capital:,.0f} AND state holds open "
            f"positions — refusing to start"
        )

    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / (
        f"paper_trading_state_{_now_ist().strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.move(str(state_path), str(dest))
    return "archived", (
        f"state capital ₹{state_capital:,.0f} ({cap_src}) conflicts with "
        f"requested ₹{requested_capital:,.0f} — archived state to {dest} — "
        f"new supervised session starts fresh at ₹{requested_capital:,.0f}"
    )


# ── Archive ─────────────────────────────────────────────────────────────────

def archive_previous_session() -> None:
    _heading("ARCHIVE PREVIOUS SESSION")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    today_str = _now_ist().strftime("%Y%m%d")

    # Rotate live_run_*.log files older than today
    archived = 0
    for log_file in sorted(LOGS_DIR.glob("live_run_*.log")):
        # Keep today's logs in place (they may already exist from a prior run today)
        if today_str in log_file.name:
            continue
        dest = ARCHIVE / log_file.name
        if not dest.exists():
            shutil.move(str(log_file), str(dest))
            archived += 1

    if archived:
        _ok(f"Archived {archived} old log file(s) to logs/archive/")
    else:
        _info("No old log files to archive")


# ── Start pipeline ───────────────────────────────────────────────────────────

def build_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()

    # Execution mode — default paper; opt-in to real_dry_run for tomorrow's test
    exec_mode = getattr(args, "execution_mode", "paper") or "paper"
    env["EXECUTION_MODE"] = exec_mode

    # Gate profile
    if args.profile:
        env["PAPER_GATE_PROFILE"] = args.profile
    elif "PAPER_GATE_PROFILE" not in env:
        env["PAPER_GATE_PROFILE"] = "default"

    # Paper-only profiles must never run armed — fail closed here too,
    # independent of the identical check in main() and the pipeline itself.
    profile_ok, reject_reason = validate_profile_mode(
        env["PAPER_GATE_PROFILE"], exec_mode
    )
    if not profile_ok:
        raise SystemExit(f"[SUPERVISOR] {reject_reason}")

    # paper_trade_unlock — ₹4L / 5% paper-risk trade-unlock experiment.
    # Sets the full env block so the pipeline runs the experiment exactly.
    if env["PAPER_GATE_PROFILE"] == "paper_trade_unlock":
        env["PAPER_RISK_PROFILE"]                 = "balanced"
        env["PAPER_TIER2_RISK_FRAC"]              = "0.05"
        env["PAPER_TIER3_RISK_FRAC"]              = "0.05"
        env["PAPER_MAX_SINGLE_TRADE_RISK_FRAC"]   = "0.05"
        env["MAX_OPEN_POSITIONS"]                 = "2"
        env["LIVE_TERMINAL_MODE"]                 = "compact"
        if not args.capital and "PAPER_STARTING_CAPITAL" not in env:
            env["PAPER_STARTING_CAPITAL"] = "400000"

    # Capital — compounding by default: when no explicit --capital is given,
    # leave PAPER_STARTING_CAPITAL unset so the pipeline rolls the basis forward
    # from the state file's current equity (realised profits compound into the
    # capital base). An explicit --capital still pins a fixed basis.
    if args.capital:
        env["PAPER_STARTING_CAPITAL"] = str(args.capital)

    # Risk profile
    if "PAPER_RISK_PROFILE" not in env:
        env["PAPER_RISK_PROFILE"] = "balanced"

    # Terminal mode
    if "LIVE_TERMINAL_MODE" not in env:
        env["LIVE_TERMINAL_MODE"] = "compact"

    # SENSEX strike-margin floor — loosen 0.05 -> 0.015. SENSEX near-ATM strikes
    # are structural near-ties in low vol (margins 0.00-0.03), so the strict 0.05
    # default blocks ~100% of SENSEX bars (137 of 154 strike-quality blocks on
    # 2026-06-25 were score_margin-only). 0.015 still floors out genuine coin-flip
    # ties. Applies to ALL modes incl. real_armed (user directive 2026-07-05:
    # real_armed must behave EXACTLY like the validated paper/dry-run sessions —
    # a stricter armed gate would deploy behaviour never observed live). An
    # explicit PAPER_SENSEX_SCORE_MARGIN_MIN (or profile override) always wins.
    if "PAPER_SENSEX_SCORE_MARGIN_MIN" not in env:
        env["PAPER_SENSEX_SCORE_MARGIN_MIN"] = "0.015"

    # Aggressive conviction-scaled sizing — ALL live modes incl. real_armed
    # (user directive 2026-07-05: real_armed must run the EXACT profile the
    # paper/dry-run sessions validated — parity is the point; a derated armed
    # profile would deploy sizing never observed live). The rupee interlocks
    # (MAX_REAL_*, REAL_MAX_LOTS_PER_ORDER) remain as absolute BACKSTOPS
    # against bugs, set at levels the sizer never reaches. setdefault() so an
    # explicit env value or a profile block (e.g. paper_trade_unlock) wins.
    #   - risk/trade 4% -> 8%            (more lots)
    #   - tier ceilings 4/6 -> 10/15     (best setups can press)
    #   - conviction top 1.25x -> 1.5x   (best setups bigger, normal unchanged)
    #   - portfolio heat 10% -> 30%      (the real binding cap for option-buying)
    # NIFTY stays capped at PAPER_NIFTY_MAX_LOTS (default 5) so the loser can't
    # out-size the winner.
    if exec_mode in ("paper", "real_dry_run", "real_armed"):
        # Approach-A recorder ON by default: capture the real
        # option chain each bar to grow the offline validation dataset (safe —
        # no extra Kite calls, never raises; RECORD_OPTION_CHAIN=0 to disable).
        env.setdefault("RECORD_OPTION_CHAIN", "1")
        env.setdefault("PAPER_TIER2_RISK_FRAC", "0.08")
        env.setdefault("PAPER_TIER3_RISK_FRAC", "0.08")
        env.setdefault("PAPER_MAX_SINGLE_TRADE_RISK_FRAC", "0.08")
        # Pure %-of-capital sizing (user directive 2026-07-03): 0 = no fixed
        # lot-count ceiling. Lots derive from the 8% risk budget, the 15%
        # single-position premium cap (PAPER_MAX_POSITION_PREMIUM_FRAC, tail
        # defense — an option buyer's worst case is the whole premium), the 30%
        # portfolio heat cap, and the exchange freeze-qty rail (all modes —
        # real_armed runs this same profile, parity directive 2026-07-05).
        env.setdefault("PAPER_TIER2_MAX_LOTS", "0")
        env.setdefault("PAPER_TIER3_MAX_LOTS", "0")
        env.setdefault("PAPER_MAX_CONF_SCALE", "1.5")
        # Deployment scale-up (user directive 2026-07-06, measured on the
        # open-drive config: risk 8% / cap 35% = +87,639 vs +78,976 at cap 15%
        # on 14 day-samples — IN-SAMPLE, rides on the open-drive edge; this
        # week's dry-runs are the zero-risk OOS test): single-position premium
        # cap 15% → 35% of session-open equity (₹1.25–1.5L at ₹4L). Heat 30% →
        # 40% so the heat rail can't silently shrink the requested size; the
        # 20% correlated same-direction rail still prevents stacking the same
        # bet, and an armed (breakeven-ratcheted) winner frees its slot.
        env.setdefault("PAPER_MAX_POSITION_PREMIUM_FRAC", "0.35")
        env.setdefault("PAPER_MAX_HEAT_FRACTION", "0.40")
        # Halt thresholds (revised 2026-07-08, user directive; measured on 9 days
        # of real recorded option data — NIFTY+SENSEX merged, 31 trades):
        #   - CONSEC halt -> OFF (0). It was the ONLY halt that ever fired (the
        #     daily floors sat at 14/15%, unreachable at these trade sizes), but
        #     removing it moved total P&L by just -₹58 over the 9 days — it rarely
        #     binds and when it did (07-06) it trimmed a ₹58 losing tail. The
        #     sizer's SMOOTH drawdown-haircut (size ∝ remaining room to account
        #     DD) is the intended continuous de-risker, not a streak cliff.
        #     CAVEAT: the one day it bound live (07-08 chop) is NOT in the sample
        #     — the surgical replay gates all 07-08 entries at the OR filter, so
        #     the chop-day counterfactual is unmeasured. Revisit with live data.
        #   - HARD daily 15% -> 6%: a real catastrophe floor (auto-resumes next
        #     session), tighter than the old effectively-dead 15%, still well
        #     below the 21% manual-restart account-DD halt.
        #   - SOFT stays 0.14 and is now INERT by design: it sits ABOVE the 6%
        #     hard stop, so the hard halt always fires first (14% is never
        #     reached). Intentional — no soft cliff; pure smooth sizer taper down
        #     to the 6% stop. Low soft-halt (6%) was measured harmful earlier
        #     (slammed to 1 lot after one loss), so we do NOT lower it.
        # (Applies to real_armed too — parity.)
        env.setdefault("PAPER_HARD_HALT_CONSEC_LOSSES", "0")
        env.setdefault("PAPER_SOFT_HALT_DAILY_LOSS", "0.14")
        env.setdefault("PAPER_HARD_HALT_DAILY_LOSS", "0.06")
        env.setdefault("PAPER_MAX_ACCOUNT_DD", "0.21")

    return env


def start_pipeline(env: dict[str, str]) -> subprocess.Popen:
    log_stamp = _now_ist().strftime("%Y%m%d_%H%M%S")
    log_path  = LOGS_DIR / f"live_run_{log_stamp}.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(ROOT / "platform_core" / "data" / "live" / "live_structure_pipeline.py"),
    ]

    _heading("STARTING LIVE PIPELINE")
    _info(f"Command        : {' '.join(cmd)}")
    _info(f"Log            : {log_path}")
    _info(f"Execution mode : EXECUTION_MODE={env.get('EXECUTION_MODE', 'paper')}")
    _info(f"Real orders    : {'true' if env.get('EXECUTION_MODE') == 'real_armed' else 'false'}")
    _info(f"Profile        : PAPER_GATE_PROFILE={env.get('PAPER_GATE_PROFILE')}")
    _info(f"Capital        : PAPER_STARTING_CAPITAL={env.get('PAPER_STARTING_CAPITAL')}")
    _info(f"Risk           : PAPER_RISK_PROFILE={env.get('PAPER_RISK_PROFILE')}")
    print()
    # Explicit config banner (the pipeline prints its own authoritative copy)
    if env.get("PAPER_STARTING_CAPITAL"):
        _capital = float(env["PAPER_STARTING_CAPITAL"])
        print(f"[CAPITAL CONFIG] starting_capital=₹{_capital:,.0f}")
    else:
        # Compounding: no fixed PAPER_STARTING_CAPITAL — the pipeline rolls the
        # basis forward from the state file's current equity. Show that carried
        # balance rather than a misleading ₹0.
        try:
            _carried = float(json.loads(
                (LOGS_DIR / "paper_trading_state.json").read_text())["equity"])
            print(f"[CAPITAL CONFIG] starting_capital=₹{_carried:,.0f} "
                  f"(compounding — carried balance)")
        except Exception:
            print("[CAPITAL CONFIG] starting_capital=compounding (carried "
                  "balance from state file)")
    _tier2 = float(env.get("PAPER_TIER2_RISK_FRAC", "0") or 0)
    if _tier2 > 0:
        print(f"[RISK CONFIG] tier2={_tier2 * 100:.1f}%")
    _profile = env.get("PAPER_GATE_PROFILE", "default")
    if _profile == "paper_trade_unlock":
        print(
            f"[GATE PROFILE] profile={_profile} paper_only=true real_funds_approved=false"
        )
        print("[WARNING] Paper trade-unlock experiment — not approved for real funds.")
    print(f"[EXECUTION MODE] {env.get('EXECUTION_MODE', 'paper')}")
    print(f"[REAL ORDERS ENABLED] {'true' if env.get('EXECUTION_MODE') == 'real_armed' else 'false'}")
    print()

    # Point current_live_log.txt at this session's log so post-market tools
    # never grep a stale/archived path.
    write_current_live_log(LOGS_DIR, log_path)

    log_fh = log_path.open("w", buffering=1)

    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )

    # Tee stdout: terminal + log file
    import threading

    def _tee() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            print(line, end="", flush=True)
            log_fh.write(line)
        log_fh.close()

    t = threading.Thread(target=_tee, daemon=True)
    t.start()

    print(f"[SUPERVISOR] Pipeline started  PID={proc.pid}  log={log_path.name}")
    print("[SUPERVISOR] Press Ctrl-C to stop.\n")
    return proc


# ── Post-market validation ───────────────────────────────────────────────────

def run_post_market(dry_run: bool) -> None:
    _heading("POST-MARKET VALIDATION")

    # Repair a stale current_live_log pointer before any consumer runs
    try:
        resolved = resolve_current_live_log(LOGS_DIR)
        if resolved is None:
            _warn("No logs/live_run_*.log found — post-market scripts may fail")
        else:
            _info(f"Current live log : {resolved.name}")
    except Exception as exc:
        _warn(f"current_live_log resolution failed: {exc}")

    scripts = [
        ("Lifecycle validator",  ["scripts/validate_live_lifecycle_state.py"]),
        ("Issue register",       ["scripts/update_live_issue_register.py"]),
        ("Daily reflection",     ["scripts/generate_daily_reflection.py"]),
        ("Near-miss analyzer",   ["scripts/analyze_near_miss_entries.py"]),
    ]

    for label, args in scripts:
        script_path = ROOT / args[0]
        if not script_path.exists():
            _warn(f"{label}: {args[0]} not found — skipping")
            continue
        if dry_run:
            _info(f"[DRY-RUN] would run: PYTHONPATH=. python3 {args[0]}")
            continue
        print(f"\n  ── {label} ──")
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        if result.returncode == 0:
            _ok(f"{label}: completed")
        else:
            _warn(f"{label}: returned exit code {result.returncode}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised live session runner")
    parser.add_argument("--run-oi-diagnostics", action="store_true",
                        help="Run OI diagnostics during preflight (market hours only)")
    parser.add_argument("--profile", default="",
                        help="Override PAPER_GATE_PROFILE "
                             "(e.g. banknifty_experiment, paper_trade_unlock)")
    parser.add_argument("--capital", type=int, default=0,
                        help="Override PAPER_STARTING_CAPITAL (e.g. 300000)")
    parser.add_argument("--execution-mode", default="paper",
                        choices=["paper", "real_dry_run", "real_armed"],
                        dest="execution_mode",
                        help="Execution mode: paper (default) | real_dry_run | real_armed")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan but do not start the pipeline")
    args = parser.parse_args()

    _heading(f"SUPERVISED LIVE SESSION — {_now_ist().strftime('%Y-%m-%d %H:%M IST')}")

    if args.dry_run:
        print("  [DRY-RUN] No pipeline will be started.")

    # Paper-only gate profiles cannot run armed — fail closed before anything
    _requested_profile = args.profile or os.environ.get("PAPER_GATE_PROFILE", "default")
    profile_ok, reject_reason = validate_profile_mode(
        _requested_profile, args.execution_mode
    )
    if not profile_ok:
        print(f"\n[SUPERVISOR] {reject_reason}")
        return 1

    # real_armed requires explicit interlock env vars — fail-closed before preflight
    if args.execution_mode == "real_armed":
        from platform_core.execution.broker_adapter import _check_real_interlocks
        ok, missing = _check_real_interlocks()
        if not ok:
            print("\n[SUPERVISOR] real_armed requested but interlocks are not set:")
            for m in missing:
                print(f"  [REAL ORDER BLOCKED] missing interlock={m}")
            print("[SUPERVISOR] Set all required env vars before using real_armed.")
            return 1

    _info(f"Execution mode: {args.execution_mode}")

    preflight_ok = run_preflight(args.run_oi_diagnostics)
    if not preflight_ok:
        print("\n[SUPERVISOR] Preflight FAILED — fix issues above before starting.")
        return 1

    archive_previous_session()

    if args.dry_run:
        run_post_market(dry_run=True)
        return 0

    env  = build_env(args)

    # Capital/state consistency.
    _heading("STATE CAPITAL RESOLUTION")
    if "PAPER_STARTING_CAPITAL" in env:
        # Explicit fixed-capital basis requested — never silently continue an
        # old-capital state; resolve or refuse.
        _requested_capital = float(env.get("PAPER_STARTING_CAPITAL", "0") or 0)
        _action, _detail = resolve_state_capital(_requested_capital)
        if _action == "refuse":
            _fail(f"[STATE CAPITAL] {_detail}")
            print("\n[SUPERVISOR] State capital conflict — fix state before starting.")
            return 1
        _ok(f"[STATE CAPITAL] action={_action}: {_detail}")
        # Defense-in-depth: the pipeline's own _load_state() guard stays fail-closed.
        env["PAPER_STATE_CAPITAL_MISMATCH"] = "refuse"
    else:
        # Compounding (default): the pipeline rolls the capital basis forward
        # from the carried balance in logs/paper_trading_state.json. Nothing to
        # reconcile — realised P&L compounds into the base.
        _ok("[STATE CAPITAL] compounding — session starts from the carried "
            "balance in logs/paper_trading_state.json (no fixed capital basis).")

    proc = start_pipeline(env)

    exit_code = 0
    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        print("\n[SUPERVISOR] Ctrl-C received — terminating pipeline…")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[SUPERVISOR] Pipeline stopped.")

    time.sleep(1)  # let tee thread flush
    run_post_market(dry_run=False)

    _heading("SESSION COMPLETE")
    print(f"  Pipeline exit code : {exit_code}")
    print(f"  Logs               : {LOGS_DIR}/")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
