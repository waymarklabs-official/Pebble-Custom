"""Google Calendar: parsing what you say, the API client, and capture/undo/agenda by voice."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest

from ringhub.actions.base import ActionError
from ringhub.config import Config
from ringhub.engine import Engine
from ringhub.gcal import ACCOUNT_KEY, REFRESH_KEY, _email_from_id_token
from ringhub.memory.calendar import describe_when, format_agenda, is_agenda_question, parse_event
from ringhub.memory.service import ATTENTION_CAPTURE
from ringhub.runtime import Runtime

from .conftest import NtfyMock, ring_event

Factory = Callable[..., Any]
TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=TZ)        # a Friday morning
API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE = {"google": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "gsecret"}}


# ----------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(("said", "title", "when"), [
    ("add dentist to my calendar friday at 3", "Dentist", "Fri Nov 13 3 pm-4 pm"),
    ("Put lunch with Sam on my calendar tomorrow at noon", "Lunch with Sam", "tomorrow 12 pm-1 pm"),
    ("add to my calendar team sync monday at 9 for 30 minutes", "Team sync", "Monday 9 am-9:30 am"),
    ("schedule a haircut next tuesday at 4pm", "Haircut", "Tuesday 4 pm-5 pm"),
    ("add mom's birthday to my calendar on sunday", "Mom's birthday", "Sunday (all day)"),
    ("calendar: flight to Denver tomorrow from 6 to 9 am", "Flight to Denver", "tomorrow 6 am-9 am"),
    ("add project review to my calendar from 11 to 1 tomorrow", "Project review", "tomorrow 11 am-1 pm"),
    ("add standup to my calendar monday from 9:30 to 10", "Standup", "Monday 9:30 am-10 am"),
    ("add dinner to my calendar saturday from 7pm to 9pm", "Dinner", "tomorrow 7 pm-9 pm"),
    ("add call with Ann to my calendar tomorrow at 2 for an hour", "Call with Ann", "tomorrow 2 pm-3 pm"),
    ("add gym to my calendar tomorrow at 6 am for half an hour", "Gym", "tomorrow 6 am-6:30 am"),
    ("add yoga to the calendar in 2 hours", "Yoga", "today 12 pm-1 pm"),
    ("please put Dr. Patel on my calendar tuesday morning", "Dr Patel", "Tuesday 9 am-10 am"),
    ("add vacation to my calendar monday all day", "Vacation", "Monday (all day)"),
])
def test_parse_event(said: str, title: str, when: str) -> None:
    ev = parse_event(said, NOW)
    assert ev is not None and ev.start is not None
    assert (ev.title, describe_when(ev, NOW)) == (title, when)


def test_all_day_events_end_the_next_day_and_timed_ones_carry_the_zone() -> None:
    allday = parse_event("add vacation to my calendar monday", NOW)
    assert allday is not None and allday.all_day
    assert (allday.start, allday.end) == (date(2026, 11, 9), date(2026, 11, 10))
    timed = parse_event("add dentist to my calendar tomorrow at 3", NOW)
    assert timed is not None and isinstance(timed.start, datetime) and timed.start.tzinfo is not None


@pytest.mark.parametrize("said", ["remind me at 5 to call Bob", "schedule something", "the meeting went well",
                                  "what's on my calendar", "schedule a call"])
def test_not_calendar_requests(said: str) -> None:
    assert parse_event(said, NOW) is None


def test_calendar_request_without_a_date_is_flagged_not_dropped() -> None:
    ev = parse_event("add pick up keys to my calendar", NOW)
    assert ev is not None and ev.title == "Pick up keys" and ev.start is None


@pytest.mark.parametrize(("said", "expect"), [
    ("what's on my calendar", ("day", "today")), ("what's on my calendar tomorrow", ("day", "tomorrow")),
    ("what do I have today", ("day", "today")), ("what's my next meeting", ("next", "")),
    ("do I have meetings this week", ("day", "this week")), ("what's on my list", None),
    ("what did I say about the calendar", None),
])
def test_agenda_questions(said: str, expect: tuple[str, str] | None) -> None:
    assert is_agenda_question(said) == expect


def test_format_agenda() -> None:
    events: list[dict[str, object]] = [
        {"summary": "Standup", "start": {"dateTime": "2026-11-06T09:30:00-05:00"}},
        {"summary": "Mom's birthday", "start": {"date": "2026-11-06"}},
    ]
    assert format_agenda(events, "Today", NOW) == "📅 Today:\n• 9:30 am Standup\n• all day: Mom's birthday"
    assert format_agenda([], "Tomorrow", NOW) == "📅 Tomorrow: nothing on your calendar."


# ----------------------------------------------------------------------------- client


def id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def test_email_from_id_token() -> None:
    assert _email_from_id_token(id_token("me@gmail.com")) == "me@gmail.com"
    assert _email_from_id_token("garbage") is None and _email_from_id_token(None) is None


class Google:
    def __init__(self, router: Any) -> None:
        self.token = router.post(TOKEN).mock(return_value=httpx.Response(200, json={
            "access_token": "g-access", "expires_in": 3600}))
        self.insert = router.post(API).mock(return_value=httpx.Response(200, json={
            "id": "evt1", "htmlLink": "https://calendar.google.com/x"}))
        self.list = router.get(API).mock(return_value=httpx.Response(200, json={"items": []}))
        self.delete = router.delete(url__regex=rf"^{API}/[^/]+$").mock(return_value=httpx.Response(204))
        self.revoke = router.post("https://oauth2.googleapis.com/revoke").mock(return_value=httpx.Response(200))

    def inserted(self) -> dict[str, Any]:
        return dict(json.loads(self.insert.calls.last.request.content))


@pytest.fixture
def google(ntfy: NtfyMock) -> Google:
    return Google(ntfy.router)


@pytest.fixture
async def rt(make_runtime: Factory, make_config: Callable[..., Config], google: Google) -> Runtime:
    runtime: Runtime = await make_runtime(make_config(GOOGLE, timezone="America/New_York"))
    await runtime.store.set(REFRESH_KEY, "g-refresh")
    return runtime


async def test_connect_flow(make_runtime: Factory, make_config: Callable[..., Config], google: Google) -> None:
    rt: Runtime = await make_runtime(make_config(GOOGLE))
    assert rt.gcal is not None and not await rt.gcal.connected()
    url = rt.gcal.authorize_url("st", "https://hub.test/oauth/google/callback")
    q = parse_qs(urlsplit(url).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/calendar.events" in q["scope"][0].split()
    google.token.mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600, "id_token": id_token("me@gmail.com")}))
    assert await rt.gcal.exchange_code("4/code", "https://hub.test/oauth/google/callback") == "me@gmail.com"
    sent = parse_qs(google.token.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"] and sent["client_secret"] == ["gsecret"]
    assert await rt.store.get(REFRESH_KEY) == "r" and await rt.store.get(ACCOUNT_KEY) == "me@gmail.com"
    await rt.gcal.disconnect()
    assert not await rt.gcal.connected() and google.revoke.called


async def test_connect_without_refresh_token_explains(make_runtime: Factory, make_config: Callable[..., Config],
                                                      google: Google) -> None:
    rt: Runtime = await make_runtime(make_config(GOOGLE))
    assert rt.gcal is not None
    with pytest.raises(ActionError, match="no refresh token"):
        await rt.gcal.exchange_code("4/code", "https://hub.test/oauth/google/callback")


async def test_revoked_access_is_explained(rt: Runtime, google: Google) -> None:
    google.token.mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    assert rt.gcal is not None
    with pytest.raises(ActionError, match="reconnect"):
        await rt.gcal.events(NOW, NOW)


async def test_add_event_bodies(rt: Runtime, google: Google) -> None:
    assert rt.gcal is not None
    start = datetime(2026, 11, 7, 15, 0, tzinfo=TZ)
    await rt.gcal.add_event("Dentist", start, start.replace(hour=16))
    assert google.inserted()["start"] == {"dateTime": "2026-11-07T15:00:00-05:00", "timeZone": "America/New_York"}
    assert google.insert.calls.last.request.headers["Authorization"] == "Bearer g-access"
    await rt.gcal.add_event("Trip", date(2026, 11, 9), date(2026, 11, 10))
    assert google.inserted()["start"] == {"date": "2026-11-09"} and google.inserted()["end"] == {"date": "2026-11-10"}


# ----------------------------------------------------------------------------- by voice


@pytest.fixture
def engine(rt: Runtime) -> Engine:
    return Engine(rt)


async def hold(engine: Engine, text: str) -> str:
    return await engine.handle(ring_event("single-click-hold", transcript=text))


async def test_adding_by_voice_then_undo(rt: Runtime, engine: Engine, google: Google, ntfy: NtfyMock) -> None:
    await hold(engine, "add dentist to my calendar tomorrow at 3")
    body = google.inserted()
    assert body["summary"] == "Dentist"
    assert body["description"].startswith("Added by ringhub from:")
    assert ntfy.last()["message"].startswith("📅 Dentist · tomorrow 3 pm-4 pm")
    att = await rt.store.get(ATTENTION_CAPTURE)
    assert att["ref"] == {"calendar": "evt1", "title": "Dentist"}
    await hold(engine, "scratch that")
    assert google.delete.called and str(google.delete.calls.last.request.url).endswith("/events/evt1")
    assert ntfy.last()["message"] == "↩️ Undone (calendar event)"


async def test_double_click_undoes_the_event_too(rt: Runtime, engine: Engine, google: Google) -> None:
    await hold(engine, "put lunch on my calendar tomorrow at noon")
    assert await engine.handle(ring_event("double-click")) == "undo"
    assert google.delete.called


async def test_no_date_keeps_the_words_as_a_note(rt: Runtime, engine: Engine, google: Google,
                                                 ntfy: NtfyMock) -> None:
    await hold(engine, "add pick up keys to my calendar")
    assert not google.insert.called
    assert ntfy.last()["message"].startswith("📅 No date heard")
    assert (await rt.notes.recent(1))[0].text == "add pick up keys to my calendar"


async def test_google_failure_keeps_the_words_as_a_note(rt: Runtime, engine: Engine, google: Google,
                                                        ntfy: NtfyMock) -> None:
    google.insert.mock(return_value=httpx.Response(500, text="backend error"))
    await hold(engine, "add dentist to my calendar tomorrow at 3")
    assert ntfy.last()["message"].startswith("📅 Couldn't add it to your calendar")
    assert (await rt.notes.recent(1))[0].text == "add dentist to my calendar tomorrow at 3"


async def test_not_connected_keeps_the_words_as_a_note(rt: Runtime, engine: Engine, google: Google,
                                                       ntfy: NtfyMock) -> None:
    await rt.store.delete(REFRESH_KEY)
    await hold(engine, "add dentist to my calendar tomorrow at 3")
    assert not google.insert.called
    assert "isn't connected" in ntfy.last()["message"]


async def test_reminders_still_work_with_the_calendar_on(rt: Runtime, engine: Engine, google: Google) -> None:
    await hold(engine, "remind me tomorrow at 9 to call mom")
    assert not google.insert.called
    assert [r.text for r in await rt.reminders.pending_timed()] == ["Call mom"]


async def test_agenda_by_voice(rt: Runtime, engine: Engine, google: Google, ntfy: NtfyMock) -> None:
    google.list.mock(return_value=httpx.Response(200, json={"items": [
        {"summary": "Standup", "start": {"dateTime": "2026-11-07T09:30:00-05:00"}},
        {"summary": "Cancelled one", "status": "cancelled", "start": {"dateTime": "2026-11-07T10:00:00-05:00"}},
    ]}))
    await hold(engine, "what's on my calendar tomorrow")
    params = google.list.calls.last.request.url.params
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
    assert ntfy.last()["message"] == "📅 Tomorrow:\n• 9:30 am Standup"


async def test_calendar_agenda_skill(rt: Runtime, google: Google, ntfy: NtfyMock) -> None:
    from ringhub.catalog import Skill

    await rt.catalog.put(Skill.model_validate({"id": "agenda", "name": "Agenda", "icon": "📅",
                                               "template": "calendar_agenda", "params": {"day": "next"}}))
    await rt.run_skill("agenda", await rt.base_vars())
    assert google.list.calls.last.request.url.params["maxResults"] == "1"
    assert ntfy.last()["message"] == "📅 Next: nothing on your calendar."


async def test_calendar_questions_are_left_alone_when_google_is_off(make_runtime: Factory,
                                                                    make_config: Callable[..., Config]) -> None:
    rt: Runtime = await make_runtime(make_config())
    assert rt.gcal is None
    result = await rt.memory.capture("add dentist to my calendar tomorrow at 3", said_at=NOW.timestamp(),
                                     place=None, mode=None)
    assert result.kind == "note"            # no Google configured: an ordinary note, as before
