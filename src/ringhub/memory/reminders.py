"""Reminder records. Delivery (timers, place triggers, pushes) lives in ``memory.service``."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..store import Store

Status = Literal["pending", "fired", "done", "cancelled"]
TriggerKind = Literal["time", "arrive", "leave", "none"]


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    created_at: float
    text: str
    trigger: str
    due_at: float | None
    place: str | None
    status: str
    fired_at: float | None
    done_at: float | None
    snoozes: int
    note_id: int | None

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "fired")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "created_at": self.created_at, "text": self.text, "trigger": self.trigger,
            "due_at": self.due_at, "place": self.place, "status": self.status, "fired_at": self.fired_at,
            "done_at": self.done_at, "snoozes": self.snoozes, "note_id": self.note_id,
        }


_COLS = "id, created_at, text, trigger, due_at, place, status, fired_at, done_at, snoozes, note_id"


def _row(r: tuple[Any, ...]) -> Reminder:
    return Reminder(*r)


class Reminders:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def add(self, text: str, trigger: TriggerKind, *, due_at: float | None = None, place: str | None = None,
                  note_id: int | None = None, created_at: float | None = None) -> Reminder:
        rid = secrets.token_hex(6)
        created = created_at if created_at is not None else time.time()

        def op(db: sqlite3.Connection) -> Reminder:
            db.execute(
                f"INSERT INTO reminders ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, ?)",
                (rid, created, text, trigger, due_at, place, note_id),
            )
            return _row(db.execute(f"SELECT {_COLS} FROM reminders WHERE id = ?", (rid,)).fetchone())

        return await self._store.run(op)

    async def get(self, rid: str) -> Reminder | None:
        row = await self._store.run(
            lambda db: db.execute(f"SELECT {_COLS} FROM reminders WHERE id = ?", (rid,)).fetchone())
        return _row(row) if row else None

    async def open(self, *, trigger: str | None = None, place: str | None = None) -> list[Reminder]:
        where: list[str] = ["status IN ('pending', 'fired')"]
        args: list[Any] = []
        if trigger:
            where.append("trigger = ?")
            args.append(trigger)
        if place:
            where.append("place = ?")
            args.append(place)
        sql = (f"SELECT {_COLS} FROM reminders WHERE {' AND '.join(where)} "
               "ORDER BY COALESCE(due_at, created_at)")
        return [_row(r) for r in await self._store.run(lambda db: db.execute(sql, args).fetchall())]

    async def pending_place(self, trigger: Literal["arrive", "leave"], place: str) -> list[Reminder]:
        sql = (f"SELECT {_COLS} FROM reminders WHERE status = 'pending' AND trigger = ? AND place = ? "
               "ORDER BY created_at")
        return [_row(r) for r in await self._store.run(lambda db: db.execute(sql, (trigger, place)).fetchall())]

    async def pending_timed(self) -> list[Reminder]:
        sql = f"SELECT {_COLS} FROM reminders WHERE status = 'pending' AND trigger = 'time' ORDER BY due_at"
        return [_row(r) for r in await self._store.run(lambda db: db.execute(sql).fetchall())]

    async def recent(self, limit: int = 50) -> list[Reminder]:
        sql = f"SELECT {_COLS} FROM reminders ORDER BY created_at DESC LIMIT ?"
        return [_row(r) for r in await self._store.run(lambda db: db.execute(sql, (limit,)).fetchall())]

    async def last_fired(self, within_seconds: float, now: float | None = None) -> Reminder | None:
        """The most recently fired, still-unacknowledged reminder (for ring acknowledgement)."""
        cutoff = (now if now is not None else time.time()) - within_seconds
        row = await self._store.run(lambda db: db.execute(
            f"SELECT {_COLS} FROM reminders WHERE status = 'fired' AND fired_at >= ? "
            "ORDER BY fired_at DESC LIMIT 1", (cutoff,)).fetchone())
        return _row(row) if row else None

    async def _transition(self, rid: str, sql: str, args: tuple[Any, ...], allowed: tuple[str, ...]) -> Reminder | None:
        def op(db: sqlite3.Connection) -> Reminder | None:
            placeholders = ",".join("?" * len(allowed))
            cur = db.execute(f"{sql} WHERE id = ? AND status IN ({placeholders})", (*args, rid, *allowed))
            if not cur.rowcount:
                return None
            return _row(db.execute(f"SELECT {_COLS} FROM reminders WHERE id = ?", (rid,)).fetchone())

        return await self._store.run(op)

    async def mark_fired(self, rid: str, at: float | None = None) -> Reminder | None:
        """pending -> fired. Returns None if it was not pending (already fired/done/cancelled),
        which makes concurrent triggers (timer + restart restore) idempotent."""
        return await self._transition(rid, "UPDATE reminders SET status = 'fired', fired_at = ?",
                                      (at or time.time(),), ("pending",))

    async def mark_done(self, rid: str) -> Reminder | None:
        return await self._transition(rid, "UPDATE reminders SET status = 'done', done_at = ?",
                                      (time.time(),), ("pending", "fired"))

    async def cancel(self, rid: str) -> Reminder | None:
        return await self._transition(rid, "UPDATE reminders SET status = 'cancelled'", (), ("pending", "fired"))

    async def snooze(self, rid: str, due_at: float) -> Reminder | None:
        """Re-arm as a timed reminder, whatever its original trigger was."""
        return await self._transition(
            rid, "UPDATE reminders SET status = 'pending', trigger = 'time', due_at = ?, snoozes = snoozes + 1",
            (due_at,), ("pending", "fired"))
