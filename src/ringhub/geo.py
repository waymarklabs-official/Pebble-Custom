"""Tiny geo helpers (no dependencies)."""

from __future__ import annotations

import math
import time
from typing import Any

FRESH_PHONE_FIX_SECONDS = 600


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fresh_fix(location: Any, now: float | None = None) -> tuple[float, float] | None:
    """(lat, lon) from a context ``location`` value if it is recent enough to trust."""
    if not isinstance(location, dict):
        return None
    lat, lon, at = location.get("lat"), location.get("lon"), location.get("at", 0)
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    if (now if now is not None else time.time()) - float(at or 0) > FRESH_PHONE_FIX_SECONDS:
        return None
    return float(lat), float(lon)
