"""
study_gex_gate_spotside_ab.py — verify the gex_wall_block wall-source switch
(night 2026-07-18 burst, P3) on RECORDED option chains.

Rebuilds the same hourly GEX snapshots as study_gex_wall_sides.py (real
GammaExposureEngine on data/option_history/ chains, parity spot, BSM greeks)
and evaluates gex_wall_block for BOTH hypothetical biases per snapshot:

  legacy source : a proxy object exposing only call_wall_strike/put_wall_strike
                  (what every pre-b42cb59 recording provides — exercises the
                  gate's fallback path)
  spot-side     : the full snapshot (gate now prefers call_wall_above_strike /
                  put_wall_below_strike)

Reported per verdict flip class (wall veto only — block_long_gamma=False; the
regime veto is untouched by the switch):
  FAIL_OPEN_FIXED   legacy ok → new would-block (legacy wall was wrong-side or
                    degenerate, gate silently fail-open)
  BLOCK_RETRACTED   legacy would-block → new ok (legacy blocked off a wall the
                    spot-side ranking places elsewhere)
Identical verdicts are counted for the concordance rate. Also asserts the
regression guarantee: on snapshots where the legacy wall already sat on the
correct side, verdicts must be identical (I2 corollary).

Usage: PYTHONPATH=. venv/bin/python scripts/study_gex_gate_spotside_ab.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from platform_core.options.gamma_exposure import GammaExposureEngine, GammaExposurePoint
from platform_core.options.gex_gate import gex_wall_block
from study_gex_wall_sides import LOT, bs_gamma, implied_vol, parity_spot

CHAIN_DIR = Path("data/option_history")
PROXIMITY = 0.005  # live default GEX_WALL_PROXIMITY_PCT


class _LegacyView:
    """What a pre-fix snapshot looks like to the gate: legacy walls only."""

    def __init__(self, snap):
        self.call_wall_strike = snap.call_wall_strike
        self.put_wall_strike = snap.put_wall_strike
        self.net_gamma_regime = snap.net_gamma_regime


def main() -> int:
    engine = GammaExposureEngine()
    n_snaps = 0
    flips = defaultdict(int)      # (bias, kind) -> count
    concordant = 0
    regression_violations = 0
    per_day_flips = defaultdict(int)

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
        for ts in (t for t in sorted(by_ts) if t[14:16] == "40"):
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
                    underlying_symbol=u, expiry_date=expiry,
                    option_symbol=r["tradingsymbol"],
                    option_type="CALL" if r["type"] == "CE" else "PUT",
                    strike_price=r["strike"], spot_price=spot, lot_size=LOT[u],
                    open_interest=int(r["oi"]), change_in_open_interest=0,
                    volume=int(r.get("volume") or 0), time_to_expiry_years=tte,
                    gamma=bs_gamma(spot, r["strike"], tte, iv),
                    implied_volatility=iv,
                    observed_at=dt.astimezone(timezone.utc),
                ))
            if len(points) < 10:
                continue
            snap = engine.build_snapshot(points=points, valuation_timestamp=dt)
            n_snaps += 1
            legacy = _LegacyView(snap)
            legacy_correct_side = (
                (snap.call_wall_strike is None or snap.call_wall_strike >= spot)
                and (snap.put_wall_strike is None or snap.put_wall_strike <= spot)
            )
            for bias in ("LONG", "SHORT"):
                old = gex_wall_block(bias=bias, spot=spot, gex=legacy,
                                     proximity_pct=PROXIMITY)
                new = gex_wall_block(bias=bias, spot=spot, gex=snap,
                                     proximity_pct=PROXIMITY)
                if (old is None) == (new is None):
                    concordant += 1
                    continue
                kind = "FAIL_OPEN_FIXED" if old is None else "BLOCK_RETRACTED"
                flips[(bias, kind)] += 1
                per_day_flips[(day, u)] += 1
                if legacy_correct_side:
                    regression_violations += 1
                    print(f"REGRESSION {path.name} {ts[11:16]} {bias}: old={old} new={new}")

    total_verdicts = n_snaps * 2
    print(f"\nsnapshots={n_snaps}  verdicts={total_verdicts}  "
          f"concordant={concordant} ({concordant / max(total_verdicts, 1):.1%})")
    for (bias, kind), n in sorted(flips.items()):
        print(f"  {bias:5} {kind:16} {n}")
    print("\nper-(day,underlying) flip counts:")
    for (day, u), n in sorted(per_day_flips.items()):
        print(f"  {day} {u:<10} {n}")
    print(f"\nregression violations (verdict changed though legacy walls were "
          f"correct-side): {regression_violations}")
    ok = n_snaps > 0 and regression_violations == 0
    print("VERIFY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
