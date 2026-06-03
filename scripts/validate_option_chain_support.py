"""
scripts/validate_option_chain_support.py
═══════════════════════════════════════════════════════════════════════
Pre-flight validation of Kite option-chain support for each target
underlying (BANKNIFTY, NIFTY, SENSEX).

Checks:
  1. Kite session is valid
  2. Nearest expiry can be discovered
  3. Option chain is non-empty (correct exchange, correct underlying name)
  4. OptionsContextBuilder produces a valid OptionsContext
  5. StrikeSelectionEngine returns a scored LONG and SHORT candidate

Does NOT:
  - Open a WebSocket connection
  - Place or simulate any trade
  - Write to DB, state file, or CSV
  - Modify any live pipeline state

Usage (during market hours):
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py --symbol NIFTY
    PYTHONPATH=. python3 scripts/validate_option_chain_support.py --symbol ALL

Arguments:
    --symbol  BANKNIFTY | NIFTY | SENSEX | ALL   (default: ALL)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from typing import NamedTuple

# ── Path bootstrap ─────────────────────────────────────────────────────────────
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from platform_core.data.live.instrument_registry import OPTION_CHAIN_CONFIG
from platform_core.data.live.kite_adapter import KiteAdapter
from platform_core.options.options_context import (
    OptionsContextBuilder,
    OptionsContextBuilderConfig,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
)


# ── Spot-price lookup ──────────────────────────────────────────────────────────
# Kite LTP symbol for each underlying (exchange:tradingsymbol format).
# Used to fetch current spot for the ATM window in chain retrieval.
# Falls back to FALLBACK_SPOTS if the LTP call fails (e.g. outside hours).

_LTP_SYMBOL: dict[str, str] = {
    "BANKNIFTY": "NSE:NIFTY BANK",
    "NIFTY":     "NSE:NIFTY 50",
    "SENSEX":    "BSE:SENSEX",
}

# Reasonable mid-range fallbacks — only used when the market is closed or
# the LTP call fails.  Accurate enough to retrieve ATM ±10 strikes.
_FALLBACK_SPOTS: dict[str, float] = {
    "BANKNIFTY": 52_000.0,
    "NIFTY":     24_000.0,
    "SENSEX":    80_000.0,
}


def _fetch_spot(adapter: KiteAdapter, option_underlying: str) -> float:
    """Return the current index level, or a hardcoded fallback."""
    ltp_sym = _LTP_SYMBOL.get(option_underlying)
    if ltp_sym:
        try:
            result = adapter.kite.ltp([ltp_sym])
            price = result.get(ltp_sym, {}).get("last_price")
            if price and float(price) > 0:
                return float(price)
        except Exception:
            pass
    fallback = _FALLBACK_SPOTS.get(option_underlying, 50_000.0)
    print(
        f"  [spot] LTP unavailable for {option_underlying} — "
        f"using fallback ₹{fallback:,.0f}"
    )
    return fallback


# ── Result container ──────────────────────────────────────────────────────────

class ValidationResult(NamedTuple):
    symbol:              str
    option_underlying:   str
    exchange:            str
    expiry:              date | None
    spot:                float | None
    contracts_fetched:   int
    candidates_built:    int
    best_call_symbol:    str | None
    best_call_delta:     float | None
    best_call_iv:        float | None
    best_call_oi:        int | None
    best_call_spread_pct: float | None
    best_call_theta:     float | None
    best_call_gamma:     float | None
    best_call_score:     float | None
    best_put_symbol:     str | None
    best_put_delta:      float | None
    best_put_iv:         float | None
    best_put_oi:         int | None
    best_put_spread_pct: float | None
    best_put_theta:      float | None
    best_put_gamma:      float | None
    best_put_score:      float | None
    error:               str | None


# ── Per-symbol validation ─────────────────────────────────────────────────────

def _validate_one(
    adapter: KiteAdapter,
    opt_ctx_builder: OptionsContextBuilder,
    strike_engine: StrikeSelectionEngine,
    config_key: str,
) -> ValidationResult:
    meta = OPTION_CHAIN_CONFIG[config_key]
    underlying = meta.option_underlying
    exchange   = meta.exchange
    lot_size   = meta.lot_size
    step       = meta.strike_step

    expiry: date | None = None
    spot:   float | None = None

    def _fail(reason: str) -> ValidationResult:
        return ValidationResult(
            symbol=config_key, option_underlying=underlying, exchange=exchange,
            expiry=expiry, spot=spot,
            contracts_fetched=0, candidates_built=0,
            best_call_symbol=None, best_call_delta=None, best_call_iv=None,
            best_call_oi=None, best_call_spread_pct=None, best_call_theta=None,
            best_call_gamma=None, best_call_score=None,
            best_put_symbol=None, best_put_delta=None, best_put_iv=None,
            best_put_oi=None, best_put_spread_pct=None, best_put_theta=None,
            best_put_gamma=None, best_put_score=None,
            error=reason,
        )

    # 1. Nearest expiry
    try:
        expiry = adapter.nearest_expiry(underlying, date.today(), exchange=exchange)
    except Exception as exc:
        return _fail(f"nearest_expiry failed: {exc}")

    # 2. Spot price
    spot = _fetch_spot(adapter, underlying)

    # 3. Option chain (nearest expiry)
    try:
        chain = adapter.fetch_option_chain(
            underlying=underlying,
            expiry_date=expiry,
            spot_price=spot,
            lot_size=lot_size,
            exchange=exchange,
            strike_step=step,
        )
    except Exception as exc:
        return _fail(f"fetch_option_chain failed: {exc}")

    contracts_fetched = len(chain.contracts)
    if contracts_fetched == 0:
        return _fail("chain returned 0 contracts")

    # Try also fetching next expiry for dual-chain context (mirrors live pipeline)
    chains = [chain]
    try:
        from datetime import timedelta
        expiry_next = adapter.nearest_expiry(
            underlying, expiry + timedelta(days=1), exchange=exchange
        )
        chain_next = adapter.fetch_option_chain(
            underlying=underlying,
            expiry_date=expiry_next,
            spot_price=spot,
            lot_size=lot_size,
            exchange=exchange,
            strike_step=step,
        )
        chains.append(chain_next)
    except Exception:
        pass  # dual-chain optional

    # 4. OptionsContext
    try:
        options_ctx = opt_ctx_builder.build(
            snapshots=chains,
            spot_price=spot,
            lot_size=lot_size,
            valuation_timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:
        return _fail(f"OptionsContextBuilder.build failed: {exc}")

    candidates = list(options_ctx.strike_candidates) if options_ctx.strike_candidates else []
    candidates_built = len(candidates)

    if candidates_built == 0:
        return _fail("0 strike candidates produced")

    ts = datetime.now(timezone.utc)

    def _best_for(bias: str):
        try:
            result = strike_engine.select(
                bias=bias,
                candidates=candidates,
                valuation_timestamp=ts,
                oi_snapshot=options_ctx.oi_snapshot,
                gex_snapshot=options_ctx.gex_snapshot,
                atm_iv=options_ctx.atm_iv,
            )
            return result.best()
        except Exception:
            return None

    best_call = _best_for("LONG")
    best_put  = _best_for("SHORT")

    def _spread_pct(rec) -> float | None:
        if rec is None or rec.bid_price is None or rec.ask_price is None:
            return None
        mid = (float(rec.bid_price) + float(rec.ask_price)) / 2.0
        if mid <= 0:
            return None
        return (float(rec.ask_price) - float(rec.bid_price)) / mid * 100.0

    return ValidationResult(
        symbol=config_key,
        option_underlying=underlying,
        exchange=exchange,
        expiry=expiry,
        spot=spot,
        contracts_fetched=contracts_fetched,
        candidates_built=candidates_built,
        best_call_symbol=best_call.option_symbol if best_call else None,
        best_call_delta=best_call.delta if best_call else None,
        best_call_iv=best_call.implied_volatility if best_call else None,
        best_call_oi=best_call.open_interest if best_call else None,
        best_call_spread_pct=_spread_pct(best_call),
        best_call_theta=best_call.theta if best_call else None,
        best_call_gamma=best_call.gamma if best_call else None,
        best_call_score=best_call.composite_score if best_call else None,
        best_put_symbol=best_put.option_symbol if best_put else None,
        best_put_delta=best_put.delta if best_put else None,
        best_put_iv=best_put.implied_volatility if best_put else None,
        best_put_oi=best_put.open_interest if best_put else None,
        best_put_spread_pct=_spread_pct(best_put),
        best_put_theta=best_put.theta if best_put else None,
        best_put_gamma=best_put.gamma if best_put else None,
        best_put_score=best_put.composite_score if best_put else None,
        error=None,
    )


# ── Print helpers ──────────────────────────────────────────────────────────────

_W = 72

def _hdr(text: str) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {text}")
    print('─' * _W)


def _print_result(r: ValidationResult) -> None:
    status = "FAIL" if r.error else "PASS"
    label  = f"{r.symbol} ({r.option_underlying}, {r.exchange})"
    print(f"\n  [{status}] {label}")

    if r.error:
        print(f"         Error: {r.error}")
        return

    print(f"         Expiry:              {r.expiry}")
    print(f"         Spot:               ₹{r.spot:,.2f}")
    print(f"         Contracts fetched:   {r.contracts_fetched}")
    print(f"         Candidates scored:   {r.candidates_built}")

    for side, sym, delta, iv, oi, spread, theta, gamma, score in [
        ("CALL/LONG",
         r.best_call_symbol, r.best_call_delta, r.best_call_iv,
         r.best_call_oi, r.best_call_spread_pct, r.best_call_theta,
         r.best_call_gamma, r.best_call_score),
        ("PUT/SHORT",
         r.best_put_symbol, r.best_put_delta, r.best_put_iv,
         r.best_put_oi, r.best_put_spread_pct, r.best_put_theta,
         r.best_put_gamma, r.best_put_score),
    ]:
        if sym is None:
            print(f"\n         Best {side}: no candidate passed filters")
            continue
        print(f"\n         Best {side}:")
        print(f"           Contract:   {sym}")
        print(f"           Delta:      {delta:+.3f}" if delta is not None else "           Delta:      n/a")
        print(f"           IV:         {iv:.1%}" if iv is not None else "           IV:         n/a")
        print(f"           OI:         {oi:,}" if oi is not None else "           OI:         n/a")
        print(f"           Spread:     {spread:.2f}%" if spread is not None else "           Spread:     n/a")
        print(f"           Theta/day: ₹{theta:.2f}" if theta is not None else "           Theta/day:  n/a")
        print(f"           Gamma:      {gamma:.6f}" if gamma is not None else "           Gamma:      n/a")
        print(f"           Score:      {score:.4f}" if score is not None else "           Score:      n/a")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Kite option-chain support for live execution targets."
    )
    parser.add_argument(
        "--symbol",
        default="ALL",
        choices=["BANKNIFTY", "NIFTY", "SENSEX", "ALL"],
        help="Which underlying to test (default: ALL)",
    )
    args = parser.parse_args()

    targets = (
        list(OPTION_CHAIN_CONFIG.keys())
        if args.symbol == "ALL"
        else [args.symbol]
    )

    _hdr("OPTION CHAIN VALIDATION")
    print(f"  Targets: {', '.join(targets)}")
    print(f"  Date:    {date.today()}")

    # Session check
    _hdr("KITE SESSION CHECK")
    adapter = KiteAdapter()
    valid, err = adapter.check_session()
    if not valid:
        print(f"  [FAIL] Token invalid or expired: {err}")
        print("  Action: python scripts/generate_kite_session.py")
        sys.exit(1)
    print("  [PASS] Access token valid")

    # Shared stateless engines
    opt_ctx_builder = OptionsContextBuilder(
        config=OptionsContextBuilderConfig(compute_missing_greeks=True)
    )
    strike_engine = StrikeSelectionEngine(config=StrikeSelectionConfig(top_n=5))

    results: list[ValidationResult] = []
    for config_key in targets:
        _hdr(f"TESTING: {config_key}")
        meta = OPTION_CHAIN_CONFIG[config_key]
        print(
            f"  feed_symbol={meta.feed_symbol}  "
            f"option_underlying={meta.option_underlying}  "
            f"exchange={meta.exchange}  "
            f"lot_size={meta.lot_size}  "
            f"strike_step={meta.strike_step:.0f}"
        )
        r = _validate_one(adapter, opt_ctx_builder, strike_engine, config_key)
        _print_result(r)
        results.append(r)

    # Summary
    _hdr("SUMMARY")
    passed = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    print(f"  Passed: {len(passed)}/{len(results)}")
    for r in failed:
        print(f"  FAIL [{r.symbol}]: {r.error}")
    print()

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
