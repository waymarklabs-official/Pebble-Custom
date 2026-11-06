"""Context: place timeline, debouncing (duplicate arrivals, leave jitter) and listeners."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ringhub.context import DUPLICATE_ARRIVE_SECONDS, LEAVE_JITTER_SECONDS, Context, PlaceEvent
from ringhub.store import Store


class Recorder:
    def __init__(self) -> None:
        self.events: list[PlaceEvent] = []

    async def __call__(self, event: PlaceEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[tuple[str, str]]:
        return [(e.kind, e.place) for e in self.events]


@pytest.fixture
async def ctx(tmp_path: Path) -> tuple[Context, Recorder]:
    store = Store(tmp_path / "db.sqlite3")
    context = Context(store)
    rec = Recorder()
    context.on_place_event(rec)
    yield context, rec
    store.close()


T0 = 1_800_000_000.0


async def test_snapshot_defaults(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    assert await c.snapshot() == {"place": None, "focus": None}


async def test_arrive_sets_place_and_since_and_emits(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    ev = await c.arrive("home", at=T0)
    assert ev == PlaceEvent("arrive", "home", T0, None)
    snap = await c.snapshot()
    assert snap["place"] == "home" and snap["place_since"] == T0
    ev2 = await c.arrive("work", at=T0 + 1000)
    assert ev2 is not None and ev2.previous == "home"
    assert rec.kinds == [("arrive", "home"), ("arrive", "work")]


async def test_duplicate_arrivals_collapse(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    assert await c.arrive("home", at=T0, source="shortcut") is not None
    assert await c.arrive("home", at=T0 + 5, source="owntracks") is None
    assert await c.arrive("home", at=T0 + DUPLICATE_ARRIVE_SECONDS - 1, source="wifi") is None
    assert rec.kinds == [("arrive", "home")]
    assert (await c.snapshot())["place_since"] == T0  # the first report wins


async def test_rearrival_after_duplicate_window_reemits(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    await c.arrive("home", at=T0)
    ev = await c.arrive("home", at=T0 + DUPLICATE_ARRIVE_SECONDS + 1)
    assert ev is not None and ev.previous == "home"  # the leave may simply have been missed
    assert rec.kinds == [("arrive", "home"), ("arrive", "home")]
    assert (await c.snapshot())["place_since"] == T0  # a repeat report doesn't restart the stay


async def test_leave_within_jitter_window_is_ignored(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    await c.arrive("home", at=T0)
    assert await c.leave("home", at=T0 + LEAVE_JITTER_SECONDS - 1) is None
    snap = await c.snapshot()
    assert snap["place"] == "home" and "last_left" not in snap
    assert rec.kinds == [("arrive", "home")]


async def test_leave_after_jitter_clears_place(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    await c.arrive("home", at=T0)
    ev = await c.leave("home", at=T0 + LEAVE_JITTER_SECONDS + 1)
    assert ev == PlaceEvent("leave", "home", T0 + LEAVE_JITTER_SECONDS + 1, "home")
    snap = await c.snapshot()
    assert snap["place"] is None
    assert snap["last_left"] == {"place": "home", "at": T0 + LEAVE_JITTER_SECONDS + 1}
    assert rec.kinds == [("arrive", "home"), ("leave", "home")]


async def test_late_leave_of_old_place_does_not_clear_newer_place(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    await c.arrive("home", at=T0)
    await c.arrive("work", at=T0 + 1800)
    ev = await c.leave("home", at=T0 + 1900)  # delivered late: we're already at work
    assert ev is not None and ev.previous == "work"
    snap = await c.snapshot()
    assert snap["place"] == "work" and snap["place_since"] == T0 + 1800
    assert snap["last_left"]["place"] == "home"  # "left home recently" still holds
    assert rec.kinds[-1] == ("leave", "home")


async def test_leave_of_other_place_is_never_debounced(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    await c.arrive("work", at=T0)
    assert await c.leave("gym", at=T0 + 1) is not None  # jitter only applies to the current place
    assert (await c.snapshot())["place"] == "work"


async def test_stale_arrival_does_not_override_newer_place(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    await c.arrive("work", at=T0 + 1000)
    await c.arrive("home", at=T0)  # an old, queued report
    assert (await c.snapshot())["place"] == "work"


async def test_visit_fires_without_moving_you(ctx: tuple[Context, Recorder]) -> None:
    c, rec = ctx
    await c.arrive("work", at=T0)
    ev = await c.visit("pharmacy", at=T0 + 10)
    assert ev == PlaceEvent("visit", "pharmacy", T0 + 10, "work")
    snap = await c.snapshot()
    assert snap["place"] == "work"
    assert snap["last_visit"] == {"place": "pharmacy", "at": T0 + 10}
    # repeated visits are never collapsed (each tap is deliberate)
    await c.visit("pharmacy", at=T0 + 11)
    assert rec.kinds.count(("visit", "pharmacy")) == 2


async def test_value_at_follows_the_timeline(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    assert await c.value_at("place", T0) is None  # never set
    await c.arrive("home", at=T0)
    await c.leave("home", at=T0 + 600)
    await c.arrive("work", at=T0 + 1200)
    assert await c.value_at("place", T0 - 1) is None
    assert await c.value_at("place", T0) == "home"
    assert await c.value_at("place", T0 + 599) == "home"
    assert await c.value_at("place", T0 + 600) is None
    assert await c.value_at("place", T0 + 1199) is None
    assert await c.value_at("place", T0 + 5000) == "work"


async def test_value_at_same_timestamp_uses_latest_write(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    await c.set("focus", "work", at=T0)
    await c.set("focus", "sleep", at=T0)
    assert await c.value_at("focus", T0) == "sleep"


async def test_set_reports_change_and_none_deletes(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    assert await c.set("guests", True) is True
    assert await c.set("guests", True) is False  # unchanged: no timeline entry
    assert (await c.snapshot())["guests"] is True
    assert await c.set("guests", None) is True
    assert "guests" not in await c.snapshot()
    history = await c.history(10)
    assert [(h["key"], h["value"]) for h in history] == [("guests", None), ("guests", True)]


async def test_history_records_source(ctx: tuple[Context, Recorder]) -> None:
    c, _ = ctx
    await c.arrive("home", source="owntracks", at=time.time())
    sources = {h["source"] for h in await c.history()}
    assert sources == {"owntracks"}


async def test_failing_listener_does_not_block_others(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = Store(tmp_path / "db.sqlite3")
    try:
        c = Context(store)

        async def boom(event: PlaceEvent) -> None:
            raise RuntimeError("listener exploded")

        rec = Recorder()
        c.on_place_event(boom)
        c.on_place_event(rec)
        ev = await c.arrive("home", at=T0)
        assert ev is not None
        assert rec.kinds == [("arrive", "home")]
        assert (await c.snapshot())["place"] == "home"
        assert "place listener failed" in caplog.text
    finally:
        store.close()
