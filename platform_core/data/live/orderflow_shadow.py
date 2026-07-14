"""
orderflow_shadow.py — SHADOW order-flow stream (batch-4 §4.1). Logs-only, no trade effect.

Orthogonal box: every existing signal is a transform of index OHLC. This taps the SAME Kite
websocket one level deeper — index tick volume, cumulative volume delta (CVD), and depth
imbalance — and logs one record per completed bar. The pre-registered test (run offline by
joining this log to pt_structure_events): a CHOCH/BOS arriving WITH same-side flow imbalance is
real; without it, a trap. Also the 3rd leg of the loser-autopsy H1 trap fingerprint.

CVD: classify each tick by the tick rule — last_price up vs prior = buy aggressor (+ltq),
down = sell (−ltq), unchanged = carry prior sign. Depth imbalance: (Σbuy_qty − Σsell_qty) /
(Σbuy_qty + Σsell_qty) from the 5-level book, averaged over the bar (needs MODE_FULL depth).

Safety: own try/except on every public call, NO locks (O(1) counters only), OFF by default
(enable with ORDERFLOW_SHADOW=1 after verifying the live tick carries volume+depth). A missing
field degrades to None; it can never raise into the tick hot path.
"""
from __future__ import annotations

import json
from pathlib import Path


def extract_tick_fields(raw_tick) -> dict:
    """Pull (last_price, ltq, buy_qty, sell_qty) from a Kite tick (dict or object). Missing → None."""
    def g(k):
        if isinstance(raw_tick, dict):
            return raw_tick.get(k)
        return getattr(raw_tick, k, None)
    last = g("last_price")
    ltq = g("last_traded_quantity") or g("last_quantity")
    depth = g("depth") or {}
    buy = sell = None
    try:
        if depth:
            buy = sum(int(lvl.get("quantity", 0)) for lvl in (depth.get("buy") or []))
            sell = sum(int(lvl.get("quantity", 0)) for lvl in (depth.get("sell") or []))
    except Exception:
        buy = sell = None
    return {"last": (float(last) if last is not None else None),
            "ltq": (int(ltq) if ltq else 0),
            "buy_qty": buy, "sell_qty": sell}


class _Acc:
    __slots__ = ("cvd", "tvol", "imb_sum", "imb_n", "last_price", "ticks")

    def __init__(self):
        self.cvd = 0.0; self.tvol = 0.0
        self.imb_sum = 0.0; self.imb_n = 0
        self.last_price = None; self.ticks = 0


class OrderFlowShadow:
    """Per-bar order-flow logger. Logging only, never affects trades."""

    def __init__(self, *, out_path: str = "logs/orderflow_shadow.jsonl") -> None:
        self._out = Path(out_path)
        self._acc: dict[str, _Acc] = {}
        self.records = 0

    def observe(self, symbol: str, raw_tick) -> None:
        """Accumulate one tick for `symbol`. O(1), no lock, NEVER raises."""
        try:
            f = extract_tick_fields(raw_tick)
            a = self._acc.get(symbol)
            if a is None:
                a = self._acc[symbol] = _Acc()
            a.ticks += 1
            a.tvol += f["ltq"]
            # tick-rule aggressor side
            sign = 0
            if f["last"] is not None and a.last_price is not None:
                sign = 1 if f["last"] > a.last_price else -1 if f["last"] < a.last_price else 0
            a.cvd += sign * f["ltq"]
            if f["last"] is not None:
                a.last_price = f["last"]
            # depth imbalance (if MODE_FULL depth present)
            b, s = f["buy_qty"], f["sell_qty"]
            if b is not None and s is not None and (b + s) > 0:
                a.imb_sum += (b - s) / (b + s)
                a.imb_n += 1
        except Exception:
            pass

    def on_bar_close(self, symbol: str, bar_ts) -> None:
        """Log the bar's accumulated flow and reset. NEVER raises."""
        try:
            a = self._acc.get(symbol)
            if a is None or a.ticks == 0:
                return
            ts = bar_ts.astimezone(__import__("datetime").timezone.utc).isoformat() \
                if hasattr(bar_ts, "astimezone") else str(bar_ts)
            rec = {
                "ts": ts, "symbol": symbol, "ticks": a.ticks,
                "tick_volume": a.tvol, "cvd": round(a.cvd, 1),
                "cvd_norm": round(a.cvd / a.tvol, 4) if a.tvol else None,
                "depth_imbalance": round(a.imb_sum / a.imb_n, 4) if a.imb_n else None,
                "flow_side": ("BUY" if a.cvd > 0 else "SELL" if a.cvd < 0 else "FLAT"),
                "source": "kite_ws" if a.imb_n else "kite_ws_no_depth",
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
            self._acc[symbol] = _Acc()   # reset for next bar
        except Exception:
            pass
