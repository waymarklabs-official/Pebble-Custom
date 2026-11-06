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
        skills += [
            {"id": "car", "name": "Car: trunk or climate", "icon": "🚗", "template": "car_smart",
             "description": "At the car (unlocked) → trunk. Away (locked/asleep) → climate on.",
             "phrases": ["car", "do the car thing"]},
            {"id": "car_climate", "name": "Warm up the car", "icon": "🌡️", "template": "car_command",
             "params": {"command": "auto_conditioning_start"},
             "phrases": ["warm up the car", "cool down the car", "precondition the car", "start the car climate"]},
            {"id": "frunk", "name": "Open the frunk", "icon": "🧳", "template": "car_command",
             "params": {"command": "actuate_trunk", "params": {"which_trunk": "front"}},
             "phrases": ["open the frunk", "pop the frunk"]},
        ]
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
