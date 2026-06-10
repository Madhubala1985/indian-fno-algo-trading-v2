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
_STALE_THRESHOLD = 3     # trading days

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


# ── Preflight ───────────────────────────────────────────────────────────────

def run_preflight(run_oi_diagnostics: bool) -> bool:
    _heading("PREFLIGHT CHECKS")
    ok = True

    # 1. Access token
    cfg_path = ROOT / "configs" / "kite_config.json"
    if not cfg_path.exists():
        _fail("configs/kite_config.json not found")
        ok = False
    else:
        try:
            cfg = json.loads(cfg_path.read_text())
            token = cfg.get("access_token", "")
            if token:
                _ok(f"Access token present (length={len(token)})")
            else:
                _fail("No access_token in kite_config.json — run scripts/generate_kite_session.py")
                ok = False
        except Exception as exc:
            _fail(f"Could not read kite_config.json: {exc}")
            ok = False

    # 2. MCB warm-start CSV age
    _PREFIX_MAP = {"BANKNIFTY": "banknifty", "NIFTY": "nifty", "SENSEX": "sensex"}
    today_ist = _now_ist().date()
    for sym, prefix in _PREFIX_MAP.items():
        csv_path = _find_warmstart_csv(prefix)
        if csv_path is None:
            _warn(f"{sym}: no warm-start CSV found in data/ — MCB will start cold")
            continue
        last_ts = _csv_last_ts(csv_path)
        if last_ts is None:
            _warn(f"{sym}: could not read last timestamp from {csv_path.name}")
            continue
        age = _trading_days_since(last_ts.date(), today_ist)
        if age > _STALE_THRESHOLD:
            _warn(
                f"{sym}: warm-start CSV is {age} trading days old ({csv_path.name})"
                f" — run scripts/refresh_mcb_warmstart_data.py"
            )
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

    # Capital
    if args.capital:
        env["PAPER_STARTING_CAPITAL"] = str(args.capital)
    elif "PAPER_STARTING_CAPITAL" not in env:
        env["PAPER_STARTING_CAPITAL"] = "300000"

    # Risk profile
    if "PAPER_RISK_PROFILE" not in env:
        env["PAPER_RISK_PROFILE"] = "balanced"

    # Terminal mode
    if "LIVE_TERMINAL_MODE" not in env:
        env["LIVE_TERMINAL_MODE"] = "compact"

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
    _capital = float(env.get("PAPER_STARTING_CAPITAL", "0") or 0)
    print(f"[CAPITAL CONFIG] starting_capital=₹{_capital:,.0f}")
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
