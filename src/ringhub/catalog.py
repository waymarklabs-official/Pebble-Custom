"""User-editable catalog: places, skills and modes (stored as validated JSON docs in SQLite)."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .config import Hhmm, TimeWindow, Weekday, normalize_steps
from .models import Gesture, parse_gesture
from .store import Store

SLUG = r"^[a-z][a-z0-9_]{0,39}$"
FALLBACK_MODE = "everywhere"
BUILTIN_SKILLS = ("capture", "journal", "where_am_i", "nothing")

Slug = Annotated[str, Field(pattern=SLUG)]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "item"
    return ("x_" + s if s[0].isdigit() else s)[:40]


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Doc(_Part):
    KIND: ClassVar[str]
    id: str


# ----------------------------------------------------------------------------- places


class Place(_Doc):
    KIND: ClassVar[str] = "place"
    id: Slug
    name: str = Field(min_length=1, max_length=60)
    icon: str = Field("📍", max_length=8)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    """Spoken names, e.g. ["the office", "work"]. The name and id are always aliases too."""
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    radius_m: int = Field(150, ge=100, le=5000)
    """iOS geofences are unreliable below ~100 m and fire ~200 m past the edge."""

    @field_validator("aliases")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return sorted({a.strip().lower() for a in v if a.strip()})

    def spoken(self) -> set[str]:
        return {self.id.replace("_", " "), self.name.lower(), *self.aliases}


# ----------------------------------------------------------------------------- skills

Template = Literal["moment", "note", "list", "next", "recall", "notify", "shortcut", "shortcut_email",
                   "webhook", "car_smart", "car_command", "car_action", "car_status", "calendar_agenda", "tv",
                   "custom", "builtin"]


class Skill(_Doc):
    KIND: ClassVar[str] = "skill"
    id: Slug
    name: str = Field(min_length=1, max_length=60)
    icon: str = Field("✨", max_length=8)
    description: str = Field("", max_length=300)
    template: Template = "custom"
    params: dict[str, Any] = Field(default_factory=dict)
    """Template parameters (what the dashboard form edits)."""
    steps: Annotated[list[dict[str, Any]], BeforeValidator(normalize_steps)] = Field(default_factory=list)
    """Only for ``custom``; generated from ``params`` for every other template."""
    phrases: list[str] = Field(default_factory=list, max_length=20)
    """Voice triggers in pattern syntax: "warm up the car", "tell {who} i'm late"."""

    @field_validator("phrases")
    @classmethod
    def _phrases(cls, v: list[str]) -> list[str]:
        from .phrases import compile_phrase  # local import: phrases imports nothing from here

        out = []
        for p in v:
            p = p.strip()
            if p:
                compile_phrase(p)  # raises ValueError with a readable message
                out.append(p)
        return out

    @property
    def builtin(self) -> bool:
        return self.id in BUILTIN_SKILLS


# ----------------------------------------------------------------------------- modes

BindingKey = str  # a gesture value, or a combo like "double-click+click"


def _binding_key(k: str) -> str:
    parts = [parse_gesture(p).value for p in k.split("+")]
    if len(parts) > 1 and any(Gesture(p).is_voice for p in parts):
        raise ValueError("voice gestures cannot be part of a combo")
    return "+".join(parts)


class Recently(_Part):
    event: Literal["arrive", "leave"]
    place: Slug
    within_minutes: int = Field(15, ge=1, le=24 * 60)


class ModeWhen(_Part):
    places: list[Slug] = Field(default_factory=list)
    not_places: list[Slug] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)
    time: TimeWindow | None = None
    days: list[Weekday] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    recently: Recently | None = None

    @field_validator("focus")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [f.strip().lower() for f in v if f.strip()]

    def is_empty(self) -> bool:
        return not (self.places or self.not_places or self.focus or self.time or self.days
                    or self.context or self.recently)


class Mode(_Doc):
    KIND: ClassVar[str] = "mode"
    id: Slug
    name: str = Field(min_length=1, max_length=40)
    icon: str = Field("◎", max_length=8)
    color: str = Field("#6b7cff", pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True
    when: ModeWhen = Field(default_factory=ModeWhen)
    bindings: dict[str, Slug] = Field(default_factory=dict)
    """gesture (or combo) -> skill id. Unbound gestures fall through to the fallback mode."""
    feedback: Literal["normal", "quiet", "silent"] = "normal"

    @field_validator("bindings", mode="before")
    @classmethod
    def _keys(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        return {_binding_key(k): s for k, s in v.items() if s}

    @model_validator(mode="after")
    def _fallback_rules(self) -> Mode:
        if self.id == FALLBACK_MODE and not self.when.is_empty():
            raise ValueError("the Everywhere mode always applies; it cannot have conditions")
        if self.id == FALLBACK_MODE and not self.enabled:
            raise ValueError("the Everywhere mode cannot be disabled")
        return self


Doc = TypeVar("Doc", Place, Skill, Mode)

# Used by a few modules that need the TimeWindow/Hhmm types without importing config directly.
__all__ = ["FALLBACK_MODE", "Catalog", "Hhmm", "Mode", "ModeWhen", "Place", "Recently", "Skill", "slugify"]


class CatalogError(ValueError):
    pass


class Catalog:
    """CRUD over the catalog table with an in-memory cache (the engine reads it per event)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._cache: dict[str, list[Any]] | None = None
        self.version = 0
        """Bumped on every write; lets dependants (phrase index, combos) rebuild lazily."""

    async def _load(self) -> dict[str, list[Any]]:
        if self._cache is None:
            rows = await self._store.run(lambda db: db.execute(
                "SELECT kind, doc FROM catalog ORDER BY kind, position, id").fetchall())
            cache: dict[str, list[Any]] = {"place": [], "skill": [], "mode": []}
            models: dict[str, type[_Doc]] = {"place": Place, "skill": Skill, "mode": Mode}
            for kind, doc in rows:
                if kind in models:
                    cache[kind].append(models[kind].model_validate(json.loads(doc)))
            self._cache = cache
        return self._cache

    async def places(self) -> list[Place]:
        return list((await self._load())["place"])

    async def skills(self) -> list[Skill]:
        return list((await self._load())["skill"])

    async def modes(self) -> list[Mode]:
        """Modes in priority order; the fallback mode is always last."""
        modes = list((await self._load())["mode"])
        return sorted(modes, key=lambda m: m.id == FALLBACK_MODE)

    async def skill(self, skill_id: str) -> Skill | None:
        return next((s for s in await self.skills() if s.id == skill_id), None)

    async def mode(self, mode_id: str) -> Mode | None:
        return next((m for m in await self.modes() if m.id == mode_id), None)

    async def place(self, place_id: str) -> Place | None:
        return next((p for p in await self.places() if p.id == place_id), None)

    async def is_empty(self) -> bool:
        data = await self._load()
        return not any(data.values())

    async def put(self, doc: Place | Skill | Mode, *, position: int | None = None) -> None:
        kind = doc.KIND
        encoded = doc.model_dump_json()

        def op(db: sqlite3.Connection) -> None:
            pos = position
            if pos is None:
                row = db.execute("SELECT position FROM catalog WHERE kind = ? AND id = ?", (kind, doc.id)).fetchone()
                if row:
                    pos = row[0]
                else:
                    pos = (db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM catalog WHERE kind = ?",
                                      (kind,)).fetchone()[0])
            db.execute(
                "INSERT INTO catalog (kind, id, position, doc, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET doc = excluded.doc, position = excluded.position, "
                "updated_at = excluded.updated_at", (kind, doc.id, pos, encoded, time.time()))

        await self._store.run(op)
        self._invalidate()

    async def delete(self, kind: str, doc_id: str) -> bool:
        cur = await self._store.run(lambda db: db.execute(
            "DELETE FROM catalog WHERE kind = ? AND id = ?", (kind, doc_id)))
        self._invalidate()
        return bool(cur.rowcount)

    async def reorder(self, kind: str, ids: Iterable[str]) -> None:
        order = list(ids)

        def op(db: sqlite3.Connection) -> None:
            existing = {r[0] for r in db.execute("SELECT id FROM catalog WHERE kind = ?", (kind,))}
            if len(order) != len(set(order)) or set(order) != existing:
                raise CatalogError("reorder must list every existing id exactly once")
            db.execute("BEGIN")
            try:
                for pos, doc_id in enumerate(order):
                    db.execute("UPDATE catalog SET position = ? WHERE kind = ? AND id = ?", (pos, kind, doc_id))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        await self._store.run(op)
        self._invalidate()

    async def export(self) -> dict[str, Any]:
        data = await self._load()
        return {kind: [d.model_dump(mode="json") for d in docs] for kind, docs in data.items()}

    async def replace_all(self, data: dict[str, Any]) -> None:
        """Import a full export atomically (validates everything first)."""
        models: dict[str, type[_Doc]] = {"place": Place, "skill": Skill, "mode": Mode}
        parsed = {kind: [models[kind].model_validate(d) for d in data.get(kind, [])] for kind in models}
        if not any(m.id == FALLBACK_MODE for m in parsed["mode"]):
            raise CatalogError("an import must contain the 'everywhere' mode")
        for kind, docs in parsed.items():
            ids = [d.id for d in docs]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise CatalogError(f"duplicate {kind} ids: {', '.join(dupes)}")
        skill_ids = {s.id for s in parsed["skill"]}
        missing_builtins = [b for b in BUILTIN_SKILLS if b not in skill_ids]
        if missing_builtins:
            raise CatalogError(f"an import must keep the built-in skills: {', '.join(missing_builtins)}")
        for mode in parsed["mode"]:
            dangling = sorted(set(mode.bindings.values()) - skill_ids)  # type: ignore[attr-defined]
            if dangling:
                raise CatalogError(f"mode {mode.id!r} uses unknown skills: {', '.join(dangling)}")
        now = time.time()

        def op(db: sqlite3.Connection) -> None:
            db.execute("BEGIN")
            try:
                db.execute("DELETE FROM catalog")
                for kind, docs in parsed.items():
                    for pos, d in enumerate(docs):
                        db.execute("INSERT INTO catalog (kind, id, position, doc, updated_at) VALUES (?, ?, ?, ?, ?)",
                                   (kind, d.id, pos, d.model_dump_json(), now))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        await self._store.run(op)
        self._invalidate()

    def _invalidate(self) -> None:
        self._cache = None
        self.version += 1
