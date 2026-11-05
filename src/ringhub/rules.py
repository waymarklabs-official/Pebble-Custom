"""Pure rule matching."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .config import When
from .models import RingEvent

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def matches(when: When, event: RingEvent, *, mode: str, state: dict[str, Any], now: datetime) -> bool:
    sequence = event.sequence or (event.gesture,)
    if when.sequence is not None:
        if tuple(when.sequence) != sequence:
            return False
    elif when.gesture is not None:
        if len(sequence) != 1 or sequence[0] not in when.gesture:
            return False
    elif len(sequence) != 1:
        return False  # catch-all rules never swallow combos
    if when.mode is not None and mode not in when.mode:
        return False
    if when.time is not None and not when.time.contains(now.time()):
        return False
    if when.days is not None and _DAYS[now.weekday()] not in when.days:
        return False
    if when.state is not None and any(state.get(k) != v for k, v in when.state.items()):
        return False
    if when.payload is not None and event.payload != when.payload:
        return False
    if when.transcript is not None and not re.search(when.transcript, event.transcript or "", re.IGNORECASE):
        return False
    return True
