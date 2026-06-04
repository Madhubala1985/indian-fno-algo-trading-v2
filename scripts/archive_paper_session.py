#!/usr/bin/env python3
"""
scripts/archive_paper_session.py

Move finished paper-trading artifacts to logs/archive/session_<timestamp>/
so the next session starts with a clean slate.

Usage:
  python scripts/archive_paper_session.py --dry-run
  python scripts/archive_paper_session.py --yes

Safety:
  - Refuses to run when live_structure_pipeline.py is already running.
  - Never touches configs/, code, or data CSVs.
  - --dry-run shows what WOULD move; --yes performs the move.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
ARCHIVE_BASE = LOGS / "archive"

ARTIFACTS = [
    LOGS / "trades.db",
    LOGS / "paper_trading_state.json",
    LOGS / "paper_trades.csv",
    LOGS / "strike_selection_audit.jsonl",
    LOGS / "strike_selection_shadow.jsonl",
]
PID_FILE = LOGS / "live_pipeline.pid"

LIVE_SCRIPT = "live_structure_pipeline.py"


def _pipeline_running() -> bool:
    """Return True if live_structure_pipeline.py is running."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # check /proc/<pid>/cmdline on Linux
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode()
                if LIVE_SCRIPT in cmdline:
                    return True
        except Exception:
            pass

    # Belt-and-suspenders: check all processes
    try:
        out = subprocess.check_output(
            ["pgrep", "-fa", LIVE_SCRIPT], stderr=subprocess.DEVNULL
        ).decode()
        if out.strip():
            return True
    except subprocess.CalledProcessError:
        pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive paper trading artifacts before a new session."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Show what would be moved without doing it.")
    group.add_argument("--yes", action="store_true",
                       help="Perform the archive (moves files).")
    args = parser.parse_args()

    if _pipeline_running():
        print(
            "[ARCHIVE] REFUSED: live_structure_pipeline.py appears to be running.\n"
            "  Stop the pipeline before archiving to avoid data corruption."
        )
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_BASE / f"session_{timestamp}"

    # Collect artifacts that actually exist
    to_move: list[Path] = [a for a in ARTIFACTS if a.exists()]

    # PID file is included only when it appears stale (process not found above)
    if PID_FILE.exists():
        to_move.append(PID_FILE)

    if not to_move:
        print("[ARCHIVE] Nothing to archive — no artifacts found.")
        return 0

    print(f"[ARCHIVE] Destination: {dest}")
    print(f"[ARCHIVE] Files to move ({len(to_move)}):")
    for f in to_move:
        print(f"    {f.relative_to(ROOT)}")

    if args.dry_run:
        print("[ARCHIVE] Dry run — no files moved.")
        return 0

    # Perform the archive
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for f in to_move:
        target = dest / f.name
        shutil.move(str(f), str(target))
        moved.append(str(target.relative_to(ROOT)))

    print(f"[ARCHIVE] Moved {len(moved)} file(s):")
    for m in moved:
        print(f"    {m}")
    print("[ARCHIVE] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
