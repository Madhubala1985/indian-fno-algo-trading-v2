# FILE: platform_core/analytics/structure/previous_day_levels.py
"""
Previous Day High/Low (PDH/PDL) tracking and confidence adjustment.

PDH and PDL are the most universally observed institutional reference levels
on NSE. This engine carries the prior completed day's OHLC forward and
computes a confidence multiplier based on spot proximity:

  SHORT bias near PDL (prior support) → reduce confidence
  SHORT bias below PDL (confirmed break) → preserve confidence
  LONG bias near PDH (prior resistance) → reduce confidence
  LONG bias above PDH (confirmed break) → preserve confidence

Updated by DailyBarAccumulator output in BacktestRunner.run().
"""
from __future__ import annotations

from dataclasses import dataclass

from platform_core.data.bar_builder import LiveBar

# Fraction of level price within which proximity penalty applies
_PROXIMITY_ZONE = 0.003   # 0.3%
_PENALTY        = 0.45    # confidence multiplied by this when near opposing level


@dataclass(frozen=True, slots=True)
class PreviousDayLevels:
    pdh: float
    pdl: float
    prev_close: float
    session_date: str  # ISO date of the completed day these came from

    def confidence_multiplier(self, spot: float, bias: str) -> tuple[float, str]:
        """
        Returns (multiplier, reason).
        reason is empty when no adjustment is made.
        """
        if self.pdh <= 0 or self.pdl <= 0:
            return 1.0, ""

        near_pdh = abs(spot - self.pdh) / self.pdh < _PROXIMITY_ZONE
        near_pdl = abs(spot - self.pdl) / self.pdl < _PROXIMITY_ZONE
        above_pdh = spot > self.pdh
        below_pdl = spot < self.pdl

        if bias == "SHORT":
            if below_pdl:
                # Confirmed break below PDL → bearish continuation
                return 1.0, ""
            if near_pdl:
                # Approaching PDL from above — prior support, SHORT setup risky
                return _PENALTY, f"near PDL ({self.pdl:.0f}) — prior support risk"
        elif bias == "LONG":
            if above_pdh:
                # Confirmed break above PDH → bullish continuation
                return 1.0, ""
            if near_pdh:
                # Approaching PDH from below — prior resistance, LONG setup risky
                return _PENALTY, f"near PDH ({self.pdh:.0f}) — prior resistance risk"

        return 1.0, ""

    def to_dict(self) -> dict[str, object]:
        return {
            "pdh": self.pdh,
            "pdl": self.pdl,
            "prev_close": self.prev_close,
            "session_date": self.session_date,
        }


class PreviousDayLevelsEngine:
    """
    Carries PDH/PDL forward from completed daily bars.

    Call update_daily_bar() whenever DailyBarAccumulator emits a bar.
    The first bar starts day-1 data; levels become available after day 1 closes.
    """

    def __init__(self) -> None:
        self._levels: PreviousDayLevels | None = None

    @property
    def levels(self) -> PreviousDayLevels | None:
        return self._levels

    @property
    def ready(self) -> bool:
        return self._levels is not None

    def update_daily_bar(self, daily_bar: LiveBar) -> None:
        """Promote the completed daily bar into PDH/PDL for the next session."""
        date_str = daily_bar.start_timestamp.date().isoformat()
        self._levels = PreviousDayLevels(
            pdh=daily_bar.high_price,
            pdl=daily_bar.low_price,
            prev_close=daily_bar.close_price,
            session_date=date_str,
        )


__all__ = ["PreviousDayLevels", "PreviousDayLevelsEngine"]
