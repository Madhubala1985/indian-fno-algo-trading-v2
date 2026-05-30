# FILE: platform_core/options/options_context/greek_resolver.py
"""
Greek resolver: bridge from OptionContractSnapshot to a full Greek set.

Resolution order (FIXED):
  1. Chain Greeks — broker/exchange provided values are authoritative.
     When delta and gamma are present in OptionGreeksSnapshot, use them.
     BSM is then used ONLY to supplement vanna (which chains never carry).
  2. BSM computation — used when chain lacks delta or gamma.
     Requires implied_volatility to be present and above min_iv_for_greeks.
  3. None — contract is excluded from Greek-dependent engines.

Quality flags set on _ContractData:
  supports_vanna — True when vanna is a real BSM output (not injected zero).
  supports_charm — Always False. GreeksEngine.compute() hardcodes charm=0.0.
                   The field is tracked explicitly so dealer analytics can
                   document the limitation rather than silently accepting
                   a fake zero.
"""
from __future__ import annotations

from platform_core.options.greeks_engine import (
    GreeksEngine,
    GreeksEngineConfig,
    OptionGreeksInput,
    GreeksEngineError,
)
from platform_core.options.options_context.helpers import _ContractData
from platform_core.options.options_context.models import OptionsContextBuilderConfig


class GreekResolver:
    """
    Resolves Greeks for one _ContractData object per call.

    Chain Greeks are treated as authoritative. GreeksEngine is a fallback
    that is called when chain lacks delta/gamma, or to supplement vanna
    when chain Greeks are present but IV is also available.
    """

    def __init__(self, config: OptionsContextBuilderConfig) -> None:
        self._config = config
        self._engine = GreeksEngine(
            config=GreeksEngineConfig(theta_scale_per_day=True)
        )

    def resolve(self, cd: _ContractData, warnings: list[str]) -> str:
        """
        Populate cd.delta/gamma/theta/vega/vanna and set quality flags.

        Returns "chain", "computed", or "none".
        """
        chain_has_delta = cd.delta is not None
        chain_has_gamma = cd.gamma is not None

        # ── Path 1: Chain Greeks are authoritative ─────────────────────────────
        if chain_has_delta and chain_has_gamma:
            if cd.theta is None:
                cd.theta = 0.0
            if cd.vega is None:
                cd.vega = 0.0

            # Supplement vanna from BSM when IV is available.
            # Chain snapshots never carry vanna; BSM provides a real value.
            if (
                self._config.compute_missing_greeks
                and cd.iv is not None
                and cd.iv >= self._config.min_iv_for_greeks
                and cd.tte_years > 0.0
            ):
                try:
                    result = self._engine.compute(
                        option_input=self._build_input(cd)
                    )
                    cd.vanna = result.vanna   # real BSM vanna
                    cd.supports_vanna = True
                except (GreeksEngineError, ValueError, ArithmeticError) as exc:
                    cd.vanna = None
                    cd.supports_vanna = False
                    warnings.append(
                        f"BSM vanna supplement failed for {cd.contract_symbol}: {exc}"
                    )
            else:
                cd.vanna = None
                cd.supports_vanna = False

            # charm is never computed — GreeksEngine limitation
            cd.charm = None
            cd.supports_charm = False
            cd.greek_source = "chain"
            return "chain"

        # ── Path 2: BSM computation from IV ───────────────────────────────────
        if (
            self._config.compute_missing_greeks
            and cd.iv is not None
            and cd.iv >= self._config.min_iv_for_greeks
            and cd.tte_years > 0.0
        ):
            try:
                result = self._engine.compute(
                    option_input=self._build_input(cd)
                )
                cd.delta = result.delta
                cd.gamma = result.gamma
                cd.theta = result.theta
                cd.vega = result.vega
                cd.vanna = result.vanna   # real BSM vanna (computed properly)
                cd.charm = None           # GreeksEngine returns 0.0 — treated as unavailable
                cd.supports_vanna = True
                cd.supports_charm = False
                cd.greek_source = "computed"
                return "computed"
            except (GreeksEngineError, ValueError, ArithmeticError) as exc:
                warnings.append(
                    f"GreeksEngine failed for {cd.contract_symbol}: {exc}"
                )

        # ── Path 3: No Greeks available ────────────────────────────────────────
        cd.vanna = None
        cd.charm = None
        cd.supports_vanna = False
        cd.supports_charm = False
        cd.greek_source = "none"
        return "none"

    def _build_input(self, cd: _ContractData) -> OptionGreeksInput:
        return OptionGreeksInput(
            instrument_id=cd.contract_symbol,
            underlying_symbol=cd.underlying_symbol,
            option_symbol=cd.contract_symbol,
            option_type=cd.option_type,
            spot_price=cd.spot_price,
            strike_price=cd.strike_price,
            time_to_expiry_years=cd.tte_years,
            implied_volatility=cd.iv,   # type: ignore[arg-type]  # guarded above
            risk_free_rate=self._config.risk_free_rate,
            dividend_yield=self._config.dividend_yield,
            lot_size=cd.lot_size,
        )


__all__ = ["GreekResolver"]
