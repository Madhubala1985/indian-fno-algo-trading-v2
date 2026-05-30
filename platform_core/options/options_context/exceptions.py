# FILE: platform_core/options/options_context/exceptions.py
"""Error hierarchy for the options context builder."""
from __future__ import annotations


class OptionsContextError(RuntimeError):
    """Base exception for options context builder failures."""


class OptionsContextInputError(OptionsContextError):
    """Raised when builder inputs are invalid or structurally inconsistent."""


__all__ = ["OptionsContextError", "OptionsContextInputError"]
