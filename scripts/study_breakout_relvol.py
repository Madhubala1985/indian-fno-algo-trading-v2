"""H5 (2026-07-19 evening): does trigger-minute RELATIVE VOLUME condition the
early day-range-breakout fade found by H3 (study_tod_breakout_decay.py)?

Pre-registered in logs/night_explorer_2026-07-19.md before computation.
Corpus: data/source/{NIFTY,BANKNIFTY,SENSEX}_FUT_1m_45d.csv (REAL futures volume).
Trigger rule = H3's: after 10:00 IST, first close making a new day-high -> LONG,
first close making a new day-low -> SHORT (one per side per day); exit 15:20
close; cost 0.006% per side. EARLY bucket only (trigger < 11:30 IST).
Signal: relvol = trigger-minute volume / mean volume of the prior 20 minutes.
PASS iff mean net% (relvol >= 2.0) < mean net% (relvol < 2.0) AND the high-relvol
mean < 0, n >= 12 per bucket, both surviving removal of the high bucket's best day.

In-sample caveat: window overlaps H3's corpus — hypothesis-generating only.

Usage: PYTHONPATH=. venv/bin/python scripts/study_breakout_relvol.py
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

FILES = {
    "NIFTY": Path("data/source/NIFTY_FUT_1m_45d.csv"),
    "BANKNIFTY": Path("data/source/BANKNIFTY_FUT_1m_45d.csv"),
    "SENSEX": Path("data/source/SENSEX_FUT_1m_45d.csv"),
}
COST_PER_SIDE = 0.00006  # 0.006% per side
RELVOL_SPLIT = 2.0


def main() -> None:
    trades = []  # (symbol, date, side, hhmm, relvol, net_pct)
    for sym, path in FILES.items():
        days: dict[str, list[tuple[str, float, float, float, float, float]]] = defaultdict(list)
        with path.open() as f:
            for row in csv.DictReader(f):
                ts = row["timestamp"]  # "2026-06-02 09:15:00+05:30" (IST)
                days[ts[:10]].append(
                    (ts[11:16], float(row["open"]), float(row["high"]),
                     float(row["low"]), float(row["close"]), float(row["volume"]))
                )
        for d in sorted(days):
            bars = sorted(days[d])
            day_hi = -1e18
            day_lo = 1e18
            fired = {"LONG": False, "SHORT": False}
            exit_close = None
            for b in bars:
                if b[0] <= "15:20":
                    exit_close = b[4]
            if exit_close is None:
                continue
            vols = [b[5] for b in bars]
            for i, (hhmm, o, h, l, c, v) in enumerate(bars):
                if hhmm >= "10:00" and hhmm < "11:30":
                    side = None
                    if not fired["LONG"] and c > day_hi:
                        side = "LONG"
                    elif not fired["SHORT"] and c < day_lo:
                        side = "SHORT"
                    if side is not None:
                        fired[side] = True
                        prior = vols[max(0, i - 20):i]
                        mv = sum(prior) / len(prior) if prior else 0.0
                        relvol = v / mv if mv > 0 else float("nan")
                        gross = (exit_close / c - 1.0) if side == "LONG" else (1.0 - exit_close / c)
                        net = gross * 100.0 - 2 * COST_PER_SIDE * 100.0
                        if relvol == relvol:  # NaN guard
                            trades.append((sym, d, side, hhmm, relvol, net))
                # day extremes update AFTER the trigger check (close-based break of
                # the PRIOR bars' range), matching study_tod_breakout_decay.py
                day_hi = max(day_hi, h)
                day_lo = min(day_lo, l)

    hi_b = [t for t in trades if t[4] >= RELVOL_SPLIT]
    lo_b = [t for t in trades if t[4] < RELVOL_SPLIT]

    def stats(bucket):
        if not bucket:
            return 0, 0.0, 0.0, 0
        n = len(bucket)
        mean = sum(t[5] for t in bucket) / n
        best = max(bucket, key=lambda t: t[5])
        ex = [t for t in bucket if t is not best]
        mean_ex = sum(t[5] for t in ex) / len(ex) if ex else 0.0
        wins = sum(1 for t in bucket if t[5] > 0)
        return n, mean, mean_ex, wins

    n_hi, m_hi, mex_hi, w_hi = stats(hi_b)
    n_lo, m_lo, mex_lo, w_lo = stats(lo_b)
    print(f"EARLY (<11:30) triggers with valid relvol: {len(trades)}")
    print(f"high relvol>= {RELVOL_SPLIT}: n={n_hi} mean_net%={m_hi:+.3f} ex-best={mex_hi:+.3f} win={w_hi}/{n_hi}")
    print(f"low  relvol<  {RELVOL_SPLIT}: n={n_lo} mean_net%={m_lo:+.3f} ex-best={mex_lo:+.3f} win={w_lo}/{n_lo}")
    # ex-best comparison uses the high bucket's best-day removal per pre-registration
    ok = (n_hi >= 12 and n_lo >= 12 and m_hi < m_lo and m_hi < 0
          and mex_hi < m_lo and mex_hi < 0)
    print(f"H5 verdict: {'PASS' if ok else 'FAIL'}")
    for sym in FILES:
        st = [t for t in trades if t[0] == sym]
        sh = [t for t in st if t[4] >= RELVOL_SPLIT]
        sl = [t for t in st if t[4] < RELVOL_SPLIT]
        mh = sum(t[5] for t in sh) / len(sh) if sh else float("nan")
        ml = sum(t[5] for t in sl) / len(sl) if sl else float("nan")
        print(f"  {sym}: hi n={len(sh)} mean={mh:+.3f} | lo n={len(sl)} mean={ml:+.3f}")
    # relvol distribution context
    rv = sorted(t[4] for t in trades)
    if rv:
        med = rv[len(rv) // 2]
        print(f"relvol median={med:.2f} p75={rv[int(len(rv)*0.75)]:.2f} max={rv[-1]:.2f}")


if __name__ == "__main__":
    main()
