"""
batch_replay.py — Phase-B3 batch validation runner (real-chain).

Loops the surgical replay (surgical_replay.SurgicalReplay, fidelity v2) over MANY
real historical days/underlyings and AGGREGATES the results, so the model can be
validated holistically on real option premiums instead of one live session/day.

This is the first real validation of Part-1-A. It aggregates toward:
  A1 — win-rate + P&L on REAL premiums (per underlying, per day).
  conviction A/B — does a conviction floor separate winners out-of-sample?
                   (run_batch twice, floor=None vs floor=X, compare — see
                   scripts/replay_batch.py --ab).
  conviction separation — winners' mean conviction vs losers' (diagnostic).

DATA LIMIT (inherited from B1): the Kite instrument dump is current-only, so only
days whose expiry is STILL LISTED are fetchable. Un-fetchable days are SKIPPED and
reported (older days need the Approach-A live recorder). Cached days replay offline.

NOTE (fidelity watch-item, 2026-07-01): the replay MCB bias can peg LONG on strongly
trending recent windows (higher-TF weight saturates trend_score>0.18). The batch is
exactly how we judge whether that actually costs P&L — read per-day bias-vs-move, do
NOT hand-tune on a few days.
"""
from __future__ import annotations

import csv as _csv
import glob
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from platform_core.backtesting.backtest_runner import LiveBar
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher
from platform_core.backtesting.surgical_replay import SurgicalReplay, ReplayResult

# underlying → feed symbol / exchange / strike step / lot size (mirrors replay_day)
UNDERLYING_CFG: dict[str, dict] = {
    "SENSEX":    {"feed": "SENSEX",    "exch": "BFO", "step": 100.0, "lot": 20},
    "NIFTY":     {"feed": "NIFTY50",   "exch": "NFO", "step": 50.0,  "lot": 75},
    "BANKNIFTY": {"feed": "BANKNIFTY", "exch": "NFO", "step": 100.0, "lot": 30},
}
_CSV_PREFIX = {"SENSEX": "sensex", "NIFTY": "nifty", "BANKNIFTY": "banknifty"}


def load_index_csv(underlying: str) -> list[LiveBar]:
    """Load the newest 1m index CSV for `underlying` as LiveBars (all days)."""
    feed = UNDERLYING_CFG[underlying]["feed"]
    files = [Path(p) for p in glob.glob(f"data/{_CSV_PREFIX[underlying]}_1m_*.csv")]
    if not files:
        return []
    path = max(files, key=lambda p: p.stat().st_mtime)
    bars: list[LiveBar] = []
    with path.open(newline="") as fh:
        for r in _csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bars.append(LiveBar(symbol=feed, timeframe="MIN_1", start_timestamp=ts,
                                    end_timestamp=ts + timedelta(minutes=1),
                                    open_price=float(r["open"]), high_price=float(r["high"]),
                                    low_price=float(r["low"]), close_price=float(r["close"]),
                                    volume=float(r.get("volume") or 0)))
            except Exception:
                pass
    return bars


@dataclass
class DayReplay:
    """One day's replay outcome (or why it was skipped)."""
    underlying: str
    day: str
    result: ReplayResult | None = None
    skipped: str = ""               # non-empty = why this day was skipped
    index_chg_pct: float = 0.0      # session open→close move (bias-vs-move context)


@dataclass
class BatchReplayResult:
    conv_floor: float | None
    days: list = field(default_factory=list)   # list[DayReplay]

    @property
    def replayed(self) -> list:
        return [d for d in self.days if d.result is not None]

    @property
    def trades(self) -> list:
        return [t for d in self.replayed for t in d.result.trades]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        n = len(self.trades)
        return (self.wins / n) if n else 0.0

    def conviction_split(self) -> tuple[float, float]:
        """(mean conviction of winners, mean conviction of losers)."""
        w = [t.conviction for t in self.trades if t.pnl > 0]
        l = [t.conviction for t in self.trades if t.pnl <= 0]
        return (sum(w) / len(w) if w else 0.0, sum(l) / len(l) if l else 0.0)

    def block_totals(self) -> dict:
        agg: dict = {}
        for d in self.replayed:
            for k, v in d.result.blocks.items():
                agg[k] = agg.get(k, 0) + v
        return agg


def replay_one_day(*, underlying: str, day: date, adapter, fetcher: HistoricalOptionChainFetcher,
                   all_bars: list[LiveBar], conv_floor: float | None = None,
                   strikes: int = 8) -> DayReplay:
    """Replay a single underlying/day on the real chain. Never raises — a fetch or
    data problem returns a SKIPPED DayReplay so the batch keeps going."""
    cfg = UNDERLYING_CFG[underlying]
    dr = DayReplay(underlying=underlying, day=day.isoformat())
    warm = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() < day]
    session = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() == day]
    if not session:
        dr.skipped = "no index bars"
        return dr
    if len(warm) < 500:
        dr.skipped = f"insufficient warm history ({len(warm)} bars)"
        return dr
    o, c = session[0].open_price, session[-1].close_price
    dr.index_chg_pct = (c - o) / o * 100.0 if o else 0.0
    try:
        expiry = adapter.select_expiry(underlying, day, min_dte=2, exchange=cfg["exch"])
        cache = fetcher.fetch_day(underlying=underlying, day=day, expiry=expiry,
                                  spot_atm=session[0].open_price, strike_step=cfg["step"],
                                  strikes_around_atm=strikes, exchange=cfg["exch"])
        chain_by_min = fetcher.load_day(cache)
        if not chain_by_min:
            dr.skipped = "empty chain (expiry likely expired / not listed)"
            return dr
    except Exception as exc:
        dr.skipped = f"chain fetch failed: {type(exc).__name__}: {str(exc)[:80]}"
        return dr
    rep = SurgicalReplay(underlying_symbol=underlying, conviction_floor=conv_floor)
    rep.warm_start(warm, cfg["feed"])
    dr.result = rep.run_day(underlying=underlying, feed_symbol=cfg["feed"], exchange=cfg["exch"],
                            expiry=expiry, lot_size=cfg["lot"], strike_step=cfg["step"], day=day,
                            session_bars=session, chain_by_min=chain_by_min, fetcher=fetcher)
    return dr


def run_batch(*, underlyings: list[str], days: list[date], conv_floor: float | None = None,
              strikes: int = 8, adapter=None, fetcher: HistoricalOptionChainFetcher | None = None,
              verbose: bool = True) -> BatchReplayResult:
    """Replay every (underlying, day) and aggregate. Builds one KiteAdapter +
    fetcher and loads each index CSV once, reusing across days."""
    if adapter is None:
        from platform_core.data.live.kite_adapter import KiteAdapter
        adapter = KiteAdapter()
    if fetcher is None:
        fetcher = HistoricalOptionChainFetcher(adapter)
    batch = BatchReplayResult(conv_floor=conv_floor)
    csv_cache: dict[str, list[LiveBar]] = {}
    for underlying in underlyings:
        if underlying not in csv_cache:
            csv_cache[underlying] = load_index_csv(underlying)
        all_bars = csv_cache[underlying]
        for day in days:
            dr = replay_one_day(underlying=underlying, day=day, adapter=adapter, fetcher=fetcher,
                                all_bars=all_bars, conv_floor=conv_floor, strikes=strikes)
            batch.days.append(dr)
            if verbose:
                if dr.result is not None:
                    n = len(dr.result.trades)
                    print(f"  {underlying:9} {dr.day}  chg={dr.index_chg_pct:+5.2f}%  "
                          f"trades={n:2}  pnl=₹{dr.result.total_pnl:>+9,.0f}")
                else:
                    print(f"  {underlying:9} {dr.day}  SKIPPED — {dr.skipped}")
    return batch


def trading_days(start: date, end: date) -> list[date]:
    """Mon–Fri days in [start, end] inclusive (holidays are skipped by the replay
    when the index CSV has no bars for them)."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
