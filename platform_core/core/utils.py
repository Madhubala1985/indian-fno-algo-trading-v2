# FILE: platform_core/core/utils.py
"""
platform_core.core.utils
═══════════════════════════════════════════════════════════════════════
Shared foundational utilities for the institutional derivatives intelligence
and automated execution platform.

Design guarantees preserved:
- Full package imports only.
- No business logic.
- JSON / Redis / PostgreSQL / TimescaleDB compatible helpers.
- Replay-safe time normalization.
- Deterministic utility functions for core platform layers.
- Async-compatible retry utility for infrastructure boundaries.
- Type-safe, mypy-friendly, pytest-friendly implementation.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import singledispatch
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar
from uuid import UUID

from pydantic import BaseModel

from platform_core.core.constants import UTC
from platform_core.core.exceptions import PlatformError

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
T = TypeVar("T")


@singledispatch
def to_json_compatible(value: Any) -> JsonValue:
    """Convert supported Python objects into JSON-compatible values."""
    if is_dataclass(value):
        return cast_json(asdict(value))
    if isinstance(value, BaseModel):
        return cast_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(k): to_json_compatible(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_compatible(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON conversion type: {type(value)!r}")


@to_json_compatible.register(datetime)
def _datetime_to_json(value: datetime) -> JsonValue:
    return ensure_utc(value).isoformat()


@to_json_compatible.register(date)
def _date_to_json(value: date) -> JsonValue:
    return value.isoformat()


@to_json_compatible.register(time)
def _time_to_json(value: time) -> JsonValue:
    return value.isoformat()


@to_json_compatible.register(Decimal)
def _decimal_to_json(value: Decimal) -> JsonValue:
    return format_decimal(value)


def cast_json(value: Any) -> JsonValue:
    """Typed wrapper around JSON-compatible conversion."""
    return to_json_compatible(value)


def json_dumps(value: Any, *, sort_keys: bool = True, separators: tuple[str, str] = (",", ":")) -> str:
    """Serialize a value to stable JSON using the platform conversion rules."""
    return json.dumps(cast_json(value), sort_keys=sort_keys, separators=separators, ensure_ascii=False)


def ensure_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current UTC timestamp as timezone-aware datetime."""
    return datetime.now(UTC)


def floor_timestamp(value: datetime, interval_seconds: int) -> datetime:
    """Floor a timestamp to the nearest lower interval boundary in UTC."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    ts = ensure_utc(value)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % interval_seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def ceil_timestamp(value: datetime, interval_seconds: int) -> datetime:
    """Ceil a timestamp to the nearest upper interval boundary in UTC."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    ts = ensure_utc(value)
    epoch = int(ts.timestamp())
    ceiled = epoch if epoch % interval_seconds == 0 else epoch + (interval_seconds - epoch % interval_seconds)
    return datetime.fromtimestamp(ceiled, tz=UTC)


def safe_decimal(value: Decimal | str | int | float, *, quantize_to: Decimal | None = None) -> Decimal:
    """Safely coerce a numeric value to Decimal, preserving string precision."""
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise ValueError("float value must be finite")
            result = Decimal(str(value))
        else:
            result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"cannot coerce to Decimal: {value!r}") from exc

    if quantize_to is not None:
        return result.quantize(quantize_to)
    return result


def format_decimal(value: Decimal, *, normalize: bool = False) -> str:
    """Format Decimal as string for JSON and DB-safe storage."""
    return format(value.normalize() if normalize else value, "f")


def finite_float(value: float | int) -> float:
    """Validate and return a finite float."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("value must be finite")
    return result


def clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a float to a closed interval."""
    if lower > upper:
        raise ValueError("lower cannot exceed upper")
    return max(lower, min(value, upper))


def non_empty_str(value: str, *, field_name: str = "value") -> str:
    """Validate and normalize non-empty text."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must be non-empty")
    return cleaned


def merge_dicts(*mappings: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings from left to right into a new dictionary."""
    merged: dict[str, Any] = {}
    for mapping in mappings:
        merged.update(dict(mapping))
    return merged


def drop_none_values(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new dictionary excluding keys with None values."""
    return {key: value for key, value in mapping.items() if value is not None}


def chunked(sequence: list[T], chunk_size: int) -> list[list[T]]:
    """Split a list into equally sized chunks, preserving order."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [sequence[idx : idx + chunk_size] for idx in range(0, len(sequence), chunk_size)]


async def async_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int,
    base_delay_sec: float,
    max_delay_sec: float | None = None,
    retry_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retry an async operation with exponential backoff."""
    if retries < 0:
        raise ValueError("retries must be non-negative")
    if base_delay_sec <= 0:
        raise ValueError("base_delay_sec must be positive")
    if max_delay_sec is not None and max_delay_sec <= 0:
        raise ValueError("max_delay_sec must be positive when provided")

    attempt = 0
    while True:
        try:
            return await operation()
        except retry_exceptions:
            if attempt >= retries:
                raise
            delay = base_delay_sec * (2 ** attempt)
            if max_delay_sec is not None:
                delay = min(delay, max_delay_sec)
            await asyncio.sleep(delay)
            attempt += 1


def exception_chain_messages(exc: BaseException) -> list[str]:
    """Extract readable messages from an exception cause/context chain."""
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    return messages


def platform_error_to_dict(exc: PlatformError) -> dict[str, JsonValue]:
    """Serialize a PlatformError into a JSON-safe dictionary."""
    data = exc.to_dict()
    return {str(key): cast_json(value) for key, value in data.items()}


__all__ = [
    "JsonScalar",
    "JsonValue",
    "async_retry",
    "cast_json",
    "ceil_timestamp",
    "chunked",
    "clamp",
    "drop_none_values",
    "ensure_utc",
    "exception_chain_messages",
    "finite_float",
    "floor_timestamp",
    "format_decimal",
    "json_dumps",
    "merge_dicts",
    "non_empty_str",
    "platform_error_to_dict",
    "safe_decimal",
    "to_json_compatible",
    "utc_now",
]
