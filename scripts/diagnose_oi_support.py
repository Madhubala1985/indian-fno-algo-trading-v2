"""
diagnose_oi_support.py
──────────────────────────────────────────────────────────────────────
Post-market OI support diagnostic.

Reads logs/strike_selection_audit.jsonl and answers:
  1. What is the raw OI of the selected strike vs. chain max OI?
  2. Is oi_support low because the chain_max_oi denominator is huge,
     or because the selected strike genuinely has thin OI?
  3. Which strikes actually have the highest OI in the chain?

Optional --live --symbol NIFTY / SENSEX / BANKNIFTY:
  Fetches the current option chain from Zerodha Kite and computes
  OI distribution for the front-month expiry (read-only, no orders).

Usage:
  # Offline (uses audit JSONL only)
  PYTHONPATH=. python3 scripts/diagnose_oi_support.py

  # Live chain (requires valid kite_config.json access token)
  PYTHONPATH=. python3 scripts/diagnose_oi_support.py --live --symbol NIFTY
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median, quantiles
from typing import Any

AUDIT_PATH = Path("logs/strike_selection_audit.jsonl")
CONFIG_PATH = Path("configs/kite_config.json")

# ── helpers ────────────────────────────────────────────────────────────────────

def _pct(x: float, total: float) -> str:
    if total <= 0:
        return "n/a"
    return f"{x / total * 100:.1f}%"


def _fmt(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.0f}"
    return str(x)


def _load_audit() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    records: list[dict] = []
    with AUDIT_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


# ── offline analysis ───────────────────────────────────────────────────────────

def _analyze_offline(records: list[dict]) -> None:
    if not records:
        print("[WARN] No audit records found in", AUDIT_PATH)
        print("       Start a live session to populate the audit log.")
        return

    live_records = [r for r in records if r.get("mode") != "shadow"]
    print(f"\n[AUDIT] {len(live_records)} live records from {AUDIT_PATH}")

    by_underlying: dict[str, list[dict]] = defaultdict(list)
    for r in live_records:
        by_underlying[r.get("underlying", "UNKNOWN")].append(r)

    for sym, recs in sorted(by_underlying.items()):
        print(f"\n{'═' * 70}")
        print(f"  UNDERLYING: {sym}  ({len(recs)} audit records)")
        print(f"{'═' * 70}")

        oi_support_vals: list[float] = []
        raw_oi_vals: list[int] = []
        chain_max_vals: list[int] = []
        score_margins: list[float] = []
        selected_oi_pcts: list[float] = []

        # top5 OI analysis per record
        runner_up_same: int = 0
        runner_up_diff: int = 0
        all_runner_up: list[str] = []

        for r in recs:
            # Factor scores for selected strike
            fs = r.get("factor_scores") or {}
            oi_supp = fs.get("oi_support")
            if oi_supp is not None:
                oi_support_vals.append(oi_supp)

            raw_oi = r.get("oi")
            if raw_oi is not None:
                raw_oi_vals.append(int(raw_oi))

            chain_max = r.get("chain_max_oi_est")
            if chain_max:
                chain_max_vals.append(int(chain_max))
                if raw_oi:
                    selected_oi_pcts.append(raw_oi / chain_max * 100.0)

            sm = r.get("score_margin")
            if sm is not None:
                score_margins.append(sm)

            # Runner-up
            rup = r.get("runner_up_contract")
            sel = r.get("selected_symbol")
            if rup:
                all_runner_up.append(rup)
                if rup == sel:
                    runner_up_same += 1
                else:
                    runner_up_diff += 1

        # ── OI summary ────────────────────────────────────────────────────
        print(f"\n  OI Support (selected strike, normalized [0,1])")
        if oi_support_vals:
            qs = quantiles(oi_support_vals, n=20)  # 5th percentile increments
            print(f"    count  : {len(oi_support_vals)}")
            print(f"    min    : {min(oi_support_vals):.4f}")
            print(f"    p25    : {qs[4]:.4f}")
            print(f"    p50    : {median(oi_support_vals):.4f}")
            print(f"    p75    : {qs[14]:.4f}")
            print(f"    p90    : {qs[17]:.4f}")
            print(f"    max    : {max(oi_support_vals):.4f}")
            below_10 = sum(1 for v in oi_support_vals if v < 0.10)
            print(f"    below 0.10 threshold: {below_10}/{len(oi_support_vals)} "
                  f"({below_10/len(oi_support_vals)*100:.0f}%)")
        else:
            print("    (no data)")

        print(f"\n  Raw OI of selected strike")
        if raw_oi_vals:
            print(f"    min  : {min(raw_oi_vals):,}")
            print(f"    p50  : {int(median(raw_oi_vals)):,}")
            print(f"    max  : {max(raw_oi_vals):,}")
        else:
            print("    (no data)")

        print(f"\n  Chain max OI (highest single strike, all candidates)")
        if chain_max_vals:
            print(f"    min  : {min(chain_max_vals):,}")
            print(f"    p50  : {int(median(chain_max_vals)):,}")
            print(f"    max  : {max(chain_max_vals):,}")
        else:
            print("    (no data)  — run after upgrading to newer audit format")

        print(f"\n  Selected strike OI as % of chain max")
        if selected_oi_pcts:
            print(f"    min  : {min(selected_oi_pcts):.2f}%")
            print(f"    p50  : {median(selected_oi_pcts):.2f}%")
            print(f"    max  : {max(selected_oi_pcts):.2f}%")
        else:
            print("    (no data)")

        # ── Score margin summary ───────────────────────────────────────────
        print(f"\n  Score Margin (selected - runner-up)")
        if score_margins:
            qs2 = quantiles(score_margins, n=20)
            print(f"    count  : {len(score_margins)}")
            print(f"    min    : {min(score_margins):.4f}")
            print(f"    p50    : {median(score_margins):.4f}")
            print(f"    p75    : {qs2[14]:.4f}")
            print(f"    p90    : {qs2[17]:.4f}")
            print(f"    max    : {max(score_margins):.4f}")
        else:
            print("    (no data)")

        # ── Runner-up analysis ────────────────────────────────────────────
        print(f"\n  Runner-up contracts")
        if all_runner_up:
            from collections import Counter
            rup_counts = Counter(all_runner_up).most_common(5)
            for contract, cnt in rup_counts:
                print(f"    {contract:40s}  n={cnt}")

        # ── Top-5 OI distribution (from newest records with enhanced top5) ─
        print(f"\n  Top-5 per-candidate OI distribution (last 5 records)")
        recent = [r for r in recs[-5:] if r.get("top5") and
                  any("oi_support" in c for c in r.get("top5", []))]
        if recent:
            for r in recent[-2:]:
                t = r.get("ts", "?")[:19]
                spot = r.get("spot", 0)
                print(f"\n    ts={t}  spot={spot:.0f}")
                print(f"    {'Rank':>4}  {'Symbol':35s}  {'Strike':>8}  "
                      f"{'OI_raw':>10}  {'oi_supp':>8}  {'Score':>7}")
                for c in r["top5"]:
                    print(f"    {c['rank']:>4}  {c['symbol']:35s}  "
                          f"{c['strike']:>8.0f}  "
                          f"{c.get('oi_raw', c.get('oi', 0)):>10,}  "
                          f"{c.get('oi_support', 0):>8.4f}  "
                          f"{c['score']:>7.4f}")
                chain_oi = r.get("chain_oi_context", {})
                if chain_oi.get("available"):
                    print(f"    chain: max_pain={_fmt(chain_oi.get('max_pain_strike'))}  "
                          f"highest_oi_strike={_fmt(chain_oi.get('highest_total_oi_strike'))}  "
                          f"total_put_oi={_fmt(chain_oi.get('total_put_oi'))}  "
                          f"pcr={chain_oi.get('put_call_ratio', 'n/a')}")
                elif r.get("chain_max_oi_est"):
                    print(f"    chain_max_oi_est={r['chain_max_oi_est']:,}  "
                          f"(no OI snapshot available)")
        else:
            print("    (audit records predate enhanced top5 format)")
            print("    Run a new live session to populate runner-up/OI breakdown data.")

        # ── Verdict ───────────────────────────────────────────────────────
        print(f"\n  VERDICT for {sym}:")
        if not oi_support_vals:
            print("    INSUFFICIENT_DATA — cannot determine OI issue source")
        elif max(oi_support_vals) < 0.10:
            if chain_max_vals and min(chain_max_vals) > 0:
                max_chain = max(chain_max_vals)
                max_sel   = max(raw_oi_vals) if raw_oi_vals else 0
                if max_chain > 0 and max_sel / max_chain < 0.10:
                    print(f"    GENUINE_LOW_OI — selected strike raw OI ({max_sel:,}) "
                          f"is consistently < 10% of chain max ({max_chain:,}).")
                    print(f"    The 0.10 oi_support threshold is correctly blocking this strike.")
                    print(f"    Action: investigate whether a different strike (at highest_oi_strike)")
                    print(f"            would be a better candidate.")
                else:
                    print(f"    POSSIBLE_NORMALIZATION_ISSUE — raw OI appears non-trivial")
                    print(f"    but normalized oi_support is still low.")
                    print(f"    Run with --live --symbol {sym} to fetch current chain OI.")
            else:
                print(f"    INSUFFICIENT_DATA_FOR_VERDICT — no chain_max_oi data.")
                print(f"    Run a new live session (audit now records chain_max_oi_est).")
        else:
            print(f"    MIXED — some records pass 0.10 oi_support; others fail.")
            print(f"    Not a systematic data failure. Review individual records.")


# ── live chain analysis ────────────────────────────────────────────────────────

def _analyze_live(symbol: str) -> None:
    try:
        import json as _json
        cfg = _json.loads(CONFIG_PATH.read_text())
        api_key    = cfg["api_key"]
        access_tok = cfg.get("access_token", "")
    except Exception as exc:
        print(f"[ERROR] Could not read Kite config: {exc}")
        sys.exit(1)

    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError:
        print("[ERROR] pykiteconnect not installed — cannot fetch live chain.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_tok)

    # Map option underlying to exchange segment
    _SEG = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}
    seg = _SEG.get(symbol.upper(), "NFO")

    print(f"\n[LIVE] Fetching option chain for {symbol} from {seg} ...")
    try:
        instruments = kite.instruments(seg)
    except Exception as exc:
        print(f"[ERROR] Failed to fetch instruments: {exc}")
        sys.exit(1)

    from datetime import date, timedelta
    today = date.today()
    # Find front-month expiry
    options = [
        i for i in instruments
        if i.get("name", "").upper() == symbol.upper()
        and i.get("instrument_type") in ("CE", "PE")
        and i.get("expiry") and i["expiry"] >= today
    ]
    if not options:
        print(f"[ERROR] No options found for {symbol}")
        sys.exit(1)

    expiries = sorted({i["expiry"] for i in options})
    front_expiry = expiries[0]
    print(f"[LIVE] Front expiry: {front_expiry}  ({len(expiries)} expiries available)")

    chain = [i for i in options if i["expiry"] == front_expiry]
    tokens = [i["instrument_token"] for i in chain[:100]]  # max 100 at a time

    try:
        quotes = kite.quote(tokens[:100])
    except Exception as exc:
        print(f"[ERROR] Failed to fetch quotes: {exc}")
        sys.exit(1)

    strikes_oi: dict[float, dict[str, int]] = defaultdict(lambda: {"CE": 0, "PE": 0})
    for instr in chain:
        tok = str(instr["instrument_token"])
        q = quotes.get(tok) or quotes.get(instr.get("tradingsymbol", ""))
        if q:
            oi = q.get("oi", 0) or 0
            strikes_oi[instr["strike"]][instr["instrument_type"]] += oi

    # Spot price
    try:
        spot_tok = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK",
                    "SENSEX": "BSE:SENSEX"}.get(symbol.upper(), "NSE:NIFTY 50")
        spot_q = kite.quote([spot_tok])
        spot = list(spot_q.values())[0].get("last_price", 0)
    except Exception:
        spot = 0.0

    print(f"[LIVE] Spot: {spot:.0f}  |  Chain strikes: {len(strikes_oi)}")
    print(f"\n  OI distribution (top 10 strikes by total OI)")
    sorted_strikes = sorted(strikes_oi.items(),
                            key=lambda kv: kv[1]["CE"] + kv[1]["PE"],
                            reverse=True)
    total_put_oi = sum(v["PE"] for v in strikes_oi.values())
    total_call_oi = sum(v["CE"] for v in strikes_oi.values())
    chain_max_oi = max((v["CE"] + v["PE"] for v in strikes_oi.values()), default=1)

    print(f"  {'Strike':>8}  {'PE_OI':>12}  {'CE_OI':>12}  {'Total':>12}  "
          f"{'NormOI':>8}  {'vs_ATM':>8}")
    for strike, oi_dict in sorted_strikes[:10]:
        total = oi_dict["CE"] + oi_dict["PE"]
        norm = total / chain_max_oi
        atm_dist = strike - spot if spot > 0 else 0
        print(f"  {strike:>8.0f}  {oi_dict['PE']:>12,}  {oi_dict['CE']:>12,}  "
              f"{total:>12,}  {norm:>8.4f}  {atm_dist:>+8.0f}")

    # Show ATM ±3 strikes
    if spot > 0:
        step_guesses = [50, 100, 200, 500]
        for step in step_guesses:
            atm = round(spot / step) * step
            near_strikes = sorted(
                (s for s in strikes_oi if abs(s - atm) <= 3 * step),
                key=lambda s: abs(s - atm)
            )[:7]
            if near_strikes:
                print(f"\n  Near-ATM strikes (±3 × {step}pt step, ATM≈{atm:.0f})")
                print(f"  {'Strike':>8}  {'PE_OI':>12}  {'CE_OI':>12}  {'NormOI':>8}")
                for s in near_strikes:
                    d = strikes_oi[s]
                    total = d["CE"] + d["PE"]
                    norm = total / chain_max_oi
                    print(f"  {s:>8.0f}  {d['PE']:>12,}  {d['CE']:>12,}  {norm:>8.4f}")
                break

    print(f"\n  PCR (OI): {total_put_oi / max(total_call_oi, 1):.3f}")
    print(f"  Chain max OI: {chain_max_oi:,}")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose OI support values")
    parser.add_argument("--live",   action="store_true", help="Fetch live chain from Kite")
    parser.add_argument("--symbol", default="NIFTY",     help="Underlying symbol for --live")
    args = parser.parse_args()

    records = _load_audit()

    if args.live:
        _analyze_live(args.symbol.upper())
    else:
        _analyze_offline(records)

    print("\n[DONE]")


if __name__ == "__main__":
    main()
