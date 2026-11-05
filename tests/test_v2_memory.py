"""The memory aid end to end on a real Runtime: capture, recall, reminder delivery, attention
layer, parking memory, digest and the Markdown mirror. ntfy bodies are captured with respx."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from ringhub.config import Config
from ringhub.engine import Engine
from ringhub.memory.service import ATTENTION_CAPTURE, ATTENTION_REMINDER, NAG_AFTER, SNOOZE
from ringhub.runtime import Runtime
from ringhub.vehicle import VehicleState

from .conftest import ADMIN, CONTEXT, NtfyMock, eventually, ring_event

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


async def press(engine: Engine, gesture: str) -> str:
    return await engine.handle(ring_event(gesture))


def clock(rt: Runtime, ts: float) -> str:
    return datetime.fromtimestamp(ts, rt.config.tz).strftime("%-I:%M %p").lower()


class FakeVehicle:
    def __init__(self, state: VehicleState | None) -> None:
        self._state = state
        self.calls: list[bool] = []

    async def state(self, *, allow_stale: bool = False) -> VehicleState | None:
        self.calls.append(allow_stale)
        return self._state

    def invalidate(self) -> None:
        pass


# ----------------------------------------------------------------------------- capture


async def test_plain_speech_is_saved_as_a_note_under_the_current_place(rt: Runtime, engine: Engine,
                                                                       ntfy: NtfyMock) -> None:
    await rt.context.arrive("work", at=time.time() - 600)
    assert await hold(engine, "The parking garage code is 4417") == "capture"
    (note,) = await rt.notes.recent(5)
    assert (note.kind, note.text, note.place, note.mode) == ("note", "The parking garage code is 4417", "work", "work")
    push = ntfy.last()
    assert push["message"] == "📝 The parking garage code is 4417 · Work"
    assert push["topic"] == "ring" and push["priority"] == 3
    assert await rt.store.get(ATTENTION_CAPTURE) == {"kind": "capture", "ref": {"note": note.id},
                                                 "at": pytest.approx(time.time(), abs=5)}
    assert await rt.reminders.open() == []


async def test_long_note_is_clipped_in_the_push_but_kept_in_full(rt: Runtime, engine: Engine,
                                                                 ntfy: NtfyMock) -> None:
    text = "idea " + "x" * 200
    await hold(engine, text)
    assert (await rt.notes.recent(1))[0].text == text
    assert len(ntfy.last()["message"]) <= 82 and ntfy.last()["message"].endswith("…")


async def test_timed_reminder_is_scheduled(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    before = time.time()
    await hold(engine, "Remind me in 20 minutes to check the oven")
    (r,) = await rt.reminders.open()
    assert (r.text, r.trigger, r.status) == ("Check the oven", "time", "pending")
    assert r.due_at == pytest.approx(before + 1200, abs=5)
    jobs = {j.id: j for j in await rt.scheduler.pending()}
    assert jobs[f"reminder:{r.id}"].run_at == pytest.approx(r.due_at, abs=1)
    assert jobs[f"reminder:{r.id}"].steps == [{"reminder.fire": {"id": r.id}}]
    assert ntfy.last()["message"] == f"⏰ {clock(rt, r.due_at)} · Check the oven"
    note = await rt.notes.get(r.note_id)  # type: ignore[arg-type]
    assert note is not None and note.kind == "reminder" and note.text == "Remind me in 20 minutes to check the oven"


@pytest.mark.parametrize("said,trigger,place,message", [
    ("When I get home take the chicken out", "arrive", "home", "📍 When you arrive at Home · Take the chicken out"),
    ("remind me when I leave work to buy milk", "leave", "work", "📍 When you leave Work · Buy milk"),
    ("next time I'm at the office ask about the badge", "arrive", "work",
     "📍 When you arrive at Work · Ask about the badge"),
    ("remind me to renew the passport", "none", None, "📋 On your list · Renew the passport"),
])
async def test_place_and_list_reminders(rt: Runtime, engine: Engine, ntfy: NtfyMock, said: str, trigger: str,
                                        place: str | None, message: str) -> None:
    await hold(engine, said)
    (r,) = await rt.reminders.open()
    assert (r.trigger, r.place, r.due_at) == (trigger, place, None)
    assert ntfy.last()["message"] == message
    assert not [j for j in await rt.scheduler.pending() if j.id.startswith("reminder:")]


async def test_unknown_place_is_flagged(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "when I get to the gym call the plumber")
    (r,) = await rt.reminders.open()
    assert (r.trigger, r.place, r.text) == ("arrive", "gym", "Call the plumber")
    assert "I don't know “gym” yet" in ntfy.last()["message"]


async def test_reminder_text_keeps_original_casing(rt: Runtime) -> None:
    result = await rt.memory.capture("Remind me to call Bob about the NASA badge", said_at=time.time(),
                                     place=None, mode=None)
    assert result.reminder is not None and result.reminder.text == "Call Bob about the NASA badge"


async def test_questions_are_answered_not_saved(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "What's on my list?")
    assert ntfy.last()["message"] == "✅ Nothing on your list."
    assert await rt.notes.recent(5) == []
    assert await rt.store.get(ATTENTION_CAPTURE) is None  # nothing to undo


async def test_voice_memo_without_transcript_keeps_the_audio(rt: Runtime, engine: Engine, ntfy: NtfyMock,
                                                             tmp_path: Path) -> None:
    audio = tmp_path / "rec.m4a"
    audio.write_bytes(b"\x00")
    await engine.handle(ring_event("single-click-hold", transcript="", audio=audio))
    (note,) = await rt.notes.recent(5)
    assert (note.text, note.audio, note.kind) == ("(voice memo)", str(audio), "note")
    assert ntfy.last()["message"] == "🎙️ Voice memo saved (no transcript)"


async def test_empty_capture_is_reported_loudly(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await engine.handle(ring_event("single-click-hold", transcript="   "))
    assert await rt.notes.recent(5) == []
    push = ntfy.last()
    assert push["message"].startswith("🤔 I didn't catch anything") and push["priority"] == 4


async def test_skill_phrase_runs_the_skill_instead_of_saving(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "Hey, raise my hand please")
    pushed = {b["topic"]: b for b in ntfy.bodies}
    assert pushed["hand-raise"]["message"] == "✋ Hand raised" and pushed["hand-raise"]["priority"] == 4
    assert pushed["ring"]["message"] == "✋ Hand raise"  # the confirmation
    assert await rt.notes.recent(5) == []


async def test_phrase_slots_reach_the_skill_templates(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    from ringhub.catalog import Skill

    await rt.catalog.put(Skill.model_validate({
        "id": "late", "name": "Running late", "template": "custom", "phrases": ["tell {who} i'm running late"],
        "steps": [{"notify": {"message": "Running late, {{ who }}", "topic": "family"}}]}))
    await hold(engine, "tell the kids I'm running late")
    assert {b["topic"]: b["message"] for b in ntfy.bodies}["family"] == "Running late, the kids"


async def test_transcript_is_data_not_a_template(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "note {{ config.server.admin_token }} {% for x in range(3) %}x{% endfor %}")
    (note,) = await rt.notes.recent(1)
    assert "{{ config.server.admin_token }}" in note.text
    assert ADMIN not in ntfy.last()["message"]


async def test_journal_keeps_raw_text_and_audio(rt: Runtime, engine: Engine, ntfy: NtfyMock, tmp_path: Path) -> None:
    audio = tmp_path / "j.m4a"
    audio.write_bytes(b"\x00")
    await engine.handle(ring_event("double-click-hold", transcript="remind me to call mom", audio=audio))
    (note,) = await rt.notes.recent(1)
    assert (note.kind, note.text, note.audio) == ("journal", "remind me to call mom", str(audio))
    assert await rt.reminders.open() == []  # the journal never interprets
    assert ntfy.last()["message"] == "📓 Journaled · remind me to call mom" and ntfy.last()["priority"] == 2


async def test_moment_records_place_and_fresh_location(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await rt.context.set("location", {"lat": 41.88, "lon": -87.63, "at": time.time()})
    assert await press(engine, "click") == "moment"
    (note,) = await rt.notes.recent(1)
    assert (note.kind, note.text, note.meta) == ("moment", "Moment", {"location": {"lat": 41.88, "lon": -87.63}})
    assert ntfy.last()["message"].startswith("📍 Moment · ")


# ----------------------------------------------------------------------------- recall


async def test_where_did_i_park_without_a_spot(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "Where did I park?")
    assert ntfy.last()["message"].startswith("🚗 I don't have a parking spot saved yet.")


async def test_where_did_i_park_with_an_apple_maps_link(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    at = time.time() - 120
    await rt.notes.add("Parked", kind="log", created_at=at, place="work",
                       meta={"location": {"lat": 41.8781, "lon": -87.6298, "source": "car"}})
    for question in ("where did I park", "Where's my car?", "where is the car"):
        await hold(engine, question)
        assert ntfy.last()["message"] == (f"🚗 Parked at Work · {clock(rt, at)}\n"
                                          "https://maps.apple.com/?q=Car&ll=41.8781,-87.6298")


async def test_where_did_i_park_without_coordinates_has_no_link(rt: Runtime) -> None:
    await rt.notes.add("Parked", kind="log", created_at=time.time())
    answer = await rt.memory.answer("where did i park")
    assert answer is not None and answer.startswith("🚗 Parked · ") and "maps.apple.com" not in answer


async def test_where_did_i_leave_my_keys_is_not_a_parking_question(rt: Runtime) -> None:
    await rt.notes.add("Keys are in the blue bowl", created_at=time.time())
    answer = await rt.memory.answer("where did I leave my keys")
    assert answer is None or "🚗" not in answer


async def test_did_i_answers_from_todays_logs(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "did I take my meds")
    assert ntfy.last()["message"] == "🤷 No record of “take my meds” today."
    await press_skill(rt, "took_meds")
    took = (await rt.notes.recent(1))[0]
    assert took.text == "Took meds ✓" and took.kind == "log"
    await hold(engine, "Did I take my meds today?")
    assert ntfy.last()["message"] == f"✅ Yes — “Took meds ✓” at {clock(rt, took.created_at)}"
    await hold(engine, "did I lock the door")
    assert ntfy.last()["message"].startswith("🤷")


async def press_skill(rt: Runtime, skill_id: str) -> None:
    await rt.run_skill(skill_id, await rt.base_vars())


async def test_did_i_ignores_yesterday_and_reminders(rt: Runtime) -> None:
    yesterday = (rt.now() - timedelta(days=1)).replace(hour=12).timestamp()
    await rt.memory.log("Took meds ✓", at=yesterday, place=None, mode=None)
    await rt.memory.capture("remind me to take my meds", said_at=time.time(), place=None, mode=None)
    assert (await rt.memory.answer("did i take my meds") or "").startswith("🤷")


async def test_whats_on_my_list(rt: Runtime) -> None:
    await rt.memory.capture("remind me to renew the passport", said_at=time.time(), place=None, mode=None)
    await rt.memory.capture("when I get home take the chicken out", said_at=time.time(), place=None, mode=None)
    await rt.memory.capture("remind me when I leave work to buy milk", said_at=time.time(), place=None, mode=None)
    await rt.memory.capture("remind me in 2 hours to stretch", said_at=time.time(), place=None, mode=None)
    answer = await rt.memory.answer("what's on my list")
    assert answer is not None and answer.startswith("📋 ")
    assert "• Renew the passport — list" in answer
    assert "• Take the chicken out — @ Home" in answer
    assert "• Buy milk — leaving Work" in answer
    assert "• Stretch — " in answer
    for alias in ("my list", "what do I need to do", "what are my reminders", "to do list"):
        assert await rt.memory.answer(alias) == answer


async def test_list_summary_truncates(rt: Runtime) -> None:
    for i in range(10):
        await rt.reminders.add(f"item {i}", "none")
    answer = await rt.memory.list_summary()
    assert answer.count("•") == 8 and answer.endswith("…and 2 more")


async def test_what_did_i_say_about(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    at = time.time() - 60
    await rt.notes.add("Dentist moved my cleaning to Friday", created_at=at)
    await rt.notes.add("Buy dental floss", created_at=at)
    await hold(engine, "What did I say about the dentist?")
    assert ntfy.last()["message"] == f"🔎 • Dentist moved my cleaning to Friday — {clock(rt, at)}"
    await hold(engine, "find notes about the plumber")
    assert ntfy.last()["message"] == "🔎 Nothing found for “the plumber”."


async def test_whats_next(rt: Runtime) -> None:
    assert await rt.memory.answer("what's next") == "✅ No timed reminders coming up."
    soon = time.time() + 3600
    await rt.reminders.add("later thing", "time", due_at=soon + 3600)
    await rt.reminders.add("soon thing", "time", due_at=soon)
    await rt.reminders.add("list thing", "none")
    assert await rt.memory.answer("What's next?") == f"⏰ Next: soon thing · {clock(rt, soon)}"


async def test_read_me_my_last_notes(rt: Runtime) -> None:
    assert await rt.memory.answer("read me my last notes") == "📝 No notes yet."
    t = time.time()
    for i in range(4):
        await rt.notes.add(f"thought {i}", created_at=t + i)
    await rt.notes.add("Parked", kind="log", created_at=t + 10)  # logs are not "notes"
    answer = await rt.memory.answer("read my last 2 notes")
    assert answer is not None and "thought 3" in answer and "thought 2" in answer and "thought 1" not in answer
    assert "Parked" not in answer


# ----------------------------------------------------------------------------- place reminders


async def test_arrive_reminder_fires_once(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("Take the chicken out", "arrive", place="home")
    t = time.time()
    await rt.context.arrive("home", at=t)
    push = ntfy.find("Take the chicken out")
    assert push["title"] == "⏰ Take the chicken out"
    assert push["message"] == "Arrived at Home · Ring: click = done · double = snooze"
    assert push["priority"] == 4 and push["sequence_id"] == f"rem-{r.id}"
    assert (await rt.reminders.get(r.id)).status == "fired"  # type: ignore[union-attr]
    await rt.context.leave("home", at=t + 600)
    await rt.context.arrive("home", at=t + 1200)
    assert len(ntfy.bodies) == 1
    assert (await rt.store.get(ATTENTION_REMINDER))["ref"] == r.id


async def test_arrive_elsewhere_does_not_fire(rt: Runtime, ntfy: NtfyMock) -> None:
    await rt.reminders.add("Take the chicken out", "arrive", place="home")
    await rt.reminders.add("Buy milk", "leave", place="home")
    await rt.context.arrive("work")
    assert ntfy.bodies == []


async def test_visit_fires_arrive_reminders_without_moving(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("Pick up the prescription", "arrive", place="pharmacy")
    await rt.context.arrive("work", at=time.time() - 600)
    await rt.context.visit("pharmacy")
    push = ntfy.find("Pick up the prescription")
    assert push["message"].startswith("Arrived at pharmacy · ")
    assert (await rt.reminders.get(r.id)).status == "fired"  # type: ignore[union-attr]
    assert await rt.context.get("place") == "work"
    await rt.context.visit("pharmacy")
    assert len(ntfy.bodies) == 1


async def test_leave_reminder_fires_on_a_real_leave_only(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("Buy milk", "leave", place="work")
    t = time.time()
    await rt.context.arrive("work", at=t - 60)
    await rt.context.leave("work", at=t)  # 60 s after arriving: geofence jitter, ignored
    assert ntfy.bodies == []
    await rt.context.leave("work", at=t + 600)
    assert ntfy.find("Buy milk")["message"].startswith("Left Work · ")
    assert (await rt.reminders.get(r.id)).status == "fired"  # type: ignore[union-attr]


async def test_place_reminder_for_several_pending(rt: Runtime, ntfy: NtfyMock) -> None:
    a = await rt.reminders.add("one", "arrive", place="home")
    b = await rt.reminders.add("two", "arrive", place="home")
    await rt.context.arrive("home")
    assert {x["sequence_id"] for x in ntfy.bodies} == {f"rem-{a.id}", f"rem-{b.id}"}


# ----------------------------------------------------------------------------- timed delivery


async def test_time_reminder_fires_via_scheduler_with_signed_buttons(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("Call the bank about the card", "time", due_at=time.time() + 0.05)
    await rt.memory.schedule(r)
    await eventually(lambda: ntfy.publish.called)
    push = ntfy.last()
    assert push["title"] == "⏰ Call the bank about the card"
    assert push["message"] == "Ring: click = done · double = snooze"
    assert push["priority"] == 4 and push["tags"] == ["alarm_clock"] and push["topic"] == "ring"
    assert push["sequence_id"] == f"rem-{r.id}"
    assert push["click"] == "https://hub.test/#/memory"
    done, snooze = push["actions"]
    assert (done["action"], done["label"], done["method"], done["clear"]) == ("http", "Done", "POST", True)
    assert snooze["label"] == "Snooze 10m"
    for button, action in ((done, "done"), (snooze, "snooze")):
        url = urlsplit(button["url"])
        assert f"{url.scheme}://{url.netloc}" == "https://hub.test"
        assert url.path == f"/r/{r.id}/{action}"
        q = parse_qs(url.query)
        assert rt.links.verify(url.path, q["exp"][0], q["sig"][0])
        assert int(q["exp"][0]) > time.time() + 6 * 86400
        other = f"/r/{r.id}/{'snooze' if action == 'done' else 'done'}"
        assert not rt.links.verify(other, q["exp"][0], q["sig"][0])  # a link authorizes one action only
    assert ADMIN not in str(push) and CONTEXT not in str(push)
    assert (await rt.reminders.get(r.id)).status == "fired"  # type: ignore[union-attr]
    # the push goes out first; the follow-up nag is scheduled right after it
    nag = await eventually(lambda: _job(rt, f"nag:{r.id}"))
    assert nag.run_at == pytest.approx(time.time() + NAG_AFTER.total_seconds(), abs=5)


async def _job(rt: Runtime, job_id: str) -> Any:
    return next((j for j in await rt.scheduler.pending() if j.id == job_id), None)


async def test_double_fire_is_idempotent(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("x", "time", due_at=time.time())
    assert await rt.memory.fire(r.id) is not None
    assert await rt.memory.fire(r.id) is None  # timer + restart restore racing
    assert len(ntfy.bodies) == 1


async def test_nag_only_when_unacknowledged(rt: Runtime, ntfy: NtfyMock) -> None:
    r = await rt.reminders.add("Water the plants", "time", due_at=time.time())
    await rt.memory.fire(r.id)
    await rt.memory.clear_attention("reminder")
    await rt.memory.nag(r.id)
    assert ntfy.last()["title"] == "⏰ Still: Water the plants"
    assert ntfy.last()["sequence_id"] == f"rem-{r.id}"
    assert (await rt.store.get(ATTENTION_REMINDER))["ref"] == r.id  # the ring answers it again
    await rt.memory.done(r.id)
    assert f"nag:{r.id}" not in {j.id for j in await rt.scheduler.pending()}
    ntfy.reset()
    await rt.memory.nag(r.id)
    assert ntfy.bodies == []


async def test_reminders_topic_and_clear_on_done(make_runtime: Factory, make_config: Callable[..., Config],
                                                 ntfy: NtfyMock) -> None:
    cfg = make_config(ntfy={"url": "https://ntfy.test", "topic": "ring", "reminders_topic": "ring-rem"})
    rt = await make_runtime(cfg)
    r = await rt.reminders.add("x", "time", due_at=time.time())
    await rt.memory.fire(r.id)
    assert ntfy.last()["topic"] == "ring-rem"
    await rt.memory.done(r.id)
    assert ntfy.clear.called
    assert str(ntfy.clear.calls.last.request.url) == f"https://ntfy.test/ring-rem/rem-{r.id}/clear"
    # confirmations still go to the main topic
    await rt.feedback.say("hi")
    assert ntfy.last()["topic"] == "ring"


async def test_no_public_url_means_no_buttons(make_runtime: Factory, make_config: Callable[..., Config],
                                              ntfy: NtfyMock, tmp_path: Path) -> None:
    cfg = make_config(server={"data_dir": str(tmp_path / "data2"), "admin_token": ADMIN})
    rt = await make_runtime(cfg)
    r = await rt.reminders.add("x", "time", due_at=time.time())
    await rt.memory.fire(r.id)
    assert "actions" not in ntfy.last() and "click" not in ntfy.last()


async def test_reminder_job_survives_restart(make_runtime: Factory, make_config: Callable[..., Config],
                                             ntfy: NtfyMock) -> None:
    cfg = make_config()
    first = await make_runtime(cfg)
    r = await first.reminders.add("after reboot", "time", due_at=time.time() + 0.2)
    await first.memory.schedule(r)
    await first.scheduler.aclose()  # the process dies before it fires
    second = await make_runtime(cfg)
    await second.scheduler.restore()
    await eventually(lambda: ntfy.publish.called)
    assert ntfy.last()["title"] == "⏰ after reboot"
    assert (await second.reminders.get(r.id)).status == "fired"  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- attention layer


async def fired(rt: Runtime, text: str = "Take out the trash") -> str:
    r = await rt.reminders.add(text, "time", due_at=time.time())
    await rt.memory.fire(r.id)
    return r.id


async def test_click_after_fire_marks_done(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    rid = await fired(rt)
    assert await press(engine, "click") == "reminder-done"
    r = await rt.reminders.get(rid)
    assert r is not None and r.status == "done"
    assert ntfy.last()["message"] == "✅ Done · Take out the trash" and ntfy.last()["priority"] == 2
    assert ntfy.clear.called
    assert await rt.store.get(ATTENTION_REMINDER) is None
    assert not [j for j in await rt.scheduler.pending() if rid in j.id]
    events = await rt.store.recent_events(5)
    assert events[0]["detail"] == f"reminder {rid} done" and events[0]["status"] == "ok"
    # the next click is an ordinary click again
    assert await press(engine, "click") == "moment"


async def test_double_after_fire_snoozes(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    rid = await fired(rt)
    assert await press(engine, "double-click") == "reminder-snoozed"
    r = await rt.reminders.get(rid)
    assert r is not None and (r.status, r.trigger, r.snoozes) == ("pending", "time", 1)
    assert r.due_at == pytest.approx(time.time() + SNOOZE.total_seconds(), abs=5)
    jobs = {j.id for j in await rt.scheduler.pending()}
    assert f"reminder:{rid}" in jobs and f"nag:{rid}" not in jobs
    assert ntfy.last()["message"] == "😴 Snoozed 10 min · Take out the trash"
    assert await rt.store.get(ATTENTION_REMINDER) is None


async def test_attention_expires_after_the_ack_window(rt: Runtime, engine: Engine) -> None:
    rid = await fired(rt)
    att = await rt.store.get(ATTENTION_REMINDER)
    await rt.store.set(ATTENTION_REMINDER, {**att, "at": time.time() - rt.config.memory.ack_window_seconds - 1})
    assert await press(engine, "click") == "moment"
    assert (await rt.reminders.get(rid)).status == "fired"  # type: ignore[union-attr]


async def test_attention_falls_through_when_reminder_already_handled(rt: Runtime, engine: Engine) -> None:
    rid = await fired(rt)
    await rt.reminders.mark_done(rid)  # e.g. tapped "Done" in the push without clearing attention
    assert await press(engine, "click") == "moment"
    assert await rt.store.get(ATTENTION_REMINDER) is None


async def test_attention_disabled_with_zero_window(make_runtime: Factory, make_config: Callable[..., Config],
                                                   tmp_path: Path) -> None:
    rt = await make_runtime(make_config(memory={"ack_window_seconds": 0, "markdown_dir": str(tmp_path / "v")}))
    await fired(rt)
    assert await Engine(rt).handle(ring_event("click")) == "moment"


async def test_double_after_capture_undoes_the_note(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "parked next to the blue van")
    (note,) = await rt.notes.recent(1)
    assert await press(engine, "double-click") == "undo"
    assert await rt.notes.get(note.id) is None and await rt.notes.search("blue van") == []
    assert ntfy.last()["message"] == "↩️ Undone (note)"
    assert await press(engine, "double-click") == "list"  # undo is one-shot


async def test_double_after_reminder_capture_cancels_both(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    await hold(engine, "remind me in 5 minutes to flip the laundry")
    (r,) = await rt.reminders.open()
    assert await press(engine, "double-click") == "undo"
    assert (await rt.reminders.get(r.id)).status == "cancelled"  # type: ignore[union-attr]
    assert await rt.notes.get(r.note_id) is None  # type: ignore[arg-type]
    assert f"reminder:{r.id}" not in {j.id for j in await rt.scheduler.pending()}
    assert ntfy.last()["message"] == "↩️ Undone (reminder + note)"


async def test_undo_window_is_short_and_click_never_undoes(rt: Runtime, engine: Engine) -> None:
    await hold(engine, "a thought")
    assert await press(engine, "click") == "moment"  # click after a capture is just a click
    assert len(await rt.notes.recent(5, kinds=["note"])) == 1
    att = await rt.store.get(ATTENTION_CAPTURE)
    await rt.store.set(ATTENTION_CAPTURE, {**att, "at": time.time() - 31})
    assert await press(engine, "double-click") == "list"
    assert len(await rt.notes.recent(5, kinds=["note"])) == 1


# ----------------------------------------------------------------------------- parking memory


async def test_leaving_the_car_saves_the_cars_gps(rt: Runtime, engine: Engine, ntfy: NtfyMock) -> None:
    vehicle = FakeVehicle(VehicleState(online=True, locked=True, lat=41.8781, lon=-87.6298, fetched_at=time.time()))
    rt.vehicle = vehicle  # type: ignore[assignment]
    await rt.context.set("location", {"lat": 1.0, "lon": 1.0, "at": time.time()})  # car wins over phone
    t = time.time()
    await rt.context.arrive("car", at=t - 1800)
    await rt.context.leave("car", at=t)
    assert vehicle.calls == [True]  # allow_stale: a sleeping car's last position is fine
    note = await rt.notes.latest_matching("log", "Parked")
    assert note is not None and note.meta == {"location": {"lat": 41.8781, "lon": -87.6298, "source": "car"}}
    assert note.created_at == t and note.place is None
    await hold(engine, "where did I park")
    assert ntfy.last()["message"].endswith("https://maps.apple.com/?q=Car&ll=41.8781,-87.6298")


async def test_parking_falls_back_to_the_phone(rt: Runtime) -> None:
    rt.vehicle = FakeVehicle(None)  # type: ignore[assignment] # Fleet API unreachable
    t = time.time()
    await rt.context.set("location", {"lat": 40.0, "lon": -88.0, "at": t - 60})
    await rt.context.arrive("car", at=t - 1800)
    await rt.context.leave("car", at=t)
    note = await rt.notes.latest_matching("log", "Parked")
    assert note is not None and note.meta == {"location": {"lat": 40.0, "lon": -88.0, "source": "phone"}}


async def test_parking_without_tesla_uses_phone_and_ignores_stale_fixes(rt: Runtime) -> None:
    assert rt.vehicle is None
    t = time.time()
    await rt.context.set("location", {"lat": 40.0, "lon": -88.0, "at": t - 3600})  # an hour old
    await rt.context.arrive("car", at=t - 1800)
    await rt.context.leave("car", at=t)
    note = await rt.notes.latest_matching("log", "Parked")
    assert note is not None and note.meta == {}
    assert "maps.apple.com" not in await rt.memory.where_parked()


async def test_parking_records_where_you_were_if_known(rt: Runtime) -> None:
    t = time.time()
    await rt.context.arrive("work", at=t - 60)  # geofence fired before the car Bluetooth dropped
    await rt.context.leave("car", at=t)
    note = await rt.notes.latest_matching("log", "Parked")
    assert note is not None and note.place == "work"
    assert (await rt.memory.where_parked()).startswith("🚗 Parked at Work · ")


async def test_car_leave_reminders_fire_too(rt: Runtime, ntfy: NtfyMock) -> None:
    await rt.reminders.add("Grab the gym bag from the trunk", "leave", place="car")
    t = time.time()
    await rt.context.arrive("car", at=t - 900)
    await rt.context.leave("car", at=t)
    assert ntfy.find("Grab the gym bag")["message"].startswith("Left Car · ")
    assert await rt.notes.latest_matching("log", "Parked") is not None


# ----------------------------------------------------------------------------- digest & mirror


async def test_digest_summarizes_today_and_reschedules(make_runtime: Factory, make_config: Callable[..., Config],
                                                       ntfy: NtfyMock, tmp_path: Path) -> None:
    rt = await make_runtime(make_config(memory={"digest_time": "21:30", "markdown_dir": str(tmp_path / "v")}))
    now = time.time()
    yesterday = (rt.now() - timedelta(days=1)).replace(hour=12).timestamp()
    await rt.notes.add("yesterday's thought", created_at=yesterday)
    await rt.notes.add("today's thought", created_at=now)
    await rt.notes.add("Parked", kind="log", created_at=now)  # logs are not captures
    await rt.memory.capture("remind me to renew the passport", said_at=now, place=None, mode=None)
    await rt.reminders.add("done already", "none")
    ntfy.reset()
    await rt.memory.digest()
    push = ntfy.last()
    assert push["title"] == "🌙 Today" and push["tags"] == ["crescent_moon"]
    lines = push["message"].split("\n")
    assert lines[0] == "Captured today: 2"
    assert "• today's thought" in lines and "• remind me to renew the passport" in lines
    assert "Open: 2" in lines and "• Renew the passport" in lines
    assert "yesterday's thought" not in push["message"] and "Parked" not in push["message"]
    job = {j.id: j for j in await rt.scheduler.pending()}["digest"]
    due = datetime.fromtimestamp(job.run_at, rt.config.tz)
    assert (due.hour, due.minute) == (21, 30) and 0 < job.run_at - time.time() <= 86400 + 5


async def test_schedule_digest_cancels_when_unset(rt: Runtime) -> None:
    await rt.scheduler.schedule([{"memory.digest": {}}], timedelta(hours=1), {}, "digest")
    await rt.memory.schedule_digest()  # digest_time is None in the default config
    assert "digest" not in {j.id for j in await rt.scheduler.pending()}


async def test_markdown_mirror_shows_parsed_reminder_text(rt: Runtime, engine: Engine) -> None:
    await rt.context.arrive("work", at=time.time() - 600)
    await hold(engine, "Buy coffee beans")
    await hold(engine, "When I get home take the chicken out")
    await rt.memory.moment(None, at=time.time(), place="work", mode="work")
    vault = rt.config.memory.markdown_dir
    assert vault is not None
    today = rt.now()
    text = (vault / f"{today:%Y-%m-%d}.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == f"# {today:%A, %B %-d}"
    assert any(line.endswith(" Buy coffee beans _(Work)_") and line.startswith("- ") for line in lines)
    assert any(line.startswith("- [ ] ⏰ ") and line.endswith("When you arrive at Home · Take the chicken out _(Work)_")
               for line in lines)
    assert any(line.startswith("- 📍 ") and "Moment at Work" in line for line in lines)
    assert text.count(f"# {today:%A}") == 1  # header written once


async def test_no_markdown_dir_writes_nothing(make_runtime: Factory, make_config: Callable[..., Config],
                                              tmp_path: Path) -> None:
    rt = await make_runtime(make_config(memory={}))
    await rt.memory.capture("hello", said_at=time.time(), place=None, mode=None)
    assert not (tmp_path / "vault").exists()



async def test_capture_after_fire_does_not_steal_click_done(rt: Runtime, engine: Engine) -> None:
    """Review finding: a note saved right after a reminder fired turned click=Done into the mode skill."""
    rid = await fired(rt)
    await hold(engine, "unrelated thought")
    assert await press(engine, "click") == "reminder-done"
    assert (await rt.reminders.get(rid)).status == "done"  # type: ignore[union-attr]


async def test_double_acts_on_the_most_recent_attention(rt: Runtime, engine: Engine) -> None:
    rid = await fired(rt)
    await hold(engine, "oops misheard")
    assert await press(engine, "double-click") == "undo"  # the capture is newer: undo it
    assert (await rt.reminders.get(rid)).status == "fired"  # type: ignore[union-attr]
    assert await press(engine, "double-click") == "reminder-snoozed"  # then the reminder


async def test_snoozing_another_reminder_keeps_this_ones_attention(rt: Runtime, engine: Engine) -> None:
    rid = await fired(rt)
    other = await rt.reminders.add("Other thing", "none")
    await rt.memory.snooze(other.id)
    assert (await rt.store.get(ATTENTION_REMINDER))["ref"] == rid
