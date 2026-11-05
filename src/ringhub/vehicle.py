"""Car state for context-aware skills, read cheaply and never by waking the car.

Reading ``vehicle_data`` on a sleeping car fails with HTTP 408 rather than waking it, and a
plain ``GET /vehicles/{vin}`` reports ``online``/``asleep``/``offline`` without waking it. So
the hub asks about the car only when a skill needs to know, caches the answer briefly, and
treats a sleeping car as "locked and nobody near it", which is true in practice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .actions.base import ActionError

if TYPE_CHECKING:
    from .actions.tesla import TeslaClient

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VehicleState:
    online: bool
    locked: bool | None = None
    user_present: bool | None = None
    shift_state: str | None = None
    lat: float | None = None
    lon: float | None = None
    climate_on: bool | None = None
    fetched_at: float = 0.0

    @property
    def summary(self) -> str:
        if not self.online:
            return "asleep"
        parts = ["locked" if self.locked else "unlocked" if self.locked is False else "lock state unknown"]
        if self.user_present:
            parts.append("someone inside")
        if self.shift_state and self.shift_state not in ("P", None):
            parts.append(f"in {self.shift_state}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"online": self.online, "locked": self.locked, "user_present": self.user_present,
                "shift_state": self.shift_state, "lat": self.lat, "lon": self.lon,
                "climate_on": self.climate_on, "fetched_at": self.fetched_at, "summary": self.summary}


def parse_vehicle_data(data: dict[str, Any], now: float) -> VehicleState:
    vs = data.get("vehicle_state") or {}
    ds = data.get("drive_state") or {}
    cs = data.get("climate_state") or {}
    loc = data.get("location_data") or {}

    def num(*vals: Any) -> float | None:
        for v in vals:
            if isinstance(v, int | float):
                return float(v)
        return None

    return VehicleState(
        online=True,
        locked=vs.get("locked") if isinstance(vs.get("locked"), bool) else None,
        user_present=vs.get("is_user_present") if isinstance(vs.get("is_user_present"), bool) else None,
        shift_state=ds.get("shift_state"),
        lat=num(ds.get("latitude"), loc.get("latitude"), ds.get("active_route_latitude")),
        lon=num(ds.get("longitude"), loc.get("longitude"), ds.get("active_route_longitude")),
        climate_on=cs.get("is_climate_on") if isinstance(cs.get("is_climate_on"), bool) else None,
        fetched_at=now,
    )


class Vehicle:
    def __init__(self, client: TeslaClient, ttl_seconds: float = 30.0) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cached: VehicleState | None = None
        self._last_good: VehicleState | None = None
        self._lock = asyncio.Lock()

    async def state(self, *, allow_stale: bool = False) -> VehicleState | None:
        """Current state, or None if the API is unreachable. Never wakes the car."""
        async with self._lock:
            now = time.time()
            if self._cached and now - self._cached.fetched_at < self._ttl:
                return self._cached
            try:
                online = await self._client.online_state()
                if online != "online":
                    state = VehicleState(online=False, fetched_at=now)
                    if allow_stale and self._last_good is not None:
                        # Asleep cars don't move: the last known position is still right.
                        state = VehicleState(online=False, locked=True, lat=self._last_good.lat,
                                             lon=self._last_good.lon, fetched_at=now)
                else:
                    state = parse_vehicle_data(await self._client.vehicle_data(), now)
                    self._last_good = state
            except ActionError as e:
                log.warning("vehicle state unavailable: %s", e)
                return self._last_good if allow_stale else None
            self._cached = state
            return state

    def invalidate(self) -> None:
        self._cached = None
