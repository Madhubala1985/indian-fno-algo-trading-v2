"""Concurrency stress test for the market-close / EOD-close locking design.

The live pipeline runs on_ticks (bar processing, account-state mutation) on the
Kite WS thread, while the main-thread watchdog runs the market-close / EOD
force-close on the same state. Both can trigger the shutdown. The pipeline
serializes them with:
  - _shutdown_lock (plain Lock): atomic check-then-set of _shutdown_triggered
    so EOD close runs exactly once.
  - _account_lock (RLock): held for the whole WS tick batch and re-entered by
    the bar-level shutdown path; the main thread blocks on it so EOD close never
    interleaves with a bar.

The pipeline module can't be imported (module-level run loop), so this models the
exact pattern and asserts the two properties that are easy to get wrong:
single EOD execution and deadlock-freedom under real thread contention.
"""
import threading
import time

_shutdown_lock = threading.Lock()
_account_lock = threading.RLock()


class _World:
    def __init__(self):
        self.shutdown_triggered = False
        self.eod_runs = 0
        self.equity = 100_000.0
        self.interleavings = 0  # nonzero => a bar mutated state during EOD close
        self._in_eod = False

    def handle_shutdown(self):
        # Atomic check-then-set (mirrors _handle_market_close_shutdown).
        with _shutdown_lock:
            if self.shutdown_triggered:
                return
            self.shutdown_triggered = True
        # Serialized EOD mutation (mirrors `with _account_lock: _eod_force_close_all()`).
        with _account_lock:
            self._in_eod = True
            self.eod_runs += 1
            self.equity += 1.0
            time.sleep(0.0005)  # widen the window to expose races
            self._in_eod = False

    def ws_batch(self, bar_is_close: bool):
        # Mirrors the on_ticks wrapper: whole batch under _account_lock.
        with _account_lock:
            # Per-bar account mutation; must never overlap an EOD close.
            if self._in_eod:
                self.interleavings += 1
            self.equity += 0.01
            time.sleep(0.0005)
            if bar_is_close:
                # Bar-level 15:30 check re-enters the RLock via handle_shutdown.
                self.handle_shutdown()


def test_eod_runs_exactly_once_and_no_interleave_under_contention():
    for _ in range(200):
        w = _World()
        threads = []
        # Several WS batches (some triggering close) + the main-thread clock close,
        # all racing.
        for i in range(6):
            threads.append(threading.Thread(target=w.ws_batch, args=(i % 2 == 0,)))
        threads.append(threading.Thread(target=w.handle_shutdown))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert all(not t.is_alive() for t in threads), "deadlock: a thread did not finish"
        assert w.eod_runs == 1, f"EOD ran {w.eod_runs} times (expected exactly 1)"
        assert w.interleavings == 0, "a bar mutated state during EOD close (interleave)"


def test_rlock_reentrancy_ws_path():
    # The WS path holds _account_lock then re-enters it via handle_shutdown —
    # a plain Lock would deadlock here; RLock must not.
    w = _World()
    done = threading.Event()

    def run():
        w.ws_batch(bar_is_close=True)  # acquires _account_lock, re-enters in shutdown
        done.set()

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "RLock reentrancy deadlocked the WS bar-level shutdown path"
    assert w.eod_runs == 1


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS {fn.__name__}")
        except Exception as exc:
            failed += 1; print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
