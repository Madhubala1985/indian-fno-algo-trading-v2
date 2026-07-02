# FILE: platform_core/analytics/structure/opening_range.py
"""
Opening Range (OR) engine for NSE sessions (09:15–09:30 IST).

The opening range captures the high and low of the first 15 minutes.
It is finalized at 09:30. Entry confidence is adjusted based on where
spot sits relative to the OR when a trade signal is generated:

  Inside OR      → no entry (choppy, no directional commitment)
  Above OR / LONG  → aligned, preserve confidence
  Below OR / SHORT → aligned, preserve confidence
  Above OR / SHORT → contradicts structure, penalize
  Below OR / LONG  → contradicts structure, penalize
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from platform_core.data.bar_builder import LiveBar

_IST = timezone(timedelta(hours=5, minutes=30))

_OR_START = 9 * 60 + 15   # 09:15 IST
_OR_END   = 9 * 60 + 30   # 09:30 IST — range finalized after this minute

_INSIDE_PENALTY      = 0.0   # no entry inside OR
_CONTRADICTION_MULT  = 0.40  # directional contradiction penalty

# Breakout buffer: a marginal poke just past the OR boundary is not a breakout.
# 2026-07-02: entry at spot 77373 vs OR high 77349 (+24 pts on a 211-pt range)
# immediately failed back into the range (-5,740). Require spot to clear the
# boundary by this fraction of the OR width before treating it as ABOVE/BELOW.
# 0 restores the old edge-trigger behaviour. Env-tunable for replay A/B.
import os as _os

_BREAKOUT_BUFFER_FRAC: float = float(
    _os.environ.get("OR_BREAKOUT_BUFFER_FRAC", "0.25") or 0.25
)

# Extension-chase guard: entering WITH a move that has already extended far
# beyond the opening range is late-trend chasing — the initiative (institutional)
# phase of the breakout is over and responsive traders fade it. Measured on the
# real-premium replay batch (06-24..07-01): every losing afternoon entry had
# extension >= 1.6 OR-widths (1.93/2.19/2.25/2.29/2.36 all lost); every winner
# entered at 0.28-1.59. Auction-theory motivated round threshold, NOT fitted to
# the sample edge: past 2 full OR-widths the move is extended — do not chase.
# This REPLACES the blunt 12:00 time cutoff: a fresh afternoon breakout (low
# extension) stays tradeable; a chase is blocked at any hour. 0 disables.
_MAX_EXTENSION: float = float(
    _os.environ.get("OR_MAX_EXTENSION", "2.0") or 2.0
)


@dataclass(frozen=True, slots=True)
class OpeningRange:
    or_high: float
    or_low: float
    or_mid: float
    trading_date: str

    @property
    def breakout_buffer(self) -> float:
        return max(self.or_high - self.or_low, 0.0) * _BREAKOUT_BUFFER_FRAC

    def position(self, spot: float) -> str:
        buf = self.breakout_buffer
        if spot > self.or_high + buf:
            return "ABOVE_OR"
        if spot < self.or_low - buf:
            return "BELOW_OR"
        return "INSIDE_OR"

    def extension(self, spot: float, bias: str) -> float:
        """Signed distance beyond the OR boundary in the BIAS direction,
        in OR-width units. Negative = spot on the other side of the range."""
        width = max(self.or_high - self.or_low, 1e-9)
        if bias == "SHORT":
            return (self.or_low - spot) / width
        return (spot - self.or_high) / width

    def confidence_multiplier(self, spot: float, bias: str) -> tuple[float, str]:
        """Returns (multiplier, reason)."""
        pos = self.position(spot)
        if pos == "INSIDE_OR":
            buf = self.breakout_buffer
            return _INSIDE_PENALTY, (
                f"spot inside opening range ({self.or_low:.0f}–{self.or_high:.0f}"
                f", breakout buffer ±{buf:.0f})"
            )
        if bias == "LONG" and pos == "ABOVE_OR":
            ext = self.extension(spot, bias)
            if 0.0 < _MAX_EXTENSION < ext:
                return 0.0, (
                    f"extension chase: spot {ext:.2f} OR-widths above range "
                    f"(max {_MAX_EXTENSION:.1f}) — move is extended, not chasing"
                )
            return 1.0, ""
        if bias == "SHORT" and pos == "BELOW_OR":
            ext = self.extension(spot, bias)
            if 0.0 < _MAX_EXTENSION < ext:
                return 0.0, (
                    f"extension chase: spot {ext:.2f} OR-widths below range "
                    f"(max {_MAX_EXTENSION:.1f}) — move is extended, not chasing"
                )
            return 1.0, ""
        if bias == "LONG" and pos == "BELOW_OR":
            return _CONTRADICTION_MULT, f"LONG signal but spot below OR ({self.or_low:.0f})"
        if bias == "SHORT" and pos == "ABOVE_OR":
            return _CONTRADICTION_MULT, f"SHORT signal but spot above OR ({self.or_high:.0f})"
        return 1.0, ""

    def to_dict(self) -> dict[str, object]:
        return {
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_mid": self.or_mid,
            "trading_date": self.trading_date,
        }


class OpeningRangeEngine:
    """
    Tracks the NSE 09:15–09:30 opening range per trading session.

    Call update_bar() for every 1-minute bar. The range is finalized
    after the first bar at or after 09:30. Carries the prior session's
    range until a new one is established.
    """

    def __init__(self) -> None:
        self._current_date: str | None = None
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._finalized: bool = False
        self._current_range: OpeningRange | None = None

    @property
    def current_range(self) -> OpeningRange | None:
        return self._current_range

    @property
    def ready(self) -> bool:
        return self._current_range is not None

    def update_bar(self, bar: LiveBar) -> None:
        ist = bar.start_timestamp.astimezone(_IST)
        tod = ist.hour * 60 + ist.minute
        date_str = ist.date().isoformat()

        # New trading day — reset accumulation state
        if date_str != self._current_date:
            self._current_date = date_str
            self._or_high = None
            self._or_low = None
            self._finalized = False
            # Retain _current_range until new day's range is ready

        if self._finalized:
            return

        if _OR_START <= tod < _OR_END:
            # Accumulate opening range bars
            self._or_high = bar.high_price if self._or_high is None else max(self._or_high, bar.high_price)
            self._or_low  = bar.low_price  if self._or_low  is None else min(self._or_low,  bar.low_price)

        elif tod >= _OR_END and self._or_high is not None:
            # Finalize: first bar at/after 09:30 triggers finalization
            self._current_range = OpeningRange(
                or_high=self._or_high,
                or_low=self._or_low,
                or_mid=(self._or_high + self._or_low) / 2.0,
                trading_date=date_str,
            )
            self._finalized = True


__all__ = ["OpeningRange", "OpeningRangeEngine"]
