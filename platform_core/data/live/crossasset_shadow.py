"""
crossasset_shadow.py — SHADOW logger for cross-asset / risk-state inputs (directive §4.2).

Hypothesis under test (logs-only, NO effect on trades, NO MCB/hot-path changes): the
overnight + cross-asset state is a BETTER directional input for the 09:15-09:35 open-drive
window than the stale 4h/1d HTF bias the pipeline currently inherits there. Every price
signal the model uses today is a transform of the same index OHLC series (BOS/CHOCH/FVG) —
this logs ORTHOGONAL inputs so a promotion test can compare, out-of-sample, whether they'd
have set the open-drive direction better.

Two roles this shadow serves:
  * Directional (open-drive): GIFT Nifty pre-open gap, overnight S&P move, Brent momentum,
    and intraday flow of the index-heavy constituents (HDFCBANK/RELIANCE/ICICIBANK for NIFTY;
    the analogous BSE heavies for SENSEX) → a coarse crossasset_bias LONG/SHORT/NEUTRAL.
  * Risk-state (Task A cond1): the same overnight block feeds the expansion detector's
    "big move likely" side, which IV-cheapness (cond2) cannot supply on its own.

FEED HONESTY: constituents + India VIX are reachable on Kite (KiteFeedProvider). GIFT Nifty,
overnight S&P, and Brent are EXTERNAL (not on the Zerodha feed) — those fields log as None
with source="NEEDS_FEED" until a macro data source is wired. The module degrades gracefully:
a missing feed never raises and never disturbs the trade path.

This module is standalone and is NOT imported by live_structure_pipeline. Wiring the pre-open
call into the live boot sequence is a hot-path change held for explicit go-ahead.

v2 (2026-07-15, batch 1): intraday flow is INDEX-WEIGHTED over top-8 constituent baskets
(_CONSTITUENTS_W, one batched kite.ltp per poll); legacy top-3 equal mean logged as
avg_flow_pct_eq; weighted participation logged as breadth. CROSSASSET_WEIGHTED=0 reverts.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


# LEGACY top-3 equal-weight baskets (pre-2026-07-15). Still computed every poll
# as `avg_flow_pct_eq` (series continuity for the shadow corpus), and becomes
# the PRIMARY flow again with CROSSASSET_WEIGHTED=0 (instant revert knob).
_CONSTITUENTS = {
    "NIFTY":     ("HDFCBANK", "RELIANCE", "ICICIBANK"),
    "SENSEX":    ("HDFCBANK", "RELIANCE", "ICICIBANK"),
    "BANKNIFTY": ("HDFCBANK", "ICICIBANK", "SBIN"),
}

# ── v2 weighted baskets (batch 1, 2026-07-15) ────────────────────────────────
# Top-8 constituents per index with their REAL index weights (%, static
# snapshot as-of 2026-06 month-end factsheets, rounded to 0.1). Static is fine:
# index weights drift slowly (free-float rebalances are quarterly and small) —
# refresh the numbers when the baskets are next touched. The top-3 equal
# basket covered only ≈31-55% of index weight and let one stock masquerade as
# "participation"; top-8 covers ≈54% (NIFTY) / ≈59% (SENSEX) / ≈87% (BANKNIFTY).
# NOTE: the NIFTY and SENSEX top-8 NAME sets genuinely coincide (both are the
# same mega-caps — SENSEX's 30-stock universe has none of the NIFTY small
# free-float tails, the divergence lives below rank 8); what differs is the
# WEIGHTS — the 30-stock SENSEX concentrates more per name. BANKNIFTY is
# banks-only by construction. All symbols quoted on NSE (SENSEX constituents
# via their NSE listings — same equity, deeper feed).
_CONSTITUENTS_W: dict[str, tuple[tuple[str, float], ...]] = {
    "NIFTY": (
        ("HDFCBANK", 13.2), ("ICICIBANK", 9.1), ("RELIANCE", 8.1),
        ("INFY", 5.0), ("BHARTIARTL", 4.7), ("LT", 4.2),
        ("ITC", 4.0), ("TCS", 3.9),
    ),
    "SENSEX": (
        ("HDFCBANK", 14.8), ("ICICIBANK", 10.4), ("RELIANCE", 9.2),
        ("INFY", 6.1), ("BHARTIARTL", 5.1), ("LT", 4.8),
        ("ITC", 4.4), ("TCS", 4.3),
    ),
    "BANKNIFTY": (
        ("HDFCBANK", 28.5), ("ICICIBANK", 25.4), ("SBIN", 8.7),
        ("KOTAKBANK", 8.5), ("AXISBANK", 8.2), ("BOB", 3.1),
        ("PNB", 2.7), ("CANBK", 2.3),
    ),
}


def _weighted_enabled() -> bool:
    """CROSSASSET_WEIGHTED=0 reverts to the legacy top-3 equal basket."""
    return os.environ.get("CROSSASSET_WEIGHTED", "1") != "0"


def _basket(underlying: str) -> tuple[tuple[str, ...], dict[str, float]]:
    """(symbols, weight-by-symbol) for the active mode. Legacy mode returns the
    top-3 basket with unit weights, which makes the weighted mean degenerate to
    the old equal-weighted mean exactly."""
    if _weighted_enabled():
        pairs = _CONSTITUENTS_W.get(underlying, ())
        if pairs:
            return tuple(s for s, _ in pairs), {s: w for s, w in pairs}
    syms = _CONSTITUENTS.get(underlying, ())
    return tuple(syms), {s: 1.0 for s in syms}


def all_basket_symbols(underlyings) -> tuple[str, ...]:
    """Union of every active basket across `underlyings` — the ONE batched
    kite.ltp() universe per poll (~24 symbols max, well under Kite's limit)."""
    seen: dict[str, None] = {}
    for u in underlyings:
        for s in _basket(u)[0]:
            seen.setdefault(s)
    return tuple(seen)

# ── Live-readable adaptive state (2026-07-14) ─────────────────────────────────
# The trade path may READ this to let real-time flow override a lagging HTF read
# (the 07-13 lesson: flow flipped LONG at the low while the static gate blocked a
# correct long all day). Plain-dict writes are GIL-atomic; readers must treat a
# stale entry (see get_fresh_flow) as "no signal". Never anything blocking here.
LAST_FLOW: dict = {}   # underlying -> {"epoch": float, "avg_flow_pct": float, "flow_bias": str}
LAST_TURN: dict = {}   # underlying -> {"epoch": float, "to": "LONG"/"SHORT", "rolling": float}


def get_fresh_flow(underlying: str, max_age_sec: float = 300.0):
    """Latest constituent-flow read for `underlying` if fresher than max_age_sec, else None."""
    try:
        import time as _time
        rec = LAST_FLOW.get(underlying)
        if rec and (_time.time() - rec.get("epoch", 0.0)) <= max_age_sec:
            return rec
    except Exception:
        pass
    return None


class CrossAssetFeeds(Protocol):
    """Supplies the raw cross-asset numbers. Any method may return None (feed absent)."""

    def gift_nifty_gap_pct(self, session_date) -> float | None: ...
    def overnight_sp_pct(self, session_date) -> float | None: ...
    def brent_momentum_pct(self, session_date, days: int) -> float | None: ...
    def constituent_ltp(self, symbol: str) -> float | None: ...
    def constituent_ltps(self, symbols) -> dict: ...
    def india_vix(self) -> float | None: ...


class NullFeeds:
    """Fallback provider — everything absent. Lets the shadow run offline/logs-only."""

    def gift_nifty_gap_pct(self, session_date):
        return None

    def overnight_sp_pct(self, session_date):
        return None

    def brent_momentum_pct(self, session_date, days: int = 2):
        return None

    def constituent_ltp(self, symbol: str):
        return None

    def constituent_ltps(self, symbols) -> dict:
        return {}

    def india_vix(self):
        return None


class KiteFeedProvider:
    """Feeds the cross-asset inputs REACHABLE on the Zerodha feed: India VIX + the
    index-heavy constituents (LTP via kite.ltp). GIFT Nifty / overnight S&P / Brent are
    NOT on the Kite feed — they are delegated to an optional ``macro_provider`` the user
    supplies (any object with gift_nifty_gap_pct / overnight_sp_pct / brent_momentum_pct),
    else they return None (source=NEEDS_FEED). Every call degrades gracefully to None so a
    dead/disconnected adapter never disturbs the trade path.
    """

    def __init__(self, adapter, *, macro_provider=None) -> None:
        self._adapter = adapter                 # KiteAdapter (has .kite = KiteConnect)
        self._macro = macro_provider

    def _ltp(self, key: str) -> float | None:
        try:
            kite = getattr(self._adapter, "kite", None)
            if kite is None:
                return None
            resp = kite.ltp([key]) or {}
            row = resp.get(key)
            lp = row.get("last_price") if isinstance(row, dict) else None
            return float(lp) if lp else None
        except Exception:
            return None

    # ── Kite-reachable ───────────────────────────────────────────────────────
    def india_vix(self) -> float | None:
        return self._ltp("NSE:INDIA VIX")

    def constituent_ltp(self, symbol: str) -> float | None:
        return self._ltp(f"NSE:{symbol}")

    def constituent_ltps(self, symbols) -> dict:
        """ALL constituent LTPs in ONE kite.ltp() batch call (batch-1 efficiency:
        one network round-trip per poll instead of ~24). Missing/errored symbols
        simply absent from the result; a dead adapter returns {}. Never raises."""
        out: dict[str, float] = {}
        try:
            kite = getattr(self._adapter, "kite", None)
            syms = list(symbols)
            if kite is None or not syms:
                return out
            resp = kite.ltp([f"NSE:{s}" for s in syms]) or {}
            for s in syms:
                row = resp.get(f"NSE:{s}")
                lp = row.get("last_price") if isinstance(row, dict) else None
                if lp:
                    out[s] = float(lp)
        except Exception:
            return out
        return out

    # ── external macro (delegated; NEEDS_FEED if no macro_provider) ───────────
    def gift_nifty_gap_pct(self, session_date) -> float | None:
        try:
            return self._macro.gift_nifty_gap_pct(session_date) if self._macro else None
        except Exception:
            return None

    def overnight_sp_pct(self, session_date) -> float | None:
        try:
            return self._macro.overnight_sp_pct(session_date) if self._macro else None
        except Exception:
            return None

    def brent_momentum_pct(self, session_date, days: int = 2) -> float | None:
        try:
            return self._macro.brent_momentum_pct(session_date, days) if self._macro else None
        except Exception:
            return None


class MacroFeedProvider:
    """External-macro provider (directive §4.2) — KITE-NATIVE feeds, no third-party API:

      * overnight_sp_pct  → Kite GLOBAL:US500 (S&P 500 index): last_price vs prior ohlc.close.
      * brent_momentum_pct→ Kite MCX CRUDEOIL front future, N-day close momentum (Brent proxy).
      * gift_nifty_gap_pct→ Kite NSEIX:GIFT NIFTY (real pre-open GIFT level) vs prior Nifty close;
                            falls back to the gap-at-open proxy, then a manual CSV.

    yfinance was removed: probe_gift_symbols.py found GLOBAL:US500 on the Kite feed, so overnight
    S&P is now single-source (Kite), causal and dependency-free. Everything degrades to None on any
    failure (dead adapter, market closed, stale token) so the shadow stays logs-only and never
    disturbs the trade path. The logged `source` field reveals feed degradation.
    """

    def __init__(self, adapter=None, *, index_quote_key: str = "NSE:NIFTY 50",
                 gift_symbol: str | None = "NSEIX:GIFT NIFTY",
                 sp_symbol: str | None = "GLOBAL:US500",
                 csv_macro=None) -> None:
        self._adapter = adapter                 # KiteAdapter (has .kite); optional
        self._index_key = index_quote_key
        # Real GIFT Nifty (probe_gift_symbols.py found NSEIX:GIFT NIFTY). First-choice for
        # gift_gap: a true PRE-OPEN feed (GIFT trades 06:30 IST) → causal by construction.
        # Falls back to the gap-at-open proxy, then a manual CSV provider, in that order.
        self._gift_symbol = gift_symbol
        # Kite-native overnight S&P (probe found GLOBAL:US500). Primary + only feed; CSV fallback.
        self._sp_symbol = sp_symbol
        self._csv_macro = csv_macro             # last-resort manual CSV provider (optional)
        self._crude_token: int | None = None    # memoized MCX CRUDEOIL front-future token

    def _quote(self, key: str):
        try:
            kite = getattr(self._adapter, "kite", None)
            if kite is None:
                return None
            return (kite.quote([key]) or {}).get(key)
        except Exception:
            return None

    # ── overnight S&P via Kite GLOBAL:US500 (single-source, causal) ───────────
    def overnight_sp_pct(self, session_date) -> float | None:
        if self._sp_symbol:
            row = self._quote(self._sp_symbol)
            if isinstance(row, dict):
                last = row.get("last_price")
                prev = (row.get("ohlc") or {}).get("close")
                if last and prev:
                    return round(100.0 * (last - prev) / prev, 3)
        # fallback: manual CSV
        if self._csv_macro is not None:
            try:
                return self._csv_macro.overnight_sp_pct(session_date)
            except Exception:
                return None
        return None

    # ── Brent proxy via Kite MCX CRUDEOIL front future ────────────────────────
    def _resolve_crude_token(self):
        if self._crude_token is not None:
            return self._crude_token
        try:
            from datetime import date as _date
            insts = self._adapter._get_instruments("MCX")
            today = _date.today()
            fut = [i for i in insts if i.get("name") == "CRUDEOIL"
                   and i.get("instrument_type") == "FUT" and i.get("expiry")
                   and i["expiry"] >= today]
            if not fut:
                return None
            front = min(fut, key=lambda i: i["expiry"])
            self._crude_token = front["instrument_token"]
            return self._crude_token
        except Exception:
            return None

    def brent_momentum_pct(self, session_date, days: int = 2) -> float | None:
        try:
            token = self._resolve_crude_token()
            if token is None or self._adapter is None:
                return None
            from datetime import date as _date, timedelta as _td
            to = _date.today()
            frm = to - _td(days=days + 6)   # pad for weekends/holidays
            candles = self._adapter.kite.historical_data(
                instrument_token=token, from_date=frm.isoformat(),
                to_date=to.isoformat(), interval="day")
            closes = [float(c["close"]) for c in candles if c.get("close")]
            if len(closes) < days + 1 or closes[-(days + 1)] == 0:
                return None
            return round(100.0 * (closes[-1] - closes[-(days + 1)]) / closes[-(days + 1)], 3)
        except Exception:
            return None

    # ── GIFT gap: real GIFT feed → gap-at-open proxy → manual CSV ─────────────
    def _nifty_prior_close(self) -> float | None:
        row = self._quote(self._index_key)
        return (row.get("ohlc") or {}).get("close") if isinstance(row, dict) else None

    def gift_nifty_gap_pct(self, session_date) -> float | None:
        # 1) FIRST-CHOICE: real pre-open GIFT Nifty level vs prior Nifty close (causal).
        if self._gift_symbol:
            row = self._quote(self._gift_symbol)
            prior = self._nifty_prior_close()
            if isinstance(row, dict) and row.get("last_price") and prior:
                return round(100.0 * (row["last_price"] - prior) / prior, 3)
        # 2) FALLBACK: gap-at-open proxy (Nifty last vs prior close — only valid at/after 09:15).
        row = self._quote(self._index_key)
        if isinstance(row, dict):
            last = row.get("last_price")
            prior = (row.get("ohlc") or {}).get("close")
            if last and prior:
                return round(100.0 * (last - prior) / prior, 3)
        # 3) LAST RESORT: manual CSV provider.
        if self._csv_macro is not None:
            try:
                return self._csv_macro.gift_nifty_gap_pct(session_date)
            except Exception:
                return None
        return None


class CrossAssetShadow:
    """Observational cross-asset/risk-state logger — logging only, never affects trades."""

    def __init__(self, *, feeds: CrossAssetFeeds | None = None,
                 out_path: str = "logs/crossasset_shadow.jsonl") -> None:
        self._feeds = feeds or NullFeeds()
        self._out = Path(out_path)
        self._open_ref: dict[str, dict[str, float]] = {}   # underlying -> {constituent: open_ltp}
        self._flow_buf: dict[str, list] = {}               # underlying -> last 5 avg_flow_pct
        self._flow_roll: dict[str, float] = {}             # underlying -> prev rolling mean
        self._flow_state: dict[str, str] = {}              # underlying -> last confirmed side (hysteresis)
        self.records = 0

    # ── pre-open directional / risk-state snapshot ───────────────────────────
    def pre_open(self, *, underlying: str, session_date) -> dict:
        """Log the overnight block once before the open. Returns the record. NEVER raises."""
        rec = {"kind": "pre_open", "underlying": underlying,
               "session_date": str(session_date),
               "logged_at": datetime.now(timezone.utc).isoformat()}
        try:
            gift = self._feeds.gift_nifty_gap_pct(session_date)
            sp = self._feeds.overnight_sp_pct(session_date)
            brent = self._feeds.brent_momentum_pct(session_date, 2)
            vix = self._feeds.india_vix()
            fii = self._fii_stance()
            rec.update({
                "gift_gap_pct": gift, "overnight_sp_pct": sp, "brent_2d_pct": brent,
                "india_vix": vix,
                "crossasset_bias": self._overnight_bias(gift, sp, brent),
                "risk_state": self._risk_state(gift, sp, brent, vix),
                # institutional book (NSE participant OI, yesterday's close — causal):
                # persistent FII net-short regime = headwind for longs (07-14 study:
                # every LONG-bleed day was against this book).
                "fii_net_index_futures": fii.get("net"),
                "fii_stance": fii.get("stance"),
                "source": "NEEDS_FEED" if gift is None and sp is None and brent is None else "kite+macro",
            })
            self._write(rec)
        except Exception:
            pass
        return rec

    # ── intraday constituent-flow snapshot ───────────────────────────────────
    def intraday_flow(self, *, underlying: str, timestamp, is_session_open: bool = False,
                      ltp_snapshot: dict | None = None) -> dict:
        """Log constituent flow vs the session open (directional lead). NEVER raises.

        v2 (batch 1, 2026-07-15): flow is INDEX-WEIGHTED over the top-8 basket —
        `avg_flow_pct` = Σ(wᵢ·moveᵢ)/Σwᵢ over the constituents with a live LTP,
        so every existing consumer (adaptive override, NIFTY gate, TURN detector,
        bias-invalidation flow input) upgrades automatically. The legacy top-3
        equal mean is still logged as `avg_flow_pct_eq` for series continuity,
        and `breadth` = weighted fraction of constituents agreeing with the flow
        sign (logged only — a 1-stock rally is not participation; do NOT gate on
        it yet). CROSSASSET_WEIGHTED=0 restores the legacy basket as primary.
        `ltp_snapshot` (symbol→LTP) lets the caller batch ONE kite.ltp() for all
        underlyings per poll; without it the feed's own batch method (then the
        per-symbol fallback) is used.
        """
        rec = {"kind": "flow", "underlying": underlying}
        try:
            ts = timestamp.astimezone(timezone.utc).isoformat() if hasattr(timestamp, "astimezone") else str(timestamp)
            rec["ts"] = ts
            syms, weights = _basket(underlying)
            ltps = self._ltps_for(syms, ltp_snapshot)
            moves: dict[str, float | None] = {}
            ref = self._open_ref.setdefault(underlying, {})
            for sym in syms:
                ltp = ltps.get(sym)
                if ltp is None or ltp <= 0:
                    moves[sym] = None
                    continue
                if is_session_open or sym not in ref:
                    ref[sym] = ltp
                base = ref.get(sym)
                moves[sym] = round(100.0 * (ltp - base) / base, 3) if base else 0.0
            valid = {s: m for s, m in moves.items() if m is not None}
            wsum = sum(weights[s] for s in valid)
            avg = (round(sum(weights[s] * m for s, m in valid.items()) / wsum, 3)
                   if valid and wsum > 0 else None)
            # breadth: weighted share of live constituents whose move agrees with
            # the weighted-flow sign (0 counts as disagreement; None when flat).
            breadth = None
            if avg is not None and avg != 0.0 and wsum > 0:
                agree = sum(weights[s] for s, m in valid.items() if m * avg > 0)
                breadth = round(agree / wsum, 3)
            # legacy top-3 equal mean, for continuity with the pre-v2 corpus
            # (identical to avg in legacy mode — the log stays self-consistent).
            eq_syms = _CONSTITUENTS.get(underlying, ())
            eq_valid = [valid[s] for s in eq_syms if s in valid]
            avg_eq = round(sum(eq_valid) / len(eq_valid), 3) if eq_valid else None
            bias = (("LONG" if (avg or 0) > 0.05 else "SHORT" if (avg or 0) < -0.05 else "NEUTRAL")
                    if avg is not None else None)
            rec.update({
                "constituent_moves_pct": moves, "avg_flow_pct": avg,
                "avg_flow_pct_eq": avg_eq, "breadth": breadth,
                "flow_bias": bias,
                "weights_mode": "weighted_top8" if _weighted_enabled() else "eq_top3",
                "source": "kite" if valid else "NEEDS_FEED",
            })
            self._write(rec)
            # live-readable state + TURN detection (rolling 5-sample mean sign flip)
            if avg is not None:
                import time as _time
                buf = self._flow_buf.setdefault(underlying, [])
                buf.append(avg)
                if len(buf) > 10:
                    buf.pop(0)
                # momentum = recent 5-sample mean minus the prior 5 — a FRESH push, not the
                # sticky from-open level (07-14: the level made the override quasi-permanent).
                mom = (sum(buf[-5:]) / 5 - sum(buf[:5]) / 5) if len(buf) == 10 else None
                LAST_FLOW[underlying] = {"epoch": _time.time(), "avg_flow_pct": avg,
                                         "flow_bias": bias, "breadth": breadth,
                                         "mom": (round(mom, 3) if mom is not None else None)}
                roll = sum(buf[-5:]) / min(len(buf), 5)
                self._flow_roll[underlying] = roll
                # Hysteresis state machine: side = LONG when roll >= +0.05, SHORT
                # when <= -0.05, RETAINED through the neutral band. A turn fires on
                # side CHANGE. The old prev-vs-current single-step comparison could
                # only fire on a >=0.5%-per-sample jerk (the mean moves (new-old)/5
                # per step) — a gradual real reversal crosses the band over minutes
                # and never satisfied it: 0 turn events ever recorded, including the
                # 2026-07-15 12:56 IST breakdown this detector exists to catch.
                if len(buf) >= 5:
                    _side = ("LONG" if roll >= 0.05
                             else "SHORT" if roll <= -0.05
                             else self._flow_state.get(underlying))
                    _prev_side = self._flow_state.get(underlying)
                    if _side is not None:
                        self._flow_state[underlying] = _side
                    if _prev_side is not None and _side is not None and _side != _prev_side:
                        LAST_TURN[underlying] = {"epoch": _time.time(), "to": _side,
                                                 "rolling": round(roll, 3)}
                        self._write({"kind": "turn", "underlying": underlying, "ts": rec.get("ts"),
                                     "from": _prev_side,
                                     "to": _side, "rolling_flow_pct": round(roll, 3)})
        except Exception:
            pass
        return rec

    def _ltps_for(self, syms, ltp_snapshot: dict | None) -> dict:
        """Resolve basket LTPs: caller-supplied batch snapshot → feed batch call
        → per-symbol fallback (legacy providers without constituent_ltps)."""
        if ltp_snapshot is not None:
            return {s: ltp_snapshot.get(s) for s in syms}
        batch = getattr(self._feeds, "constituent_ltps", None)
        if callable(batch):
            try:
                got = batch(syms) or {}
                if got:
                    return {s: got.get(s) for s in syms}
            except Exception:
                pass
        return {s: self._feeds.constituent_ltp(s) for s in syms}

    @staticmethod
    def _fii_stance() -> dict:
        """Latest NSE participant-OI file (fetched by kite_source_pull 08:50) → FII net index
        futures + stance. Empty dict if unavailable; never raises."""
        try:
            import csv as _csv
            files = sorted(Path("data/source/participant_oi").glob("*.csv"))
            if not files:
                return {}
            for row in _csv.reader(files[-1].read_text().splitlines()):
                if row and row[0].strip() == "FII":
                    net = int(row[1]) - int(row[2])
                    return {"net": net, "stance": "NET_SHORT" if net < 0 else "NET_LONG",
                            "as_of": files[-1].stem}
        except Exception:
            pass
        return {}

    # ── coarse derivations ───────────────────────────────────────────────────
    @staticmethod
    def _overnight_bias(gift, sp, brent) -> str | None:
        parts = [x for x in (gift, sp) if x is not None]   # brent is risk-state, not directional
        if not parts:
            return None
        score = sum(parts)
        return "LONG" if score > 0.15 else "SHORT" if score < -0.15 else "NEUTRAL"

    @staticmethod
    def _risk_state(gift, sp, brent, vix) -> str | None:
        signals = 0
        if gift is not None and abs(gift) >= 0.35:
            signals += 1
        if sp is not None and abs(sp) >= 0.5:
            signals += 1
        if brent is not None and abs(brent) >= 3.0:   # crude 2d momentum (07-08 was +8%)
            signals += 1
        if vix is not None and vix >= 15.0:
            signals += 1
        if gift is None and sp is None and brent is None and vix is None:
            return None
        return "ELEVATED" if signals >= 1 else "CALM"

    def _write(self, rec: dict) -> None:
        try:
            with self._out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.records += 1
        except Exception:
            pass


def spawn_session_logger(adapter, underlyings, *, macro_provider=None,
                         window_ist=((9, 15), (15, 25)), poll_sec: int = 60):
    """Start a DAEMON thread that logs the cross-asset shadow for one session — pre_open once,
    then intraday constituent flow every poll_sec across the FULL session (07-14: was 09:15-09:35
    open-drive only; the 07-13 recovery proved the turn signal matters ALL day). It also keeps
    LAST_FLOW fresh so the trade path's adaptive override can read it. ALL network I/O
    (kite.quote) runs in this thread so it NEVER touches the tick/trade path.
    Logs-only writes; every failure is swallowed. Returns the Thread (already started) or None.
    """
    import threading
    import time
    from datetime import datetime, timezone, timedelta, date as _date

    _IST = timezone(timedelta(hours=5, minutes=30))

    def _run():
        try:
            feeds = KiteFeedProvider(adapter, macro_provider=macro_provider or MacroFeedProvider(adapter))
            shadow = CrossAssetShadow(feeds=feeds)
            today = datetime.now(_IST).date()
            for u in underlyings:
                shadow.pre_open(underlying=u, session_date=today)
            (sh, sm), (eh, em) = window_ist
            first = True
            while True:
                now = datetime.now(_IST)
                hm = (now.hour, now.minute)
                if hm > (eh, em):
                    break
                if hm >= (sh, sm):
                    # ONE kite.ltp() batch for every basket symbol across all
                    # underlyings per poll (batch-1 efficiency) — still entirely
                    # inside this daemon thread, so the tick path never blocks.
                    snap = feeds.constituent_ltps(all_basket_symbols(underlyings))
                    for u in underlyings:
                        # `snap or None`: a transiently-failed batch falls back to
                        # the feed's own resolution inside intraday_flow.
                        shadow.intraday_flow(underlying=u, timestamp=datetime.now(timezone.utc),
                                             is_session_open=first, ltp_snapshot=snap or None)
                    first = False
                time.sleep(max(5, poll_sec))
        except Exception:
            pass

    try:
        import threading as _t
        th = _t.Thread(target=_run, name="crossasset-shadow", daemon=True)
        th.start()
        return th
    except Exception:
        return None
