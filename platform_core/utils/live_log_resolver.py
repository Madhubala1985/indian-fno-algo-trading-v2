"""
platform_core.utils.live_log_resolver
═══════════════════════════════════════════════════════════════════════
Single source of truth for resolving the "current" live session log.

Problem solved:
  logs/current_live_log.txt is a pointer file holding the path of the
  active (or most recent) live_run_*.log.  After log archival the pointer
  can reference a file that no longer exists, which broke post-market
  tooling (grep against a missing path).

Resolution rules (resolve_current_live_log):
  1. If the pointer file exists and the referenced log exists → use it.
  2. If the pointer is stale (referenced log missing) → fall back to the
     newest existing logs/live_run_*.log, rewrite the pointer, and print:
       [CURRENT LOG FIX] stale path=<old> replaced_with=<new>
  3. If the pointer file does not exist → use the newest live_run_*.log
     and (re)create the pointer.
  4. If no live_run_*.log exists at all → return None; callers must fail
     with a clear message.

Never raises; never touches anything outside logs/current_live_log.txt.
"""
from __future__ import annotations

from pathlib import Path

POINTER_NAME = "current_live_log.txt"


def _newest_live_log(logs_dir: Path) -> Path | None:
    try:
        candidates = sorted(
            logs_dir.glob("live_run_*.log"),
            key=lambda p: p.stat().st_mtime,
        )
        return candidates[-1] if candidates else None
    except Exception:
        return None


def _read_pointer(pointer: Path, root: Path) -> Path | None:
    """Return the path referenced by the pointer file, or None."""
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    ref = Path(raw)
    if not ref.is_absolute():
        ref = root / ref
    return ref


def write_current_live_log(logs_dir: Path, log_path: Path) -> None:
    """Point logs/current_live_log.txt at log_path (relative to repo root)."""
    try:
        root = logs_dir.parent
        try:
            rel = log_path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = log_path
        (logs_dir / POINTER_NAME).write_text(f"{rel}\n", encoding="utf-8")
    except Exception:
        pass


def resolve_current_live_log(
    logs_dir: Path,
    *,
    update_pointer: bool = True,
) -> Path | None:
    """
    Return the path of the current live log, repairing a stale pointer.

    Returns None only when no logs/live_run_*.log exists at all.
    """
    logs_dir = Path(logs_dir)
    root     = logs_dir.parent
    pointer  = logs_dir / POINTER_NAME

    referenced = _read_pointer(pointer, root) if pointer.exists() else None

    if referenced is not None and referenced.exists():
        return referenced

    fallback = _newest_live_log(logs_dir)
    if fallback is None:
        if referenced is not None:
            print(
                f"[CURRENT LOG ERROR] {POINTER_NAME} references missing file "
                f"{referenced} and no logs/live_run_*.log exists to fall back to."
            )
        return None

    if referenced is not None:
        # Stale pointer — repair it
        print(
            f"[CURRENT LOG FIX] stale path={referenced} "
            f"replaced_with={fallback}"
        )
    if update_pointer:
        write_current_live_log(logs_dir, fallback)
    return fallback


__all__ = ["resolve_current_live_log", "write_current_live_log", "POINTER_NAME"]
