"""Context: where you are and what you're doing, fed by your phone.

iOS Shortcuts automations (arrive/leave a location, car Bluetooth/CarPlay connect, Focus on/off,
NFC tags, alarm stopped...) POST tiny updates here. The hub keeps the current values plus a
timeline, so a voice note the ring stored offline and delivered 20 minutes late is still filed
under the place where it was *said*.

Reserved keys:
- ``place``: the named place you're at (``home``, ``work``, ``car``, ...) or ``None``.
- ``focus``: the active iOS Focus (``work``, ``sleep``, ...) or ``None``.
- ``location``: ``{"lat", "lon", "accuracy"}`` from the phone, when a Shortcut sends it.
Anything else is free-form (``{"set": {"guests": true}}``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from .store import Store

log = logging.getLogger(__name__)

PREFIX = "ctx."
KEY_RE = r"^[a-z][a-z0-9_]{0,31}$"
DUPLICATE_ARRIVE_SECONDS = 120
"""Shortcuts, OwnTracks and Wi-Fi can all report the same arrival; collapse them."""
LEAVE_JITTER_SECONDS = 240
"""iOS geofences flap near the boundary: a leave this soon after arriving is ignored."""


@dataclass(frozen=True, slots=True)
class PlaceEvent:
    kind: Literal["arrive", "leave", "visit"]
    place: str
    at: float
    previous: str | None


Listener = Callable[[PlaceEvent], Awaitable[None]]


class Context:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._listeners: list[Listener] = []
        self._lock = asyncio.Lock()

    def on_place_event(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def snapshot(self) -> dict[str, Any]:
        def op(db: sqlite3.Connection) -> dict[str, Any]:
            rows = db.execute("SELECT key, value FROM state WHERE key LIKE ?", (PREFIX + "%",)).fetchall()
            return {k[len(PREFIX):]: json.loads(v) for k, v in rows}

        snap = await self._store.run(op)
        snap.setdefault("place", None)
        snap.setdefault("focus", None)
        return snap

    async def get(self, key: str, default: Any = None) -> Any:
        return await self._store.get(PREFIX + key, default)

    async def value_at(self, key: str, at: float) -> Any:
        """Value of ``key`` as it was at time ``at`` (None if never set before then)."""
        row = await self._store.run(lambda db: db.execute(
            "SELECT value FROM context_log WHERE key = ? AND at <= ? ORDER BY at DESC, seq DESC LIMIT 1",
            (key, at)).fetchone())
        return json.loads(row[0]) if row else None

    async def set(self, key: str, value: Any, *, source: str = "api", at: float | None = None) -> bool:
        """Set a context value. Returns True if it changed."""
        stamp = at if at is not None else time.time()
        encoded = json.dumps(value, sort_keys=True)

        def op(db: sqlite3.Connection) -> bool:
            row = db.execute("SELECT value FROM state WHERE key = ?", (PREFIX + key,)).fetchone()
            if row is not None and row[0] == encoded:
                return False
            if value is None:
                db.execute("DELETE FROM state WHERE key = ?", (PREFIX + key,))
            else:
                db.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (PREFIX + key, encoded))
            db.execute("INSERT INTO context_log (at, key, value, source) VALUES (?, ?, ?, ?)",
                       (stamp, key, encoded, source))
            db.execute("DELETE FROM context_log WHERE at < ?", (stamp - 90 * 86400,))
            return True

        return await self._store.run(op)

    async def arrive(self, place: str, *, source: str = "api", at: float | None = None) -> PlaceEvent | None:
        """Returns the event, or None when it was a duplicate report of the current place."""
        async with self._lock:
            previous = await self.get("place")
            stamp = at if at is not None else time.time()
            since = await self.get("place_since")
            if previous == place and isinstance(since, int | float) and stamp - since < DUPLICATE_ARRIVE_SECONDS:
                return None
            # A later re-arrival at the "same" place still counts: the leave may have been missed
            # (dead battery, no signal), and pending "when I get home" reminders must still fire.
            await self.set("place", place, source=source, at=stamp)
            await self.set("place_since", stamp, source=source, at=stamp)
            event = PlaceEvent("arrive", place, stamp, previous)
        await self._emit(event)
        return event

    async def visit(self, place: str, *, source: str = "api", at: float | None = None) -> PlaceEvent:
        """A passing visit (a Wallet tap at a store, an NFC tag): fires "arrive" reminders for
        that place without changing where you are."""
        stamp = at if at is not None else time.time()
        await self.set("last_visit", {"place": place, "at": stamp}, source=source, at=stamp)
        event = PlaceEvent("visit", place, stamp, await self.get("place"))
        await self._emit(event)
        return event

    async def leave(self, place: str, *, source: str = "api", at: float | None = None) -> PlaceEvent | None:
        async with self._lock:
            previous = await self.get("place")
            stamp = at if at is not None else time.time()
            since = await self.get("place_since")
            if previous == place and isinstance(since, int | float) and stamp - since < LEAVE_JITTER_SECONDS:
                log.info("ignoring leave %s %.0fs after arriving (geofence jitter)", place, stamp - since)
                return None
            if previous == place:
                # Only clear if we're leaving where we think we are; a late "leave home" after
                # "arrive work" must not wipe the newer place.
                await self.set("place", None, source=source, at=stamp)
                await self.set("place_since", stamp, source=source, at=stamp)
            await self.set("last_left", {"place": place, "at": stamp}, source=source, at=stamp)
            event = PlaceEvent("leave", place, stamp, previous)
        await self._emit(event)
        return event

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._store.run(lambda db: db.execute(
            "SELECT at, key, value, source FROM context_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall())
        return [{"at": r[0], "key": r[1], "value": json.loads(r[2]), "source": r[3]} for r in rows]

    async def _emit(self, event: PlaceEvent) -> None:
        for listener in self._listeners:
            try:
                await listener(event)
            except Exception:  # one bad listener must not block the rest
                log.exception("place listener failed for %s %s", event.kind, event.place)
