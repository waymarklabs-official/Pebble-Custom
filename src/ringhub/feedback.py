"""One place that decides how loudly the hub talks back.

Levels, from loudest: ``important`` (reminders, failures: always delivered, high priority),
``normal``, ``quiet`` (low priority, so no sound on most phones), ``silent`` (not sent).
A mode's feedback setting caps confirmations, so Driving or Sleep mode stays out of the way.
"""

from __future__ import annotations

import logging
from typing import Literal

from .actions.base import ActionError
from .notifier import Notifier

log = logging.getLogger(__name__)

Level = Literal["important", "normal", "quiet", "silent"]
_PRIORITY = {"important": 4, "normal": 3, "quiet": 2}
_ORDER = ["silent", "quiet", "normal", "important"]


def cap(level: Level, mode_feedback: str) -> Level:
    """The quieter of ``level`` and the mode's setting (important always passes)."""
    if level == "important":
        return level
    allowed = mode_feedback if mode_feedback in _ORDER else "normal"
    return level if _ORDER.index(level) <= _ORDER.index(allowed) else allowed  # type: ignore[return-value]


class Feedback:
    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self.sent: list[str] = []
        """Last messages (for the dashboard's "what did the ring say" view)."""

    async def say(self, message: str, *, title: str | None = None, level: Level = "normal",
                  tags: list[str] | None = None, actions: str | None = None, click: str | None = None) -> bool:
        if level == "silent":
            return False
        self.sent = [message, *self.sent][:20]
        if not self._notifier.enabled:
            log.info("feedback (ntfy not configured): %s", message.replace("\n", " | "))
            return False
        try:
            await self._notifier.publish(message, title=title, priority=_PRIORITY[level], tags=tags,
                                         actions=actions, click=click)
            return True
        except ActionError as e:
            log.warning("feedback push failed: %s", e)
            return False
