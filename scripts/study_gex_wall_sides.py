"""
study_gex_wall_sides.py — Unit check for the GEX wall degeneracy fix on
RECORDED option chains (night 2026-07-17).

BUG (flagged 07-15, quantified tonight from options_analytics_shadow.jsonl):
  call_wall_strike / put_wall_strike rank strikes by |gamma OI| with no
  spot-side constraint, so one dominant round strike frequently tops BOTH
  rankings — e.g. 07-15 BANKNIFTY call wall == put wall == 58000 in 221/379
  records, and the "put wall" sat ABOVE spot in 260/379. A support level
  above price is meaningless, and the (opt-in) gex_wall_block SHORT veto can
  never engage off it (fails its pw<=spot guard).

FIX UNDER TEST (additive fields in gamma_exposure.py, existing fields and
all existing consumers untouched):
  call_wall_above_strike — max |gamma OI| CALL strike at/above spot
  put_wall_below_strike  — max |gamma OI| PUT  strike at/below spot

METHOD (recorded data only):
  data/option_history/<U>_<day>_<expiry>.jsonl holds per-minute chain rows
  (strike, type, ltp, oi). Spot is recovered per snapshot via put-call
  parity (median of C−P+K·e^{−rT} across the 11 most balanced strikes); IV
  is implied from recorded ltp by bisection and gamma is Black-Scholes —
  the same construction the live path uses (GreeksEngine is BSM).
  Snapshots are taken hourly; the real GammaExposureEngine builds each one.

INVARIANTS CHECKED (hard PASS/FAIL):
  I1 new call wall is never below spot; new put wall never above spot.
  I2 when the legacy wall already sat on the correct side, new == legacy.
  I3 whenever any CALL (PUT) strike exists at/above (at/below) spot, the
     new field is non-None.
Plus a report: legacy degeneracy / wrong-side rates vs new, per day.

Usage: PYTHONPATH=. venv/bin/python scripts/study_gex_wall_sides.py
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from platform_core.options.gamma_exposure import (
    GammaExposureEngine,
    GammaExposurePoint,
)

R = 0.065
LOT = {"BANKNIFTY": 30, "NIFTY": 65, "SENSEX": 20}
CHAIN_DIR = Path("data/option_history")


def bs_price(cp: str, s: float, k: float, t: float, sigma: float) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, (s - k) if cp == "CE" else (k - s))
    d1 = (math.log(s / k) + (R + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    nd = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    if cp == "CE":
        return s * nd(d1) - k * math.exp(-R * t) * nd(d2)
    return k * math.exp(-R * t) * nd(-d2) - s * nd(-d1)


def implied_vol(cp: str, s: float, k: float, t: float, price: float) -> float | None:
    intrinsic = max(0.0, (s - k) if cp == "CE" else (k - s))
    if price <= intrinsic + 0.05:
        return None
    lo, hi = 0.01, 3.0
    if bs_price(cp, s, k, t, hi) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(cp, s, k, t, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_gamma(s: float, k: float, t: float, sigma: float) -> float:
    d1 = (math.log(s / k) + (R + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2.0 * math.pi) * s * sigma * math.sqrt(t))


def parity_spot(rows: list[dict], t: float) -> float | None:
    """Median C−P+K·e^{−rT} over the strikes with the smallest |C−P|."""
    by_strike: dict[float, dict] = defaultdict(dict)
    for r in rows:
        by_strike[r["strike"]][r["type"]] = r["ltp"]
    cands = []
    for k, d in by_strike.items():
        if "CE" in d and "PE" in d and d["CE"] > 0 and d["PE"] > 0:
            cands.append((abs(d["CE"] - d["PE"]), d["CE"] - d["PE"] + k * math.exp(-R * t)))
    if len(cands) < 3:
        return None
    cands.sort()
    vals = sorted(v for _, v in cands[:11])
    return vals[len(vals) // 2]


def main() -> int:
    engine = GammaExposureEngine()
    viol = {"I1": 0, "I2": 0, "I3": 0}
    n_snaps = 0
    print(f"{'file':<44} {'snap(UTC)':>9} {'spot':>8}  legacy cw/pw       new cw/pw")
    per_day = defaultdict(lambda: [0, 0, 0, 0])  # legacy_deg, legacy_wrongside, new_deg, snaps

    for path in sorted(CHAIN_DIR.glob("*.jsonl")):
        u, day, expiry_s = path.stem.split("_")
        expiry = date.fromisoformat(expiry_s)
        by_ts: dict[str, list[dict]] = defaultdict(list)
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_ts[r["ts"]].append(r)
        hourly = [ts for ts in sorted(by_ts) if ts[14:16] == "40"]  # :40 each hour
        for ts in hourly:
            rows = by_ts[ts]
            dt = datetime.fromisoformat(ts)
            tte = max(((expiry - dt.date()).days + 0.65) / 365.0, 1e-4)
            spot = parity_spot(rows, tte)
            if spot is None:
                continue
            points = []
            for r in rows:
                if not r.get("oi"):
                    continue
                iv = implied_vol(r["type"], spot, r["strike"], tte, r["ltp"])
                if iv is None:
                    continue
                points.append(GammaExposurePoint(
                    underlying_symbol=u,
                    expiry_date=expiry,
                    option_symbol=r["tradingsymbol"],
                    option_type="CALL" if r["type"] == "CE" else "PUT",
                    strike_price=r["strike"],
                    spot_price=spot,
                    lot_size=LOT[u],
                    open_interest=int(r["oi"]),
                    change_in_open_interest=0,
                    volume=int(r.get("volume") or 0),
                    time_to_expiry_years=tte,
                    gamma=bs_gamma(spot, r["strike"], tte, iv),
                    implied_volatility=iv,
                    observed_at=dt.astimezone(timezone.utc),
                ))
            if len(points) < 10:
                continue
            snap = engine.build_snapshot(points=points, valuation_timestamp=dt)
            n_snaps += 1
            cw, pw = snap.call_wall_strike, snap.put_wall_strike
            ncw, npw = snap.call_wall_above_strike, snap.put_wall_below_strike
            # invariants
            if (ncw is not None and ncw < spot) or (npw is not None and npw > spot):
                viol["I1"] += 1
            if cw is not None and cw >= spot and ncw != cw:
                viol["I2"] += 1
            if pw is not None and pw <= spot and npw != pw:
                viol["I2"] += 1
            has_call_above = any(p.option_type == "CALL" and p.strike_price >= spot for p in points)
            has_put_below = any(p.option_type == "PUT" and p.strike_price <= spot for p in points)
            if (has_call_above and ncw is None) or (has_put_below and npw is None):
                viol["I3"] += 1
            key = (day, u)
            per_day[key][3] += 1
            if cw == pw:
                per_day[key][0] += 1
            if (cw is not None and cw < spot) or (pw is not None and pw > spot):
                per_day[key][1] += 1
            if ncw is not None and npw is not None and ncw == npw:
                per_day[key][2] += 1
            print(f"{path.name:<44} {ts[11:16]:>9} {spot:8.0f}  "
                  f"{cw or 0:.0f}/{pw or 0:.0f}   {ncw or 0:.0f}/{npw or 0:.0f}")

    print("\n── per-day summary (legacy_degenerate / legacy_wrong_side / new_degenerate / snaps)")
    for (day, u), (ld, lw, nd_, n) in sorted(per_day.items()):
        print(f"  {day} {u:<10} legacy deg {ld}/{n}  wrong-side {lw}/{n}  new deg {nd_}/{n}")
    print(f"\nsnapshots={n_snaps}  I1 violations={viol['I1']}  I2={viol['I2']}  I3={viol['I3']}")
    ok = n_snaps > 0 and not any(viol.values())
    print("UNIT CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
