"""
platform_core.data.live.instrument_registry
═══════════════════════════════════════════
WebSocket subscription tokens and option-chain configuration for live
index feeds.

INSTRUMENTS       — Kite WebSocket instrument tokens; keys match the
                    symbol names emitted by BarBuilder.  Do not rename.

OPTION_CHAIN_CONFIG — Per-underlying metadata required to fetch and
                    evaluate option chains.  Keyed by the canonical
                    option-underlying name used by the Kite instrument
                    master (e.g. "NIFTY", not "NIFTY50").

FEED_TO_OPTION_UNDERLYING — Maps a WebSocket feed symbol to the Kite
                    option-underlying name.  "NIFTY50" → "NIFTY" is the
                    critical translation; BANKNIFTY and SENSEX are 1-to-1.

OPTION_UNDERLYING_TO_FEED — Inverse of the above.
"""
from __future__ import annotations

from dataclasses import dataclass

from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.core.enums import UnderlyingFamily


# ── WebSocket subscription tokens ─────────────────────────────────────────────
# Keys are the symbol names that BarBuilder / StructureEngine will use.
# Do NOT rename these keys — they are used throughout the live pipeline.

INSTRUMENTS = {

    # NSE INDEXES
    "NIFTY50": 256265,

    "BANKNIFTY": 260105,

    # BSE INDEXES
    "SENSEX": 265,

}


# ── Option-chain metadata ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class OptionChainMeta:
    """
    All metadata required to fetch and evaluate an option chain for one
    underlying.

    feed_symbol       — the symbol key used in INSTRUMENTS and BarBuilder
                        output (e.g. "NIFTY50").
    option_underlying — the name field used in the Kite instrument master
                        and passed to KiteAdapter.fetch_option_chain /
                        nearest_expiry (e.g. "NIFTY").
    exchange          — Kite exchange segment: "NFO" (NSE F&O) or
                        "BFO" (BSE F&O).  SENSEX options trade on BFO.
    lot_size          — reference contract lot size (units).
    strike_step       — distance between consecutive strikes (₹).
    """

    feed_symbol:       str
    option_underlying: str
    exchange:          str
    lot_size:          int
    strike_step:       float


OPTION_CHAIN_CONFIG: dict[str, OptionChainMeta] = {

    "BANKNIFTY": OptionChainMeta(
        feed_symbol       = "BANKNIFTY",
        option_underlying = "BANKNIFTY",
        exchange          = "NFO",
        lot_size          = REFERENCE_LOT_SIZES[UnderlyingFamily.BANKNIFTY],
        strike_step       = float(REFERENCE_STRIKE_STEPS[UnderlyingFamily.BANKNIFTY]),
    ),

    "NIFTY": OptionChainMeta(
        feed_symbol       = "NIFTY50",       # WebSocket key in INSTRUMENTS
        option_underlying = "NIFTY",          # Kite instrument master name
        exchange          = "NFO",
        lot_size          = REFERENCE_LOT_SIZES[UnderlyingFamily.NIFTY],
        strike_step       = float(REFERENCE_STRIKE_STEPS[UnderlyingFamily.NIFTY]),
    ),

    "SENSEX": OptionChainMeta(
        feed_symbol       = "SENSEX",
        option_underlying = "SENSEX",
        exchange          = "BFO",            # BSE F&O — not NFO
        lot_size          = REFERENCE_LOT_SIZES[UnderlyingFamily.SENSEX],
        strike_step       = float(REFERENCE_STRIKE_STEPS[UnderlyingFamily.SENSEX]),
    ),

}


# ── Feed ↔ option-underlying translation helpers ───────────────────────────────
# Use these whenever translating between a BarBuilder symbol and a Kite
# chain-query name.  The NIFTY50 → NIFTY translation is the critical one.

FEED_TO_OPTION_UNDERLYING: dict[str, str] = {
    meta.feed_symbol: meta.option_underlying
    for meta in OPTION_CHAIN_CONFIG.values()
}

OPTION_UNDERLYING_TO_FEED: dict[str, str] = {
    meta.option_underlying: meta.feed_symbol
    for meta in OPTION_CHAIN_CONFIG.values()
}
