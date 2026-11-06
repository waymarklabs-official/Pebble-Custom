"""Sensible defaults written to an empty catalog on first boot. Everything is editable afterwards."""

from __future__ import annotations

from typing import Any

from .catalog import Catalog, Mode, Place, Skill
from .config import Config


def default_catalog(config: Config) -> dict[str, list[dict[str, Any]]]:
    car = config.tesla is not None
    places = [
        {"id": "home", "name": "Home", "icon": "🏠", "aliases": ["home", "the house", "house"]},
        {"id": "work", "name": "Work", "icon": "💼", "aliases": ["work", "the office", "office"]},
        {"id": "car", "name": "Car", "icon": "🚗", "aliases": ["the car", "my car", "car"]},
    ]
    skills: list[dict[str, Any]] = [
        {"id": "capture", "name": "Smart capture", "icon": "🎙️", "template": "builtin",
         "description": "Talk: commands run, questions get answered, reminders get scheduled, "
                        "everything else is saved as a note."},
        {"id": "journal", "name": "Journal", "icon": "📓", "template": "builtin",
         "description": "Raw capture with audio, no interpretation."},
        {"id": "where_am_i", "name": "Where am I?", "icon": "🧭", "template": "builtin",
         "description": "Says the active mode and what each gesture does. Twice quickly: switch mode."},
        {"id": "nothing", "name": "Nothing", "icon": "⛔", "template": "builtin",
         "description": "Deliberately does nothing (e.g. to block a gesture in Sleep mode)."},
        {"id": "moment", "name": "Mark this moment", "icon": "📍", "template": "moment",
         "description": "Timestamped note with where you are. Find it later in Memory."},
        {"id": "list", "name": "What's on my list", "icon": "📋", "template": "list"},
        {"id": "recall", "name": "Read back recent notes", "icon": "📝", "template": "recall", "params": {"count": 3}},
        {"id": "took_meds", "name": "Took meds ✓", "icon": "💊", "template": "note",
         "params": {"text": "Took meds ✓"}, "phrases": ["took my meds", "i took my meds", "meds taken"]},
        {"id": "locked_up", "name": "Locked up ✓", "icon": "🔒", "template": "note",
         "params": {"text": "Locked the door ✓"}, "phrases": ["locked the door", "i locked the door"]},
        {"id": "next", "name": "What's next", "icon": "⏭️", "template": "next"},
        {"id": "where_parked", "name": "Where did I park?", "icon": "🅿️", "template": "custom",
         "steps": [{"car.where": {}}]},
    ]
    if car:
        skills += car_skills()
    leaving_click = "car" if car else "locked_up"
    modes: list[dict[str, Any]] = [
        {"id": "sleep", "name": "Sleep", "icon": "🌙", "color": "#5b5bd6", "feedback": "quiet",
         "when": {"focus": ["sleep"]},
         "bindings": {"click": "moment", "double-click": "nothing"}},
        {"id": "driving", "name": "In the car", "icon": "🚗", "color": "#0f9d58", "feedback": "quiet",
         "when": {"places": ["car"]},
         "bindings": {"click": "moment", "double-click": "list"}},
        {"id": "work", "name": "Work", "icon": "💼", "color": "#e8710a", "feedback": "quiet",
         "when": {"places": ["work"]},
         "bindings": {"click": "moment", "double-click": "next"}},
        {"id": "leaving", "name": "Heading out", "icon": "🚪", "color": "#d93025",
         "when": {"recently": {"event": "leave", "place": "home", "within_minutes": 10}},
         "bindings": {"click": leaving_click, "double-click": "locked_up" if car else "list"}},
        {"id": "morning", "name": "Morning", "icon": "☀️", "color": "#f9ab00",
         "when": {"places": ["home"], "time": {"after": "05:00", "before": "10:30"}},
         "bindings": {"click": "took_meds", "double-click": "car" if car else "list"}},
        {"id": "home", "name": "Home", "icon": "🏠", "color": "#1a73e8",
         "when": {"places": ["home"]},
         "bindings": {"click": "car" if car else "moment", "double-click": "list"}},
        {"id": "everywhere", "name": "Everywhere", "icon": "◎", "color": "#6b7cff",
         "bindings": {"click": "moment", "double-click": "list", "triple-click": "where_am_i",
                      "single-click-hold": "capture", "double-click-hold": "journal"}},
    ]
    return {"place": places, "skill": skills, "mode": modes}


def _car(sid: str, name: str, icon: str, action: str, phrases: list[str], **params: Any) -> dict[str, Any]:
    return {"id": sid, "name": name, "icon": icon, "template": "car_action",
            "params": {"action": action, **params}, "phrases": phrases}


def car_skills() -> list[dict[str, Any]]:
    """The car's default skills. Added once when the car is connected; yours to edit after that."""
    return [
        {"id": "car", "name": "Car: trunk or climate", "icon": "🚗", "template": "car_smart",
         "description": "At the car (unlocked) → trunk. Away (locked/asleep) → climate on.",
         "phrases": ["car", "do the car thing"]},
        {"id": "car_status", "name": "How's the car?", "icon": "🔋", "template": "car_status",
         "phrases": ["how's the car", "how is the car", "car status", "is the car locked", "car battery",
                     "how much battery does the car have", "how much charge does the car have",
                     "what's the car's battery", "how charged is the car"]},
        _car("car_climate", "Warm up the car", "🌡️", "climate_on",
             ["warm up the car", "cool down the car", "precondition the car", "start the car climate",
              "turn on the car climate", "turn on the car's climate", "start the car"]),
        _car("car_climate_off", "Car climate off", "❄️", "climate_off",
             ["turn off the car climate", "turn off the car's climate", "stop the car climate", "stop the car"]),
        _car("car_temp", "Set the car temperature", "🌡️", "set_temp",
             ["set the car to {value:number}", "set the car temperature to {value:number}",
              "car temperature {value:number}", "make the car {value:number}"]),
        _car("car_defrost", "Defrost the car", "🧊", "defrost_on",
             ["defrost the car", "defrost the windshield", "defrost the windows"]),
        _car("car_lock", "Lock the car", "🔒", "lock", ["lock the car", "lock my car", "lock the tesla"]),
        _car("car_unlock", "Unlock the car", "🔓", "unlock", ["unlock the car", "unlock my car", "unlock the tesla"]),
        _car("frunk", "Open the frunk", "🧳", "frunk", ["open the frunk", "pop the frunk"]),
        _car("car_trunk", "Open the trunk", "🧳", "trunk", ["open the trunk", "pop the trunk", "open the hatch",
                                                            "close the trunk"]),
        _car("car_honk", "Honk", "📯", "honk", ["honk the horn", "honk the car", "honk"]),
        _car("car_flash", "Flash the lights", "💡", "flash", ["flash the lights", "flash the car lights",
                                                              "find my car"]),
        _car("car_port_open", "Open the charge port", "🔌", "charge_port_open",
             ["open the charge port", "open the charging port"]),
        _car("car_port_close", "Close the charge port", "🔌", "charge_port_close",
             ["close the charge port", "close the charging port"]),
        _car("car_charge_start", "Start charging", "⚡", "charge_start", ["start charging", "start charging the car"]),
        _car("car_charge_stop", "Stop charging", "⚡", "charge_stop", ["stop charging", "stop charging the car"]),
        _car("car_charge_limit", "Set the charge limit", "🔋", "charge_limit",
             ["set the charge limit to {value:number}", "set the charge limit to {value:number} percent",
              "charge limit {value:number}", "charge the car to {value:number}",
              "charge the car to {value:number} percent"]),
        _car("car_sentry_on", "Sentry on", "🛡️", "sentry_on", ["turn on sentry mode", "turn on sentry", "sentry on"]),
        _car("car_sentry_off", "Sentry off", "🛡️", "sentry_off",
             ["turn off sentry mode", "turn off sentry", "sentry off"]),
        _car("car_vent", "Vent the windows", "🪟", "vent_windows",
             ["vent the windows", "vent the car windows", "crack the windows"]),
        _car("car_close_windows", "Close the windows", "🪟", "close_windows",
             ["close the windows", "close the car windows"]),
        _car("car_seat_heat", "Seat heater on", "💺", "seat_heat_on",
             ["turn on my seat heater", "turn on the seat heater", "heat my seat", "warm my seat"]),
        _car("car_wheel_heat", "Steering wheel heater on", "🔥", "wheel_heat_on",
             ["turn on the steering wheel heater", "heat the steering wheel", "warm the steering wheel"]),
        _car("car_garage", "Garage door", "🏠", "garage",
             ["open the garage", "close the garage", "open the garage door", "close the garage door",
              "garage door", "garage"]),
    ]


async def ensure_skills(catalog: Catalog, skills: list[dict[str, Any]]) -> list[str]:
    """Add the skills that don't exist yet (by id), after the existing ones. Never overwrites."""
    have = {s.id for s in await catalog.skills()}
    added = []
    pos = len(have)
    for s in skills:
        if s["id"] not in have:
            await catalog.put(Skill.model_validate(s), position=pos)
            pos += 1
            added.append(s["id"])
    return added


async def seed_if_empty(catalog: Catalog, config: Config) -> bool:
    if not await catalog.is_empty():
        return False
    data = default_catalog(config)
    for pos, p in enumerate(data["place"]):
        await catalog.put(Place.model_validate(p), position=pos)
    for pos, s in enumerate(data["skill"]):
        await catalog.put(Skill.model_validate(s), position=pos)
    for pos, m in enumerate(data["mode"]):
        await catalog.put(Mode.model_validate(m), position=pos)
    return True
