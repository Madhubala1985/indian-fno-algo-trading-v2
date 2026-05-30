# FILE: platform_core/analytics/structure/structure_engine.py
"""
platform_core.analytics.structure.structure_engine
═══════════════════════════════════════════════════════════════════════
Production-grade causal market structure orchestration engine.

This module coordinates finalized-bar structural analytics components while
preserving event-driven, replay-safe, no-hindsight processing. It integrates:
- liquidity sweep detection,
- break of structure (BOS),
- change of character (CHOCH),
- fair value gap (FVG),
- support/resistance zones.

No hidden lookahead is introduced: all sub-engines are updated only from
confirmed swings, precomputed liquidity levels, prior emitted events, and the
current finalized bar.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from platform_core.analytics.structure.bos_detector import BOSDetectionResult
from platform_core.analytics.structure.bos_detector import BOSDetector
from platform_core.analytics.structure.choch_detector import CHOCHDetectionResult
from platform_core.analytics.structure.choch_detector import CHOCHDetector
from platform_core.analytics.structure.fvg_engine import FVGEngine
from platform_core.analytics.structure.fvg_engine import FVGProcessingResult
from platform_core.analytics.structure.liquidity_sweep_detector import LiquiditySweepDetectionResult
from platform_core.analytics.structure.liquidity_sweep_detector import LiquiditySweepDetector
from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import LiquidityLevel
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructureEvent
from platform_core.analytics.structure.models import StructureEventType
from platform_core.analytics.structure.models import StructurePoint
from platform_core.analytics.structure.models import StructureSide
from platform_core.analytics.structure.support_resistance_engine import SupportResistanceEngine
from platform_core.analytics.structure.support_resistance_engine import SupportResistanceProcessingResult


class StructureEngineError(RuntimeError):
    """Base exception for structure engine failures."""


@dataclass(frozen=True, slots=True)
class StructureEngineConfig:
    """Configuration for the orchestration layer."""

    instrument_id: str
    timeframe: str
    require_strict_bar_order: bool = True
    deduplicate_event_ids: bool = True
    max_recent_event_ids: int = 2048

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        if self.max_recent_event_ids <= 0:
            raise ValueError("max_recent_event_ids must be positive")


@dataclass(frozen=True, slots=True)
class StructureEngineInput:
    """Per-bar structure-engine input package."""

    bar: StructureBar
    swing_points: tuple[StructurePoint, ...] = ()
    liquidity_levels: tuple[LiquidityLevel, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "bar": self.bar.to_dict(),
            "swing_points": [point.to_dict() for point in self.swing_points],
            "liquidity_levels": [level.to_dict() for level in self.liquidity_levels],
        }


@dataclass(frozen=True, slots=True)
class StructureEngineResult:
    """Aggregated output for one finalized bar."""

    input_payload: dict[str, JsonValue]
    liquidity_sweep_result: dict[str, JsonValue]
    bos_result: dict[str, JsonValue]
    choch_result: dict[str, JsonValue]
    fvg_result: dict[str, JsonValue]
    support_resistance_result: dict[str, JsonValue]
    emitted_events: tuple[StructureEvent, ...]
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    inferred_structure_side: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "input_payload": self.input_payload,
            "liquidity_sweep_result": self.liquidity_sweep_result,
            "bos_result": self.bos_result,
            "choch_result": self.choch_result,
            "fvg_result": self.fvg_result,
            "support_resistance_result": self.support_resistance_result,
            "emitted_events": [event.to_dict() for event in self.emitted_events],
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "inferred_structure_side": self.inferred_structure_side,
        }


@dataclass(frozen=True, slots=True)
class StructureEngineState:
    """Serializable structure-engine state."""

    instrument_id: str
    timeframe: str
    processed_bar_count: int
    last_bar_timestamp: datetime | None
    last_inferred_structure_side: str | None
    recent_event_ids: tuple[str, ...]
    liquidity_sweep_state: dict[str, JsonValue]
    bos_state: dict[str, JsonValue]
    choch_state: dict[str, JsonValue]
    fvg_state: dict[str, JsonValue]
    support_resistance_state: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count": self.processed_bar_count,
            "last_bar_timestamp": self.last_bar_timestamp.isoformat() if self.last_bar_timestamp else None,
            "last_inferred_structure_side": self.last_inferred_structure_side,
            "recent_event_ids": list(self.recent_event_ids),
            "liquidity_sweep_state": self.liquidity_sweep_state,
            "bos_state": self.bos_state,
            "choch_state": self.choch_state,
            "fvg_state": self.fvg_state,
            "support_resistance_state": self.support_resistance_state,
        }


class StructureEngine:
    """Event-driven orchestration layer for causal structure analytics."""

    def __init__(
        self,
        *,
        config: StructureEngineConfig,
        liquidity_sweep_detector: LiquiditySweepDetector,
        bos_detector: BOSDetector,
        choch_detector: CHOCHDetector,
        fvg_engine: FVGEngine,
        support_resistance_engine: SupportResistanceEngine,
    ) -> None:
        self._config = config
        self._liquidity_sweep_detector = liquidity_sweep_detector
        self._bos_detector = bos_detector
        self._choch_detector = choch_detector
        self._fvg_engine = fvg_engine
        self._support_resistance_engine = support_resistance_engine
        self._processed_bar_count = 0
        self._last_bar_timestamp: datetime | None = None
        self._last_inferred_structure_side: StructureSide = StructureSide.NEUTRAL
        self._recent_event_ids: list[str] = []

    @property
    def state(self) -> StructureEngineState:
        return StructureEngineState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            last_inferred_structure_side=None
            if self._last_inferred_structure_side == StructureSide.NEUTRAL
            else self._last_inferred_structure_side.value,
            recent_event_ids=tuple(self._recent_event_ids),
            liquidity_sweep_state=self._liquidity_sweep_detector.state.to_dict(),
            bos_state=self._bos_detector.state.to_dict(),
            choch_state=self._choch_detector.state.to_dict(),
            fvg_state=self._fvg_engine.state.to_dict(),
            support_resistance_state=self._support_resistance_engine.state.to_dict(),
        )

    @property
    def active_liquidity_levels(self) -> tuple[LiquidityLevel, ...]:
        """
        Active SR zones converted to LiquidityLevel for sweep detection.

        Called by BacktestRunner after each bar to carry levels forward into
        the next bar's StructureEngineInput, enabling LiquiditySweepDetector
        to detect actual sweeps against established price zones.
        """
        from datetime import timezone as _tz
        _UTC = _tz.utc
        levels: list[LiquidityLevel] = []
        for zone in self._support_resistance_engine.active_zones:
            if not zone.is_active or zone.is_broken:
                continue
            levels.append(LiquidityLevel(
                level_id=zone.zone_id,
                instrument_id=zone.instrument_id,
                timeframe=zone.timeframe,
                side=zone.side,
                reference_price=zone.center_price,
                upper_bound_price=zone.upper_price,
                lower_bound_price=zone.lower_price,
                first_seen_timestamp=zone.first_touch_timestamp,
                last_updated_timestamp=zone.last_touch_timestamp,
                touch_count=zone.touch_count,
                estimated_resting_liquidity_score=zone.strength_score,
                metadata={},
            ))
        return tuple(levels)

    def reset(self) -> None:
        self._liquidity_sweep_detector.reset()
        self._bos_detector.reset()
        self._choch_detector.reset()
        self._fvg_engine.reset()
        self._support_resistance_engine.reset()
        self._processed_bar_count = 0
        self._last_bar_timestamp = None
        self._last_inferred_structure_side = StructureSide.NEUTRAL
        self._recent_event_ids.clear()

    def process(self, *, payload: StructureEngineInput) -> StructureEngineResult:
        self._validate_bar(payload.bar)
        support_resistance_result = self._support_resistance_engine.process(
            bar=payload.bar,
            points=payload.swing_points,
        )
        liquidity_sweep_result = self._liquidity_sweep_detector.process_bar(
            bar=payload.bar,
            liquidity_levels=payload.liquidity_levels,
        )
        bos_result = self._bos_detector.process_bar(
            bar=payload.bar,
            swing_points=payload.swing_points,
        )
        choch_result = self._choch_detector.process_bar(
            bar=payload.bar,
            swing_points=payload.swing_points,
            bos_events=bos_result.emitted_events,
        )
        fvg_result = self._fvg_engine.process_bar(bar=payload.bar)
        aggregated_events = self._aggregate_events(
            liquidity_sweep_result=liquidity_sweep_result,
            bos_result=bos_result,
            choch_result=choch_result,
            fvg_result=fvg_result,
            support_resistance_result=support_resistance_result,
        )
        self._processed_bar_count += 1
        self._last_bar_timestamp = payload.bar.bar_timestamp_utc
        self._update_structure_side(aggregated_events)
        return StructureEngineResult(
            input_payload=payload.to_dict(),
            liquidity_sweep_result=liquidity_sweep_result.to_dict(),
            bos_result=bos_result.to_dict(),
            choch_result=choch_result.to_dict(),
            fvg_result=fvg_result.to_dict(),
            support_resistance_result=support_resistance_result.to_dict(),
            emitted_events=aggregated_events,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            inferred_structure_side=None
            if self._last_inferred_structure_side == StructureSide.NEUTRAL
            else self._last_inferred_structure_side.value,
        )

    def process_batch(self, *, payloads: Iterable[StructureEngineInput]) -> tuple[StructureEngineResult, ...]:
        return tuple(self.process(payload=payload) for payload in payloads)

    def _aggregate_events(
        self,
        *,
        liquidity_sweep_result: LiquiditySweepDetectionResult,
        bos_result: BOSDetectionResult,
        choch_result: CHOCHDetectionResult,
        fvg_result: FVGProcessingResult,
        support_resistance_result: SupportResistanceProcessingResult,
    ) -> tuple[StructureEvent, ...]:
        ordered_events: list[StructureEvent] = []
        ordered_events.extend(liquidity_sweep_result.emitted_sweeps)
        ordered_events.extend(bos_result.emitted_events)
        ordered_events.extend(choch_result.emitted_events)
        ordered_events.extend(fvg_result.emitted_events)
        ordered_events.extend(support_resistance_result.emitted_events)
        ordered_events.sort(key=self._event_sort_key)
        if not self._config.deduplicate_event_ids:
            return tuple(ordered_events)
        deduplicated: list[StructureEvent] = []
        for event in ordered_events:
            if event.event_id in self._recent_event_ids:
                continue
            deduplicated.append(event)
            self._remember_event_id(event.event_id)
        return tuple(deduplicated)

    def _event_sort_key(self, event: StructureEvent) -> tuple[str, int, str]:
        event_priority = {
            StructureEventType.LIQUIDITY_SWEEP_CONFIRMED: 10,
            StructureEventType.BOS: 20,
            StructureEventType.CHOCH: 30,
            StructureEventType.FVG_CREATED: 40,
            StructureEventType.FVG_UPDATED: 50,
            StructureEventType.FVG_MITIGATED: 60,
            StructureEventType.SR_ZONE_CREATED: 70,
            StructureEventType.SR_ZONE_UPDATED: 80,
            StructureEventType.SR_ZONE_TOUCHED: 90,
            StructureEventType.SR_ZONE_BROKEN: 100,
        }
        return (
            event.event_timestamp.isoformat(),
            event_priority.get(event.event_type, 999),
            event.event_id,
        )

    def _update_structure_side(self, events: tuple[StructureEvent, ...]) -> None:
        for event in events:
            if event.event_type in {StructureEventType.BOS, StructureEventType.CHOCH}:
                if event.side in {StructureSide.BULLISH, StructureSide.BEARISH}:
                    self._last_inferred_structure_side = event.side

    def _remember_event_id(self, event_id: str) -> None:
        self._recent_event_ids.append(event_id)
        overflow = len(self._recent_event_ids) - self._config.max_recent_event_ids
        if overflow > 0:
            del self._recent_event_ids[:overflow]

    def _validate_bar(self, bar: StructureBar) -> None:
        if bar.instrument_id != self._config.instrument_id:
            raise StructureEngineError("bar instrument_id does not match structure engine configuration")
        if bar.timeframe != self._config.timeframe:
            raise StructureEngineError("bar timeframe does not match structure engine configuration")
        if (
            self._config.require_strict_bar_order
            and self._last_bar_timestamp is not None
            and bar.bar_timestamp_utc <= self._last_bar_timestamp
        ):
            raise StructureEngineError("bars must arrive in strictly increasing finalized timestamp order")



def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "StructureEngine",
    "StructureEngineConfig",
    "StructureEngineError",
    "StructureEngineInput",
    "StructureEngineResult",
    "StructureEngineState",
]
