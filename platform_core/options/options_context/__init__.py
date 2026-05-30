"""
platform_core.options.options_context
Options context orchestration package.

Public API:
  OptionsContextBuilder      — main entry point
  OptionsContextBuilderConfig — static configuration
  OptionsContext             — unified analytics output
  OptionsContextError        — base exception
  OptionsContextInputError   — input validation exception
  TimeToExpiryProvider       — Protocol for TTE computation
  CalendarDaysTTEProvider    — default calendar-day TTE implementation
"""
from platform_core.options.options_context.exceptions import (
    OptionsContextError,
    OptionsContextInputError,
)
from platform_core.options.options_context.models import (
    OptionsContextBuilderConfig,
    OptionsContext,
)
from platform_core.options.options_context.helpers import (
    TimeToExpiryProvider,
    CalendarDaysTTEProvider,
)
from platform_core.options.options_context.builder import OptionsContextBuilder

__all__ = [
    "CalendarDaysTTEProvider",
    "OptionsContext",
    "OptionsContextBuilder",
    "OptionsContextBuilderConfig",
    "OptionsContextError",
    "OptionsContextInputError",
    "TimeToExpiryProvider",
]
