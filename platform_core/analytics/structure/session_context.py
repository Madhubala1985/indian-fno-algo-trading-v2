# FILE: platform_core/analytics/structure/session_context.py
"""
NSE session phase classification for trade filtering.

Session structure (IST):
  09:00–09:14  PRE_OPEN   — auction, no entry
  09:15–09:29  OPENING    — high volatility, no entry
  09:30–14:29  MAIN       — full confidence, entries allowed
  14:30–14:59  PRE_CLOSE  — reduced confidence (0.65), entries allowed
  15:00–15:29  CLOSING    — illiquid, no entry
  outside      CLOSED     — no entry
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


_IST = timezone(timedelta(hours=5, minutes=30))

# NSE minute-of-day boundaries (total minutes since midnight IST)
_PRE_OPEN_START  = 9 * 60        # 09:00
_SESSION_OPEN    = 9 * 60 + 15   # 09:15
_MAIN_START      = 9 * 60 + 30   # 09:30
_PRE_CLOSE_START = 14 * 60 + 30  # 14:30
_CLOSING_START   = 15 * 60       # 15:00
_SESSION_CLOSE   = 15 * 60 + 30  # 15:30


class SessionPhase:
    PRE_OPEN  = "PRE_OPEN"
    OPENING   = "OPENING"
    MAIN      = "MAIN"
    PRE_CLOSE = "PRE_CLOSE"
    CLOSING   = "CLOSING"
    CLOSED    = "CLOSED"


@dataclass(frozen=True, slots=True)
class SessionContext:
    phase: str
    allow_entry: bool
    confidence_multiplier: float   # applied to market_ctx.confidence
    minutes_to_close: int          # -1 when outside session
    filter_reason: str             # non-empty string when entry is blocked

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "allow_entry": self.allow_entry,
            "confidence_multiplier": self.confidence_multiplier,
            "minutes_to_close": self.minutes_to_close,
            "filter_reason": self.filter_reason,
        }


_BLOCKED_PRE_OPEN  = SessionContext(SessionPhase.PRE_OPEN,  False, 0.0, -1, "pre-open auction")
_BLOCKED_OPENING   = SessionContext(SessionPhase.OPENING,   False, 0.0, -1, "opening volatility (09:15-09:30)")
_BLOCKED_CLOSING   = SessionContext(SessionPhase.CLOSING,   False, 0.0,  0, "closing window (15:00-15:30)")
_BLOCKED_CLOSED    = SessionContext(SessionPhase.CLOSED,    False, 0.0, -1, "outside market hours")


class SessionContextEngine:
    """
    Classifies a bar timestamp into its NSE session phase.
    Thread-safe and stateless — safe to call per bar.
    """

    def classify(self, timestamp: datetime) -> SessionContext:
        ist = timestamp.astimezone(_IST)
        tod = ist.hour * 60 + ist.minute  # minutes since midnight IST

        if tod < _PRE_OPEN_START or tod >= _SESSION_CLOSE:
            return _BLOCKED_CLOSED

        if tod < _SESSION_OPEN:
            return _BLOCKED_PRE_OPEN

        if tod < _MAIN_START:
            return _BLOCKED_OPENING

        minutes_to_close = _SESSION_CLOSE - tod

        if tod < _PRE_CLOSE_START:
            return SessionContext(
                phase=SessionPhase.MAIN,
                allow_entry=True,
                confidence_multiplier=1.0,
                minutes_to_close=minutes_to_close,
                filter_reason="",
            )

        if tod < _CLOSING_START:
            return SessionContext(
                phase=SessionPhase.PRE_CLOSE,
                allow_entry=True,
                confidence_multiplier=0.65,
                minutes_to_close=minutes_to_close,
                filter_reason="",
            )

        return _BLOCKED_CLOSING


__all__ = ["SessionContext", "SessionContextEngine", "SessionPhase"]
