"""Regression tests for defects found in the adversarial review (one test per finding)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from ringhub.catalog import Mode, Skill
from ringhub.context import LEAVE_JITTER_SECONDS
from ringhub.engine import Engine
from ringhub.limits import BodyLimitMiddleware
from ringhub.links import LinkSigner
from ringhub.runtime import Runtime
from ringhub.webhook import WebhookError, authenticate

from .conftest import CONTEXT, SECRET, NtfyMock, ring_event, signed_request

Factory = Callable[..., Any]


@pytest.fixture
async def rt(make_runtime: Factory) -> Runtime:
    runtime: Runtime = await make_runtime()
    return runtime


@pytest.fixture
def engine(rt: Runtime) -> Engine:
    return Engine(rt)


async def hold(engine: Engine, text: str, **kw: Any) -> str:
    return await engine.handle(ring_event("single-click-hold", transcript=text, **kw))


# ----------------------------------------------------------------------------- memory semantics


@pytest.mark.parametrize("statement", [
    "Remember where I parked, level 3 spot 42",
    "Mom asked where I left the spare key, it's under the blue pot",
    "Note to self: the next reminder email goes out Friday",
])
async def test_statements_mentioning_questions_are_saved(rt: Runtime, engine: Engine, statement: str) -> None:
    await hold(engine, statement)
    notes = await rt.notes.recent(5)
    assert [n.text for n in notes] == [statement]


async def test_did_i_needs_every_word_and_says_not_sure_otherwise(rt: Runtime) -> None:
    now = time.time()
    await rt.notes.add("Need to take the trash out tonight", created_at=now)
    answer = await rt.memory.answer("did I take my meds")
    assert answer is not None and answer.startswith("🤔 Not sure")  # one word matched: never a false yes
    await rt.notes.add("Took meds ✓", kind="log", created_at=now)
    answer = await rt.memory.answer("did I take my meds")
    assert answer is not None and answer.startswith("✅ Yes") and "Took meds" in answer


async def test_did_i_is_not_crowded_out_by_old_reminders(rt: Runtime) -> None:
    now = time.time()
    for day in range(1, 12):
        await rt.notes.add("remind me to take my meds at 8 pm", kind="reminder", created_at=now - day * 86400)
    await rt.notes.add("Took meds ✓", kind="log", created_at=now)
    answer = await rt.memory.answer("did I take my meds today?")
    assert answer is not None and answer.startswith("✅ Yes")


async def test_this_weekday_reminder_is_scheduled_not_lost(rt: Runtime, engine: Engine) -> None:
    assert await hold(engine, "Remind me this Friday to pay the rent") == "capture"
    [r] = await rt.reminders.open()
    assert r.trigger == "time" and r.text == "Pay the rent"


async def test_late_voice_resolves_times_against_when_it_was_said(rt: Runtime, engine: Engine,
                                                                   ntfy: NtfyMock) -> None:
    said = time.time() - 3 * 3600
    await hold(engine, "remind me in 2 hours to move the laundry", at=said)
    [r] = await rt.reminders.open()
    assert r.due_at == pytest.approx(said + 2 * 3600, abs=2)  # an hour ago: overdue, not 2h from now
    assert "Overdue" in ntfy.find("Move the laundry")["message"]
    assert f"reminder:{r.id}" in {j.id for j in await rt.scheduler.pending()}


async def test_failing_voice_skill_still_saves_the_words(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await rt.catalog.put(Skill.model_validate({"id": "broken", "name": "Broken", "template": "webhook",
                                               "params": {"url": "https://hook.test/fail"}}))
    everywhere = await rt.catalog.mode("everywhere")
    assert everywhere is not None
    await rt.catalog.put(everywhere.model_copy(update={"bindings": {**everywhere.bindings,
                                                                     "single-click-hold": "broken"}}))
    ntfy.router.post("https://hook.test/fail").respond(500)
    outcome = await hold(engine, "the boiler code is 7281")
    assert outcome.startswith("error:")
    assert [n.text for n in await rt.notes.recent(5)] == ["the boiler code is 7281"]
    assert "saved as a note" in ntfy.find("failed")["message"]


# ----------------------------------------------------------------------------- context


async def test_jittery_leave_is_committed_when_no_reentry(rt: Runtime) -> None:
    events: list[tuple[str, str]] = []

    async def listener(e: Any) -> None:
        events.append((e.kind, e.place))

    rt.context.on_place_event(listener)
    now = time.time()
    await rt.context.arrive("car", at=now - LEAVE_JITTER_SECONDS + 0.2)  # a short drive
    assert await rt.context.leave("car", at=now) is None  # held back for now...
    for _ in range(100):
        if ("leave", "car") in events:
            break
        await asyncio.sleep(0.02)
    assert ("leave", "car") in events  # ...then committed: parking memory and leave reminders run
    assert (await rt.context.snapshot())["place"] is None
    parked = await rt.notes.latest_matching("log", "Parked")
    assert parked is not None


async def test_jittery_leave_is_cancelled_by_reentry(rt: Runtime) -> None:
    now = time.time()
    await rt.context.arrive("home", at=now - 10)
    assert await rt.context.leave("home", at=now - 5) is None
    assert await rt.context.arrive("home", at=now) is None  # it never left
    assert (await rt.context.snapshot())["place"] == "home"
    assert "pending_leave" not in await rt.context.snapshot()


async def test_arriving_elsewhere_commits_the_held_leave(rt: Runtime) -> None:
    now = time.time()
    await rt.context.arrive("car", at=now - 60)
    await rt.context.leave("car", at=now - 30)
    await rt.context.arrive("work", at=now)
    snap = await rt.context.snapshot()
    assert snap["place"] == "work" and snap["last_left"]["place"] == "car"


async def test_wrongly_typed_context_cannot_break_mode_resolution(rt: Runtime, engine: Engine) -> None:
    await rt.context.set("focus", 5)
    await rt.context.set("last_left", "x")
    await rt.context.set("place_since", "soon")
    res = await rt.resolution()
    assert res.mode.id  # no AttributeError
    assert await hold(engine, "still works") == "capture"


# ----------------------------------------------------------------------------- catalog


async def test_import_must_keep_builtin_skills(rt: Runtime) -> None:
    data = await rt.catalog.export()
    data["skill"] = [s for s in data["skill"] if s["id"] != "capture"]
    with pytest.raises(ValueError, match="built-in skills: capture"):
        await rt.catalog.replace_all(data)
    assert await rt.catalog.skill("capture") is not None  # untouched


# ----------------------------------------------------------------------------- scheduler


async def test_boot_rearms_timed_reminders_without_a_job(rt: Runtime) -> None:
    r = await rt.reminders.add("Orphan", "time", due_at=time.time() + 3600)
    assert await rt.memory.reconcile() == 1
    assert f"reminder:{r.id}" in {j.id for j in await rt.scheduler.pending()}
    assert await rt.memory.reconcile() == 0


async def test_self_rescheduling_job_keeps_its_new_row(make_runtime: Factory, make_config: Factory,
                                                       tmp_path: Any) -> None:
    rt = await make_runtime(make_config(memory={"digest_time": "21:30", "markdown_dir": str(tmp_path / "v")}))
    await rt.scheduler.schedule([{"memory.digest": {}}], timedelta(seconds=0), {}, "digest")
    for _ in range(100):
        jobs = {j.id: j for j in await rt.scheduler.pending()}
        if "digest" in jobs and jobs["digest"].run_at > time.time() + 60:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("digest did not reschedule itself")


async def test_fire_schedules_the_nag_before_pushing(rt: Runtime, ntfy: NtfyMock) -> None:
    ntfy.publish.side_effect = RuntimeError("push hung")
    r = await rt.reminders.add("Pills", "time", due_at=time.time())
    with pytest.raises(RuntimeError):
        await rt.memory.fire(r.id)
    assert f"nag:{r.id}" in {j.id for j in await rt.scheduler.pending()}  # it will still reach you


# ----------------------------------------------------------------------------- security


async def test_body_limit_cuts_off_chunked_uploads_while_streaming() -> None:
    read: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        while True:
            msg = await receive()
            read.append(len(msg.get("body", b"")))
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunks = [{"type": "http.request", "body": b"x" * 1000, "more_body": True} for _ in range(1000)]
    sent: list[Any] = []

    async def receive() -> Any:
        return chunks.pop(0)

    async def send(msg: Any) -> None:
        sent.append(msg)

    mw = BodyLimitMiddleware(app, default=5000, overrides={})
    await mw({"type": "http", "path": "/ring", "headers": []}, receive, send)
    assert sent[0]["status"] == 413
    assert sum(read) <= 5000  # stopped after ~5 KB, not 1 MB
    assert len(chunks) > 990


def test_oversized_declared_body_rejected_before_auth(make_client: Factory) -> None:
    c = make_client()
    r = c.post("/api/v1/session", content=b"x" * (2 * 1024 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_non_ascii_signatures_are_rejected_not_500() -> None:
    body, headers = signed_request("click")
    headers["X-Index-Signature"] = "é" * 64
    with pytest.raises(WebhookError) as exc:
        authenticate(headers, body, [SECRET], max_skew_seconds=300)
    assert exc.value.status == 401
    signer = LinkSigner(b"k" * 32, "https://hub.test")
    assert signer.verify("/r/abc/done", "9999999999", "é" * 32) is False
    assert signer.verify("/r/abc/done", "٩٩٩٩٩٩٩٩٩٩", "0" * 32) is False


def test_owntracks_cannot_inject_text_into_location(make_client: Factory) -> None:
    c = make_client()
    auth = ("u", CONTEXT)
    r = c.post("/api/v1/owntracks", auth=auth, json={"_type": "location", "lat": 37,
                                                      "lon": "0\nSession expired: https://evil.example"})
    assert r.status_code == 200
    ctx = c.get("/api/v1/context", headers={"Authorization": "Bearer admin-token-for-tests"}).json()["now"]
    assert "location" not in ctx
    r = c.post("/api/v1/owntracks", auth=auth, json={"_type": "location", "lat": 37.5, "lon": -122.1, "tst": "nan"})
    assert r.status_code == 200


async def test_mode_bound_combo_still_resolves(rt: Runtime) -> None:
    """Sanity: the stricter catalog import keeps valid combo bindings."""
    data = await rt.catalog.export()
    everywhere = next(m for m in data["mode"] if m["id"] == "everywhere")
    everywhere["bindings"]["double-click+click"] = "list"
    await rt.catalog.replace_all(data)
    mode = await rt.catalog.mode("everywhere")
    assert isinstance(mode, Mode) and mode.bindings["double-click+click"] == "list"



# ----------------------------------------------------------------------------- product pass


async def test_leading_filler_is_not_part_of_the_reminder(rt: Runtime, engine: Engine) -> None:
    await hold(engine, "Um, remind me in ten minutes to take the pizza out")
    [r] = await rt.reminders.open()
    assert r.text == "Take the pizza out"


async def test_where_did_i_park_uses_what_you_said(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "I parked on level 4 near the elevators")
    await hold(engine, "Where did I park?")
    assert ntfy.last()["message"].startswith("🚗 You said: “I parked on level 4 near the elevators”")


async def test_accidental_holds_are_not_saved(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    for noise in ("ok", "Um.", "test"):
        await hold(engine, noise)
    assert await rt.notes.recent(5) == []
    assert ntfy.last()["message"] == "👍 Nothing saved."


async def test_scratch_that_undoes_the_last_capture(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "remind me at 7 to call the bnak")
    await hold(engine, "scratch that")
    assert await rt.reminders.open() == []
    assert await rt.notes.recent(5) == []
    assert ntfy.last()["message"].startswith("↩️ Undone")
    await hold(engine, "never mind")
    assert ntfy.last()["message"] == "🤷 Nothing recent to undo."
