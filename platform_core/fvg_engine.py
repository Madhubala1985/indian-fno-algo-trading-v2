# FILE: platform_core/analytics/structure/fvg_engine.py

"""
platform_core.analytics.structure.fvg_engine
═══════════════════════════════════════════════════════════════════════
Production-grade causal Fair Value Gap (FVG) engine for finalized bars.

This module detects and tracks three-bar imbalances using finalized bars only.
It preserves no-hindsight behavior, supports deterministic replay, and manages
active gap lifecycle state including fresh, partially mitigated, and fully
mitigated transitions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from platform_core.analytics.structure.models import JsonValue
from platform_core.analytics.structure.models import StructureBar
from platform_core.analytics.structure.models import StructureEvent
from platform_core.analytics.structure.models import StructureEventType
from platform_core.analytics.structure.models import StructureSide


class FVGEngineError(RuntimeError):
    """Base exception for FVG engine failures."""


@dataclass(frozen=True, slots=True)
class FVGEngineConfig:
    """Configuration for causal FVG detection and lifecycle tracking."""

    instrument_id: str
    timeframe: str
    minimum_gap_size: float = 0.0
    minimum_gap_ratio: float = 0.0
    mitigation_threshold_ratio: float = 1.0
    mitigation_uses_close: bool = False
    max_active_gaps: int = 512
    max_recent_events: int = 512

    def __post_init__(self) -> None:

        if not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")

        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")

        if self.minimum_gap_size < 0:
            raise ValueError("minimum_gap_size must be non-negative")

        if self.minimum_gap_ratio < 0:
            raise ValueError("minimum_gap_ratio must be non-negative")

        if not 0.0 < self.mitigation_threshold_ratio <= 1.0:
            raise ValueError(
                "mitigation_threshold_ratio must be in (0, 1]"
            )

        if self.max_active_gaps <= 0:
            raise ValueError("max_active_gaps must be positive")

        if self.max_recent_events <= 0:
            raise ValueError("max_recent_events must be positive")


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """Serializable active fair value gap state."""

    gap_id: str
    instrument_id: str
    timeframe: str
    side: StructureSide

    created_timestamp: datetime

    anchor_start_timestamp: datetime
    anchor_middle_timestamp: datetime
    anchor_end_timestamp: datetime

    lower_price: float
    upper_price: float

    gap_size: float
    gap_ratio: float

    midpoint_price: float

    source_bar_index: int

    source_bar_ids: tuple[str, str, str]

    mitigation_ratio: float = 0.0

    is_fresh: bool = True
    is_partially_mitigated: bool = False
    is_fully_mitigated: bool = False

    last_touch_timestamp: datetime | None = None
    last_touch_price: float | None = None

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "gap_id": self.gap_id,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "created_timestamp": self.created_timestamp.isoformat(),
            "anchor_start_timestamp":
                self.anchor_start_timestamp.isoformat(),
            "anchor_middle_timestamp":
                self.anchor_middle_timestamp.isoformat(),
            "anchor_end_timestamp":
                self.anchor_end_timestamp.isoformat(),
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "gap_size": self.gap_size,
            "gap_ratio": self.gap_ratio,
            "midpoint_price": self.midpoint_price,
            "source_bar_index": self.source_bar_index,
            "source_bar_ids": list(self.source_bar_ids),
            "mitigation_ratio": self.mitigation_ratio,
            "is_fresh": self.is_fresh,
            "is_partially_mitigated":
                self.is_partially_mitigated,
            "is_fully_mitigated":
                self.is_fully_mitigated,
            "last_touch_timestamp":
                (
                    self.last_touch_timestamp.isoformat()
                    if self.last_touch_timestamp
                    else None
                ),
            "last_touch_price": self.last_touch_price,
        }


@dataclass(frozen=True, slots=True)
class FVGProcessingResult:
    """Output of processing one finalized bar."""

    created_gaps: tuple[FairValueGap, ...]
    updated_gaps: tuple[FairValueGap, ...]
    mitigated_gaps: tuple[FairValueGap, ...]
    emitted_events: tuple[StructureEvent, ...]

    processed_bar_count: int

    last_bar_timestamp: datetime | None

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "created_gaps":
                [gap.to_dict() for gap in self.created_gaps],

            "updated_gaps":
                [gap.to_dict() for gap in self.updated_gaps],

            "mitigated_gaps":
                [gap.to_dict() for gap in self.mitigated_gaps],

            "emitted_events":
                [event.to_dict() for event in self.emitted_events],

            "processed_bar_count":
                self.processed_bar_count,

            "last_bar_timestamp":
                (
                    self.last_bar_timestamp.isoformat()
                    if self.last_bar_timestamp
                    else None
                ),
        }


@dataclass(frozen=True, slots=True)
class FVGEngineState:
    """Serializable engine state."""

    instrument_id: str
    timeframe: str

    processed_bar_count: int

    last_bar_timestamp: datetime | None

    active_gaps: tuple[dict[str, JsonValue], ...]

    recent_events: tuple[dict[str, JsonValue], ...]

    buffered_bar_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:

        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "processed_bar_count":
                self.processed_bar_count,
            "last_bar_timestamp":
                (
                    self.last_bar_timestamp.isoformat()
                    if self.last_bar_timestamp
                    else None
                ),
            "active_gaps": list(self.active_gaps),
            "recent_events": list(self.recent_events),
            "buffered_bar_ids": list(self.buffered_bar_ids),
        }


class FVGEngine:
    """Causal three-bar FVG detection and mitigation tracking engine."""

    def __init__(
        self,
        *,
        config: FVGEngineConfig,
    ) -> None:

        self._config = config

        self._bar_buffer: deque[StructureBar] = deque(
            maxlen=3
        )

        self._active_gaps: dict[str, FairValueGap] = {}

        self._recent_events: deque[StructureEvent] = deque(
            maxlen=config.max_recent_events
        )

        self._processed_bar_count = 0

        self._last_bar_timestamp: datetime | None = None

    @property
    def active_gaps(self) -> tuple[FairValueGap, ...]:

        return tuple(
            sorted(
                self._active_gaps.values(),
                key=lambda gap: gap.created_timestamp,
            )
        )

    @property
    def recent_events(self) -> tuple[StructureEvent, ...]:

        return tuple(self._recent_events)

    @property
    def state(self) -> FVGEngineState:

        return FVGEngineState(
            instrument_id=self._config.instrument_id,
            timeframe=self._config.timeframe,
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
            active_gaps=tuple(
                gap.to_dict()
                for gap in self.active_gaps
            ),
            recent_events=tuple(
                event.to_dict()
                for event in self._recent_events
            ),
            buffered_bar_ids=tuple(
                str(bar.event_sequence_number)
                for bar in self._bar_buffer
            ),
        )

    def reset(self) -> None:

        self._bar_buffer.clear()

        self._active_gaps.clear()

        self._recent_events.clear()

        self._processed_bar_count = 0

        self._last_bar_timestamp = None

    def process_bar(
        self,
        *,
        bar: StructureBar,
    ) -> FVGProcessingResult:

        self._validate_bar(bar)

        created_gaps: list[FairValueGap] = []

        updated_gaps: list[FairValueGap] = []

        mitigated_gaps: list[FairValueGap] = []

        emitted_events: list[StructureEvent] = []

        self._bar_buffer.append(bar)

        mitigation_updates, mitigation_events = (
            self._update_active_gaps(bar)
        )

        updated_gaps.extend(mitigation_updates[0])

        mitigated_gaps.extend(mitigation_updates[1])

        emitted_events.extend(mitigation_events)

        if len(self._bar_buffer) == 3:

            new_gap = self._detect_gap_from_buffer()

            if new_gap is not None:

                self._enforce_active_gap_capacity()

                self._active_gaps[new_gap.gap_id] = new_gap

                created_gaps.append(new_gap)

                event = self._build_gap_created_event(
                    new_gap
                )

                self._recent_events.append(event)

                emitted_events.append(event)

        for event in mitigation_events:
            self._recent_events.append(event)

        self._processed_bar_count += 1

        self._last_bar_timestamp = bar.bar_timestamp

        return FVGProcessingResult(
            created_gaps=tuple(created_gaps),
            updated_gaps=tuple(updated_gaps),
            mitigated_gaps=tuple(mitigated_gaps),
            emitted_events=tuple(emitted_events),
            processed_bar_count=self._processed_bar_count,
            last_bar_timestamp=self._last_bar_timestamp,
        )

    def process_bars(
        self,
        *,
        bars: list[StructureBar],
    ) -> tuple[FVGProcessingResult, ...]:

        return tuple(
            self.process_bar(bar=bar)
            for bar in bars
        )

    def _detect_gap_from_buffer(
        self,
    ) -> FairValueGap | None:

        left_bar, middle_bar, right_bar = tuple(
            self._bar_buffer
        )

        bullish_gap_lower = left_bar.high_price

        bullish_gap_upper = right_bar.low_price

        bullish_gap_size = (
            bullish_gap_upper - bullish_gap_lower
        )

        bullish_gap_ratio = (
            bullish_gap_size / bullish_gap_lower
            if bullish_gap_lower > 0
            else 0.0
        )

        if (
            bullish_gap_upper > bullish_gap_lower
            and self._passes_gap_filters(
                gap_size=bullish_gap_size,
                gap_ratio=bullish_gap_ratio,
            )
        ):

            midpoint = (
                bullish_gap_lower + bullish_gap_upper
            ) / 2.0

            return FairValueGap(

                gap_id=self._gap_id(
                    side=StructureSide.BULLISH,
                    right_bar=right_bar,
                ),

                instrument_id=right_bar.instrument_id,

                timeframe=right_bar.timeframe,

                side=StructureSide.BULLISH,

                created_timestamp=right_bar.bar_timestamp,

                anchor_start_timestamp=left_bar.bar_timestamp,

                anchor_middle_timestamp=middle_bar.bar_timestamp,

                anchor_end_timestamp=right_bar.bar_timestamp,

                lower_price=bullish_gap_lower,

                upper_price=bullish_gap_upper,

                gap_size=bullish_gap_size,

                gap_ratio=bullish_gap_ratio,

                midpoint_price=midpoint,

                source_bar_index=self._processed_bar_count,

                source_bar_ids=(
                    str(left_bar.event_sequence_number),
                    str(middle_bar.event_sequence_number),
                    str(right_bar.event_sequence_number),
                ),
            )

        bearish_gap_upper = left_bar.low_price

        bearish_gap_lower = right_bar.high_price

        bearish_gap_size = (
            bearish_gap_upper - bearish_gap_lower
        )

        bearish_gap_ratio = (
            bearish_gap_size / bearish_gap_upper
            if bearish_gap_upper > 0
            else 0.0
        )

        if (
            bearish_gap_upper > bearish_gap_lower
            and self._passes_gap_filters(
                gap_size=bearish_gap_size,
                gap_ratio=bearish_gap_ratio,
            )
        ):

            midpoint = (
                bearish_gap_lower + bearish_gap_upper
            ) / 2.0

            return FairValueGap(

                gap_id=self._gap_id(
                    side=StructureSide.BEARISH,
                    right_bar=right_bar,
                ),

                instrument_id=right_bar.instrument_id,

                timeframe=right_bar.timeframe,

                side=StructureSide.BEARISH,

                created_timestamp=right_bar.bar_timestamp,

                anchor_start_timestamp=left_bar.bar_timestamp,

                anchor_middle_timestamp=middle_bar.bar_timestamp,

                anchor_end_timestamp=right_bar.bar_timestamp,

                lower_price=bearish_gap_lower,

                upper_price=bearish_gap_upper,

                gap_size=bearish_gap_size,

                gap_ratio=bearish_gap_ratio,

                midpoint_price=midpoint,

                source_bar_index=self._processed_bar_count,

                source_bar_ids=(
                    str(left_bar.event_sequence_number),
                    str(middle_bar.event_sequence_number),
                    str(right_bar.event_sequence_number),
                ),
            )

        return None

    def _update_active_gaps(
        self,
        bar: StructureBar,
    ) -> tuple[
        tuple[list[FairValueGap], list[FairValueGap]],
        list[StructureEvent],
    ]:

        updated_gaps: list[FairValueGap] = []

        mitigated_gaps: list[FairValueGap] = []

        emitted_events: list[StructureEvent] = []

        for gap_id, gap in list(
            self._active_gaps.items()
        ):

            updated_gap = self._apply_bar_to_gap(
                gap=gap,
                bar=bar,
            )

            if updated_gap is None:
                continue

            self._active_gaps[gap_id] = updated_gap

            if updated_gap.is_fully_mitigated:

                mitigated_gaps.append(updated_gap)

                emitted_events.append(
                    self._build_gap_mitigated_event(
                        updated_gap,
                        bar,
                    )
                )

                del self._active_gaps[gap_id]

            elif (
                updated_gap.mitigation_ratio
                > gap.mitigation_ratio
            ):

                updated_gaps.append(updated_gap)

                emitted_events.append(
                    self._build_gap_updated_event(
                        updated_gap,
                        bar,
                    )
                )

        return (
            updated_gaps,
            mitigated_gaps,
        ), emitted_events

    def _apply_bar_to_gap(
        self,
        *,
        gap: FairValueGap,
        bar: StructureBar,
    ) -> FairValueGap | None:

        if bar.bar_timestamp <= gap.created_timestamp:
            return None

        interaction_price = self._mitigation_price(
            bar=bar,
            side=gap.side,
        )

        overlap_ratio = self._calculate_overlap_ratio(
            gap=gap,
            interaction_price=interaction_price,
        )

        if overlap_ratio <= gap.mitigation_ratio:
            return None

        fully_mitigated = (
            overlap_ratio
            >= self._config.mitigation_threshold_ratio
        )

        last_touch_price = min(
            max(interaction_price, gap.lower_price),
            gap.upper_price,
        )

        return FairValueGap(
            gap_id=gap.gap_id,
            instrument_id=gap.instrument_id,
            timeframe=gap.timeframe,
            side=gap.side,
            created_timestamp=gap.created_timestamp,
            anchor_start_timestamp=gap.anchor_start_timestamp,
            anchor_middle_timestamp=gap.anchor_middle_timestamp,
            anchor_end_timestamp=gap.anchor_end_timestamp,
            lower_price=gap.lower_price,
            upper_price=gap.upper_price,
            gap_size=gap.gap_size,
            gap_ratio=gap.gap_ratio,
            midpoint_price=gap.midpoint_price,
            source_bar_index=gap.source_bar_index,
            source_bar_ids=gap.source_bar_ids,
            mitigation_ratio=overlap_ratio,
            is_fresh=False,
            is_partially_mitigated=not fully_mitigated,
            is_fully_mitigated=fully_mitigated,
            last_touch_timestamp=bar.bar_timestamp,
            last_touch_price=last_touch_price,
        )

    def _mitigation_price(
        self,
        *,
        bar: StructureBar,
        side: StructureSide,
    ) -> float:

        if side == StructureSide.BULLISH:

            return (
                bar.close_price
                if self._config.mitigation_uses_close
                else bar.low_price
            )

        return (
            bar.close_price
            if self._config.mitigation_uses_close
            else bar.high_price
        )

    def _calculate_overlap_ratio(
        self,
        *,
        gap: FairValueGap,
        interaction_price: float,
    ) -> float:

        if gap.side == StructureSide.BULLISH:

            if interaction_price >= gap.upper_price:
                return 0.0

            touched_depth = (
                gap.upper_price
                - max(interaction_price, gap.lower_price)
            )

            return min(
                1.0,
                max(0.0, touched_depth / gap.gap_size),
            )

        if interaction_price <= gap.lower_price:
            return 0.0

        touched_depth = (
            min(interaction_price, gap.upper_price)
            - gap.lower_price
        )

        return min(
            1.0,
            max(0.0, touched_depth / gap.gap_size),
        )

    def _passes_gap_filters(
        self,
        *,
        gap_size: float,
        gap_ratio: float,
    ) -> bool:

        if gap_size < self._config.minimum_gap_size:
            return False

        if gap_ratio < self._config.minimum_gap_ratio:
            return False

        return True

    def _build_gap_created_event(
        self,
        gap: FairValueGap,
    ) -> StructureEvent:

        return StructureEvent(
            event_id=f"{gap.gap_id}:created",
            instrument_id=gap.instrument_id,
            timeframe=gap.timeframe,
            event_type=StructureEventType.FVG_CREATED,
            side=gap.side,
            event_timestamp=gap.created_timestamp,
            trigger_price=gap.midpoint_price,
            reference_price=(
                gap.lower_price
                if gap.side == StructureSide.BULLISH
                else gap.upper_price
            ),
            point_id=gap.gap_id,
            related_point_ids=gap.source_bar_ids,
            confidence_score=gap.gap_ratio,
            causal_bar_index=gap.source_bar_index,
            metadata={
                "lower_price": gap.lower_price,
                "upper_price": gap.upper_price,
                "gap_size": gap.gap_size,
                "gap_ratio": gap.gap_ratio,
                "midpoint_price": gap.midpoint_price,
            },
        )

    def _build_gap_updated_event(
        self,
        gap: FairValueGap,
        bar: StructureBar,
    ) -> StructureEvent:

        return StructureEvent(
            event_id=(
                f"{gap.gap_id}:updated:"
                f"{bar.bar_timestamp.isoformat()}"
            ),
            instrument_id=gap.instrument_id,
            timeframe=gap.timeframe,
            event_type=StructureEventType.FVG_UPDATED,
            side=gap.side,
            event_timestamp=bar.bar_timestamp,
            trigger_price=(
                gap.last_touch_price
                if gap.last_touch_price is not None
                else gap.midpoint_price
            ),
            reference_price=gap.midpoint_price,
            point_id=gap.gap_id,
            related_point_ids=gap.source_bar_ids,
            confidence_score=gap.mitigation_ratio,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "mitigation_ratio":
                    gap.mitigation_ratio,

                "is_partial":
                    gap.is_partially_mitigated,

                "is_fully_mitigated":
                    gap.is_fully_mitigated,

                "last_touch_timestamp":
                    (
                        gap.last_touch_timestamp.isoformat()
                        if gap.last_touch_timestamp
                        else None
                    ),

                "last_touch_price":
                    gap.last_touch_price,
            },
        )

    def _build_gap_mitigated_event(
        self,
        gap: FairValueGap,
        bar: StructureBar,
    ) -> StructureEvent:

        return StructureEvent(
            event_id=(
                f"{gap.gap_id}:mitigated:"
                f"{bar.bar_timestamp.isoformat()}"
            ),
            instrument_id=gap.instrument_id,
            timeframe=gap.timeframe,
            event_type=StructureEventType.FVG_MITIGATED,
            side=gap.side,
            event_timestamp=bar.bar_timestamp,
            trigger_price=(
                gap.last_touch_price
                if gap.last_touch_price is not None
                else gap.midpoint_price
            ),
            reference_price=gap.midpoint_price,
            point_id=gap.gap_id,
            related_point_ids=gap.source_bar_ids,
            confidence_score=gap.mitigation_ratio,
            causal_bar_index=self._processed_bar_count,
            metadata={
                "mitigation_ratio":
                    gap.mitigation_ratio,

                "mitigation_threshold_ratio":
                    self._config.mitigation_threshold_ratio,

                "last_touch_timestamp":
                    (
                        gap.last_touch_timestamp.isoformat()
                        if gap.last_touch_timestamp
                        else None
                    ),

                "last_touch_price":
                    gap.last_touch_price,
            },
        )

    def _gap_id(
        self,
        *,
        side: StructureSide,
        right_bar: StructureBar,
    ) -> str:

        return (
            f"{right_bar.instrument_id}:"
            f"{right_bar.timeframe}:"
            f"fvg:{side.value}:"
            f"{right_bar.bar_timestamp.isoformat()}:"
            f"{right_bar.event_sequence_number}"
        )

    def _enforce_active_gap_capacity(self) -> None:

        if len(self._active_gaps) < self._config.max_active_gaps:
            return

        oldest_gap_id = min(
            self._active_gaps,
            key=lambda gap_id:
                self._active_gaps[gap_id].created_timestamp,
        )

        del self._active_gaps[oldest_gap_id]

    def _validate_bar(
        self,
        bar: StructureBar,
    ) -> None:

        if (
            bar.instrument_id
            != self._config.instrument_id
        ):
            raise FVGEngineError(
                "bar instrument_id does not match engine configuration"
            )

        if (
            bar.timeframe
            != self._config.timeframe
        ):
            raise FVGEngineError(
                "bar timeframe does not match engine configuration"
            )

        current_timestamp = bar.bar_timestamp

        if (
            self._last_bar_timestamp is not None
            and current_timestamp <= self._last_bar_timestamp
        ):
            raise FVGEngineError(
                "bars must arrive in strictly increasing finalized timestamp order"
            )


def _ensure_utc(
    value: datetime,
) -> datetime:

    if value.tzinfo is None:
        return value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    )


__all__ = [
    "FVGEngine",
    "FVGEngineConfig",
    "FVGEngineError",
    "FVGEngineState",
    "FVGProcessingResult",
    "FairValueGap",
]
