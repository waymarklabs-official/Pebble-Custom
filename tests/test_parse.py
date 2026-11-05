from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ringhub.memory.parse import parse_reminder

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 3, 14, 10, tzinfo=TZ)  # Tuesday 14:10
PLACES = {"home": "home", "work": "work", "the office": "work", "car": "car", "gym": "gym"}


def p(text: str):  # type: ignore[no-untyped-def]
    return parse_reminder(text, NOW, PLACES)


@pytest.mark.parametrize("text,at,body", [
    ("Remind me in 20 minutes to check the oven", (11, 3, 14, 30), "Check the oven"),
    ("remind me in an hour to move the car", (11, 3, 15, 10), "Move the car"),
    ("remind me in half an hour to stretch", (11, 3, 14, 40), "Stretch"),
    ("Remind me at 7 to call mom.", (11, 3, 19, 0), "Call mom"),
    ("remind me at 7:45 am to take my meds", (11, 4, 7, 45), "Take my meds"),
    ("remind me at 3 pm to submit the form", (11, 3, 15, 0), "Submit the form"),
    ("remind me at 9 am to submit the form", (11, 4, 9, 0), "Submit the form"),
    ("remind me at noon to eat", (11, 4, 12, 0), "Eat"),
    ("Tomorrow morning remind me to bring the charger", (11, 4, 9, 0), "Bring the charger"),
    ("remind me tomorrow at 8 to water plants", (11, 4, 8, 0), "Water plants"),
    ("remind me tonight to lock the shed", (11, 3, 20, 0), "Lock the shed"),
    ("remind me tonight at 9 to take out trash", (11, 3, 21, 0), "Take out trash"),
    ("remind me on friday to pay rent", (11, 6, 9, 0), "Pay rent"),
    ("remind me friday evening to call grandma", (11, 6, 19, 0), "Call grandma"),
    ("remind me next tuesday to renew parking", (11, 10, 9, 0), "Renew parking"),
    ("don't let me forget to text Sam later", (11, 3, 17, 10), "Text sam"),
])
def test_time_reminders(text: str, at: tuple[int, int, int, int], body: str) -> None:
    r = p(text)
    assert r is not None and r.trigger.kind == "time"
    assert r.trigger.at == datetime(2026, at[0], at[1], at[2], at[3], tzinfo=TZ)
    assert r.text == body


@pytest.mark.parametrize("text,kind,place,body", [
    ("When I get home take the chicken out of the freezer", "arrive", "home", "Take the chicken out of the freezer"),
    ("remind me when I get to the office to print the slides", "arrive", "work", "Print the slides"),
    ("next time I'm at the gym bring a towel", "arrive", "gym", "Bring a towel"),
    ("when I'm in the car remind me to grab the package", "arrive", "car", "Grab the package"),
    ("remind me when I leave work to buy milk", "leave", "work", "Buy milk"),
    ("before I leave the house check the stove", "leave", "home", "Check the stove"),
    ("when I get back put the laundry in the dryer", "arrive", "home", "Put the laundry in the dryer"),
])
def test_place_reminders(text: str, kind: str, place: str, body: str) -> None:
    r = p(text)
    assert r is not None
    assert (r.trigger.kind, r.trigger.place, r.trigger.place_known) == (kind, place, True)
    assert r.text == body


def test_unknown_place_is_flagged_not_dropped() -> None:
    r = p("when I get to the pharmacy pick up the prescription")
    assert r is not None and r.trigger.place == "pharmacy" and r.trigger.place_known is False
    assert r.text == "Pick up the prescription"


def test_reminder_without_trigger_goes_to_list() -> None:
    r = p("remind me to renew my passport")
    assert r is not None and r.trigger.kind == "none" and r.text == "Renew my passport"


@pytest.mark.parametrize("text", [
    "the meeting is at 3 tomorrow",          # a fact, not a request
    "great idea for the garage shelves",
    "parked on level 3 row G",
    "remind me",                              # nothing to remind
])
def test_plain_notes_are_not_reminders(text: str) -> None:
    assert p(text) is None


def test_time_already_passed_today_falls_back_to_list() -> None:
    r = p("remind me this morning to call the bank")
    assert r is not None and r.trigger.kind == "none"
