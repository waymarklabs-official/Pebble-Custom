"""SQLite persistence: delivery dedup, key/value state, event log and scheduled jobs.

One small file survives restarts, so a sleep timer set at 23:40 still fires after a reboot
and a recording the app re-delivers after a phone restart is not acted on twice.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    delivery_id TEXT,
    gesture TEXT,
    detail TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_at REAL NOT NULL,
    steps TEXT NOT NULL,
    vars TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    run_at: float
    steps: list[dict[str, Any]]
    vars: dict[str, Any]


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn(self._db)

        return await asyncio.to_thread(locked)

    # ---------------------------------------------------------------- dedup

    async def claim_delivery(self, delivery_id: str, ttl_seconds: float) -> bool:
        """Atomically record a delivery id. Returns False if it was already seen."""
        now = time.time()

        def op(db: sqlite3.Connection) -> bool:
            db.execute("DELETE FROM deliveries WHERE seen_at < ?", (now - ttl_seconds,))
            cur = db.execute(
                "INSERT OR IGNORE INTO deliveries (id, seen_at) VALUES (?, ?)", (delivery_id, now)
            )
            return cur.rowcount == 1

        return await self._run(op)

    # ---------------------------------------------------------------- state

    async def get(self, key: str, default: Any = None) -> Any:
        def op(db: sqlite3.Connection) -> Any:
            row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

        return await self._run(op)

    async def set(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, default=str)
        await self._run(lambda db: db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, encoded)))

    async def delete(self, key: str) -> None:
        await self._run(lambda db: db.execute("DELETE FROM state WHERE key = ?", (key,)))

    async def all_state(self) -> dict[str, Any]:
        def op(db: sqlite3.Connection) -> dict[str, Any]:
            return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM state")}

        return await self._run(op)

    # ---------------------------------------------------------------- event log

    async def log_event(self, *, delivery_id: str | None, gesture: str | None, detail: str,
                        status: str, error: str | None = None, keep: int = 2000) -> None:
        def op(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT INTO events (at, delivery_id, gesture, detail, status, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), delivery_id, gesture, detail, status, error),
            )
            db.execute("DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?", (keep,))

        await self._run(op)

    async def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        def op(db: sqlite3.Connection) -> list[dict[str, Any]]:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT at, delivery_id, gesture, detail, status, error FROM events "
                    "ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                db.row_factory = None

        return await self._run(op)

    # ---------------------------------------------------------------- jobs

    async def put_job(self, job: Job) -> None:
        await self._run(lambda db: db.execute(
            "INSERT INTO jobs (id, run_at, steps, vars) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET run_at=excluded.run_at, steps=excluded.steps, "
            "vars=excluded.vars",
            (job.id, job.run_at, json.dumps(job.steps), json.dumps(job.vars, default=str))))

    async def delete_job(self, job_id: str) -> bool:
        cur = await self._run(lambda db: db.execute("DELETE FROM jobs WHERE id = ?", (job_id,)))
        return cur.rowcount > 0

    async def jobs(self) -> list[Job]:
        def op(db: sqlite3.Connection) -> list[Job]:
            rows = db.execute("SELECT id, run_at, steps, vars FROM jobs ORDER BY run_at").fetchall()
            return [Job(r[0], r[1], json.loads(r[2]), json.loads(r[3])) for r in rows]

        return await self._run(op)
