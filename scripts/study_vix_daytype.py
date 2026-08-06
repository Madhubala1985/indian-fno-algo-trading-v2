"""H4 (2026-07-19 evening): prior-day VIX level / change vs next-day NIFTY day-type.

Pre-registered in logs/night_explorer_2026-07-19.md before computation.
H4a: prior-day VIX close vs next-day range% — PASS iff Spearman rho >= +0.30,
     n >= 40, rho >= +0.20 ex-largest-range day.
H4b: prior-day VIX d% vs next-day range% — same bar.
Measurement (ungraded): both predictors vs trend_ratio = |C-O|/(H-L).

Usage: PYTHONPATH=. venv/bin/python scripts/study_vix_daytype.py
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

VIX_CSV = Path("data/source/indiavix_daily_120d.csv")
NIFTY_1M = Path("data/nifty_1m_2026-05-15_2026-07-16.csv")


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def main() -> None:
    # Daily VIX: date -> (close, dpct vs prior close)
    vix_rows = []
    with VIX_CSV.open() as f:
        for row in csv.DictReader(f):
            vix_rows.append((row["date"], float(row["close"])))
    vix_rows.sort()
    vix: dict[str, tuple[float, float]] = {}
    for i in range(1, len(vix_rows)):
        d, c = vix_rows[i]
        pc = vix_rows[i - 1][1]
        vix[d] = (c, (c / pc - 1.0) * 100.0)

    # NIFTY day metrics from 1m bars (chronological; O = first bar open, C = last bar close)
    days: dict[str, list[tuple[str, float, float, float, float]]] = defaultdict(list)
    with NIFTY_1M.open() as f:
        for row in csv.DictReader(f):
            ts = row["timestamp"]
            days[ts[:10]].append(
                (ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
            )

    day_metrics = []  # (date, range_pct, trend_ratio)
    for d in sorted(days):
        bars = sorted(days[d])
        o = bars[0][1]
        c = bars[-1][4]
        hi = max(b[2] for b in bars)
        lo = min(b[3] for b in bars)
        rng = hi - lo
        if o <= 0 or rng <= 0:
            continue
        day_metrics.append((d, rng / o * 100.0, abs(c - o) / rng))

    # Join: predictor from the most recent VIX day STRICTLY BEFORE day D
    vix_dates = sorted(vix)
    samples = []  # (date, vix_close_prev, vix_dpct_prev, range_pct, trend_ratio)
    for d, rng_pct, tr in day_metrics:
        prior = [vd for vd in vix_dates if vd < d]
        if not prior:
            continue
        pv = prior[-1]
        lvl, dpct = vix[pv]
        samples.append((d, lvl, dpct, rng_pct, tr))

    n = len(samples)
    lvls = [s[1] for s in samples]
    dpcts = [s[2] for s in samples]
    rngs = [s[3] for s in samples]
    trs = [s[4] for s in samples]

    rho_a = spearman(lvls, rngs)
    rho_b = spearman(dpcts, rngs)

    # ex-largest-range day
    imax = max(range(n), key=lambda i: rngs[i])
    ex = [i for i in range(n) if i != imax]
    rho_a_ex = spearman([lvls[i] for i in ex], [rngs[i] for i in ex])
    rho_b_ex = spearman([dpcts[i] for i in ex], [rngs[i] for i in ex])

    rho_tr_lvl = spearman(lvls, trs)
    rho_tr_dp = spearman(dpcts, trs)

    print(f"n={n} joined sessions {samples[0][0]}..{samples[-1][0]}")
    print(f"largest-range day (excluded in ex- rows): {samples[imax][0]} range%={rngs[imax]:.2f}")
    pass_a = rho_a >= 0.30 and n >= 40 and rho_a_ex >= 0.20
    pass_b = rho_b >= 0.30 and n >= 40 and rho_b_ex >= 0.20
    print(f"H4a VIX level  -> next-day range%: rho={rho_a:+.3f} ex-best={rho_a_ex:+.3f} -> {'PASS' if pass_a else 'FAIL'}")
    print(f"H4b VIX d%     -> next-day range%: rho={rho_b:+.3f} ex-best={rho_b_ex:+.3f} -> {'PASS' if pass_b else 'FAIL'}")
    print(f"measurement: VIX level -> trend_ratio rho={rho_tr_lvl:+.3f}; VIX d% -> trend_ratio rho={rho_tr_dp:+.3f}")

    # context: quartile means for readability
    order = sorted(range(n), key=lambda i: lvls[i])
    q = n // 4
    lo_q = [rngs[i] for i in order[:q]]
    hi_q = [rngs[i] for i in order[-q:]]
    print(f"context: mean range% lowest-VIX quartile={sum(lo_q)/len(lo_q):.2f}  highest-VIX quartile={sum(hi_q)/len(hi_q):.2f}")


if __name__ == "__main__":
    main()
