"""Engine: late voice, unbound gestures, failures, combos from mode bindings, where-am-I."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ringhub.catalog import Mode, Skill
from ringhub.config import Config
from ringhub.engine import LATE_DELIVERY_SECONDS, Engine, label
from ringhub.models import Gesture
from ringhub.runtime import Runtime

from .conftest import NtfyMock, eventually, ring_event

Factory = Callable[..., Any]


@pytest.fixture
async def rt(make_runtime: Factory) -> Runtime:
    runtime: Runtime = await make_runtime()
    return runtime


@pytest.fixture
def engine(rt: Runtime) -> Engine:
    return Engine(rt)


@pytest.fixture
async def running(rt: Runtime) -> Engine:
    eng = Engine(rt)
    await eng.start()
    yield eng
    await eng.stop()


async def edit_mode(rt: Runtime, mode_id: str, **changes: Any) -> None:
    mode = await rt.catalog.mode(mode_id)
    assert mode is not None
    await rt.catalog.put(Mode.model_validate({**mode.model_dump(), **changes}))


async def unbind(rt: Runtime, mode_id: str, *keys: str) -> None:
    mode = await rt.catalog.mode(mode_id)
    assert mode is not None
    await edit_mode(rt, mode_id, bindings={k: v for k, v in mode.bindings.items() if k not in keys})


def test_labels() -> None:
    assert label("double-click+click") == "double+click"
    assert label("single-click-hold") == "hold" and label("double-click-hold") == "click-hold"


# ----------------------------------------------------------------------------- late voice


async def test_late_voice_is_filed_under_the_place_where_it_was_said(rt: Runtime, engine: Engine) -> None:
    t = time.time()
    await rt.context.arrive("home", at=t - 3600)
    await rt.context.set("focus", "personal", at=t - 3600)
    await rt.context.arrive("work", at=t - 300)
    await rt.context.set("focus", "work", at=t - 300)
    said = t - 1800  # recorded at home, delivered half an hour later at work
    assert await engine.handle(ring_event("single-click-hold", transcript="the spare key is under the mat",
                                          at=said)) == "capture"
    (note,) = await rt.notes.recent(1)
    assert note.place == "home" and note.created_at == pytest.approx(said, abs=0.01)


async def test_prompt_voice_uses_the_current_place(rt: Runtime, engine: Engine) -> None:
    t = time.time()
    await rt.context.arrive("home", at=t - 3600)
    await rt.context.arrive("work", at=t - 300)
    await engine.handle(ring_event("single-click-hold", transcript="on time", at=t - LATE_DELIVERY_SECONDS + 20))
    (note,) = await rt.notes.recent(1)
    assert note.place == "work"


async def test_late_voice_said_nowhere_has_no_place(rt: Runtime, engine: Engine) -> None:
    t = time.time()
    await rt.context.arrive("work", at=t - 300)
    await engine.handle(ring_event("single-click-hold", transcript="before any place", at=t - 7200))
    assert (await rt.notes.recent(1))[0].place is None


async def test_late_reminder_relative_time_counts_from_when_it_was_said(rt: Runtime, engine: Engine) -> None:
    said = time.time() - 600
    await engine.handle(ring_event("single-click-hold", transcript="remind me in 30 minutes to move the car",
                                   at=said))
    (r,) = await rt.reminders.open()
    assert r.due_at == pytest.approx(said + 1800, abs=2)


# ----------------------------------------------------------------------------- unbound & fallbacks


async def test_unbound_click_says_nothing_on_quietly_and_logs_unmatched(rt: Runtime, engine: Engine,
                                                                       ntfy: NtfyMock) -> None:
    await unbind(rt, "everywhere", "click")
    assert await engine.handle(ring_event("click", delivery="d-unbound")) == "unbound"
    push = ntfy.last()
    assert push["message"] == "◎ Everywhere: nothing on click" and push["priority"] == 2
    event = (await rt.store.recent_events(1))[0]
    assert (event["delivery_id"], event["gesture"], event["status"], event["detail"]) == (
        "d-unbound", "click", "unmatched", "everywhere: unbound")


async def test_unbound_in_silent_mode_pushes_nothing(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await rt.catalog.put(Mode.model_validate({"id": "dnd", "name": "DND", "feedback": "silent",
                                              "when": {"focus": ["dnd"]}}), position=0)
    await unbind(rt, "everywhere", "click")
    await rt.context.set("focus", "dnd")
    assert await engine.handle(ring_event("click")) == "unbound"
    assert ntfy.bodies == []
    assert (await rt.store.recent_events(1))[0]["detail"] == "dnd: unbound"


async def test_unbound_voice_is_never_dropped(rt: Runtime, engine: Engine) -> None:
    await unbind(rt, "everywhere", "single-click-hold", "double-click-hold")
    assert await engine.handle(ring_event("single-click-hold", transcript="still captured")) == "capture"
    assert await engine.handle(ring_event("double-click-hold", transcript="still journaled")) == "journal"
    kinds = {n.text: n.kind for n in await rt.notes.recent(5)}
    assert kinds == {"still captured": "note", "still journaled": "journal"}


async def test_mode_binding_overrides_fallback_and_feedback_is_capped(rt: Runtime, engine: Engine,
                                                                      ntfy: NtfyMock) -> None:
    await rt.context.arrive("work", at=time.time() - 600)
    assert await engine.handle(ring_event("click")) == "hand_raise"
    assert ntfy.find("Hand raised")["topic"] == "hand-raise"
    await rt.context.arrive("car", at=time.time())
    ntfy.reset()
    assert await engine.handle(ring_event("double-click")) == "list"  # "In the car" mode is quiet
    assert ntfy.last()["priority"] == 2
    assert (await rt.store.recent_events(1))[0]["detail"] == "driving: list"


async def test_blocking_a_gesture_with_nothing(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await rt.context.set("focus", "sleep")
    assert await engine.handle(ring_event("double-click")) == "nothing"
    assert ntfy.bodies == []


# ----------------------------------------------------------------------------- failures


async def test_skill_failure_pushes_an_error_and_logs_it(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    ntfy.router.post("https://hook.test/fail").mock(return_value=httpx.Response(500))
    await rt.catalog.put(Skill.model_validate({"id": "garage", "name": "Garage door", "icon": "🚪",
                                               "template": "webhook", "params": {"url": "https://hook.test/fail"}}))
    await edit_mode(rt, "everywhere", bindings={**(await rt.catalog.mode("everywhere")).bindings,  # type: ignore[union-attr]
                                                "click": "garage"})
    outcome = await engine.handle(ring_event("click", delivery="d-fail"))
    assert outcome == "error: 🚪 Garage door: POST hook.test: HTTP 500"  # host only: URLs may hold keys
    (push,) = ntfy.bodies  # no success confirmation
    assert push["title"] == "⚠️ click failed" and push["priority"] == 3 and push["tags"] == ["warning"]
    assert "Garage door" in push["message"] and "500" in push["message"]
    event = (await rt.store.recent_events(1))[0]
    assert (event["delivery_id"], event["status"], event["detail"]) == ("d-fail", "error", "everywhere: garage")
    assert "500" in event["error"]


async def test_error_push_ignores_quiet_modes(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await rt.catalog.put(Mode.model_validate({"id": "quiet", "name": "Q", "feedback": "silent",
                                              "bindings": {"click": "ghost"}}), position=0)
    assert (await engine.handle(ring_event("click"))).startswith("error: skill 'ghost' no longer exists")
    assert ntfy.last()["priority"] == 3


async def test_errors_are_not_pushed_when_disabled(make_runtime: Factory, make_config: Callable[..., Config],
                                                   ntfy: NtfyMock) -> None:
    rt = await make_runtime(make_config(notify_on_error=False))
    await rt.catalog.put(Mode.model_validate({"id": "m", "name": "M", "bindings": {"click": "ghost"}}), position=0)
    assert (await Engine(rt).handle(ring_event("click"))).startswith("error:")
    assert ntfy.bodies == []
    assert (await rt.store.recent_events(1))[0]["status"] == "error"


async def test_ntfy_outage_never_breaks_handling(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    ntfy.publish.mock(return_value=httpx.Response(502))
    assert await engine.handle(ring_event("single-click-hold", transcript="saved anyway")) == "capture"
    assert (await rt.notes.recent(1))[0].text == "saved anyway"


# ----------------------------------------------------------------------------- where am I / override


async def test_triple_says_where_and_twice_switches_mode(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    assert await engine.handle(ring_event("triple-click")) == "where_am_i"
    push = ntfy.last()
    assert push["title"] == "◎ Everywhere — no other mode applies"
    assert push["message"].splitlines() == ["click: 📍 Mark this moment", "double: 📋 What's on my list",
                                            "hold: 🎙️ Smart capture", "click-hold: 📓 Journal",
                                            "Triple again within 10 s to switch."]
    await engine.handle(ring_event("triple-click"))
    res = await rt.resolution()
    first_enabled = (await rt.catalog.modes())[0]
    assert res.manual and res.mode.id == first_enabled.id
    assert ntfy.last()["title"] == f"{first_enabled.icon} Switched to {first_enabled.name}"
    # a third triple starts over: it says where you are (now the override)
    await engine.handle(ring_event("triple-click"))
    assert ntfy.last()["title"].endswith("set by hand")


async def test_slow_second_triple_does_not_switch(rt: Runtime, engine: Engine) -> None:
    await engine.handle(ring_event("triple-click"))
    await rt.store.set("where_am_i_at", time.time() - 11)
    await engine.handle(ring_event("triple-click"))
    assert not (await rt.resolution()).manual


async def test_override_lasts_until_the_place_changes(rt: Runtime, engine: Engine) -> None:
    await engine.handle(ring_event("triple-click"))
    await engine.handle(ring_event("triple-click"))
    assert (await rt.resolution()).manual
    assert await engine.handle(ring_event("double-click")) != "unbound"
    await rt.context.arrive("work")
    res = await rt.resolution()
    assert (res.mode.id, res.manual) == ("work", False)


async def test_cycle_skips_disabled_modes_and_wraps(rt: Runtime) -> None:
    await edit_mode(rt, "sleep", enabled=False)
    enabled = [m.id for m in await rt.catalog.modes() if m.enabled]
    seen = [(await rt.cycle_override()).id for _ in range(len(enabled) + 1)]
    assert "sleep" not in seen
    assert seen[:len(enabled)] == enabled  # from "everywhere" (last) it wraps to the first mode
    assert seen[-1] == enabled[0]


async def test_set_override_validates(rt: Runtime) -> None:
    from datetime import timedelta

    from ringhub.actions.base import ActionError

    with pytest.raises(ActionError):
        await rt.set_override("ghost")
    await edit_mode(rt, "work", enabled=False)
    with pytest.raises(ActionError):
        await rt.set_override("work")
    await rt.set_override("sleep", timedelta(minutes=5))
    ov = await rt.store.get("mode_override")
    assert ov["mode"] == "sleep" and ov["expires_at"] == pytest.approx(time.time() + 300, abs=5)
    await rt.set_override(None)
    assert await rt.store.get("mode_override") is None


# ----------------------------------------------------------------------------- combos & ordering


async def details(rt: Runtime) -> list[str]:
    return [e["detail"] for e in reversed(await rt.store.recent_events(50)) if e["delivery_id"]]


async def test_combo_bound_in_a_mode_runs_and_rebuilds_after_catalog_change(rt: Runtime, running: Engine) -> None:
    bindings = dict((await rt.catalog.mode("everywhere")).bindings)  # type: ignore[union-attr]
    await edit_mode(rt, "everywhere", bindings={**bindings, "double+click": "took_meds"})
    await running.submit(ring_event(Gesture.DOUBLE_CLICK))
    await running.submit(ring_event(Gesture.CLICK))
    await eventually(lambda: details(rt))
    assert await details(rt) == ["everywhere: took_meds"]
    assert (await rt.store.recent_events(1))[0]["gesture"] == "double+click"
    assert (await rt.notes.recent(1))[0].text == "Took meds ✓"

    # remove the combo: the same presses are two plain gestures again, without any wait
    await edit_mode(rt, "everywhere", bindings=bindings)
    await running.submit(ring_event(Gesture.DOUBLE_CLICK))
    await running.submit(ring_event(Gesture.CLICK))
    await eventually(lambda: _count(rt, 3))
    assert await details(rt) == ["everywhere: took_meds", "everywhere: list", "everywhere: moment"]


async def _count(rt: Runtime, n: int) -> bool:
    return len(await details(rt)) >= n


async def test_combo_prefix_alone_fires_after_the_window(rt: Runtime, running: Engine) -> None:
    bindings = dict((await rt.catalog.mode("everywhere")).bindings)  # type: ignore[union-attr]
    await edit_mode(rt, "everywhere", bindings={**bindings, "double+click": "took_meds"})
    started = time.monotonic()
    await running.submit(ring_event(Gesture.DOUBLE_CLICK))
    await eventually(lambda: details(rt))
    assert time.monotonic() - started >= rt.config.ring.combo_window_ms / 1000 * 0.9
    assert await details(rt) == ["everywhere: list"]


async def test_combo_bound_only_in_another_mode_still_resolves_against_active_mode(rt: Runtime,
                                                                                  running: Engine) -> None:
    await edit_mode(rt, "work", bindings={"click": "hand_raise", "double+click": "took_meds"})
    # not at work: the combo exists (so double waits), but the active mode doesn't bind it
    await running.submit(ring_event(Gesture.DOUBLE_CLICK))
    await running.submit(ring_event(Gesture.CLICK))
    await eventually(lambda: details(rt))
    assert await details(rt) == ["everywhere: unbound"]


async def test_voice_flushes_held_clicks_first(rt: Runtime, running: Engine) -> None:
    bindings = dict((await rt.catalog.mode("everywhere")).bindings)  # type: ignore[union-attr]
    await edit_mode(rt, "everywhere", bindings={**bindings, "double+click": "took_meds"})
    await running.submit(ring_event(Gesture.DOUBLE_CLICK))
    await running.submit(ring_event(Gesture.HOLD, transcript="after the double"))
    await eventually(lambda: _count(rt, 2))
    assert await details(rt) == ["everywhere: list", "everywhere: capture"]


async def test_events_are_handled_in_arrival_order(rt: Runtime, running: Engine) -> None:
    for i in range(5):
        await running.submit(ring_event(Gesture.HOLD, transcript=f"note number {i}"))
    await eventually(lambda: _count(rt, 5))
    notes = await rt.notes.recent(10)
    assert [n.text for n in reversed(notes)] == [f"note number {i}" for i in range(5)]
