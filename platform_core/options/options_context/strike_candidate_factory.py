# FILE: platform_core/options/options_context/strike_candidate_factory.py
"""
StrikeCandidateFactory: converts _ContractData objects into StrikeCandidate
objects for direct consumption by StrikeSelectionEngine.

Only contracts with a complete Greek set (delta, gamma, theta, vega) and
non-None IV, OI, and LTP are included.
"""
from __future__ import annotations

from platform_core.options.strike_selection import StrikeCandidate
from platform_core.options.options_context.helpers import _ContractData, _has_full_greeks


class StrikeCandidateFactory:
    """Stateless factory. Converts a list of _ContractData to StrikeCandidates."""

    def build_candidates(
        self,
        all_contracts: list[_ContractData],
    ) -> tuple[StrikeCandidate, ...]:
        return tuple(
            self._convert(cd)
            for cd in all_contracts
            if (
                _has_full_greeks(cd)
                and cd.iv is not None
                and cd.oi is not None
                and cd.ltp is not None
            )
        )

    @staticmethod
    def _convert(cd: _ContractData) -> StrikeCandidate:
        return StrikeCandidate(
            underlying_symbol=cd.underlying_symbol,
            expiry_date=cd.expiry_date,
            option_symbol=cd.contract_symbol,
            strike_price=cd.strike_price,
            option_type=cd.option_type,
            spot_price=cd.spot_price,
            last_traded_price=cd.ltp or 0.0,
            open_interest=cd.oi or 0,
            change_in_open_interest=cd.change_oi,
            volume=cd.volume,
            implied_volatility=cd.iv or 0.0,
            delta=cd.delta or 0.0,
            gamma=cd.gamma or 0.0,
            theta=cd.theta or 0.0,
            vega=cd.vega or 0.0,
            time_to_expiry_years=cd.tte_years,
            lot_size=cd.lot_size,
            bid_price=cd.bid,
            ask_price=cd.ask,
        )


__all__ = ["StrikeCandidateFactory"]
