"""Core domain types shared across the hub."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Gesture(StrEnum):
    """Gestures exactly as the Index app reports them in ``X-Index-Trigger``.

    The app documents these values as stable ("Endpoints key off these, do not rename them").
    """

    CLICK = "click"
    DOUBLE_CLICK = "double-click"
    TRIPLE_CLICK = "triple-click"
    HOLD = "single-click-hold"
    CLICK_HOLD = "double-click-hold"

    @property
    def is_voice(self) -> bool:
        return self in (Gesture.HOLD, Gesture.CLICK_HOLD)


TEST_TRIGGER = "test-event"

# Friendlier spellings accepted in config files.
_ALIASES: dict[str, Gesture] = {
    "single": Gesture.CLICK,
    "double": Gesture.DOUBLE_CLICK,
    "triple": Gesture.TRIPLE_CLICK,
    "hold": Gesture.HOLD,
    "click-hold": Gesture.CLICK_HOLD,
    "double-hold": Gesture.CLICK_HOLD,
}


def parse_gesture(value: str) -> Gesture:
    """Parse a gesture name from config or the wire, accepting aliases. Raises ``ValueError``."""
    key = value.strip().lower().replace("_", "-").replace(" ", "-")
    if key in _ALIASES:
        return _ALIASES[key]
    return Gesture(key)


@dataclass(frozen=True, slots=True)
class RingEvent:
    """A normalized, authenticated event from the ring.

    ``sequence`` is filled by the combo engine: a lone click has ``sequence == (CLICK,)``,
    a ``double-click`` followed quickly by a ``click`` has ``(DOUBLE_CLICK, CLICK)``.
    """

    delivery_id: str
    gesture: Gesture
    received_at: datetime
    occurred_at: datetime
    transcript: str | None = None
    audio_path: Path | None = None
    payload: Any = None
    sequence: tuple[Gesture, ...] = field(default=())

    def with_sequence(self, sequence: tuple[Gesture, ...]) -> RingEvent:
        return replace(self, sequence=sequence)

    def as_template_vars(self) -> dict[str, Any]:
        return {
            "id": self.delivery_id,
            "gesture": self.gesture.value,
            "sequence": [g.value for g in self.sequence] or [self.gesture.value],
            "transcript": self.transcript or "",
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "payload": self.payload,
            "at": self.occurred_at,
        }
