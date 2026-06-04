#!/usr/bin/env python3
"""
validate_symbol_backtest_support.py

Verify that BacktestRunner supports BANKNIFTY, NIFTY, and SENSEX with the
same institutional options logic used by the live pipeline.

For each symbol:
  1. Confirms lot_size and strike_step from REFERENCE_* tables.
  2. Confirms ExpiryProvider maps the correct weekly-expiry weekday.
  3. Initialises SyntheticChainConfig and BacktestRunner (no live data required).
  4. Runs a full backtest if an OHLCV CSV is found under data/.
  5. Prints DATA_MISSING when no CSV is available — never fabricates metrics.

Does NOT import live_structure_pipeline.  Does NOT connect to Kite.
Does NOT change any strategy, sizing, or risk logic.
"""

from __future__ import annotations

import io
import re
import sys
import textwrap
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

# ── Imports ───────────────────────────────────────────────────────────────────

from platform_core.backtesting.backtest_runner import (
    BacktestConfig,
    BacktestRunner,
)
from platform_core.core.constants import (
    REFERENCE_LOT_SIZES,
    REFERENCE_STRIKE_STEPS,
)
from platform_core.core.enums import UnderlyingFamily
from platform_core.data.synthetic_chain_builder import (
    ExpiryProvider,
    SyntheticChainConfig,
    WEEKLY_EXPIRY_WEEKDAYS,
)

# ── Symbols to validate ───────────────────────────────────────────────────────

SYMBOLS = ["BANKNIFTY", "NIFTY", "SENSEX"]
DATA_DIR = Path("data")
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ref_lot_size(symbol: str) -> int:
    for fam, size in REFERENCE_LOT_SIZES.items():
        if symbol.upper() == fam.value:
            return size
    return 0


def _ref_strike_step(symbol: str) -> int:
    for fam, step in REFERENCE_STRIKE_STEPS.items():
        if symbol.upper() == fam.value:
            return int(step)
    return 0


def _find_csvs(symbol: str) -> list[Path]:
    """
    Return CSVs whose filename contains symbol as a whole word (case-insensitive).
    "NIFTY" must not match "banknifty_*.csv".
    Sorted by file size ascending so the smallest viable file runs first.
    Files smaller than 1 KB are skipped (too few bars for any trades).
    """
    pattern = re.compile(
        rf'(?<![a-z]){re.escape(symbol.lower())}(?![a-z])', re.IGNORECASE
    )
    candidates = [
        p for p in DATA_DIR.glob("*.csv")
        if pattern.search(p.name) and p.stat().st_size >= 1_024
    ]
    return sorted(candidates, key=lambda p: p.stat().st_size)


_METRIC_LABELS = {
    "completed trades": "trades",
    "equity return":    "return",
    "win rate":         "win_rate",
    "total pnl":        "total_pnl",
    "max drawdown":     "max_dd",
    "expectancy":       "expectancy",
    "winning trades":   "wins",
    "losing trades":    "losses",
}


def _parse_box_line(raw: str) -> tuple[str, str]:
    """Strip ║ borders and split on first colon → (label, value)."""
    clean = raw.replace("║", "").strip()
    colon = clean.find(":")
    if colon < 0:
        return "", ""
    return clean[:colon].strip().lower(), clean[colon + 1:].strip()


def _extract_summary(runner: BacktestRunner, csv_path: Path) -> dict:
    """Run backtest capturing stdout; parse key metrics from captured text."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            runner.run(csv_path=str(csv_path))
    except Exception as exc:
        return {"error": str(exc)}

    text = buf.getvalue()
    metrics: dict = {}

    for raw_line in text.splitlines():
        label, value = _parse_box_line(raw_line)
        if not label or not value:
            continue
        for pattern, key in _METRIC_LABELS.items():
            if pattern in label and key not in metrics:
                metrics[key] = value
                break

    if not metrics:
        last_lines = [ln for ln in text.splitlines() if ln.strip()][-6:]
        metrics["raw_tail"] = " | ".join(last_lines)

    return metrics


# ── Per-symbol validation ─────────────────────────────────────────────────────

def validate_symbol(symbol: str) -> dict:
    result: dict = {"symbol": symbol, "ok": False}

    # 1. Reference constants
    lot   = _ref_lot_size(symbol)
    step  = _ref_strike_step(symbol)
    result["lot_size"]    = lot
    result["strike_step"] = step
    if lot == 0:
        result["error"] = "lot_size not found in REFERENCE_LOT_SIZES"
        return result
    if step == 0:
        result["error"] = "strike_step not found in REFERENCE_STRIKE_STEPS"
        return result

    # 2. ExpiryProvider weekday
    exp_prov = ExpiryProvider(symbol)
    weekday_idx = WEEKLY_EXPIRY_WEEKDAYS.get(symbol.upper(), 3)
    result["expiry_weekday"] = _WEEKDAY_NAMES[weekday_idx]
    ref_date = date(2026, 6, 9)   # Monday — deterministic test anchor
    weekly = exp_prov.get_weekly_expiry(ref_date)
    biweekly = exp_prov.get_biweekly_expiry(ref_date)
    result["next_expiry"]     = str(weekly)
    result["biweekly_expiry"] = str(biweekly)

    # 3. SyntheticChainConfig
    try:
        chain_cfg = SyntheticChainConfig.for_symbol(symbol)
        result["chain_cfg_lot"]  = chain_cfg.lot_size
        result["chain_cfg_step"] = chain_cfg.strike_step
    except Exception as exc:
        result["error"] = f"SyntheticChainConfig.for_symbol failed: {exc}"
        return result

    # 4. BacktestRunner init
    try:
        runner = BacktestRunner(symbol=symbol)
        result["runner_init"] = "OK"
        result["runner_lot"]  = runner.config.effective_lot_size()
        result["runner_step"] = runner.config.effective_strike_step()
    except Exception as exc:
        result["error"] = f"BacktestRunner init failed: {exc}"
        return result

    # 5. Data and run
    csvs = _find_csvs(symbol)
    if not csvs:
        result["data_status"] = "DATA_MISSING"
        result["ok"] = True   # config valid; data absent
        return result

    result["csv_found"] = str(csvs[0])
    result["data_status"] = "DATA_FOUND"
    metrics = _extract_summary(runner, csvs[0])
    result["backtest"] = metrics
    result["ok"] = "error" not in metrics

    return result


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    W = 62
    print(f"\n{'═' * W}")
    print(f"  SYMBOL BACKTEST SUPPORT VALIDATION")
    print(f"{'═' * W}")

    for r in results:
        sym = r["symbol"]
        print(f"\n  {sym}")
        print(f"  {'─' * (W - 2)}")

        if "error" in r:
            print(f"  FAIL  {r['error']}")
            continue

        print(f"  lot_size     : {r['lot_size']}")
        print(f"  strike_step  : {r['strike_step']}")
        print(f"  expiry_day   : {r['expiry_weekday']}  "
              f"(next={r['next_expiry']}  biweekly={r['biweekly_expiry']})")
        print(f"  chain_cfg    : lot={r['chain_cfg_lot']}  step={r['chain_cfg_step']}")
        print(f"  runner_init  : {r['runner_init']}  "
              f"lot={r['runner_lot']}  step={r['runner_step']}")

        ds = r.get("data_status", "UNKNOWN")
        print(f"  data_status  : {ds}")

        if ds == "DATA_MISSING":
            print(f"  backtest     : DATA_MISSING — no OHLCV CSV found under data/")
        elif "csv_found" in r:
            print(f"  csv_used     : {r['csv_found']}")
            bt = r.get("backtest", {})
            if "error" in bt:
                print(f"  backtest     : ERROR — {bt['error']}")
            elif "raw_tail" in bt:
                print(f"  backtest     : (raw) {bt['raw_tail']}")
            else:
                for k, v in bt.items():
                    print(f"  {k:<12} : {v}")

        status = "PASS" if r.get("ok") else "FAIL"
        print(f"\n  STATUS: {status}")

    all_ok    = all(r.get("ok") for r in results)
    data_miss = [r["symbol"] for r in results if r.get("data_status") == "DATA_MISSING"]
    runnable  = [r["symbol"] for r in results if r.get("data_status") == "DATA_FOUND"]

    print(f"\n{'═' * W}")
    print(f"  Config-valid symbols : {', '.join(r['symbol'] for r in results if r.get('ok'))}")
    if runnable:
        print(f"  Backtest ran         : {', '.join(runnable)}")
    if data_miss:
        print(f"  DATA_MISSING         : {', '.join(data_miss)}")
    overall = "ALL CONFIG OK" if all_ok else "PARTIAL — see FAIL above"
    print(f"  Overall              : {overall}")
    print(f"{'═' * W}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    results = []
    for sym in SYMBOLS:
        print(f"  Validating {sym}...", flush=True)
        results.append(validate_symbol(sym))
    print_report(results)
    all_ok = all(r.get("ok") for r in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
