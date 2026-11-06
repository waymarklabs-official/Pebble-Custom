"""Notes: everything said into the ring, searchable, stamped with when and where it was said."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from ..store import Store

NoteKind = Literal["note", "journal", "moment", "log", "reminder"]


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    created_at: float
    kind: str
    text: str
    place: str | None = None
    mode: str | None = None
    audio: str | None = None
    source: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "created_at": self.created_at, "kind": self.kind, "text": self.text,
            "place": self.place, "mode": self.mode, "audio": self.audio, "meta": self.meta,
        }


_COLS = "id, created_at, kind, text, place, mode, audio, source, meta"


def _row(r: tuple[Any, ...]) -> Note:
    return Note(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], json.loads(r[8] or "{}"))


# Irregular past tenses the porter stemmer can't connect ("did I take my meds" vs "Took meds ✓").
_IRREGULAR = {
    "take": "took", "eat": "ate", "go": "went", "feed": "fed", "give": "gave", "pay": "paid", "buy": "bought",
    "bring": "brought", "drink": "drank", "send": "sent", "get": "got", "make": "made", "write": "wrote",
    "sleep": "slept", "put": "put", "leave": "left", "tell": "told", "find": "found", "drive": "drove",
    "run": "ran", "swim": "swam", "throw": "threw", "wear": "wore", "hide": "hid", "lose": "lost",
}
_IRREGULAR_BACK = {v: k for k, v in _IRREGULAR.items()}


def fts_query(text: str, *, all_terms: bool = False) -> str | None:
    """Build a safe FTS5 query from free speech: quoted terms, prefix-matched, OR'ed.

    "what did I say about the dentist" -> "dentist"*  (stop words dropped). Quoting every
    term means user input can never inject FTS operators.
    """
    stop = {
        "a", "an", "the", "i", "me", "my", "about", "what", "did", "do", "say", "said", "was", "is",
        "of", "to", "for", "and", "or", "on", "in", "at", "it", "that", "this", "any", "find", "notes",
        "note", "tell", "search", "remember", "remind", "something", "anything", "with",
    }
    terms = [t.replace('"', "") for t in re.findall(r"[\w']+", text.lower()) if t not in stop and len(t) > 1]
    if not terms:
        return None
    groups = []
    for t in dict.fromkeys(terms[:8]):
        alts = sorted({t, *([_IRREGULAR[t]] if t in _IRREGULAR else []),
                       *([_IRREGULAR_BACK[t]] if t in _IRREGULAR_BACK else [])})
        group = " OR ".join(f'"{a}"*' for a in alts)
        groups.append(f"({group})" if len(alts) > 1 else group)
    return (" AND " if all_terms else " OR ").join(groups)


class Notes:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def add(self, text: str, *, kind: NoteKind = "note", created_at: float, place: str | None = None,
                  mode: str | None = None, audio: str | None = None, source: str | None = None,
                  meta: dict[str, Any] | None = None) -> Note:
        def op(db: sqlite3.Connection) -> Note:
            cur = db.execute(
                f"INSERT INTO notes ({_COLS.replace('id, ', '', 1)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (created_at, kind, text, place, mode, audio, source, json.dumps(meta or {})),
            )
            row = db.execute(f"SELECT {_COLS} FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _row(row)

        return await self._store.run(op)

    async def get(self, note_id: int) -> Note | None:
        row = await self._store.run(
            lambda db: db.execute(f"SELECT {_COLS} FROM notes WHERE id = ?", (note_id,)).fetchone())
        return _row(row) if row else None

    async def recent(self, limit: int = 20, *, kinds: list[str] | None = None,
                     since: float | None = None, place: str | None = None) -> list[Note]:
        where: list[str] = ["1=1"]
        args: list[Any] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if since is not None:
            where.append("created_at >= ?")
            args.append(since)
        if place is not None:
            where.append("place = ?")
            args.append(place)
        sql = f"SELECT {_COLS} FROM notes WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT ?"
        rows = await self._store.run(lambda db: db.execute(sql, (*args, limit)).fetchall())
        return [_row(r) for r in rows]

    async def search(self, text: str, limit: int = 10, *, since: float | None = None,
                     exclude_kinds: list[str] | None = None, all_terms: bool = False) -> list[Note]:
        """Full-text search. Filters apply *before* the limit, so old notes can't crowd out today's."""
        query = fts_query(text, all_terms=all_terms)
        if query is None:
            return []
        where: list[str] = ["notes_fts MATCH ?"]
        args: list[Any] = [query]
        if since is not None:
            where.append("n.created_at >= ?")
            args.append(since)
        if exclude_kinds:
            where.append(f"n.kind NOT IN ({','.join('?' * len(exclude_kinds))})")
            args.extend(exclude_kinds)
        sql = (f"SELECT {', '.join('n.' + c.strip() for c in _COLS.split(','))} FROM notes_fts f "
               f"JOIN notes n ON n.id = f.rowid WHERE {' AND '.join(where)} "
               "ORDER BY bm25(notes_fts), n.created_at DESC LIMIT ?")
        rows = await self._store.run(lambda db: db.execute(sql, (*args, limit)).fetchall())
        return [_row(r) for r in rows]

    async def latest_matching(self, kind: str, text_like: str) -> Note | None:
        """Most recent note of ``kind`` whose text contains ``text_like`` (case-insensitive)."""
        row = await self._store.run(lambda db: db.execute(
            f"SELECT {_COLS} FROM notes WHERE kind = ? AND text LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT 1",
            (kind, "%" + text_like.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
        ).fetchone())
        return _row(row) if row else None

    async def update_text(self, note_id: int, text: str) -> bool:
        cur = await self._store.run(lambda db: db.execute(
            "UPDATE notes SET text = ? WHERE id = ?", (text, note_id)))
        return bool(cur.rowcount)

    async def delete(self, note_id: int) -> bool:
        cur = await self._store.run(lambda db: db.execute("DELETE FROM notes WHERE id = ?", (note_id,)))
        return bool(cur.rowcount)
