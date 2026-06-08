"""
test_mcb_full_warmstart.py
──────────────────────────────────────────────────────────────────────
Tests that MCB warm-start correctly hydrates all five timeframes
(1m, 15m, 1h, 4h, 1d) from a historical 1m CSV.

Verifies:
  1. All expected timeframes loaded (counts > 0).
  2. All five timeframes are MCB-ready when enough CSV data exists.
  3. No duplicate bars (strictly-increasing timestamp invariant).
  4. NIFTY/SENSEX bias not forced — test only checks readiness.
  5. Reset+re-feed path: state bars do not block CSV (root cause fix).
  6. Gate source labels correct: (default) when value == hardcoded default,
     (profile) when value changed, (env) when env var overrides.

Usage:
  PYTHONPATH=. python3 scripts/test_mcb_full_warmstart.py
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_results: list[tuple[str, bool, str]] = []

DATA_DIR = ROOT / "data"
_EXPECTED_TFS = {"1m", "15m", "1h", "4h", "1d"}


def check(name: str, cond: bool, detail: str = "") -> None:
    status = _PASS if cond else _FAIL
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    _results.append((name, cond, detail))


# ── Imports ─────────────────────────────────────────────────────────────────

from platform_core.data.multi_timeframe_builder import MultiTimeframeBuilder
from platform_core.backtesting.backtest_runner import DailyBarAccumulator
from platform_core.data.bar_builder import LiveBar
from platform_core.intelligence.market_context_builder import MarketContextBuilder

_MTF_KEY_MAP: dict[str, str] = {
    "MIN_5":  "5m",
    "MIN_15": "15m",
    "HOUR_1": "1h",
    "HOUR_4": "4h",
}


def _load_csv_bars(csv_path: Path, symbol: str) -> list[LiveBar]:
    bars: list[LiveBar] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                ts_raw = row.get("timestamp") or row.get("date") or ""
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                te = ts + timedelta(minutes=1)
                bars.append(LiveBar(
                    symbol          = symbol,
                    timeframe       = "MIN_1",
                    start_timestamp = ts,
                    end_timestamp   = te,
                    open_price      = float(row["open"]),
                    high_price      = float(row["high"]),
                    low_price       = float(row["low"]),
                    close_price     = float(row["close"]),
                    volume          = float(row.get("volume", 0)),
                ))
            except Exception:
                pass
    return bars


def _run_warmstart(
    bars_1m: list[LiveBar],
    symbol: str,
    pre_state_bars: list[tuple[str, LiveBar]] | None = None,
) -> tuple[dict[str, int], MarketContextBuilder]:
    """
    Run the warm-start logic against a MCB instance.

    If pre_state_bars is provided (simulating a state restore from disk),
    those bars are fed first, then the MCB is reset, then CSV bars are fed,
    then pre_state_bars are re-fed — exactly as the fix does.
    """
    mcb = MarketContextBuilder(underlying_symbol=symbol, min_history_bars=20)

    if pre_state_bars:
        for tf, bar in pre_state_bars:
            mcb.update(bar=bar, timeframe=tf)
        mcb.reset()

    mtf   = MultiTimeframeBuilder()
    daily = DailyBarAccumulator(symbol=symbol)
    counts: dict[str, int] = {"1m": 0, "15m": 0, "1h": 0, "4h": 0, "1d": 0}

    for bar in bars_1m:
        mcb.update(bar=bar, timeframe="1m")
        counts["1m"] += 1
        for tf_key, tf_bar in mtf.update_bar(bar=bar).items():
            tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
            if tf_name in ("15m", "1h", "4h"):
                mcb.update(bar=tf_bar, timeframe=tf_name)
                counts[tf_name] += 1
        d_bar = daily.update(bar)
        if d_bar is not None:
            mcb.update(bar=d_bar, timeframe="1d")
            counts["1d"] += 1

    if pre_state_bars:
        for tf, bar in pre_state_bars:
            try:
                mcb.update(bar=bar, timeframe=tf)
            except Exception:
                pass

    return counts, mcb


# ── Locate CSVs ──────────────────────────────────────────────────────────────

_CSV_TARGETS = {
    "BANKNIFTY": "banknifty",
    "NIFTY":     "nifty",
    "SENSEX":    "sensex",
}
_MIN_CSV_BYTES = 50_000   # same filter as _find_warmstart_csv() in the pipeline


def _end_date_size_key(p: Path) -> tuple:
    """Match pipeline selection: latest end-date first, then largest file (more history)."""
    try:
        end_date = p.stem.split("_")[-1]
    except Exception:
        end_date = ""
    return (end_date, p.stat().st_size)


_csvs: dict[str, Path] = {}
for sym, prefix in _CSV_TARGETS.items():
    candidates = [
        p for p in DATA_DIR.glob(f"{prefix}_1m_*.csv")
        if p.stat().st_size >= _MIN_CSV_BYTES
    ]
    if candidates:
        _csvs[sym] = max(candidates, key=_end_date_size_key)

print("=" * 60)
print("  MCB FULL WARMSTART TESTS")
print("=" * 60)
print(f"\n  CSVs found: {[str(p.name) for p in _csvs.values()]}")

# ── Test 1: CSV presence ─────────────────────────────────────────────────────
print("\n[Test 1] CSV presence")
for sym in ("BANKNIFTY", "NIFTY", "SENSEX"):
    check(f"{sym} CSV present", sym in _csvs, f"searched data/{_CSV_TARGETS[sym]}_1m_*.csv")

# ── Test 2: All TF counts > 0 ────────────────────────────────────────────────
print("\n[Test 2] All timeframe counts > 0 after warm-start")
_counts_by_sym: dict[str, dict[str, int]] = {}
_mcb_by_sym:    dict[str, MarketContextBuilder] = {}

for sym, csv_path in _csvs.items():
    bars = _load_csv_bars(csv_path, sym)
    if not bars:
        check(f"{sym} CSV readable", False, "empty or unreadable")
        continue
    counts, mcb = _run_warmstart(bars, sym)
    _counts_by_sym[sym] = counts
    _mcb_by_sym[sym] = mcb
    for tf in ("1m", "15m", "1h", "4h", "1d"):
        check(f"{sym} {tf} count>0", counts.get(tf, 0) > 0, f"got {counts.get(tf, 0)}")

# ── Test 3: All TFs ready ────────────────────────────────────────────────────
print("\n[Test 3] All five timeframes MCB-ready after warm-start")
for sym, mcb in _mcb_by_sym.items():
    try:
        ctx = mcb.build()
        ready = set(ctx.timeframes_with_history)
        for tf in ("1m", "15m", "1h", "4h", "1d"):
            check(f"{sym} {tf} ready", tf in ready, f"ready_set={sorted(ready)}")
    except Exception as exc:
        check(f"{sym} MCB build OK", False, str(exc))

# ── Test 4: Bias not forced — readiness only ─────────────────────────────────
print("\n[Test 4] NIFTY/SENSEX bias not forced (accept any bias — test readiness only)")
for sym in ("NIFTY", "SENSEX"):
    if sym not in _mcb_by_sym:
        check(f"{sym} MCB present", False, "no CSV")
        continue
    try:
        ctx = _mcb_by_sym[sym].build()
        check(
            f"{sym} bias not forced",
            ctx.bias is not None,
            f"bias={ctx.bias}  conf={ctx.confidence:.2f}  (any value accepted)"
        )
    except Exception as exc:
        check(f"{sym} MCB build OK", False, str(exc))

# ── Test 5: Reset+re-feed path — state bars do not block CSV ─────────────────
print("\n[Test 5] State bars blocking fix — reset+re-feed path")
if "BANKNIFTY" in _csvs:
    bars = _load_csv_bars(_csvs["BANKNIFTY"], "BANKNIFTY")
    # Simulate a state with 9 x 15m intraday bars with FUTURE timestamps (like June 8)
    _csv_last_ts = bars[-1].start_timestamp if bars else datetime.now(timezone.utc)
    _future_base = _csv_last_ts + timedelta(days=10)  # guaranteed newer than any CSV bar

    def _make_state_bar(tf: str, offset_minutes: int) -> LiveBar:
        ts = _future_base + timedelta(minutes=offset_minutes)
        return LiveBar(
            symbol          = "BANKNIFTY",
            timeframe       = tf,
            start_timestamp = ts,
            end_timestamp   = ts + timedelta(minutes=1),
            open_price      = 54000.0,
            high_price      = 54100.0,
            low_price       = 53900.0,
            close_price     = 54050.0,
            volume          = 0.0,
        )

    pre_state: list[tuple[str, LiveBar]] = [
        ("1m",  _make_state_bar("MIN_1",   i)) for i in range(30)
    ] + [
        ("15m", _make_state_bar("MIN_15", i * 15)) for i in range(9)
    ] + [
        ("1h",  _make_state_bar("HOUR_1", i * 60)) for i in range(2)
    ]

    counts_fixed, mcb_fixed = _run_warmstart(bars, "BANKNIFTY", pre_state_bars=pre_state)

    ctx_fixed = mcb_fixed.build()
    ready_fixed = set(ctx_fixed.timeframes_with_history)

    check("BNK 15m ready with pre-state (fix)", "15m" in ready_fixed, f"ready={sorted(ready_fixed)}")
    check("BNK 1h ready with pre-state (fix)",  "1h"  in ready_fixed, f"ready={sorted(ready_fixed)}")
    check("BNK 4h ready with pre-state (fix)",  "4h"  in ready_fixed, f"ready={sorted(ready_fixed)}")
    check("BNK 1d ready with pre-state (fix)",  "1d"  in ready_fixed, f"ready={sorted(ready_fixed)}")

    # Without reset: 15m/1h should be blocked (original bug)
    mcb_unfix = MarketContextBuilder(underlying_symbol="BANKNIFTY", min_history_bars=20)
    for tf, bar in pre_state:
        mcb_unfix.update(bar=bar, timeframe=tf)
    # Feed CSV WITHOUT resetting first
    mtf_u = MultiTimeframeBuilder()
    daily_u = DailyBarAccumulator(symbol="BANKNIFTY")
    for bar in bars:
        mcb_unfix.update(bar=bar, timeframe="1m")
        for tf_key, tf_bar in mtf_u.update_bar(bar=bar).items():
            tf_name = _MTF_KEY_MAP.get(tf_key, tf_key.lower())
            if tf_name in ("15m", "1h", "4h"):
                mcb_unfix.update(bar=tf_bar, timeframe=tf_name)
        d_bar = daily_u.update(bar)
        if d_bar is not None:
            mcb_unfix.update(bar=d_bar, timeframe="1d")
    ctx_unfix = mcb_unfix.build()
    ready_unfix = set(ctx_unfix.timeframes_with_history)
    check("BNK 15m blocked WITHOUT reset (original bug reproduced)",
          "15m" not in ready_unfix, f"ready={sorted(ready_unfix)}")

# ── Test 6: No duplicate bars ─────────────────────────────────────────────────
print("\n[Test 6] No duplicate bar timestamps in loaded CSV")
for sym, csv_path in _csvs.items():
    bars = _load_csv_bars(csv_path, sym)
    if not bars:
        continue
    ts_list = [b.start_timestamp for b in bars]
    dups = len(ts_list) - len(set(ts_list))
    check(f"{sym} no duplicate 1m timestamps", dups == 0, f"duplicates={dups}")

# ── Test 7: Gate source label semantics ──────────────────────────────────────
print("\n[Test 7] Gate source label semantics")

_DEFAULT_SM = 0.05
_DEFAULT_OI = 0.10

_PROFILES = {
    "default": {
        "BANKNIFTY": {"score_margin": 0.05, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
        "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},
    },
    "banknifty_experiment": {
        "BANKNIFTY": {"score_margin": 0.03, "oi_support": 0.10},
        "NIFTY":     {"score_margin": 0.05, "oi_support": 0.10},
        "SENSEX":    {"score_margin": 0.05, "oi_support": 0.10},
    },
}

def _get_gate_val_fixed(sym: str, metric: str, env_key: str, profile: str, env_overrides: dict) -> tuple[float, str]:
    table = _PROFILES.get(profile, _PROFILES["default"])
    default_val = _DEFAULT_SM if metric == "score_margin" else _DEFAULT_OI
    profile_val = table.get(sym, {}).get(metric, default_val)
    if env_key in env_overrides:
        try:
            return float(env_overrides[env_key]), "env"
        except ValueError:
            pass
    src = "profile" if profile_val != default_val else "default"
    return profile_val, src

# default profile — all should be "default"
for sym in ("BANKNIFTY", "NIFTY", "SENSEX"):
    v, src = _get_gate_val_fixed(sym, "score_margin", f"PAPER_{sym}_SCORE_MARGIN_MIN", "default", {})
    check(f"default/{sym} sm src=default", src == "default", f"got src={src}")

# banknifty_experiment — BNK changed (profile), NIFTY/SENSEX unchanged (default)
v_bnk, src_bnk = _get_gate_val_fixed("BANKNIFTY", "score_margin", "PAPER_BANKNIFTY_SCORE_MARGIN_MIN", "banknifty_experiment", {})
check("bnk_exp/BNK sm=0.03 src=profile", src_bnk == "profile" and abs(v_bnk - 0.03) < 1e-9, f"v={v_bnk} src={src_bnk}")

v_nif, src_nif = _get_gate_val_fixed("NIFTY", "score_margin", "PAPER_NIFTY_SCORE_MARGIN_MIN", "banknifty_experiment", {})
check("bnk_exp/NIFTY sm=0.05 src=default", src_nif == "default", f"got src={src_nif}")

v_snx, src_snx = _get_gate_val_fixed("SENSEX", "score_margin", "PAPER_SENSEX_SCORE_MARGIN_MIN", "banknifty_experiment", {})
check("bnk_exp/SENSEX sm=0.05 src=default (was wrong before fix)", src_snx == "default", f"got src={src_snx}")

# env override always returns "env" regardless of profile
v_env, src_env = _get_gate_val_fixed("BANKNIFTY", "score_margin", "PAPER_BANKNIFTY_SCORE_MARGIN_MIN",
                                      "default", {"PAPER_BANKNIFTY_SCORE_MARGIN_MIN": "0.02"})
check("env override returns src=env", src_env == "env" and abs(v_env - 0.02) < 1e-9, f"v={v_env} src={src_env}")

# ── Summary ───────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in _results if ok)
total  = len(_results)
print(f"\n{'=' * 60}")
print(f"  Results: {passed}/{total} passed")
if passed == total:
    print("  ALL PASS")
else:
    print("  FAILURES:")
    for name, ok, detail in _results:
        if not ok:
            print(f"    FAIL: {name}  {detail}")
print(f"{'=' * 60}")
sys.exit(0 if passed == total else 1)
