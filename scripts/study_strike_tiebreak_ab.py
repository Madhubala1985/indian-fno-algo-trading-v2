#!/usr/bin/env python3
"""
P1 replay A/B — STRIKE_TIEBREAK variants vs base (night 2026-07-18 burst).

Motivation (07-17 report §3a/§7): the live 07-17 BANKNIFTY trend day was lost to
115 consecutive SCORE_MARGIN blocks caused by a scoring near-tie between 58000CE
and the 59,000-OI-wall's 59000CE (oi_support=1.00 offsets worse delta/theta).

PRE-REGISTERED before computation:
- Corpus: every (underlying, day) with a cached chain in data/option_history/
  AND 1m index bars in data/<u>_1m_*.csv. 2026-07-17 has chains but no index
  bars (CSVs end 07-16) — excluded from the primary corpus, noted in report.
- Variants: base (knob OFF), delta (near-tie -> higher-|delta| entry instead of
  SCORE_MARGIN block), oicap (chain-max-OI wall strike's oi_support capped at
  0.60 before ranking; margin recomputed; gate unchanged).
- Metrics per variant: total net (after costs), ex-best-day net, n trades,
  win rate, per-day diff vs base.
- Decision rule: a variant is proposal-worthy iff net > base AND ex-best-day
  net > base ex-best-day. Otherwise FAIL — no cherry-picking days.

Usage:
  PYTHONPATH=. venv/bin/python scripts/study_strike_tiebreak_ab.py \
      [--underlying NIFTY] [--variants base,delta,oicap] [--days 2026-07-15,...]
Appends one JSON line per (underlying, day, variant) run to
logs/study_strike_tiebreak_ab.jsonl and prints a summary table.
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import json
import re
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import platform_core.backtesting.surgical_replay as sr
import platform_core.options.strike_selection as ss
from platform_core.backtesting.backtest_runner import LiveBar
from platform_core.backtesting.historical_option_chain import HistoricalOptionChainFetcher

_CFG = {
    "SENSEX":    {"feed": "SENSEX",    "exch": "BFO", "step": 100.0, "lot": 20},
    "NIFTY":     {"feed": "NIFTY50",   "exch": "NFO", "step": 50.0,  "lot": 75},
    "BANKNIFTY": {"feed": "BANKNIFTY", "exch": "NFO", "step": 100.0, "lot": 30},
}
_CACHE_DIR = Path("data/option_history")
_OUT = Path("logs/study_strike_tiebreak_ab.jsonl")


def _load_csv(path: Path, feed: str) -> list[LiveBar]:
    bars = []
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


def _newest_csv(underlying: str) -> Path | None:
    pat = {"SENSEX": "sensex", "NIFTY": "nifty", "BANKNIFTY": "banknifty"}[underlying]
    files = [Path(p) for p in glob.glob(f"data/{pat}_1m_*.csv")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _corpus(underlying: str) -> list[tuple[date, date]]:
    """[(day, expiry)] — nearest cached expiry with dte>=2, mirrors replay_day's
    select_expiry(min_dte=2)."""
    by_day: dict[date, list[date]] = {}
    rx = re.compile(rf"{underlying}_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})\.jsonl$")
    for p in _CACHE_DIR.glob(f"{underlying}_*.jsonl"):
        m = rx.search(p.name)
        if not m:
            continue
        d, e = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        by_day.setdefault(d, []).append(e)
    out = []
    for d, exps in sorted(by_day.items()):
        ok = sorted(e for e in exps if (e - d).days >= 2)
        if ok:
            out.append((d, ok[0]))
    return out


def _synth_session_bars(underlying: str, day: date, feed: str) -> list[LiveBar]:
    """FLAGGED LOWER-FIDELITY EXTENSION: 1m bars from logs/regime_shadow.jsonl
    real recorded closes (O=prev close, H/L=max/min(O,C) — NO wicks). Structure
    events are degraded identically in every arm, so only the A/B *delta* is
    meaningful on these days, not absolute nets."""
    rows = []
    with open("logs/regime_shadow.jsonl") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("underlying") != underlying:
                continue
            ts = datetime.fromisoformat(d["ts"])
            if ts.astimezone(timezone.utc).date() != day:
                continue
            rows.append((ts, float(d["close"])))
    rows.sort()
    bars, prev = [], None
    for ts, close in rows:
        o = prev if prev is not None else close
        bars.append(LiveBar(symbol=feed, timeframe="MIN_1", start_timestamp=ts,
                            end_timestamp=ts + timedelta(minutes=1),
                            open_price=o, high_price=max(o, close),
                            low_price=min(o, close), close_price=close, volume=0.0))
        prev = close
    return bars


def _run_one(underlying: str, day: date, expiry: date, variant: str,
             warm: list[LiveBar], session: list[LiveBar], chain_by_min: dict,
             fetcher: HistoricalOptionChainFetcher, floor: float) -> dict:
    cfg = _CFG[underlying]
    # variant = "+"-joined tokens: base | delta | oicap | both (P1 tiebreak),
    # softNN (P2 same-contract soft cooldown, NN minutes, e.g. soft30),
    # gexspot (P3 gex_proximity scored on spot-side walls),
    # tf5 | tf15 (exit-TF promotion after arm), tf5e | tf15e (same, from ENTRY),
    # wideNN (suppress pre-arm 1m structure exits until peak ≥ +NN%)
    sr._STRIKE_TIEBREAK = ""
    sr._SOFT_CONTRACT_BLOCK_MIN = 0
    ss._GEX_SPOTSIDE = False
    sr._EXIT_STRUCT_TF_AFTER_ARM = ""
    sr._EXIT_STRUCT_TF_FROM_ENTRY = False
    sr._EXIT_BOS_MIN_PEAK_PCT = 0.0
    for tok in variant.split("+"):
        if tok in ("delta", "oicap", "both"):
            sr._STRIKE_TIEBREAK = tok
        elif tok.startswith("soft"):
            sr._SOFT_CONTRACT_BLOCK_MIN = int(tok[4:] or 30)
        elif tok == "gexspot":
            ss._GEX_SPOTSIDE = True
        elif tok.startswith("tf"):
            sr._EXIT_STRUCT_TF_AFTER_ARM = "MIN_" + tok[2:].rstrip("e")
            sr._EXIT_STRUCT_TF_FROM_ENTRY = tok.endswith("e")
        elif tok.startswith("wide"):
            sr._EXIT_BOS_MIN_PEAK_PCT = int(tok[4:]) / 100.0
    # Live tier-2/3 runs score_margin 0.05 (gate_profiles default); the replay
    # class default 0.015 is the confirmation-only profile and NEVER blocks on
    # this corpus — override per run for a live-faithful gate.
    sr.SurgicalReplay.MIN_SCORE_MARGIN = floor
    rep = sr.SurgicalReplay(underlying_symbol=underlying, feed_symbol=cfg["feed"])
    rep.warm_start(warm, cfg["feed"])
    res = rep.run_day(underlying=underlying, feed_symbol=cfg["feed"], exchange=cfg["exch"],
                      expiry=expiry, lot_size=cfg["lot"], strike_step=cfg["step"], day=day,
                      session_bars=session, chain_by_min=chain_by_min, fetcher=fetcher)
    return {
        "underlying": underlying, "day": day.isoformat(), "expiry": expiry.isoformat(),
        "floor": floor,
        "variant": variant, "net": round(sum(t.pnl for t in res.trades), 2), "n_trades": len(res.trades),
        "trades": [{"contract": t.contract, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
                    "lots": t.lots, "entry": t.entry_prem, "exit": t.exit_prem,
                    "pnl": round(t.pnl, 2), "reason": t.reason} for t in res.trades],
        "blocks": res.blocks,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default=None, choices=list(_CFG))
    ap.add_argument("--variants", default="base,delta,oicap")
    ap.add_argument("--days", default=None, help="comma-separated YYYY-MM-DD filter")
    ap.add_argument("--out", default=str(_OUT))
    ap.add_argument("--floor", type=float, default=0.015,
                    help="SCORE_MARGIN floor (0.015 = replay default; 0.05 = live tier-2/3)")
    ap.add_argument("--synth-days", default=None,
                    help="comma-separated days whose SESSION bars come from regime_shadow "
                         "closes (no index CSV coverage). Lower fidelity — flagged in output.")
    args = ap.parse_args()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    day_filter = set(args.days.split(",")) if args.days else None
    out = Path(args.out)

    rows = []
    for u in ([args.underlying] if args.underlying else list(_CFG)):
        cfg = _CFG[u]
        csv_path = _newest_csv(u)
        if csv_path is None:
            print(f"[{u}] no index CSV — skipped")
            continue
        all_bars = _load_csv(csv_path, cfg["feed"])
        fetcher = HistoricalOptionChainFetcher(None)
        for day, expiry in _corpus(u):
            if day_filter and day.isoformat() not in day_filter:
                continue
            warm = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() < day]
            session = [b for b in all_bars if b.start_timestamp.astimezone(timezone.utc).date() == day]
            synth = False
            if not session and args.synth_days and day.isoformat() in args.synth_days.split(","):
                session = _synth_session_bars(u, day, cfg["feed"])
                synth = True
            if not session:
                print(f"[{u} {day}] chain cached but no index bars — skipped")
                continue
            cache = _CACHE_DIR / f"{u}_{day.isoformat()}_{expiry.isoformat()}.jsonl"
            chain_by_min = fetcher.load_day(cache)
            for variant in variants:
                t0 = time.time()
                r = _run_one(u, day, expiry, variant, warm, session, chain_by_min,
                             fetcher, args.floor)
                r["secs"] = round(time.time() - t0, 1)
                r["synth_bars"] = synth
                rows.append(r)
                with out.open("a") as fh:
                    fh.write(json.dumps(r) + "\n")
                print(f"[{u} {day} {variant:5}] net={r['net']:>+10.0f} trades={r['n_trades']} "
                      f"({r['secs']}s)", flush=True)

    # ── summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("SUMMARY (net after costs, ₹)")
    print("=" * 72)
    for variant in variants:
        vr = [r for r in rows if r["variant"] == variant]
        if not vr:
            continue
        by_day: dict[str, float] = {}
        for r in vr:
            by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["net"]
        total = sum(by_day.values())
        best_day = max(by_day, key=by_day.get) if by_day else None
        ex_best = total - (by_day[best_day] if best_day else 0.0)
        trades = [t for r in vr for t in r["trades"]]
        wins = sum(1 for t in trades if t["pnl"] > 0)
        print(f"{variant:6}  days={len(by_day):2}  net={total:>+10.0f}  "
              f"ex-best({best_day})={ex_best:>+10.0f}  trades={len(trades)}  "
              f"win={wins}/{len(trades)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
