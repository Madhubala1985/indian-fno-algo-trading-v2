"""
option_chain_recorder.py — Approach-A live chain recorder (Priority 3).

Captures the REAL live OptionChainSnapshot the model consumes each bar and appends
it to disk in the EXACT same JSONL format the historical fetcher writes
(data/option_history/{underlying}_{day}_{expiry}.jsonl). Recorded days are then
indistinguishable from Approach-B fetched days — batch_replay / surgical_replay
read them with zero changes.

Why it exists: Kite's instrument dump is current-only, so the historical fetch
(Approach B) can only reach ~1 week/underlying (expiries still listed). Recording
live grows the validation dataset day-by-day with real premiums+OI, unbounded by
expiry-listing — the ONLY way past the historical ceiling (see project_replay_harness).

SAFETY (hot path — see feedback_no_untested_hotpath_changes):
  - OFF by default. Enable with env RECORD_OPTION_CHAIN=1.
  - NO extra Kite calls: it only serialises a snapshot the live path ALREADY
    fetched. Hooked where the chain is already in hand.
  - NEVER raises: every public entry point swallows all exceptions, so a recorder
    fault can never disturb the live on_ticks callback. No locks.

COVERAGE LIMIT (v1): live only fetches the chain on directional-bias bars, so
NEUTRAL-bias minutes are not recorded. That's exactly the minutes the model can't
enter on anyway; a position held into a NEUTRAL stretch may lack per-minute marks
for those bars in replay. Full every-bar coverage would need an extra fetch on the
hot path (deliberately avoided in v1).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_CACHE_DIR = Path("data/option_history")

# In-process guard: (file_name, minute_iso) already written this session, so a
# repeated bar/minute (reconnect, double-fire) doesn't append duplicate lines.
_written: set[tuple[str, str]] = set()
_announced = False


def is_enabled() -> bool:
    """True when RECORD_OPTION_CHAIN is a truthy env value."""
    return str(os.environ.get("RECORD_OPTION_CHAIN", "")).strip().lower() in ("1", "true", "yes", "on")


def _minute_iso(bar_ts: datetime) -> str:
    """Minute-floored UTC isoformat, matching the historical fetcher + replay key
    (bar.start_timestamp.astimezone(utc).isoformat())."""
    if bar_ts.tzinfo is None:
        bar_ts = bar_ts.replace(tzinfo=timezone.utc)
    return bar_ts.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def record_chain(live_chain, *, underlying: str, expiry, bar_ts: datetime) -> None:
    """Append one live OptionChainSnapshot to its day/expiry JSONL cache.

    Same schema the historical fetcher writes: one line per contract with
    {ts, tradingsymbol, strike, type, ltp, volume, oi}. Never raises.
    """
    if not is_enabled():
        return
    try:
        global _announced
        contracts = getattr(live_chain, "contracts", None)
        if not contracts:
            return
        ts_iso = _minute_iso(bar_ts)
        day_iso = ts_iso[:10]
        fname = f"{underlying}_{day_iso}_{expiry.isoformat()}.jsonl"
        key = (fname, ts_iso)
        if key in _written:
            return  # this minute already recorded this session
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _CACHE_DIR / fname
        lines = []
        for c in contracts:
            ltp = getattr(c, "last_traded_price", None)
            side = getattr(c, "option_side", None)
            try:
                lines.append(json.dumps({
                    "ts": ts_iso,
                    "tradingsymbol": c.contract_symbol,
                    "strike": float(c.strike_price),
                    "type": "CE" if str(side) == "CALL" else "PE",
                    "ltp": (float(ltp) if ltp is not None else None),
                    "volume": c.traded_volume,
                    "oi": c.open_interest,
                }))
            except Exception:
                continue  # skip a malformed contract, keep the rest
        if not lines:
            return
        with path.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
        _written.add(key)
        if not _announced:
            _announced = True
            print(f"[CHAIN RECORDER] ON — appending live chains to {_CACHE_DIR}/ "
                  f"(first: {fname}). Replay-ready (Approach A).")
    except Exception:
        pass  # recorder must NEVER disturb the live path
