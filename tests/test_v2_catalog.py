"""Catalog models and CRUD, skill templates, and the first-boot seed."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ringhub.catalog import FALLBACK_MODE, Catalog, CatalogError, Mode, Place, Skill, slugify
from ringhub.config import Config
from ringhub.executor import validate_skill_steps, validate_steps
from ringhub.phrases import compile_phrase
from ringhub.seed import default_catalog, seed_if_empty
from ringhub.store import Store
from ringhub.templates import TEMPLATES, TemplateError, compile_skill, describe, should_confirm

from .conftest import TESLA_CONFIG

# ----------------------------------------------------------------------------- models


@pytest.mark.parametrize("bad_id", ["", "Home", "1home", "home-2", "home 2", "a" * 41, "_x", "hôme"])
def test_ids_must_be_slugs(bad_id: str) -> None:
    for model, extra in ((Place, {}), (Skill, {}), (Mode, {})):
        with pytest.raises(ValidationError):
            model.model_validate({"id": bad_id, "name": "X", **extra})


def test_slugify() -> None:
    assert slugify("The Office!") == "the_office"
    assert slugify("2nd floor") == "x_2nd_floor"
    assert slugify("!!!") == "item"
    assert len(slugify("a" * 100)) == 40
    assert Place.model_validate({"id": slugify("Grandma's House"), "name": "x"}).id == "grandma_s_house"


def test_place_alias_normalization_and_spoken_names() -> None:
    p = Place.model_validate({"id": "the_office", "name": "Work HQ",
                              "aliases": [" The Office ", "office", "OFFICE", ""]})
    assert p.aliases == ["office", "the office"]
    assert p.spoken() == {"the office", "work hq", "office"}


@pytest.mark.parametrize("field,value", [("radius_m", 99), ("radius_m", 5001), ("lat", 91), ("lon", -181),
                                         ("name", ""), ("name", "x" * 61), ("aliases", ["a"] * 21)])
def test_place_bounds(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        Place.model_validate({"id": "p", "name": "P", field: value})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Place.model_validate({"id": "p", "name": "P", "radius": 100})
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "when": {"place": ["home"]}})


@pytest.mark.parametrize("raw,key", [
    ("click", "click"), ("single", "click"), ("double", "double-click"), ("Double Click", "double-click"),
    ("triple", "triple-click"), ("hold", "single-click-hold"), ("click-hold", "double-click-hold"),
    ("double_hold", "double-click-hold"), ("double+single", "double-click+click"),
    ("double-click+double-click+click", "double-click+double-click+click"),
])
def test_binding_keys_are_normalized(raw: str, key: str) -> None:
    m = Mode.model_validate({"id": "m", "name": "M", "bindings": {raw: "moment"}})
    assert m.bindings == {key: "moment"}


@pytest.mark.parametrize("raw", ["hold+click", "click+hold", "double+click-hold", "quadruple", "click+", ""])
def test_bad_binding_keys_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "bindings": {raw: "moment"}})


def test_empty_binding_values_are_dropped_and_targets_must_be_slugs() -> None:
    assert Mode.model_validate({"id": "m", "name": "M", "bindings": {"click": "", "double": None}}).bindings == {}
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "bindings": {"click": "Not A Slug"}})


def test_everywhere_rules() -> None:
    with pytest.raises(ValidationError, match="cannot have conditions"):
        Mode.model_validate({"id": FALLBACK_MODE, "name": "E", "when": {"places": ["home"]}})
    with pytest.raises(ValidationError, match="cannot be disabled"):
        Mode.model_validate({"id": FALLBACK_MODE, "name": "E", "enabled": False})
    assert Mode.model_validate({"id": FALLBACK_MODE, "name": "E"}).when.is_empty()
    # any other mode may be conditionless or disabled
    assert not Mode.model_validate({"id": "x", "name": "X", "enabled": False}).enabled


def test_mode_color_and_feedback_validated() -> None:
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "color": "red"})
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "feedback": "loud"})
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "when": {"recently": {"event": "visit", "place": "home"}}})
    with pytest.raises(ValidationError):
        Mode.model_validate({"id": "m", "name": "M", "when": {"time": {"after": "25:00", "before": "06:00"}}})


def test_skill_phrases_validated_and_trimmed() -> None:
    s = Skill.model_validate({"id": "s", "name": "S", "phrases": ["  warm up the car ", "", "tell {who} hi"]})
    assert s.phrases == ["warm up the car", "tell {who} hi"]
    with pytest.raises(ValidationError, match="malformed"):
        Skill.model_validate({"id": "s", "name": "S", "phrases": ["tell {who"]})
    with pytest.raises(ValidationError, match="twice"):
        Skill.model_validate({"id": "s", "name": "S", "phrases": ["{a} {a}"]})


def test_skill_steps_shape() -> None:
    s = Skill.model_validate({"id": "s", "name": "S", "steps": ["noop", {"notify": "hi"}]})
    assert s.steps == [{"noop": {}}, {"notify": "hi"}]
    with pytest.raises(ValidationError):
        Skill.model_validate({"id": "s", "name": "S", "steps": [{"a": 1, "b": 2}]})
    with pytest.raises(ValidationError):
        Skill.model_validate({"id": "s", "name": "S", "template": "nope"})


# ----------------------------------------------------------------------------- templates

GOOD_PARAMS: dict[str, dict[str, Any]] = {
    "moment": {"label": "Great idea"},
    "note": {"text": "Took meds ✓"},
    "list": {},
    "recall": {"count": 5},
    "notify": {"message": "✋ Hand raised", "topic": "hand-raise", "priority": 4, "tags": ["raised_hand"]},
    "webhook": {"url": "https://ha.local/api/webhook/x", "method": "PUT", "headers": {"X": "1"}, "body": {"a": 1}},
    "car_smart": {"unlocked": "frunk", "locked": "nothing"},
    "car_command": {"command": "flash_lights", "params": {}},
    "tv": {"command": "pause", "target": "bedroom"},
    "custom": {},
}


@pytest.mark.parametrize("template", sorted(GOOD_PARAMS))
def test_every_template_compiles_to_valid_steps(template: str, make_config: Callable[..., Config]) -> None:
    cfg = make_config(TESLA_CONFIG)
    skill = Skill.model_validate({"id": "s", "name": "S", "template": template, "params": GOOD_PARAMS[template],
                                  "steps": [{"noop": {}}] if template == "custom" else []})
    steps = compile_skill(skill)
    assert steps and all(isinstance(s, dict) and len(s) == 1 for s in steps)
    validate_skill_steps(steps, cfg, "test")


def test_template_outputs() -> None:
    def steps(template: str, params: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        return compile_skill(Skill.model_validate({"id": "s", "name": "S", "template": template,
                                                   "params": params, **kw}))

    assert steps("moment", {}) == [{"memory.moment": {"label": None}}]
    assert steps("note", {"text": "x"}) == [{"memory.log": {"text": "x"}}]
    assert steps("notify", {"message": "hi"}) == [{"notify": {"message": "hi", "priority": 3}}]
    assert steps("webhook", {"url": "http://x"}) == [{"http": {"url": "http://x", "method": "POST", "headers": {}}}]
    assert steps("car_smart", {}) == [{"car.smart": {"unlocked": "trunk", "locked": "precondition"}}]
    assert steps("car_command", {"command": "door_lock"}) == [{"tesla": {"command": "door_lock", "params": {}}}]
    assert steps("tv", {"command": "power_off"}) == [{"tv": {"command": "power_off", "target": "here"}}]
    assert steps("custom", {}, steps=[{"noop": {}}]) == [{"noop": {}}]
    for builtin, action in (("capture", "memory.capture"), ("journal", "memory.journal"),
                            ("where_am_i", "mode.where_am_i"), ("nothing", "noop")):
        assert steps("builtin", {}, id=builtin) == [{action: {}}]


@pytest.mark.parametrize("template,params", [
    ("note", {}), ("note", {"text": ""}), ("note", {"text": "x" * 121}),
    ("recall", {"count": 0}), ("recall", {"count": 11}),
    ("notify", {}), ("notify", {"message": "x", "priority": 6}), ("notify", {"message": "x", "topic": "bad topic"}),
    ("webhook", {"url": "ftp://x"}), ("webhook", {"url": "https://x", "method": "TRACE"}),
    ("car_smart", {"unlocked": "sunroof"}), ("car_command", {"command": "Rm -rf"}),
    ("tv", {"command": "explode"}), ("list", {"extra": 1}), ("moment", {"label": "x" * 81}),
])
def test_bad_template_params_rejected(template: str, params: dict[str, Any]) -> None:
    with pytest.raises(TemplateError):
        compile_skill(Skill.model_validate({"id": "s", "name": "S", "template": template, "params": params}))


def test_custom_needs_steps_and_builtin_needs_known_id() -> None:
    with pytest.raises(TemplateError, match="at least one step"):
        compile_skill(Skill.model_validate({"id": "s", "name": "S", "template": "custom"}))
    with pytest.raises(KeyError):
        compile_skill(Skill.model_validate({"id": "s", "name": "S", "template": "builtin"}))


def test_should_confirm() -> None:
    def skill(t: str) -> Skill:
        return Skill.model_validate({"id": "s", "name": "S", "template": t})

    assert {t for t in TEMPLATES if should_confirm(skill(t))} == {"notify", "webhook", "car_command", "tv", "custom"}


def test_describe_publishes_json_schemas() -> None:
    described = describe()
    ids = [d["id"] for d in described]
    assert "builtin" not in ids and set(ids) == set(TEMPLATES) - {"builtin"}
    by_id = {d["id"]: d for d in described}
    for d in described:
        assert d["schema"]["type"] == "object" and d["name"] and d["icon"] and d["description"]
        json.dumps(d)  # serializable for the dashboard
    assert by_id["note"]["schema"]["required"] == ["text"]
    assert by_id["notify"]["needs"] == ["ntfy"]
    assert by_id["car_smart"]["needs"] == ["tesla"] and by_id["tv"]["needs"] == ["devices"]
    assert set(by_id["car_smart"]["schema"]["properties"]["unlocked"]["enum"]) == {"trunk", "frunk", "nothing"}
    assert by_id["notify"]["schema"]["additionalProperties"] is False


# ----------------------------------------------------------------------------- catalog CRUD


@pytest.fixture
async def catalog(tmp_path: Path) -> Catalog:
    store = Store(tmp_path / "db.sqlite3")
    yield Catalog(store)
    store.close()


def P(pid: str, **kw: Any) -> Place:
    return Place.model_validate({"id": pid, "name": pid.title(), **kw})


def M(mid: str, **kw: Any) -> Mode:
    return Mode.model_validate({"id": mid, "name": mid.title(), **kw})


async def test_put_get_update_delete(catalog: Catalog) -> None:
    assert await catalog.is_empty()
    v0 = catalog.version
    await catalog.put(P("home"))
    await catalog.put(P("work"))
    assert catalog.version > v0
    assert [p.id for p in await catalog.places()] == ["home", "work"]
    await catalog.put(P("home", name="Casa"))  # update keeps its position
    assert [(p.id, p.name) for p in await catalog.places()] == [("home", "Casa"), ("work", "Work")]
    assert (await catalog.place("home")).name == "Casa"  # type: ignore[union-attr]
    assert await catalog.delete("place", "home") is True
    assert await catalog.delete("place", "home") is False
    assert await catalog.place("home") is None
    assert not await catalog.is_empty()


async def test_cache_is_invalidated_on_write(catalog: Catalog) -> None:
    await catalog.put(P("home"))
    first = await catalog.places()
    first.clear()  # callers get a copy, never the cache itself
    assert len(await catalog.places()) == 1
    await catalog.put(P("gym"))
    assert len(await catalog.places()) == 2


async def test_modes_keep_fallback_last(catalog: Catalog) -> None:
    await catalog.put(M(FALLBACK_MODE), position=0)
    await catalog.put(M("work"))
    await catalog.put(M("home"))
    assert [m.id for m in await catalog.modes()] == ["work", "home", FALLBACK_MODE]


async def test_reorder_must_list_every_id(catalog: Catalog) -> None:
    for mid in ("a", "b", "c"):
        await catalog.put(M(mid))
    await catalog.reorder("mode", ["c", "a", "b"])
    assert [m.id for m in await catalog.modes()] == ["c", "a", "b"]
    for bad in (["a", "b"], ["a", "b", "c", "d"], []):
        with pytest.raises(CatalogError):
            await catalog.reorder("mode", bad)
    assert [m.id for m in await catalog.modes()] == ["c", "a", "b"]  # untouched by failed attempts


async def test_reorder_rejects_duplicates(catalog: Catalog) -> None:
    for mid in ("a", "b", "c"):
        await catalog.put(M(mid))
    with pytest.raises(CatalogError):
        await catalog.reorder("mode", ["a", "a", "b", "c"])


async def test_export_replace_all_round_trip(tmp_path: Path, make_config: Callable[..., Config]) -> None:
    store = Store(tmp_path / "a.sqlite3")
    other = Store(tmp_path / "b.sqlite3")
    try:
        src, dst = Catalog(store), Catalog(other)
        await seed_if_empty(src, make_config(TESLA_CONFIG))
        exported = await src.export()
        json.dumps(exported)
        await dst.replace_all(json.loads(json.dumps(exported)))
        assert await dst.export() == exported
        assert [m.id for m in await dst.modes()] == [m.id for m in await src.modes()]
    finally:
        store.close()
        other.close()


async def test_failed_import_leaves_catalog_untouched(catalog: Catalog, make_config: Callable[..., Config]) -> None:
    await seed_if_empty(catalog, make_config())
    before = await catalog.export()
    version = catalog.version
    bad_skill = json.loads(json.dumps(before))
    bad_skill["skill"].append({"id": "Bad Id", "name": "x"})
    no_fallback = json.loads(json.dumps(before))
    no_fallback["mode"] = [m for m in no_fallback["mode"] if m["id"] != FALLBACK_MODE]
    bad_mode = json.loads(json.dumps(before))
    bad_mode["mode"][0]["bindings"] = {"hold+click": "moment"}
    with pytest.raises(ValidationError):
        await catalog.replace_all(bad_skill)
    with pytest.raises(CatalogError, match="everywhere"):
        await catalog.replace_all(no_fallback)
    with pytest.raises(ValidationError):
        await catalog.replace_all(bad_mode)
    assert catalog.version == version
    assert await catalog.export() == before


async def test_failed_import_inside_the_transaction_rolls_back(catalog: Catalog,
                                                               make_config: Callable[..., Config]) -> None:
    await seed_if_empty(catalog, make_config())
    before = await catalog.export()
    dup = json.loads(json.dumps(before))
    dup["place"].append(dict(dup["place"][0]))  # duplicate primary key: fails mid-transaction
    with pytest.raises(Exception):  # noqa: B017 - the type is asserted at the API layer (see test_v2_api)
        await catalog.replace_all(dup)
    assert await catalog.export() == before


async def test_import_rejects_dangling_bindings(catalog: Catalog, make_config: Callable[..., Config]) -> None:
    await seed_if_empty(catalog, make_config())
    data = await catalog.export()
    data["skill"] = [s for s in data["skill"] if s["id"] != "capture"]
    with pytest.raises((CatalogError, ValidationError)):
        await catalog.replace_all(data)


# ----------------------------------------------------------------------------- seed


@pytest.mark.parametrize("with_tesla", [False, True], ids=["no-tesla", "tesla"])
def test_seed_is_internally_consistent(with_tesla: bool, make_config: Callable[..., Config]) -> None:
    cfg = make_config(TESLA_CONFIG if with_tesla else None)
    data = default_catalog(cfg)
    places = [Place.model_validate(p) for p in data["place"]]
    skills = [Skill.model_validate(s) for s in data["skill"]]
    modes = [Mode.model_validate(m) for m in data["mode"]]
    skill_ids = {s.id for s in skills}
    place_ids = {p.id for p in places}

    assert len(skill_ids) == len(skills) and len(place_ids) == len(places) and len({m.id for m in modes}) == len(modes)
    assert {"capture", "journal", "where_am_i", "nothing"} <= skill_ids
    assert ({"car", "car_climate", "frunk"} <= skill_ids) is with_tesla
    if not with_tesla:
        assert not skill_ids & {"car", "car_climate", "frunk"}
    for skill in skills:
        steps = compile_skill(skill)
        validate_steps(steps, cfg, f"seed skill {skill.id}")
    for m in modes:
        assert set(m.bindings.values()) <= skill_ids, m.id
        assert set(m.when.places) | set(m.when.not_places) <= place_ids, m.id
        if m.when.recently:
            assert m.when.recently.place in place_ids
    assert modes[-1].id == FALLBACK_MODE
    fallback = modes[-1].bindings
    assert fallback["single-click-hold"] == "capture" and fallback["double-click-hold"] == "journal"
    assert fallback["triple-click"] == "where_am_i"

    phrases = [(s.id, p) for s in skills for p in s.phrases]
    for _, p in phrases:
        compile_phrase(p)
    texts = [p.lower() for _, p in phrases]
    assert len(texts) == len(set(texts)), "a phrase is claimed by two skills"


async def test_seed_only_writes_an_empty_catalog(catalog: Catalog, make_config: Callable[..., Config]) -> None:
    cfg = make_config()
    assert await seed_if_empty(catalog, cfg) is True
    await catalog.delete("skill", "recall")
    assert await seed_if_empty(catalog, cfg) is False
    assert await catalog.skill("recall") is None  # user edits survive restarts
    assert [m.id for m in await catalog.modes()][-1] == FALLBACK_MODE
