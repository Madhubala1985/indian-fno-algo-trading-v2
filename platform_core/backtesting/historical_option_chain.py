"""
historical_option_chain.py — Phase-B replay data layer (Approach B).

Fetches REAL historical option data from Kite (1-minute OHLC + OI per strike) and
reconstructs, for any minute of a past session, the exact ``OptionChainSnapshot``
the live pipeline consumes — so replay is faithful, not synthetic. Greeks/IV are
NOT stored by Kite (historical or live); they are computed downstream by the same
OptionsContextBuilder the live path uses, so there is no replay-vs-live
difference. Bid/ask depth is not historical; left None so the same 30-bps spread
fallback the live path uses applies. Premium (LTP) and OI are the real recorded
values.

Historical reach: Kite's instrument dump is current-only, so only strikes on
expiries that STILL EXIST can be fetched (recent/near expiries). Older expired
contracts are unavailable — those days need Approach A (the live recorder).

Fetched candles are cached under data/option_history/ so a day is fetched once
and replayed infinitely.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_CACHE_DIR = Path("data/option_history")


class HistoricalOptionChainFetcher:
    """Fetch + cache real historical option chains; rebuild per-minute snapshots."""

    def __init__(self, adapter, *, cache_dir: Path = _CACHE_DIR) -> None:
        self._adapter = adapter
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── instrument discovery ─────────────────────────────────────────────────
    def _strikes_for(self, underlying, expiry, atm, step, n, exchange):
        """Return the current-master instruments for ATM±n strikes (CE+PE)."""
        insts = self._adapter._get_instruments(exchange)
        lo, hi = atm - n * step, atm + n * step
        out = []
        for i in insts:
            if (i["name"] == underlying and i["expiry"] == expiry
                    and i["instrument_type"] in ("CE", "PE")
                    and lo <= float(i["strike"]) <= hi):
                out.append(i)
        return out

    # ── fetch + cache one day ────────────────────────────────────────────────
    def fetch_day(self, *, underlying, day: date, expiry: date, spot_atm: float,
                  strike_step: float, strikes_around_atm: int = 10,
                  exchange: str = "BFO", pause: float = 0.35) -> Path:
        """Fetch 1m OHLC+OI for every ATM±n strike on `day`; cache to JSONL.

        Returns the cache path. Re-uses an existing cache file if present.
        """
        cache = self._cache_dir / f"{underlying}_{day.isoformat()}_{expiry.isoformat()}.jsonl"
        if cache.exists() and cache.stat().st_size > 0:
            return cache
        atm = round(spot_atm / strike_step) * strike_step
        insts = self._strikes_for(underlying, expiry, atm, strike_step, strikes_around_atm, exchange)
        frm = f"{day.isoformat()} 09:15:00"
        to = f"{day.isoformat()} 15:30:00"
        rows = 0
        with cache.open("w") as fh:
            for inst in insts:
                try:
                    candles = self._adapter.kite.historical_data(
                        instrument_token=inst["instrument_token"],
                        from_date=frm, to_date=to, interval="minute", oi=1,
                    )
                except Exception:
                    candles = []
                for c in candles:
                    ts = c["date"]
                    ts_iso = ts.astimezone(timezone.utc).isoformat() if hasattr(ts, "astimezone") else str(ts)
                    fh.write(json.dumps({
                        "ts": ts_iso,
                        "tradingsymbol": inst["tradingsymbol"],
                        "strike": float(inst["strike"]),
                        "type": inst["instrument_type"],  # CE / PE
                        "ltp": c["close"], "volume": c.get("volume"), "oi": c.get("oi"),
                    }) + "\n")
                    rows += 1
                time.sleep(pause)  # respect Kite ~3 req/s
        return cache

    # ── load a cached day into a minute-indexed structure ────────────────────
    def load_day(self, cache: Path) -> dict:
        """{minute_iso: [contract_dict, ...]} from a cache file."""
        by_min: dict[str, list] = {}
        for line in cache.read_text().splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            by_min.setdefault(d["ts"], []).append(d)
        return by_min

    # ── build the model-facing snapshot for one minute ───────────────────────
    def snapshot_at(self, *, underlying, exchange, expiry, minute_iso, contracts,
                    ) -> OptionChainSnapshot | None:
        """Reconstruct the exact OptionChainSnapshot the live pipeline consumes."""
        if not contracts:
            return None
        try:
            ts = datetime.fromisoformat(minute_iso)
        except Exception:
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        snaps: list[OptionContractSnapshot] = []
        for c in contracts:
            ltp = c.get("ltp")
            if ltp is None or ltp <= 0:
                continue
            snaps.append(OptionContractSnapshot(
                contract_symbol=c["tradingsymbol"],
                underlying_symbol=underlying,
                exchange=exchange,
                expiry_date=expiry,
                strike_price=Decimal(str(c["strike"])),
                option_side="CALL" if c["type"] == "CE" else "PUT",
                event_timestamp=ts,
                received_timestamp=ts,
                last_traded_price=Decimal(str(ltp)),
                bid_price=None, ask_price=None,           # depth not historical → 30bps fallback
                traded_volume=int(c["volume"]) if c.get("volume") is not None else None,
                open_interest=int(c["oi"]) if c.get("oi") is not None else None,
                source="kite_historical",
            ))
        if not snaps:
            return None
        return OptionChainSnapshot(
            chain_id=f"{underlying}|{expiry.isoformat()}|hist",
            underlying_symbol=underlying,
            expiry_date=expiry,
            exchange=exchange,
            event_timestamp=ts,
            received_timestamp=ts,
            contracts=tuple(snaps),
            sequence_number=int(ts.timestamp()) % (2 ** 31),
            update_kind="SNAPSHOT",
            source="kite_historical",
        )
