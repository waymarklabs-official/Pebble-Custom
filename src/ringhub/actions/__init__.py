"""Importing this package registers every built-in action."""

from . import calendar, car, content, core, memory, tesla, tv
from .base import REGISTRY, ActionError

__all__ = ["REGISTRY", "ActionError", "calendar", "car", "content", "core", "memory", "tesla", "tv"]
