"""
option_flow_shadow.py — SHADOW option-book order-flow stream. Logs-only, no trade effect.

THE GAP THIS CLOSES (2026-07-26): the live pipeline fetches the full option chain every bar
(kite.quote → OptionChainSnapshot) carrying per-strike bid_quantity, ask_quantity,
traded_volume and open_interest on the strikes around ATM — the real book on the contracts we
actually trade. Until now that book was consumed ONLY for liquidity gating; its DIRECTIONAL
content was discarded. orderflow_shadow.py taps the INDEX-FUTURES book (CVD/depth); this taps
the OPTION book, which is where positioning shows up (call-side demand vs put-side demand) and
which futures flow cannot see.

WHAT IT COMPUTES per bar, near-ATM (ATM ± `atm_band` strikes), CALL side vs PUT side:
  • book_imbalance  = (bid_qty − ask_qty)/(bid_qty + ask_qty), qty-weighted over the band.
      CALL bid-heavy = demand for upside = bullish(+); PUT bid-heavy = demand for downside =
      bearish(−). Net directional read = call_book_imb − put_book_imb, in [−2, 2].
  • vol_delta       = Σ Δtraded_volume bar-over-bar (fresh trading this bar), CE and PE apart.
  • oi_delta        = Σ Δopen_interest bar-over-bar (fresh positioning), CE and PE apart.
  • flow_bias       = LONG / SHORT / FLAT from the net book imbalance with a deadband.

HONEST FRAMING: this is a POSITIONING / book-pressure proxy, NOT a validated edge. A snapshot
cannot classify trade aggressor (no tick rule), so vol_delta is unsigned pressure paired with
the signed book imbalance. Direction is at-chance in every price-derived signal tested so far
(Disease B); this is logged so the recorded corpus can say whether OPTION book flow is
different, BEFORE it is ever allowed to gate a trade. Wire-to-decision only after a
pre-registered OOS test clears (HR>0.52, n≥few-hundred, ex-best-day, net of costs).

SAFETY: own try/except on every public call, NO locks, O(strikes) per bar, degrades to None on
any missing field, NEVER raises into the pipeline. Kill with PAPER_OPTION_FLOW_SHADOW=0.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path


def _num(x):
    """Coerce Decimal/str/int/None → float or None. Never raises."""
    try:
        return None if x is None else float(x)
    except Exception:
        return None


def summarize_chain(snapshot, spot: float, strike_step: float, atm_band: int) -> dict:
    """Pure: reduce one OptionChainSnapshot to near-ATM CALL/PUT book aggregates.

    Returns raw sums (no deltas — the caller owns bar-over-bar state). Missing fields are
    skipped, not faulted. Never raises.
    """
    atm = round(spot / strike_step) * strike_step if strike_step else spot
    lo, hi = atm - atm_band * strike_step, atm + atm_band * strike_step
    out = {
        "CALL": {"bid": 0.0, "ask": 0.0, "vol": 0.0, "oi": 0.0, "n": 0},
        "PUT":  {"bid": 0.0, "ask": 0.0, "vol": 0.0, "oi": 0.0, "n": 0},
        "atm": atm,
        # per-strike volume/oi keyed "SIDE|strike" for the caller's delta memory
        "by_strike": {},
    }
    for c in getattr(snapshot, "contracts", ()) or ():
        side = getattr(c, "option_side", None)
        if side not in ("CALL", "PUT"):
            continue
        strike = _num(getattr(c, "strike_price", None))
        if strike is None or strike < lo or strike > hi:
            continue
        agg = out[side]
        bq = _num(getattr(c, "bid_quantity", None))
        aq = _num(getattr(c, "ask_quantity", None))
        vol = _num(getattr(c, "traded_volume", None))
        oi = _num(getattr(c, "open_interest", None))
        if bq is not None:
            agg["bid"] += bq
        if aq is not None:
            agg["ask"] += aq
        if vol is not None:
            agg["vol"] += vol
        if oi is not None:
            agg["oi"] += oi
        agg["n"] += 1
        out["by_strike"][f"{side}|{strike:.0f}"] = {"vol": vol, "oi": oi}
    return out


def _book_imb(agg) -> float | None:
    b, a = agg["bid"], agg["ask"]
    tot = b + a
    return round((b - a) / tot, 4) if tot > 0 else None


class OptionFlowShadow:
    """Per-bar option-book flow logger. Logging only, never affects trades."""

    def __init__(
        self,
        *,
        out_path: str = "logs/option_flow_shadow.jsonl",
        atm_band: int = 2,
        deadband: float = 0.10,
    ) -> None:
        self._out = Path(out_path)
        self._atm_band = atm_band
        self._deadband = deadband
        # per-symbol prior-bar per-strike {vol, oi} for deltas
        self._prev: dict[str, dict] = {}
        self.records = 0

    def observe_chain(self, symbol: str, snapshot, spot, strike_step, bar_ts=None) -> None:
        """Summarize one chain snapshot and append a flow record. NEVER raises."""
        try:
            spot_f = _num(spot)
            step_f = _num(strike_step) or 0.0
            if spot_f is None:
                return
            s = summarize_chain(snapshot, spot_f, step_f, self._atm_band)
            call, put = s["CALL"], s["PUT"]
            if call["n"] == 0 and put["n"] == 0:
                return

            call_imb = _book_imb(call)
            put_imb = _book_imb(put)
            net_imb = None
            if call_imb is not None and put_imb is not None:
                net_imb = round(call_imb - put_imb, 4)

            # bar-over-bar deltas vs this symbol's previous snapshot
            prev = self._prev.get(symbol, {})
            ce_vol_d = pe_vol_d = ce_oi_d = pe_oi_d = 0.0
            have_prev = bool(prev)
            for key, cur in s["by_strike"].items():
                p = prev.get(key)
                if not p:
                    continue
                if cur["vol"] is not None and p["vol"] is not None:
                    d = cur["vol"] - p["vol"]
                    if d < 0:               # cumulative volume never falls intraday; reset = skip
                        d = 0.0
                    if key.startswith("CALL"):
                        ce_vol_d += d
                    else:
                        pe_vol_d += d
                if cur["oi"] is not None and p["oi"] is not None:
                    od = cur["oi"] - p["oi"]
                    if key.startswith("CALL"):
                        ce_oi_d += od
                    else:
                        pe_oi_d += od
            self._prev[symbol] = s["by_strike"]

            flow_bias = "FLAT"
            if net_imb is not None:
                if net_imb >= self._deadband:
                    flow_bias = "LONG"
                elif net_imb <= -self._deadband:
                    flow_bias = "SHORT"

            ts = bar_ts
            if hasattr(bar_ts, "astimezone"):
                ts = bar_ts.astimezone(_dt.timezone.utc).isoformat()
            elif bar_ts is None:
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            else:
                ts = str(bar_ts)

            rec = {
                "ts": ts, "symbol": symbol, "atm": s["atm"],
                "call_n": call["n"], "put_n": put["n"],
                "call_book_imb": call_imb, "put_book_imb": put_imb,
                "net_book_imb": net_imb, "flow_bias": flow_bias,
                "call_vol_delta": round(ce_vol_d, 1) if have_prev else None,
                "put_vol_delta": round(pe_vol_d, 1) if have_prev else None,
                "call_oi_delta": round(ce_oi_d, 1) if have_prev else None,
                "put_oi_delta": round(pe_oi_d, 1) if have_prev else None,
                "source": "kite_chain_snapshot",
            }
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            pass
