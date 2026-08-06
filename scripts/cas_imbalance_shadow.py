#!/usr/bin/env python3
"""CAS (Closing Auction Session) imbalance shadow — data-first, LOGS ONLY, trades nothing.

WHY THIS EXISTS
---------------
NSE launched the Closing Auction Session (CAS) on 2026-08-03 for F&O-segment stocks:
a ~20-min auction (3:15-3:35 PM IST) that discovers the official closing price and,
critically, PUBLISHES the order IMBALANCE (net buy vs sell interest at the indicative
equilibrium price) in real time. Imbalance is genuine order-flow information the
continuous price does not yet contain — in every global closing auction a large buy
imbalance drifts the price up into equilibrium, and vice-versa.

Index options (NIFTY/SENSEX) keep trading continuously until 3:40 PM — five minutes
AFTER the CAS reveals where the heavyweight constituents (and therefore the index) will
close. So IF the aggregate constituent imbalance predicts the index close, there is a
legitimate, tradeable window. This is exactly a "new data" edge (path 3), not cleverness.

DISCIPLINE
----------
This script only COLLECTS and LOGS. We do not trade the signal until the logged data
proves that imbalance predicts the close→move. LEGAL line: we READ and REACT to the
exchange-published imbalance (what pros do). We NEVER submit orders to CREATE or mark an
imbalance — that is manipulation and this script does no order entry of any kind.

DATA SOURCE
-----------
NSE public auction feed (same infra as pre-open):
  https://www.nseindia.com/api/market-data-pre-open?key={FO|CAS|NIFTY|BANKNIFTY}
Per stock it returns: iep (indicative equilibrium price), lastPrice, previousClose,
and detail.preOpenMarket.{totalBuyQuantity,totalSellQuantity,finalPrice,finalQuantity,
lastUpdateTime} + the full price-ladder book. imbalance = totalBuyQty - totalSellQty.

OUTPUT
------
data/cas_history/cas_YYYY-MM-DD.jsonl — one JSON object per poll snapshot:
  {ts, key, n, indices:{NIFTY 50:.., NIFTY BANK:.., ...}, stocks:[{sym,iep,ltp,pc,tbq,tsq,imb,upd}]}

Run standalone (auto-waits for the CAS window if launched early) or via cron ~15:08 IST.
Env: CAS_KEYS (default "FO,CAS"), CAS_POLL_SEC (20), CAS_END_HHMM (1542), CAS_ONESHOT=1
to fetch a single snapshot now and exit (for testing).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time

try:
    import requests
except Exception:  # pragma: no cover
    print("requests not available", file=sys.stderr)
    raise

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT_DIR = os.path.join(_ROOT, "data", "cas_history")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_BASE = "https://www.nseindia.com"
_KEYS = [k.strip() for k in os.environ.get("CAS_KEYS", "FO,CAS").split(",") if k.strip()]
_POLL_SEC = int(os.environ.get("CAS_POLL_SEC", "20") or 20)
_END_HHMM = int(os.environ.get("CAS_END_HHMM", "1542") or 1542)  # stop after 15:42 IST
_ONESHOT = os.environ.get("CAS_ONESHOT", "0") == "1"


def _now_ist() -> _dt.datetime:
    if _IST is not None:
        return _dt.datetime.now(_IST)
    return _dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _BASE + "/market-data/pre-open-market-cm-and-emerge-market",
    })
    try:  # warm cookies
        s.get(_BASE + "/", timeout=12)
    except Exception:
        pass
    return s


def _get_json(s: requests.Session, url: str, retry: int = 2):
    for attempt in range(retry + 1):
        try:
            r = s.get(url, timeout=12)
            if r.status_code == 403 and attempt < retry:
                s.get(_BASE + "/", timeout=12)  # re-warm cookies
                time.sleep(1.0)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            if attempt < retry:
                time.sleep(1.0)
                continue
    return None


def _fetch_indices(s: requests.Session) -> dict:
    """Spot index levels so we can later measure imbalance vs the actual close move."""
    out = {}
    d = _get_json(s, _BASE + "/api/allIndices")
    if d and isinstance(d.get("data"), list):
        want = {"NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "INDIA VIX", "NIFTY MIDCAP SELECT"}
        for row in d["data"]:
            nm = row.get("index") or row.get("indexSymbol")
            if nm in want:
                out[nm] = {"last": row.get("last"), "pc": row.get("previousClose"),
                           "pchg": row.get("percentChange")}
    return out


def _parse_auction(d: dict) -> list[dict]:
    rows = []
    for item in (d.get("data") or []):
        meta = item.get("metadata") or {}
        det = ((item.get("detail") or {}).get("preOpenMarket")) or {}
        sym = meta.get("symbol")
        if not sym:
            continue
        tbq = det.get("totalBuyQuantity")
        tsq = det.get("totalSellQuantity")
        imb = None
        if isinstance(tbq, (int, float)) and isinstance(tsq, (int, float)):
            imb = tbq - tsq
        rows.append({
            "sym": sym,
            "iep": det.get("IEP", meta.get("iep")),
            "ltp": meta.get("lastPrice"),
            "pc": meta.get("previousClose"),
            "fin": det.get("finalPrice"),
            "tbq": tbq,
            "tsq": tsq,
            "imb": imb,
            "upd": det.get("lastUpdateTime"),
        })
    return rows


def _poll_once(s: requests.Session) -> list[dict]:
    """One snapshot per key (never conflate morning pre-open with the closing auction).

    Each record carries key + per-stock lastUpdateTime; validation filters to rows whose
    upd falls in the CAS window (15:15-15:35 IST) to isolate genuine closing-auction data.
    """
    now = _now_ist()
    indices = _fetch_indices(s)
    out = []
    for key in _KEYS:
        d = _get_json(s, _BASE + f"/api/market-data-pre-open?key={key}")
        if not d:
            continue
        rows = _parse_auction(d)
        # freshest lastUpdateTime across the book — lets us see at a glance whether this
        # key is serving live CAS (~15:1x-15:3x) or stale morning pre-open (~09:0x).
        upds = [r["upd"] for r in rows if r.get("upd")]
        out.append({
            "ts": now.isoformat(),
            "key": key,
            "n": len(rows),
            "max_upd": max(upds) if upds else None,
            "indices": indices,
            "stocks": rows,
        })
    return out


def _outfile(now: _dt.datetime) -> str:
    os.makedirs(_OUT_DIR, exist_ok=True)
    return os.path.join(_OUT_DIR, f"cas_{now.strftime('%Y-%m-%d')}.jsonl")


def _write(rec: dict) -> None:
    path = _outfile(_now_ist())
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def main() -> int:
    s = _session()

    if _ONESHOT:
        recs = _poll_once(s)
        for rec in recs:
            _write(rec)
        idx = (recs[0]["indices"].get("NIFTY 50", {}) if recs else {})
        print(f"[CAS oneshot] NIFTY={idx.get('last')} -> wrote {_outfile(_now_ist())}")
        for rec in recs:
            print(f"  key={rec['key']:<10} stocks={rec['n']:<4} max_upd={rec['max_upd']}")
            top = sorted((r for r in rec["stocks"] if r["imb"] is not None),
                         key=lambda r: abs(r["imb"]), reverse=True)[:3]
            for r in top:
                print(f"      {r['sym']:<12} imb={r['imb']:>12,.0f}  iep={r['iep']}  pc={r['pc']}")
        if not any(r["n"] for r in recs):
            print("    (no auction data — expected outside 09:00-09:08 / 15:15-15:35 IST)")
        return 0

    print(f"[CAS shadow] keys={_KEYS} poll={_POLL_SEC}s end={_END_HHMM} IST -> {_OUT_DIR}")
    snaps = 0
    while True:
        now = _now_ist()
        hhmm = now.hour * 100 + now.minute
        if hhmm >= _END_HHMM:
            print(f"[CAS shadow] reached end {hhmm} IST — {snaps} snapshots written to "
                  f"{_outfile(now)}")
            return 0
        try:
            recs = _poll_once(s)
            wrote = False
            for rec in recs:
                # write any key that carries auction data; always keep the index path near
                # the close so we can measure imbalance vs the actual settle.
                if rec["n"] > 0 or hhmm >= 1533:
                    _write(rec)
                    wrote = wrote or rec["n"] > 0
            if wrote:
                snaps += 1
            idx = (recs[0]["indices"].get("NIFTY 50", {}) if recs else {})
            summary = "  ".join(f"{r['key']}:{r['n']}@{(r['max_upd'] or '-')[-8:]}" for r in recs)
            print(f"[CAS {now.strftime('%H:%M:%S')}] {summary}  NIFTY={idx.get('last')} "
                  f"(snaps={snaps})")
        except Exception as exc:  # never die mid-session
            print(f"[CAS shadow] poll error: {exc}")
        time.sleep(_POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
