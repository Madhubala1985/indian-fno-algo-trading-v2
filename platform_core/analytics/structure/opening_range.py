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


@dataclass(frozen=True, slots=True)
class OpeningRange:
    or_high: float
    or_low: float
    or_mid: float
    trading_date: str

    def position(self, spot: float) -> str:
        if spot > self.or_high:
            return "ABOVE_OR"
        if spot < self.or_low:
            return "BELOW_OR"
        return "INSIDE_OR"

    def confidence_multiplier(self, spot: float, bias: str) -> tuple[float, str]:
        """Returns (multiplier, reason)."""
        pos = self.position(spot)
        if pos == "INSIDE_OR":
            return _INSIDE_PENALTY, f"spot inside opening range ({self.or_low:.0f}–{self.or_high:.0f})"
        if bias == "LONG" and pos == "ABOVE_OR":
            return 1.0, ""
        if bias == "SHORT" and pos == "BELOW_OR":
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
