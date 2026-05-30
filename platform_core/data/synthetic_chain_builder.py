# FILE: platform_core/data/synthetic_chain_builder.py
"""
platform_core.data.synthetic_chain_builder
═══════════════════════════════════════════════════════════════════════
Synthetic option chain builder for replay and backtesting.

When historical option chain data is unavailable, this module generates
a BSM-consistent OptionChainSnapshot from index bar data. It is used by
BacktestRunner to feed the OptionsContextBuilder pipeline.

Chain generation:
  - Strikes: spot ± N steps around ATM (configurable)
  - IV:      ATM IV estimated from recent realized volatility; put skew
             applied via a configurable slope
  - Greeks:  Real BSM outputs from GreeksEngine (delta, gamma, theta, vega)
  - OI:      Synthetic Gaussian distribution peaked at ATM
  - LTP:     BSM theoretical price from GreeksEngine.option_price
  - Spread:  Synthetic 1% of LTP each side

ExpiryProvider computes nearest weekly and biweekly expiry dates from the
bar date. Expiry weekday is configurable per underlying via
WEEKLY_EXPIRY_WEEKDAYS, enabling correct modeling of:
  NIFTY     → Thursday
  BANKNIFTY → Wednesday
  FINNIFTY  → Tuesday
  SENSEX    → Friday (BSE)

Replay safety:
  ATM IV is estimated from the past vol_lookback_bars only. No future
  data is accessed. The chain snapshot is causally constructed from
  information available at the bar timestamp.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import exp, log, sqrt
from statistics import stdev
from typing import Deque

from platform_core.data.bar_builder import LiveBar
from platform_core.data.option_chain_ingestion import (
    OptionChainSnapshot,
    OptionContractSnapshot,
    OptionGreeksSnapshot,
)
from platform_core.options.greeks_engine import (
    GreeksEngine,
    GreeksEngineConfig,
    OptionGreeksInput,
)
from platform_core.core.constants import REFERENCE_LOT_SIZES, REFERENCE_STRIKE_STEPS
from platform_core.core.enums import UnderlyingFamily


# ── Expiry calendar constants ──────────────────────────────────────────────────

# NSE/BSE weekly option expiry weekdays (Monday=0 … Sunday=6)
WEEKLY_EXPIRY_WEEKDAYS: dict[str, int] = {
    "NIFTY":     3,   # Thursday
    "BANKNIFTY": 2,   # Wednesday
    "FINNIFTY":  1,   # Tuesday
    "SENSEX":    4,   # Friday (BSE)
}
_DEFAULT_EXPIRY_WEEKDAY = 3   # Thursday fallback for unknown underlyings


# ── Expiry provider ────────────────────────────────────────────────────────────

class ExpiryProvider:
    """
    Computes nearest weekly and biweekly expiry dates from a given bar date.

    The expiry weekday is looked up from WEEKLY_EXPIRY_WEEKDAYS for known
    underlyings. Unknown underlyings default to Thursday.
    """

    def __init__(self, underlying_symbol: str) -> None:
        self._weekday = WEEKLY_EXPIRY_WEEKDAYS.get(
            underlying_symbol.upper(), _DEFAULT_EXPIRY_WEEKDAY
        )

    def get_weekly_expiry(self, from_date: date) -> date:
        """Nearest future expiry on the configured weekday (minimum 1 day ahead)."""
        return self._next_weekday(from_date, self._weekday, offset_weeks=0)

    def get_biweekly_expiry(self, from_date: date) -> date:
        """Second-nearest weekly expiry (7 days after the weekly expiry)."""
        weekly = self.get_weekly_expiry(from_date)
        return weekly + timedelta(days=7)

    def _next_weekday(self, from_date: date, weekday: int, offset_weeks: int) -> date:
        """Return the next occurrence of weekday >= from_date + 1."""
        days_ahead = weekday - from_date.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        result = from_date + timedelta(days=days_ahead + offset_weeks * 7)
        return result


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SyntheticChainConfig:
    """Configuration for synthetic option chain construction."""

    underlying_symbol: str
    lot_size: int
    strike_step: int
    risk_free_rate: float = 0.068
    dividend_yield: float = 0.0
    strikes_around_atm: int = 10       # strikes each side of ATM
    put_skew_slope: float = 0.0015     # IV increase per unit distance (put side)
    call_skew_slope: float = 0.0005    # IV increase per unit distance (call side)
    base_iv: float = 0.18              # fallback IV when insufficient history
    vol_lookback_bars: int = 20        # bars for realized vol estimation
    bars_per_day: int = 375            # NSE session = 375 1-minute bars
    oi_peak_fraction: float = 0.025    # Gaussian width as fraction of spot
    oi_atm_peak: int = 150_000         # OI at ATM strike

    def __post_init__(self) -> None:
        if not self.underlying_symbol.strip():
            raise ValueError("underlying_symbol must be non-empty")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.strike_step <= 0:
            raise ValueError("strike_step must be positive")
        if self.strikes_around_atm <= 0:
            raise ValueError("strikes_around_atm must be positive")

    @classmethod
    def for_symbol(cls, symbol: str) -> "SyntheticChainConfig":
        """Auto-populate lot_size and strike_step from core constants."""
        lot_size = _lookup_lot_size(symbol)
        strike_step = _lookup_strike_step(symbol)
        return cls(
            underlying_symbol=symbol,
            lot_size=lot_size,
            strike_step=strike_step,
        )


# ── Builder ────────────────────────────────────────────────────────────────────

class SyntheticOptionChainBuilder:
    """
    Generates OptionChainSnapshot objects from index bar data.

    Usage:
        builder = SyntheticOptionChainBuilder(config)
        for bar in bars:
            builder.update_bar(bar)          # maintain rolling vol history
            snap = builder.build_snapshot(   # generate chain at bar's close
                spot_price=bar.close_price,
                expiry_date=weekly_expiry,
                valuation_timestamp=bar.start_timestamp,
            )
    """

    def __init__(self, config: SyntheticChainConfig) -> None:
        self._config = config
        self._greeks_engine = GreeksEngine(
            config=GreeksEngineConfig(theta_scale_per_day=True)
        )
        self._close_prices: Deque[float] = deque(maxlen=config.vol_lookback_bars + 1)
        self._sequence = 0

    def update_bar(self, bar: LiveBar) -> None:
        """Feed one bar to maintain the rolling price history for IV estimation."""
        self._close_prices.append(bar.close_price)

    def estimate_atm_iv(self) -> float:
        """
        Estimate ATM IV from recent realized volatility.

        Uses annualised daily close-to-close log returns over vol_lookback_bars.
        Falls back to base_iv when insufficient history.
        """
        closes = list(self._close_prices)
        if len(closes) < 3:
            return self._config.base_iv
        log_returns = [
            log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        if len(log_returns) < 2:
            return self._config.base_iv
        per_bar_vol = stdev(log_returns)
        # Annualise from bar frequency to annual: sqrt(bars_per_day × 252)
        annualised = per_bar_vol * sqrt(self._config.bars_per_day * 252)
        return max(annualised, 0.05)   # floor at 5%

    def build_snapshot(
        self,
        *,
        spot_price: float,
        expiry_date: date,
        valuation_timestamp: datetime,
    ) -> OptionChainSnapshot:
        """
        Build a synthetic OptionChainSnapshot for one expiry.

        Uses current rolling IV estimate and BSM for all contract values.
        Replay-safe: only past bar data is used for IV estimation.
        """
        cfg = self._config
        atm_iv = self.estimate_atm_iv()
        today = (
            valuation_timestamp.date()
            if hasattr(valuation_timestamp, "date")
            else date.today()
        )
        tte = max((expiry_date - today).days, 1) / 365.25

        # Generate strikes around ATM
        atm_strike = round(spot_price / cfg.strike_step) * cfg.strike_step
        strike_range = [
            atm_strike + i * cfg.strike_step
            for i in range(-cfg.strikes_around_atm, cfg.strikes_around_atm + 1)
            if atm_strike + i * cfg.strike_step > 0
        ]

        ts = _ensure_utc(valuation_timestamp)
        contracts: list[OptionContractSnapshot] = []

        for strike in strike_range:
            for option_type in ("CALL", "PUT"):
                iv = self._compute_iv(strike, spot_price, atm_iv, option_type)
                oi = self._compute_oi(strike, spot_price, option_type)

                try:
                    result = self._greeks_engine.compute(
                        option_input=OptionGreeksInput(
                            instrument_id=f"{cfg.underlying_symbol}_{strike}_{option_type[0]}",
                            underlying_symbol=cfg.underlying_symbol,
                            option_symbol=self._symbol(strike, option_type, expiry_date),
                            option_type=option_type,
                            spot_price=spot_price,
                            strike_price=float(strike),
                            time_to_expiry_years=tte,
                            implied_volatility=iv,
                            risk_free_rate=cfg.risk_free_rate,
                            dividend_yield=cfg.dividend_yield,
                            lot_size=cfg.lot_size,
                        )
                    )
                    ltp = max(result.option_price, 0.01)
                    delta = result.delta
                    gamma = result.gamma
                    theta = result.theta
                    vega = result.vega
                except Exception:
                    ltp = 0.01
                    delta = gamma = theta = vega = 0.0

                half_spread = max(ltp * 0.008, 0.25)
                bid = max(ltp - half_spread, 0.01)   # floor bid at 0.01
                ask = ltp + half_spread
                sym = self._symbol(strike, option_type, expiry_date)
                self._sequence += 1

                contracts.append(OptionContractSnapshot(
                    contract_symbol=sym,
                    underlying_symbol=cfg.underlying_symbol,
                    exchange="NSE" if cfg.underlying_symbol != "SENSEX" else "BSE",
                    expiry_date=expiry_date,
                    strike_price=Decimal(str(strike)),
                    option_side=option_type,   # type: ignore[arg-type]
                    event_timestamp=ts,
                    received_timestamp=ts,
                    last_traded_price=Decimal(str(round(ltp, 2))),
                    bid_price=Decimal(str(round(bid, 2))),
                    ask_price=Decimal(str(round(ask, 2))),
                    traded_volume=max(int(oi * 0.06), 100),
                    open_interest=oi,
                    implied_volatility=Decimal(str(round(iv, 4))),
                    greeks=OptionGreeksSnapshot(
                        delta=Decimal(str(round(delta, 6))),
                        gamma=Decimal(str(round(gamma, 8))),
                        theta=Decimal(str(round(theta, 4))),
                        vega=Decimal(str(round(vega, 4))),
                    ),
                    is_tradable=True,
                    source="synthetic",
                ))

        return OptionChainSnapshot(
            chain_id=f"{cfg.underlying_symbol}-{expiry_date.isoformat()}-synthetic",
            underlying_symbol=cfg.underlying_symbol,
            expiry_date=expiry_date,
            exchange="NSE" if cfg.underlying_symbol != "SENSEX" else "BSE",
            event_timestamp=ts,
            received_timestamp=ts,
            contracts=tuple(contracts),
            sequence_number=self._sequence,
            update_kind="SNAPSHOT",
            is_replay=True,
            source="synthetic",
        )

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _compute_iv(
        self, strike: float, spot: float, atm_iv: float, option_type: str
    ) -> float:
        """Apply simple skew to ATM IV based on moneyness."""
        moneyness = (strike - spot) / spot
        if option_type == "PUT":
            skew = max(-moneyness, 0.0) * self._config.put_skew_slope * 100.0
        else:
            skew = max(moneyness, 0.0) * self._config.call_skew_slope * 100.0
        return max(atm_iv + skew, 0.05)

    def _compute_oi(self, strike: float, spot: float, option_type: str) -> int:
        """Gaussian OI distribution peaked at ATM, larger for OTM puts."""
        cfg = self._config
        d = abs(strike - spot) / spot
        base = cfg.oi_atm_peak * exp(-0.5 * (d / cfg.oi_peak_fraction) ** 2)
        if option_type == "PUT" and strike < spot:
            base *= 1.35
        return max(int(base), 500)

    def _symbol(self, strike: int, option_type: str, expiry_date: date) -> str:
        tag = expiry_date.strftime("%d%b%y").upper()
        return f"{self._config.underlying_symbol}{tag}{strike}{option_type[0]}E"


# ── Constant lookups ───────────────────────────────────────────────────────────

def _lookup_lot_size(symbol: str) -> int:
    for underlying, lot_size in REFERENCE_LOT_SIZES.items():
        if symbol.upper() == underlying.value:
            return lot_size
    return 1


def _lookup_strike_step(symbol: str) -> int:
    for underlying, step in REFERENCE_STRIKE_STEPS.items():
        if symbol.upper() == underlying.value:
            return int(step)
    return 50


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ExpiryProvider",
    "SyntheticChainConfig",
    "SyntheticOptionChainBuilder",
    "WEEKLY_EXPIRY_WEEKDAYS",
]
