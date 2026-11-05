"""Store migrations, notes full-text search and the reminder state machine."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ringhub import store as store_mod
from ringhub.memory.notes import Notes, fts_query
from ringhub.memory.reminders import Reminders
from ringhub.store import SCHEMA_VERSION, Job, Store

# The exact schema the v1 release created (executescript, no PRAGMA user_version).
V1_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, delivery_id TEXT, gesture TEXT,
    detail TEXT, status TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, run_at REAL NOT NULL, steps TEXT NOT NULL, vars TEXT NOT NULL);
"""


def _version(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return int(db.execute("PRAGMA user_version").fetchone()[0])
    finally:
        db.close()


def _tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
    finally:
        db.close()


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "db.sqlite3")
    yield s
    s.close()


# ----------------------------------------------------------------------------- migrations


def test_fresh_database_is_migrated_to_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    Store(path).close()
    assert SCHEMA_VERSION == 2
    assert _version(path) == SCHEMA_VERSION
    assert {"deliveries", "state", "events", "jobs", "notes", "notes_fts", "reminders", "context_log",
            "catalog", "notes_ai", "notes_ad", "notes_au"} <= _tables(path)


async def test_reopen_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    s = Store(path)
    await s.set("k", {"v": 1})
    await Notes(s).add("first note", created_at=1.0)
    s.close()
    for _ in range(2):  # re-running migrations must not fail ("table already exists") or wipe data
        s = Store(path)
        assert await s.get("k") == {"v": 1}
        assert [n.text for n in await Notes(s).search("first")] == ["first note"]
        s.close()
    assert _version(path) == SCHEMA_VERSION


async def test_v1_database_upgrades_in_place_keeping_state_and_jobs(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    db.execute("INSERT INTO state (key, value) VALUES ('sleep_timer', '\"23:40\"')")
    db.execute("INSERT INTO jobs (id, run_at, steps, vars) VALUES ('sleep', 123.0, '[{\"noop\": {}}]', '{\"a\": 1}')")
    db.execute("INSERT INTO deliveries (id, seen_at) VALUES ('d1', 1.0)")
    db.commit()
    db.close()
    assert _version(path) == 0

    s = Store(path)
    try:
        assert await s.get("sleep_timer") == "23:40"
        assert await s.jobs() == [Job("sleep", 123.0, [{"noop": {}}], {"a": 1})]
        # v2 tables work on the upgraded file
        note = await Notes(s).add("hello upgraded world", created_at=2.0)
        assert [n.id for n in await Notes(s).search("upgraded")] == [note.id]
        r = await Reminders(s).add("buy milk", "none")
        assert (await Reminders(s).get(r.id)) == r
        assert await s.claim_delivery("d1", 10**12) is False  # dedup rows survived too
    finally:
        s.close()
    assert _version(path) == SCHEMA_VERSION


def test_failed_migration_rolls_back_and_keeps_previous_version(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "db.sqlite3"
    Store(path).close()
    broken = "CREATE TABLE half_done (x INTEGER);\nCREATE TABLE notes (id INTEGER);\n"  # 2nd stmt fails
    monkeypatch.setattr(store_mod, "_MIGRATIONS", [*store_mod._MIGRATIONS, broken])
    with pytest.raises(sqlite3.OperationalError):
        Store(path)
    assert _version(path) == SCHEMA_VERSION
    assert "half_done" not in _tables(path)


def test_split_sql_keeps_trigger_bodies_whole() -> None:
    script = """
    CREATE TABLE a (x);
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
        INSERT INTO a VALUES (1);
        INSERT INTO a VALUES (2);
    END;
    CREATE TABLE b (y);
    """
    parts = store_mod._split_sql(script)
    assert len(parts) == 3
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].count("VALUES") == 2
    assert parts[1].rstrip().endswith("END")


# ----------------------------------------------------------------------------- state & events


async def test_state_roundtrip_and_event_log_trim(store: Store) -> None:
    await store.set("a", [1, 2])
    await store.set("a", {"x": True})
    assert await store.get("a") == {"x": True}
    assert await store.get("missing", 7) == 7
    await store.delete("a")
    assert await store.get("a") is None
    for i in range(5):
        await store.log_event(delivery_id=f"d{i}", gesture="click", detail=str(i), status="ok", keep=3)
    recent = await store.recent_events(10)
    assert [e["detail"] for e in recent] == ["4", "3", "2"]


async def test_claim_delivery_is_once_only(store: Store) -> None:
    assert await store.claim_delivery("x", 3600) is True
    assert await store.claim_delivery("x", 3600) is False
    assert await store.claim_delivery("y", 3600) is True


# ----------------------------------------------------------------------------- notes / FTS


@pytest.fixture
async def notes(store: Store) -> Notes:
    n = Notes(store)
    base = time.time()
    await n.add("Dentist appointment moved to Friday at 3", created_at=base - 50)
    await n.add("Running shoes are in the garage", created_at=base - 40)
    await n.add("Café near the station has great croissants", created_at=base - 30)
    await n.add("Parked on level 3", kind="log", created_at=base - 20)
    await n.add("Idea: a NEAR-field reader (with AND gates) for the door", created_at=base - 10)
    return n


@pytest.mark.parametrize("query,expected", [
    ("dentists", "Dentist appointment"),       # porter stemming: plural finds singular
    ("dent", "Dentist appointment"),           # prefix match
    ("ran", None),                             # stemming is not lemmatization
    ("running", "Running shoes"),
    ("run", "Running shoes"),
    ("RUNNING", "Running shoes"),              # case-insensitive
    ("cafe", "Café near"),                     # diacritics folded (unicode61)
    ("café", "Café near"),
    ("croissant", "Café near"),
    ("what did I say about the garage", "Running shoes"),  # stop words dropped
])
async def test_search_stemming_prefix_and_unicode(notes: Notes, query: str, expected: str | None) -> None:
    hits = await notes.search(query)
    if expected is None:
        assert hits == []
    else:
        assert hits and hits[0].text.startswith(expected)


@pytest.mark.parametrize("hostile", [
    '"', '""', '"dentist', 'dentist"', "dentist*", "*", "NEAR(dentist garage)", "dentist NEAR garage",
    "dentist AND", "OR garage", "NOT dentist", "dentist OR", "(dentist", "dentist)", "text:dentist",
    "^dentist", "dentist -garage", "{dentist}", "''", "__", "'", "\\", "dentist; DROP TABLE notes;--",
    "ü🙂 ünïcödé", "\u0000", "﻿dentist", "a" * 5000,
])
async def test_fts_query_cannot_inject_syntax(notes: Notes, hostile: str) -> None:
    query = fts_query(hostile)
    if query is not None:
        # Only quoted, prefix-matched terms joined by OR: no bare operators survive.
        for part in query.split(" OR "):
            assert part.startswith('"') and part.endswith('"*') and part.count('"') == 2
    await notes.search(hostile)  # must never raise sqlite3.OperationalError (fts5 syntax error)
    assert len(await notes.recent(100)) == 5  # and certainly never modify anything


async def test_fts_operators_are_searched_as_words(notes: Notes) -> None:
    hits = await notes.search("near field reader")
    assert [h.text[:4] for h in hits][0] == "Idea"
    # "and"/"or"/"not" are stop words or plain terms, never boolean operators
    assert fts_query("dentist AND garage") == '"dentist"* OR "garage"*'
    assert fts_query("NOT dentist") == '"not"* OR "dentist"*'


def test_fts_query_drops_stop_words_and_caps_terms() -> None:
    assert fts_query("what did I say about the dentist") == '"dentist"*'
    assert fts_query("the a I of") is None
    assert fts_query("") is None
    many = " ".join(f"word{i}" for i in range(20))
    assert (fts_query(many) or "").count(" OR ") == 7  # at most 8 terms


async def test_update_and_delete_keep_fts_index_in_sync(notes: Notes) -> None:
    (hit,) = await notes.search("garage")
    assert await notes.update_text(hit.id, "Running shoes moved to the attic")
    assert await notes.search("garage") == []
    assert [n.id for n in await notes.search("attic")] == [hit.id]
    assert (await notes.get(hit.id)).text == "Running shoes moved to the attic"  # type: ignore[union-attr]
    assert await notes.delete(hit.id)
    assert await notes.search("attic") == []
    assert await notes.get(hit.id) is None
    assert await notes.update_text(hit.id, "x") is False
    assert await notes.delete(hit.id) is False


async def test_recent_filters_and_order(notes: Notes) -> None:
    newest = await notes.recent(2)
    assert [n.text[:4] for n in newest] == ["Idea", "Park"]
    assert [n.kind for n in await notes.recent(10, kinds=["log"])] == ["log"]
    cutoff = newest[1].created_at
    assert len(await notes.recent(10, since=cutoff)) == 2


async def test_latest_matching_escapes_like_wildcards(store: Store) -> None:
    n = Notes(store)
    await n.add("Parked at 50% off lot", kind="log", created_at=1.0)
    await n.add("Took meds", kind="log", created_at=2.0)
    assert (await n.latest_matching("log", "50%")).text.startswith("Parked")  # type: ignore[union-attr]
    assert await n.latest_matching("log", "5_%") is None   # '_' is literal, not "any char"
    assert await n.latest_matching("log", "%") is not None and (await n.latest_matching("log", "%%")) is None
    assert await n.latest_matching("note", "Parked") is None


async def test_note_roundtrip_keeps_provenance(store: Store) -> None:
    n = Notes(store)
    note = await n.add("hi", kind="journal", created_at=5.0, place="home", mode="sleep", audio="/a.m4a",
                       source="dlv-1", meta={"location": {"lat": 1.0, "lon": 2.0}})
    got = await n.get(note.id)
    assert got == note
    assert got.as_dict() == {"id": note.id, "created_at": 5.0, "kind": "journal", "text": "hi", "place": "home",
                             "mode": "sleep", "audio": "/a.m4a", "meta": {"location": {"lat": 1.0, "lon": 2.0}}}


# ----------------------------------------------------------------------------- reminders


@pytest.fixture
async def reminders(store: Store) -> Reminders:
    return Reminders(store)


async def test_reminder_lifecycle_pending_fired_done(reminders: Reminders) -> None:
    r = await reminders.add("call mom", "time", due_at=time.time() + 60)
    assert r.status == "pending" and r.is_open and r.snoozes == 0
    fired = await reminders.mark_fired(r.id, at=1000.0)
    assert fired is not None and fired.status == "fired" and fired.fired_at == 1000.0
    assert await reminders.mark_fired(r.id) is None  # idempotent: a double fire does nothing
    done = await reminders.mark_done(r.id)
    assert done is not None and done.status == "done" and done.done_at and not done.is_open
    # every transition out of a terminal state is refused
    assert await reminders.mark_done(r.id) is None
    assert await reminders.mark_fired(r.id) is None
    assert await reminders.cancel(r.id) is None
    assert await reminders.snooze(r.id, time.time() + 600) is None
    assert (await reminders.get(r.id)).status == "done"  # type: ignore[union-attr]


async def test_cancelled_is_terminal(reminders: Reminders) -> None:
    r = await reminders.add("x", "none")
    assert (await reminders.cancel(r.id)).status == "cancelled"  # type: ignore[union-attr]
    for op in (reminders.mark_done(r.id), reminders.mark_fired(r.id), reminders.cancel(r.id),
               reminders.snooze(r.id, 1.0)):
        assert await op is None


async def test_unknown_reminder_transitions_return_none(reminders: Reminders) -> None:
    assert await reminders.get("deadbeef0000") is None
    assert await reminders.mark_fired("deadbeef0000") is None
    assert await reminders.mark_done("deadbeef0000") is None


async def test_snooze_rearms_place_reminder_as_timed(reminders: Reminders) -> None:
    r = await reminders.add("take chicken out", "arrive", place="home")
    await reminders.mark_fired(r.id)
    s = await reminders.snooze(r.id, 5000.0)
    assert s is not None
    assert (s.status, s.trigger, s.due_at, s.snoozes, s.place) == ("pending", "time", 5000.0, 1, "home")
    s2 = await reminders.snooze(r.id, 6000.0)  # snoozing a pending reminder is allowed too
    assert s2 is not None and s2.snoozes == 2 and s2.due_at == 6000.0
    assert [x.id for x in await reminders.pending_timed()] == [r.id]
    assert await reminders.pending_place("arrive", "home") == []


async def test_last_fired_window(reminders: Reminders) -> None:
    now = 10_000.0
    old = await reminders.add("old", "none")
    new = await reminders.add("new", "none")
    await reminders.mark_fired(old.id, at=now - 300)
    assert await reminders.last_fired(120, now=now) is None          # outside the window
    await reminders.mark_fired(new.id, at=now - 60)
    assert (await reminders.last_fired(120, now=now)).id == new.id    # type: ignore[union-attr]
    assert (await reminders.last_fired(600, now=now)).id == new.id    # type: ignore[union-attr] # newest wins
    await reminders.mark_done(new.id)
    assert await reminders.last_fired(120, now=now) is None          # acknowledged ones don't count


async def test_open_and_pending_queries(reminders: Reminders) -> None:
    t = time.time()
    a = await reminders.add("later", "time", due_at=t + 500)
    b = await reminders.add("sooner", "time", due_at=t + 100)
    c = await reminders.add("at home", "arrive", place="home")
    d = await reminders.add("leaving work", "leave", place="work")
    e = await reminders.add("list item", "none")
    await reminders.mark_fired(c.id)
    await reminders.cancel(e.id)
    assert {r.id for r in await reminders.open()} == {a.id, b.id, c.id, d.id}
    assert [r.id for r in await reminders.open(trigger="time")] == [b.id, a.id]  # by due time
    assert [r.id for r in await reminders.open(place="work")] == [d.id]
    assert await reminders.pending_place("arrive", "home") == []  # already fired
    assert [r.id for r in await reminders.pending_place("leave", "work")] == [d.id]
    assert [r.id for r in await reminders.pending_timed()] == [b.id, a.id]
    assert len(await reminders.recent(10)) == 5
