"""SQLite persistence: dedup, state, event log, jobs, memory, context and the editable catalog.

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

# Append-only: each entry upgrades the database by one version (PRAGMA user_version).
_MIGRATIONS: list[str] = [
    # v1: core
    """
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
    """,
    # v2: memory (notes + full-text search, reminders), context timeline, editable catalog
    """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        place TEXT,
        mode TEXT,
        audio TEXT,
        source TEXT,
        meta TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX notes_created ON notes(created_at);
    CREATE VIRTUAL TABLE notes_fts USING fts5(
        text, content='notes', content_rowid='id', tokenize='porter unicode61'
    );
    CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
    CREATE TRIGGER notes_au AFTER UPDATE OF text ON notes BEGIN
        INSERT INTO notes_fts(notes_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO notes_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TABLE reminders (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        text TEXT NOT NULL,
        trigger TEXT NOT NULL,
        due_at REAL,
        place TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        fired_at REAL,
        done_at REAL,
        snoozes INTEGER NOT NULL DEFAULT 0,
        note_id INTEGER
    );
    CREATE INDEX reminders_open ON reminders(status, trigger, place);
    CREATE TABLE context_log (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        source TEXT
    );
    CREATE INDEX context_log_key_at ON context_log(key, at);
    CREATE TABLE catalog (
        kind TEXT NOT NULL,
        id TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        doc TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (kind, id)
    );
    """,
]


def _migrate(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    for target, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        db.execute("BEGIN")
        try:
            for statement in _split_sql(script):
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {target}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _split_sql(script: str) -> list[str]:
    """Split on ';' outside of trigger bodies (BEGIN ... END)."""
    out, buf, depth = [], [], 0
    for line in script.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TRIGGER"):
            depth += 1
        buf.append(line)
        if depth and stripped == "END;":
            depth -= 1
        if not depth and stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";")
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


SCHEMA_VERSION = len(_MIGRATIONS)


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
        _migrate(self._db)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(connection)`` on a worker thread, serialized with all other DB access."""
        return await self._run(fn)

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
