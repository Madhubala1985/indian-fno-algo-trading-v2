"""
platform_core.options
═══════════════════════════════════════════════════════════════════════
Options intelligence package for institutional Indian index derivatives.

Engines (all deterministic, replay-safe, JSON-serialisable):
  GreeksEngine          — BSM price and Greeks (delta, gamma, vega, theta, rho,
                          vomma, vanna) with notional scaling.
  IVEngine              — Implied volatility inversion (Newton + bisection).
  IVSurfaceEngine       — IV surface across strikes and expiries; ATM term
                          structure; skew slope and convexity per slice.
  OIAnalyticsEngine     — PCR, max pain, OI concentration, S/R levels.
  GammaExposureEngine   — GEX by strike, gamma flip estimation, call/put walls.
  DealerHedgingEngine   — Dealer delta/gamma/vanna/charm approximation from
                          public chain data.
  PremiumBehaviorEngine — Intrinsic/extrinsic decomposition, moneyness buckets,
                          premium-OI notional, theta drag.
  StrikeSelectionEngine — Terminal decision layer. Converts directional bias +
                          options analytics context into ranked contract
                          recommendations with quantified reasoning.

Pipeline:
  OptionChainSnapshot → OI / GEX / IV / Premium engines
      → StrikeSelectionEngine → StrikeSelectionResult
          → "Buy BANKNIFTY 52500 CE Weekly | δ=+0.38 IV=18.4% Score=0.74"
"""
from platform_core.options.greeks_engine import (
    GreeksEngine,
    GreeksEngineConfig,
    GreeksEngineError,
    GreeksEngineState,
    GreeksInputError,
    GreeksComputationError,
    OptionGreeksInput,
    OptionGreeksResult,
)
from platform_core.options.iv_engine import (
    IVEngine,
    IVEngineConfig,
    IVEngineError,
    IVEngineState,
    IVInputError,
    IVComputationError,
    IVInput,
    IVResult,
)
from platform_core.options.iv_surface_engine import (
    IVSurfaceEngine,
    IVSurfaceEngineConfig,
    IVSurfaceEngineError,
    IVSurfaceEngineState,
    IVSurfaceInputError,
    IVSurfaceComputationError,
    IVSurfacePoint,
    IVSurfaceSnapshot,
    IVSurfaceQuery,
    IVSurfaceQueryResult,
    ExpirySlice,
    SurfaceNode,
)
from platform_core.options.oi_analytics import (
    OIAnalyticsEngine,
    OIAnalyticsConfig,
    OIAnalyticsError,
    OIAnalyticsInputError,
    OIAnalyticsComputationError,
    OIAnalyticsSnapshot,
    OIAnalyticsState,
    OIContractPoint,
    OIClusterLevel,
    StrikeOIProfile,
)
from platform_core.options.gamma_exposure import (
    GammaExposureEngine,
    GammaExposureConfig,
    GammaExposureError,
    GammaExposureInputError,
    GammaExposureComputationError,
    GammaExposureSnapshot,
    GammaExposureState,
    GammaExposurePoint,
    GammaExposureLevel,
    GammaWallLevel,
)
from platform_core.options.dealer_hedging import (
    DealerHedgingEngine,
    DealerHedgingConfig,
    DealerHedgingError,
    DealerHedgingInputError,
    DealerHedgingComputationError,
    DealerHedgingSnapshot,
    DealerHedgingState,
    DealerGreekPoint,
    DealerExposureLevel,
)
from platform_core.options.premium_behavior import (
    PremiumBehaviorEngine,
    PremiumBehaviorConfig,
    PremiumBehaviorError,
    PremiumBehaviorInputError,
    PremiumBehaviorComputationError,
    PremiumBehaviorSnapshot,
    PremiumBehaviorState,
    PremiumBehaviorPoint,
    PremiumBehaviorLevel,
    PremiumPressureLevel,
)
from platform_core.options.strike_selection import (
    StrikeSelectionEngine,
    StrikeSelectionConfig,
    StrikeSelectionError,
    StrikeSelectionInputError,
    StrikeSelectionComputationError,
    StrikeCandidate,
    StrikeScoreBreakdown,
    StrikeRecommendation,
    StrikeSelectionResult,
)
from platform_core.options.options_context import (
    OptionsContextBuilder,
    OptionsContextBuilderConfig,
    OptionsContext,
    OptionsContextError,
    OptionsContextInputError,
    TimeToExpiryProvider,
    CalendarDaysTTEProvider,
)

__all__ = [
    # Greeks
    "GreeksEngine", "GreeksEngineConfig", "GreeksEngineError", "GreeksEngineState",
    "GreeksInputError", "GreeksComputationError", "OptionGreeksInput", "OptionGreeksResult",
    # IV
    "IVEngine", "IVEngineConfig", "IVEngineError", "IVEngineState",
    "IVInputError", "IVComputationError", "IVInput", "IVResult",
    # IV Surface
    "IVSurfaceEngine", "IVSurfaceEngineConfig", "IVSurfaceEngineError", "IVSurfaceEngineState",
    "IVSurfaceInputError", "IVSurfaceComputationError",
    "IVSurfacePoint", "IVSurfaceSnapshot", "IVSurfaceQuery", "IVSurfaceQueryResult",
    "ExpirySlice", "SurfaceNode",
    # OI Analytics
    "OIAnalyticsEngine", "OIAnalyticsConfig", "OIAnalyticsError",
    "OIAnalyticsInputError", "OIAnalyticsComputationError",
    "OIAnalyticsSnapshot", "OIAnalyticsState",
    "OIContractPoint", "OIClusterLevel", "StrikeOIProfile",
    # Gamma Exposure
    "GammaExposureEngine", "GammaExposureConfig", "GammaExposureError",
    "GammaExposureInputError", "GammaExposureComputationError",
    "GammaExposureSnapshot", "GammaExposureState",
    "GammaExposurePoint", "GammaExposureLevel", "GammaWallLevel",
    # Dealer Hedging
    "DealerHedgingEngine", "DealerHedgingConfig", "DealerHedgingError",
    "DealerHedgingInputError", "DealerHedgingComputationError",
    "DealerHedgingSnapshot", "DealerHedgingState",
    "DealerGreekPoint", "DealerExposureLevel",
    # Premium Behavior
    "PremiumBehaviorEngine", "PremiumBehaviorConfig", "PremiumBehaviorError",
    "PremiumBehaviorInputError", "PremiumBehaviorComputationError",
    "PremiumBehaviorSnapshot", "PremiumBehaviorState",
    "PremiumBehaviorPoint", "PremiumBehaviorLevel", "PremiumPressureLevel",
    # Strike Selection
    "StrikeSelectionEngine", "StrikeSelectionConfig", "StrikeSelectionError",
    "StrikeSelectionInputError", "StrikeSelectionComputationError",
    "StrikeCandidate", "StrikeScoreBreakdown", "StrikeRecommendation", "StrikeSelectionResult",
    # Options Context (orchestration)
    "OptionsContextBuilder", "OptionsContextBuilderConfig", "OptionsContext",
    "OptionsContextError", "OptionsContextInputError",
    "TimeToExpiryProvider", "CalendarDaysTTEProvider",
]
