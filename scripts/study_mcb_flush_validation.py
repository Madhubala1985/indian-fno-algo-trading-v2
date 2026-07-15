"""
study_mcb_flush_validation.py — Night study 2026-07-15 (Priority A)
──────────────────────────────────────────────────────────────────────
Validates the MCB warm-start final-day flush fix (bug #2, deployed
2026-07-15 between the 14:25 and 18:51 restarts) across ALL recorded
warm-start vintages.

For each recorded warm-start CSV vintage (the exact file each live
morning loaded) and each underlying:
  1. Rebuild the boot-time MCB state by feeding the vintage's 1m bars
     through a private MultiTimeframeBuilder + DailyBarAccumulator —
     byte-for-byte the live _warm_start_mcb_from_csv() path (state-bar
     re-feed skipped; those bars duplicate the CSV's last day).
  2. Variant NOFLUSH = old live behavior (final CSV day never reaches
     the 1d classifier). Variant FLUSH = deployed fix (accumulator
     flushed into 1d). NOFLUSH must reproduce the bias/conf printed in
     that morning's live log (ground-truth QA).
  3. Feed the actual session day's 1m bars into both variants
     (fresh session MTF builder, like live) and track bias per minute.
  4. Cross-reference logs/trades.db pt_trades entries and
     pt_decisions gate rows for that day/underlying against the
     per-minute bias of both variants.

Read-only outside stdout. No live-path modules are modified.

Usage:
  PYTHONPATH=. venv/bin/python scripts/study_mcb_flush_validation.py
"""
from __future__ import annotations

import copy
import csv
import glob
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from platform_core.data.bar_builder import LiveBar
from platform_core.data.multi_timeframe_builder import MultiTimeframeBuilder
from platform_core.backtesting.backtest_runner import DailyBarAccumulator
from platform_core.intelligence.market_context_builder import MarketContextBuilder

_IST = timezone(timedelta(hours=5, minutes=30))

_MTF_KEY_MAP = {"MIN_15": "15m", "HOUR_1": "1h", "HOUR_4": "4h"}

_UNDERLYINGS = {
    "BANKNIFTY": "banknifty",
    "NIFTY":     "nifty",
    "SENSEX":    "sensex",
}
_FEED = {"BANKNIFTY": "BANKNIFTY", "NIFTY": "NIFTY50", "SENSEX": "SENSEX"}


def _load_bars(path: Path, feed: str) -> list[LiveBar]:
    bars: list[LiveBar] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bars.append(LiveBar(
                    symbol=feed, timeframe="MIN_1",
                    start_timestamp=ts, end_timestamp=ts + timedelta(minutes=1),
                    open_price=float(row["open"]), high_price=float(row["high"]),
                    low_price=float(row["low"]), close_price=float(row["close"]),
                    volume=float(row.get("volume") or 0)))
            except Exception:
                pass
    return bars


def _warm_boot(bars: list[LiveBar], underlying: str, feed: str):
    """Replicate live _warm_start_mcb_from_csv for one file. Returns
    (ctx_noflush, ctx_flush) MarketContextBuilder instances at boot."""
    ctx = MarketContextBuilder(underlying_symbol=underlying)
    mtf = MultiTimeframeBuilder()
    daily = DailyBarAccumulator(symbol=feed)
    for b in bars:
        ctx.update(bar=b, timeframe="1m")
        for tf_key, tf_bar in mtf.update_bar(bar=b).items():
            tf_name = _MTF_KEY_MAP.get(tf_key)
            if tf_name:
                ctx.update(bar=tf_bar, timeframe=tf_name)
        db = daily.update(b)
        if db is not None:
            ctx.update(bar=db, timeframe="1d")
    # Variant split: deepcopy state, flush final day into the FLUSH copy.
    ctx_flush = copy.deepcopy(ctx)
    final = copy.deepcopy(daily).flush()
    if final is not None:
        ctx_flush.update(bar=final, timeframe="1d")
    return ctx, ctx_flush


def _describe(ctx: MarketContextBuilder) -> str:
    c = ctx.build()
    d = c.daily
    return (f"bias={c.bias:<7} conf={c.confidence:.2f} trend={c.trend_score:+.2f} "
            f"1d_dir={d.direction if d else '—'}")


def _session_bias_series(ctx: MarketContextBuilder, session: list[LiveBar]):
    """Feed session 1m bars (fresh session MTF, like live boot) and return
    [(ist_hhmm, bias, conf)] per minute."""
    mtf = MultiTimeframeBuilder()
    out = []
    for b in session:
        ctx.update(bar=b, timeframe="1m")
        for tf_key, tf_bar in mtf.update_bar(bar=b).items():
            tf_name = _MTF_KEY_MAP.get(tf_key)
            if tf_name:
                ctx.update(bar=tf_bar, timeframe=tf_name)
        c = ctx.build()
        out.append((b.start_timestamp.astimezone(_IST).strftime("%H:%M"),
                    str(c.bias), c.confidence))
    return out


def _diff_windows(sa, sb) -> list[str]:
    """Compress minutes where bias differs into windows."""
    win, cur = [], None
    for (t, ba, _), (_, bb, _) in zip(sa, sb):
        if ba != bb:
            if cur is None:
                cur = [t, t, ba, bb]
            else:
                cur[1] = t
        elif cur is not None:
            win.append(f"{cur[0]}–{cur[1]} ({cur[2]}→{cur[3]})")
            cur = None
    if cur is not None:
        win.append(f"{cur[0]}–{cur[1]} ({cur[2]}→{cur[3]})")
    return win


def main() -> int:
    dbc = sqlite3.connect("logs/trades.db")

    # Recorded vintages: end-date -> session day it served (next trading day).
    vintages: dict[str, dict[str, Path]] = {}
    for und, prefix in _UNDERLYINGS.items():
        for p in sorted(glob.glob(f"data/{prefix}_1m_*.csv")):
            end = Path(p).stem.split("_")[-1]
            vintages.setdefault(end, {})[und] = Path(p)

    for end_date in sorted(vintages):
        files = vintages[end_date]
        end_d = date.fromisoformat(end_date)
        # session day = next weekday after the vintage's end date
        sd = end_d + timedelta(days=1)
        while sd.weekday() >= 5:
            sd += timedelta(days=1)
        print(f"\n{'='*78}\nVINTAGE ending {end_date}  →  served session {sd}\n{'='*78}")

        for und, path in sorted(files.items()):
            feed = _FEED[und]
            bars = _load_bars(path, feed)
            if not bars:
                print(f"  {und}: no bars in {path.name}")
                continue
            ctx_nf, ctx_fl = _warm_boot(bars, und, feed)
            print(f"  {und:<10} boot NOFLUSH: {_describe(ctx_nf)}")
            print(f"  {und:<10} boot FLUSH  : {_describe(ctx_fl)}")

            # Session bars for day sd: only exist in a LATER vintage file.
            later = [vintages[e][und] for e in sorted(vintages)
                     if e >= sd.isoformat() and und in vintages[e]]
            session = []
            if later:
                allb = _load_bars(later[0], feed)
                session = [b for b in allb
                           if b.start_timestamp.astimezone(_IST).date() == sd]
            if not session:
                print(f"  {und:<10} session {sd}: no bars available — boot diff only")
                continue

            sa = _session_bias_series(ctx_nf, session)
            sb = _session_bias_series(ctx_fl, session)
            ndiff = sum(1 for x, y in zip(sa, sb) if x[1] != y[1])
            wins = _diff_windows(sa, sb)
            print(f"  {und:<10} session {sd}: {ndiff}/{len(sa)} minutes bias differs"
                  + (f"  windows: {'; '.join(wins[:6])}" if wins else ""))

            # Cross-reference actual trades / gate decisions that day.
            biasmap_nf = {t: b for t, b, _ in sa}
            biasmap_fl = {t: b for t, b, _ in sb}
            day_lo = f"{sd}T00:00"
            day_hi = f"{sd}T23:59"
            for ets, direction, otype, pnl in dbc.execute(
                    "SELECT entry_ts, direction, option_type, pnl FROM pt_trades "
                    "WHERE underlying=? AND entry_ts BETWEEN ? AND ?",
                    (und, day_lo, day_hi)):
                m = datetime.fromisoformat(ets).astimezone(_IST).strftime("%H:%M")
                bnf, bfl = biasmap_nf.get(m, "?"), biasmap_fl.get(m, "?")
                changed = "WOULD CHANGE" if bnf != bfl else "unchanged"
                print(f"    TRADE {m} {direction} {otype} pnl=₹{pnl:+,.0f}  "
                      f"bias noflush={bnf} flush={bfl}  → {changed}")
            for bts, bias, outcome in dbc.execute(
                    "SELECT bar_ts, bias, outcome FROM pt_decisions "
                    "WHERE underlying=? AND bar_ts BETWEEN ? AND ? AND outcome IN "
                    "('CONTEXT_CONTRADICTION_REJECTED','ENTRY_TIER2','ENTRY_TIER3')",
                    (und, day_lo, day_hi)):
                m = datetime.fromisoformat(bts).astimezone(_IST).strftime("%H:%M")
                bnf, bfl = biasmap_nf.get(m, "?"), biasmap_fl.get(m, "?")
                if outcome != "ENTRY_TIER2":  # entries already covered via pt_trades
                    changed = "WOULD CHANGE" if bnf != bfl else "unchanged"
                    print(f"    BLOCK {m} {outcome} (logged bias={bias})  "
                          f"noflush={bnf} flush={bfl}  → {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
