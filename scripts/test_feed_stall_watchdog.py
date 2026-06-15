#!/usr/bin/env python3
"""
scripts/test_feed_stall_watchdog.py

Offline tests for the tick-staleness watchdog added to KiteAdapter (2026-06-15).

Background: a silent Kite WebSocket stall (socket stays open but no data flows)
does NOT fire on_close, so the adapter's reconnect machinery never triggers.
The pipeline's main-loop watchdog polls KiteAdapter.seconds_since_last_tick()
and calls force_reconnect() during market hours when the feed goes quiet.

These tests exercise the adapter API without any network / KiteConnect session.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.data.live.kite_adapter import KiteAdapter


def _bare_adapter() -> KiteAdapter:
    """Build a KiteAdapter without running __init__ (no config/credentials)."""
    a = KiteAdapter.__new__(KiteAdapter)
    a._last_tick_ts = 0.0
    a._stopping = False
    a._reconnecting = False
    a._reconnect_lock = threading.Lock()
    a._reconnect_count = 0
    a._connected = True
    return a


def main() -> int:
    passed = 0
    failed = 0

    def check(name: str, cond: bool) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")

    print("== feed-stall watchdog tests ==")

    # 1) Never ticked -> inf (distinguishes "never" from "recent").
    a = _bare_adapter()
    check("seconds_since_last_tick = inf before first tick",
          a.seconds_since_last_tick() == float("inf"))

    # 2) Recent tick -> small gap.
    a._last_tick_ts = time.time() - 5.0
    check("reports ~5s gap after a recent tick",
          4.0 < a.seconds_since_last_tick() < 7.0)

    # 3) force_reconnect schedules a reconnect, marks down, resets tick clock.
    calls: list[str] = []
    a._schedule_reconnect = lambda: calls.append("scheduled")  # type: ignore[method-assign]
    a.force_reconnect("test stall")
    check("force_reconnect schedules a reconnect", calls == ["scheduled"])
    check("force_reconnect marks connection down", a._connected is False)
    check("force_reconnect resets the tick clock", a.seconds_since_last_tick() < 2.0)

    # 4) No-op while shutting down.
    calls.clear()
    a._stopping = True
    a.force_reconnect("during shutdown")
    check("force_reconnect is a no-op while stopping", calls == [])

    # 5) No-op while a reconnect is already in flight (storm protection).
    calls.clear()
    a._stopping = False
    a._reconnecting = True
    a.force_reconnect("already reconnecting")
    check("force_reconnect is a no-op while reconnect in flight", calls == [])

    print(f"\n== {passed} passed, {failed} failed ==")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
