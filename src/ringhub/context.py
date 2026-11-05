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
"""iOS geofences flap near the boundary: a leave this soon after arriving is held back, and only
committed if you don't re-arrive within this window (a 3-minute drive still counts)."""


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
        self._pending_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

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
        """Returns the event, or None when it changed nothing (duplicate report, or older than
        the place we already know about)."""
        events: list[PlaceEvent] = []
        async with self._lock:
            previous = await self.get("place")
            stamp = at if at is not None else time.time()
            since = await self.get("place_since")
            since = float(since) if isinstance(since, int | float) else None
            pending = await self.get("pending_leave")
            if isinstance(pending, dict) and pending.get("place") == place:
                # Re-arrived before a jittery leave was committed: it never left.
                await self._clear_pending()
                return None
            if previous == place and since is not None and stamp - since < DUPLICATE_ARRIVE_SECONDS:
                return None
            if previous != place and since is not None and stamp < since:
                # A queued report (OwnTracks replays transitions with their original time) must not
                # move you back in time. Keep it in the timeline only.
                await self._log("stale_arrive", {"place": place}, source=source, at=stamp)
                return None
            if isinstance(pending, dict):
                # Arriving somewhere else proves the held-back leave was real: commit it first.
                left = await self._commit_leave(str(pending.get("place")), float(pending.get("at", stamp)),
                                                str(pending.get("source", source)))
                if left is not None:
                    events.append(left)
                previous = await self.get("place")
            # A re-arrival at the "same" place still counts (the leave may have been missed), so
            # pending "when I get home" reminders fire; but it doesn't restart place_since.
            await self.set("place", place, source=source, at=stamp)
            if previous != place:
                await self.set("place_since", stamp, source=source, at=stamp)
            event = PlaceEvent("arrive", place, stamp, previous)
            events.append(event)
        for e in events:
            await self._emit(e)
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
        """Returns the event, or None when the leave was held back as possible geofence jitter."""
        async with self._lock:
            previous = await self.get("place")
            stamp = at if at is not None else time.time()
            since = await self.get("place_since")
            if previous == place and isinstance(since, int | float) and stamp - since < LEAVE_JITTER_SECONDS:
                log.info("holding leave %s (%.0fs after arriving) in case it's geofence jitter", place, stamp - since)
                held = {"place": place, "at": stamp, "source": source}
                await self.set("pending_leave", held, source=source, at=stamp)
                self._arm_pending(float(since) + LEAVE_JITTER_SECONDS - time.time())
                return None
            event = await self._commit_leave(place, stamp, source)
        if event is not None:
            await self._emit(event)
        return event

    async def resume(self) -> None:
        """Re-arm a held-back leave after a restart (commits at once if its window has passed)."""
        pending = await self.get("pending_leave")
        since = await self.get("place_since")
        if isinstance(pending, dict):
            base = float(since) if isinstance(since, int | float) else float(pending.get("at", 0))
            self._arm_pending(base + LEAVE_JITTER_SECONDS - time.time())

    async def _commit_leave(self, place: str, stamp: float, source: str) -> PlaceEvent | None:
        """Apply a leave. Caller holds the lock."""
        await self._clear_pending()
        previous = await self.get("place")
        if previous == place:
            # Only clear if we're leaving where we think we are; a late "leave home" after
            # "arrive work" must not wipe the newer place.
            await self.set("place", None, source=source, at=stamp)
            await self.set("place_since", stamp, source=source, at=stamp)
        await self.set("last_left", {"place": place, "at": stamp}, source=source, at=stamp)
        return PlaceEvent("leave", place, stamp, previous)

    async def _clear_pending(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        if await self.get("pending_leave") is not None:
            await self._store.delete(PREFIX + "pending_leave")

    def _arm_pending(self, delay: float) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
        loop = asyncio.get_running_loop()
        self._pending_timer = loop.call_later(max(0.0, delay), self._spawn_commit)

    def _spawn_commit(self) -> None:
        task = asyncio.get_running_loop().create_task(self._commit_pending())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _commit_pending(self) -> None:
        async with self._lock:
            self._pending_timer = None
            pending = await self.get("pending_leave")
            if not isinstance(pending, dict):
                return
            event = await self._commit_leave(str(pending.get("place")), float(pending.get("at", time.time())),
                                             str(pending.get("source", "api")))
        if event is not None:
            log.info("committed held-back leave of %s", event.place)
            await self._emit(event)

    async def _log(self, key: str, value: Any, *, source: str, at: float) -> None:
        encoded = json.dumps(value, sort_keys=True)
        await self._store.run(lambda db: db.execute(
            "INSERT INTO context_log (at, key, value, source) VALUES (?, ?, ?, ?)", (at, key, encoded, source)))

    async def aclose(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
        for task in list(self._tasks):
            task.cancel()

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
