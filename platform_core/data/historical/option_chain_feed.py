# FILE: platform_core/data/historical/option_chain_feed.py
"""
HistoricalOptionChainFeed — serves real option-chain snapshots for backtesting.

Reads per-expiry CSV files written by scripts/record_option_chains.py (or any
compatible recorder) and serves OptionChainSnapshot objects through the same
interface consumed by OptionsContextBuilder.build(snapshots=...).

CSV format (one file per expiry date, e.g. data/option_chains/BANKNIFTY/2026-06-30.csv):
    timestamp,contract_symbol,underlying,expiry_date,strike,option_type,
    ltp,bid,ask,oi,volume,iv,delta,gamma,theta,vega,lot_size

When a snapshot is unavailable for the requested timestamp / expiry combination,
get_snapshot() returns None so BacktestRunner can fall back to SyntheticOptionChainBuilder.

Integration:
  In BacktestRunner.__init__:
      self.chain_feed = HistoricalOptionChainFeed(
          data_dir=cfg.chain_data_dir, symbol=self.symbol)
  In BacktestRunner.run() per bar:
      weekly_snap = self.chain_feed.get_snapshot(bar_ts, weekly_expiry, ...)
      if weekly_snap is None:
          weekly_snap = self.chain_builder.build_snapshot(...)

No new data models — OptionChainSnapshot and OptionContractSnapshot are imported
directly from platform_core.data.option_chain_ingestion, the same type used by
KiteAdapter.fetch_option_chain() and OptionsContextBuilder.build().
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
    OptionGreeksSnapshot,
)

_LOG = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))

# Tolerance window: accept data within ±MATCH_MINUTES of requested timestamp
MATCH_MINUTES: int = 2

# Required CSV columns (others are optional)
_REQUIRED_COLS = {"timestamp", "contract_symbol", "strike", "option_type", "ltp"}


class HistoricalOptionChainFeed:
    """
    Serves pre-recorded option-chain snapshots for replay in BacktestRunner.

    All data is loaded into memory at construction time. For a full year of
    BANKNIFTY monthly option chains sampled every minute during market hours,
    memory footprint is approximately 200–400 MB.

    Call ready before using get_snapshot() — if False, no CSV files were found
    and the caller should use synthetic chains for all bars.
    """

    def __init__(self, data_dir: str, symbol: str) -> None:
        self._symbol = symbol
        # _index[(minute_str, expiry_str)] = list of contract-row dicts
        self._index: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        self._expiries_loaded: set[str] = set()
        self._ready = False

        if data_dir:
            target = Path(data_dir) / symbol
            self._load(target)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def expiries_loaded(self) -> frozenset[str]:
        return frozenset(self._expiries_loaded)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load(self, dir_path: Path) -> None:
        if not dir_path.exists():
            _LOG.info("option chain data dir not found: %s", dir_path)
            return

        files = sorted(dir_path.glob("*.csv"))
        if not files:
            _LOG.info("no option chain CSV files in %s", dir_path)
            return

        rows_total = 0
        for csv_path in files:
            expiry_str = csv_path.stem  # e.g. "2026-06-30"
            rows_loaded = self._load_file(csv_path, expiry_str)
            if rows_loaded > 0:
                self._expiries_loaded.add(expiry_str)
                rows_total += rows_loaded

        _LOG.info(
            "HistoricalOptionChainFeed: loaded %d rows across %d expiries from %s",
            rows_total, len(self._expiries_loaded), dir_path,
        )
        self._ready = bool(self._expiries_loaded)

    def _load_file(self, csv_path: Path, expiry_str: str) -> int:
        rows = 0
        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or not _REQUIRED_COLS.issubset(reader.fieldnames):
                    _LOG.warning("skipping %s: missing required columns", csv_path.name)
                    return 0
                for row in reader:
                    ts = row.get("timestamp", "")
                    if len(ts) < 16:
                        continue
                    minute_str = ts[:16]  # "2026-05-28T09:15" (truncate seconds/tz)
                    self._index[(minute_str, expiry_str)].append(row)
                    rows += 1
        except Exception as exc:
            _LOG.warning("failed to load %s: %s", csv_path.name, exc)
        return rows

    # ── Snapshot construction ─────────────────────────────────────────────────

    def get_snapshot(
        self,
        bar_ts: datetime,
        expiry_date: date,
        spot_price: float,
        lot_size: int,
        exchange: str = "NFO",
    ) -> OptionChainSnapshot | None:
        """
        Return a real OptionChainSnapshot for bar_ts × expiry_date, or None.

        Tries the exact minute first; if not found, searches within ±MATCH_MINUTES.
        """
        if not self._ready:
            return None

        expiry_str = expiry_date.isoformat()
        ist = bar_ts.astimezone(_IST)
        minute_str = ist.strftime("%Y-%m-%dT%H:%M")

        rows = self._index.get((minute_str, expiry_str))

        if not rows:
            # Try adjacent minutes within tolerance
            for delta in range(1, MATCH_MINUTES + 1):
                for sign in (1, -1):
                    shifted = ist + timedelta(minutes=sign * delta)
                    alt_key = (shifted.strftime("%Y-%m-%dT%H:%M"), expiry_str)
                    rows = self._index.get(alt_key)
                    if rows:
                        break
                if rows:
                    break

        if not rows:
            return None

        return self._build_snapshot(rows, bar_ts, expiry_date, exchange)

    def _build_snapshot(
        self,
        rows: list[dict[str, str]],
        bar_ts: datetime,
        expiry_date: date,
        exchange: str,
    ) -> OptionChainSnapshot | None:
        contracts: list[OptionContractSnapshot] = []
        underlying = self._symbol

        for row in rows:
            try:
                ltp_raw = float(row.get("ltp", 0) or 0)
                if ltp_raw <= 0:
                    continue

                strike_raw = row.get("strike", "")
                opt_type = row.get("option_type", "").upper()
                if opt_type not in ("CALL", "PUT"):
                    continue

                # Optional fields — gracefully absent
                bid_raw  = _opt_float(row.get("bid"))
                ask_raw  = _opt_float(row.get("ask"))
                oi_raw   = _opt_int(row.get("oi"))
                vol_raw  = _opt_int(row.get("volume"))
                iv_raw   = _opt_decimal(row.get("iv"))
                delta    = _opt_decimal(row.get("delta"))
                gamma    = _opt_decimal(row.get("gamma"))
                theta    = _opt_decimal(row.get("theta"))
                vega     = _opt_decimal(row.get("vega"))

                contracts.append(OptionContractSnapshot(
                    contract_symbol=row["contract_symbol"],
                    underlying_symbol=underlying,
                    exchange=exchange,
                    expiry_date=expiry_date,
                    strike_price=Decimal(str(float(strike_raw))),
                    option_side=opt_type,
                    event_timestamp=bar_ts,
                    received_timestamp=bar_ts,
                    last_traded_price=Decimal(str(ltp_raw)),
                    bid_price=Decimal(str(bid_raw)) if bid_raw is not None else None,
                    ask_price=Decimal(str(ask_raw)) if ask_raw is not None else None,
                    traded_volume=vol_raw,
                    open_interest=oi_raw,
                    implied_volatility=iv_raw,
                    greeks=OptionGreeksSnapshot(
                        delta=delta, gamma=gamma, theta=theta, vega=vega,
                    ),
                    source="historical_real",
                ))
            except Exception:
                continue

        if not contracts:
            return None

        seq = int(bar_ts.timestamp() * 1000) % (2 ** 31)
        return OptionChainSnapshot(
            chain_id=f"{underlying}|{expiry_date.isoformat()}|historical",
            underlying_symbol=underlying,
            expiry_date=expiry_date,
            exchange=exchange,
            event_timestamp=bar_ts,
            received_timestamp=bar_ts,
            contracts=tuple(contracts),
            sequence_number=seq,
            update_kind="SNAPSHOT",
            is_replay=True,
            source="historical_real",
        )


# ── CSV writer (used by recorder script and live pipeline) ────────────────────

CSV_HEADER = [
    "timestamp", "contract_symbol", "underlying", "expiry_date",
    "strike", "option_type", "ltp", "bid", "ask", "oi", "volume",
    "iv", "delta", "gamma", "theta", "vega", "lot_size",
]


def snapshot_to_rows(snap: OptionChainSnapshot, lot_size: int) -> list[dict[str, str]]:
    """Convert an OptionChainSnapshot to a list of CSV row dicts for recording."""
    rows = []
    ts_str = snap.event_timestamp.isoformat()
    for c in snap.contracts:
        rows.append({
            "timestamp":       ts_str,
            "contract_symbol": c.contract_symbol,
            "underlying":      c.underlying_symbol,
            "expiry_date":     c.expiry_date.isoformat(),
            "strike":          str(c.strike_price),
            "option_type":     c.option_side,
            "ltp":             str(c.last_traded_price or ""),
            "bid":             str(c.bid_price or ""),
            "ask":             str(c.ask_price or ""),
            "oi":              str(c.open_interest or ""),
            "volume":          str(c.traded_volume or ""),
            "iv":              str(c.implied_volatility or ""),
            "delta":           str(c.greeks.delta or ""),
            "gamma":           str(c.greeks.gamma or ""),
            "theta":           str(c.greeks.theta or ""),
            "vega":            str(c.greeks.vega or ""),
            "lot_size":        str(lot_size),
        })
    return rows


# ── Private helpers ───────────────────────────────────────────────────────────

def _opt_float(v: str | None) -> float | None:
    if not v:
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


def _opt_int(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _opt_decimal(v: str | None) -> Decimal | None:
    if not v:
        return None
    try:
        f = float(v)
        return Decimal(str(f)) if f != 0 else None
    except (ValueError, TypeError):
        return None


__all__ = [
    "HistoricalOptionChainFeed",
    "CSV_HEADER",
    "snapshot_to_rows",
]
