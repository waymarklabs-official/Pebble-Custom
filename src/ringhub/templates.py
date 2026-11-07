"""Skill templates: a small form in the dashboard compiles to plain steps on the server.

Keeping the compiler here (not in the browser) means the dashboard can never produce steps the
engine can't run, and improving a template upgrades every skill built from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalog import Skill
from .devices import TvCommand


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_P):
    pass


class MomentP(_P):
    label: str | None = Field(None, max_length=80, title="Label (optional)",
                              description="Saved instead of “Moment at <place>”, e.g. “Great idea”")


class NoteP(_P):
    text: str = Field(min_length=1, max_length=120, title="What to log",
                      description="e.g. “Took meds ✓”. Ask “did I take my meds?” later to check.")


class RecallP(_P):
    count: int = Field(3, ge=1, le=10, title="How many notes")


class NotifyP(_P):
    message: str = Field(min_length=1, max_length=500, title="Message")
    title: str | None = Field(None, max_length=80, title="Title (optional)")
    topic: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$", title="ntfy topic",
                              description="Who receives it. Leave blank to send to your own topic.")
    priority: int = Field(3, ge=1, le=5, title="Priority",
                          description="1-2 silent · 3 normal · 4-5 time-sensitive (breaks through Focus)")
    tags: list[str] = Field(default_factory=list, title="Tags", description="ntfy emoji tags, e.g. raised_hand")


class WebhookP(_P):
    url: str = Field(pattern=r"^https?://", title="URL")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field("POST", title="Method")
    headers: dict[str, str] = Field(default_factory=dict, title="Headers")
    body: dict[str, Any] | None = Field(None, title="JSON body (optional)")


class CarActionP(_P):
    action: Literal[
        "wake", "lock", "unlock", "climate_on", "climate_off", "defrost_on", "defrost_off", "set_temp",
        "frunk", "trunk", "honk", "flash", "charge_port_open", "charge_port_close", "charge_start", "charge_stop",
        "charge_limit", "sentry_on", "sentry_off", "vent_windows", "close_windows",
        "seat_heat_on", "seat_heat_off", "wheel_heat_on", "wheel_heat_off", "garage",
    ] = Field(title="What to do")
    value: float | None = Field(None, title="Value (optional)",
                                description="Temperature (°F or °C), charge limit (%) or seat heat 1-3. "
                                            "Or put {value:number} in a phrase, e.g. “set the car to {value:number}”.")
    require_near: bool | None = Field(None, title="Only when I'm at the car",
                                      description="Default: on for unlock, trunk, frunk, vent and garage.")


class ShortcutP(_P):
    name: str = Field(min_length=1, max_length=100, title="Shortcut name",
                      description="Exactly as it's spelled in the Shortcuts app, e.g. “Lock Car”.")
    message: str | None = Field(None, max_length=200, title="Notification text (optional)",
                                description="Default: “Tap to run <name>”.")


def shortcut_url(name: str) -> str:
    return "shortcuts://run-shortcut?name=" + quote(name, safe="")


def _shortcut(p: ShortcutP, s: Skill) -> list[dict[str, Any]]:
    """iOS never lets anything run a Shortcut unseen, so the press sends a push that runs it in one tap."""
    url = shortcut_url(p.name)
    label = f"Run {p.name}"
    return [{"notify": {"message": p.message or f"Tap to run “{p.name}”", "title": f"▶️ {p.name}",
                        "priority": 4, "click": url,
                        "actions": [{"action": "view", "label": label if len(label) <= 40 else "Run it",
                                     "url": url, "clear": True}]}}]


class CalendarAgendaP(_P):
    day: Literal["today", "tomorrow", "this week", "next"] = Field("today", title="Which events",
                                                                   description="“next” = your next event")


class CarSmartP(_P):
    unlocked: Literal["trunk", "frunk", "nothing"] = Field(
        "trunk", title="When unlocked (you're at the car)", description="Phone Key unlocks the car as you walk up.")
    locked: Literal["precondition", "nothing"] = Field(
        "precondition", title="When locked or asleep (you're away)",
        description="Starts climate; wakes the car if needed.")


class CarCommandP(_P):
    command: str = Field(pattern=r"^[a-z_]{3,64}$", title="Fleet API command",
                         description="e.g. door_lock, flash_lights, honk_horn, charge_port_door_open")
    params: dict[str, Any] = Field(default_factory=dict, title="Parameters (JSON)")


class TvP(_P):
    command: TvCommand = Field(title="Button")
    target: str = Field("here", title="Which TV", description="A device or room name, “all”, or “here”")


class CustomP(_P):
    pass


@dataclass(frozen=True)
class TemplateSpec:
    id: str
    name: str
    icon: str
    description: str
    params: type[_P]
    compile: Callable[[Any, Skill], list[dict[str, Any]]]
    needs: tuple[str, ...] = field(default_factory=tuple)
    """Config sections required (shown as a warning in the dashboard)."""
    confirm: bool = True
    """Whether the engine should send a short confirmation after it runs."""


def _builtin(skill: Skill) -> list[dict[str, Any]]:
    return {
        "capture": [{"memory.capture": {}}],
        "journal": [{"memory.journal": {}}],
        "where_am_i": [{"mode.where_am_i": {}}],
        "nothing": [{"noop": {}}],
    }[skill.id]


TEMPLATES: dict[str, TemplateSpec] = {t.id: t for t in [
    TemplateSpec("moment", "Mark this moment", "📍", "Saves a timestamped note with where you are.",
                 MomentP, lambda p, s: [{"memory.moment": {"label": p.label}}], confirm=False),
    TemplateSpec("note", "Log something", "✔️", "Logs a fixed entry (\"Took meds ✓\"). Answers \"did I…?\" later.",
                 NoteP, lambda p, s: [{"memory.log": {"text": p.text}}], confirm=False),
    TemplateSpec("list", "What's on my list", "📋", "Pushes your open reminders and list items.",
                 NoParams, lambda p, s: [{"memory.list": {}}], confirm=False),
    TemplateSpec("next", "What's next", "⏭️", "Pushes your next timed reminder.",
                 NoParams, lambda p, s: [{"memory.next": {}}], confirm=False),
    TemplateSpec("recall", "Read back recent notes", "📝", "Pushes your last few captures.",
                 RecallP, lambda p, s: [{"memory.recall": {"count": p.count}}], confirm=False),
    TemplateSpec("notify", "Send a push", "📣",
                 "Push a message to any ntfy topic, e.g. \"running late\" to a family topic.",
                 NotifyP, lambda p, s: [{"notify": {k: v for k, v in p.model_dump().items() if v not in (None, [])}}],
                 needs=("ntfy",)),
    TemplateSpec("shortcut", "Run an iPhone Shortcut", "▶️",
                 "Sends a push that runs one of your Shortcuts when you tap it (iOS never runs them unseen). "
                 "E.g. the Tesla app's Lock action.", ShortcutP, _shortcut, needs=("ntfy",), confirm=False),
    TemplateSpec("webhook", "Call a webhook", "🔗", "HTTP request to anything: Home Assistant, IFTTT, your Pi.",
                 WebhookP, lambda p, s: [{"http": {"url": p.url, "method": p.method, "headers": p.headers,
                                                   **({"json": p.body} if p.body is not None else {})}}]),
    TemplateSpec("car_smart", "Car: trunk or climate", "🚗",
                 "Unlocked (you're at the car) → open the trunk. Locked/away → start climate.",
                 CarSmartP, lambda p, s: [{"car.smart": {"unlocked": p.unlocked, "locked": p.locked}}],
                 needs=("tesla",), confirm=False),
    TemplateSpec("car_action", "Car", "🚘", "Wake, lock, climate, temperature, trunk, charging, sentry, windows, "
                 "seat heat, garage door. Unlock/trunk/garage only run when you're at the car.",
                 CarActionP, lambda p, s: [{"car.command": {k: v for k, v in p.model_dump().items()
                                                            if v is not None}}], needs=("tesla",), confirm=False),
    TemplateSpec("car_status", "Car status", "🔋", "Battery, range, locks and climate (never wakes the car).",
                 NoParams, lambda p, s: [{"car.status": {}}], needs=("tesla",), confirm=False),
    TemplateSpec("car_command", "Car command", "🔋", "Any Fleet API command (lock, honk, charge port…).",
                 CarCommandP, lambda p, s: [{"tesla": {"command": p.command, "params": p.params}}], needs=("tesla",)),
    TemplateSpec("calendar_agenda", "What's on my calendar", "📅", "Pushes today's (or tomorrow's) events "
                 "from Google Calendar.", CalendarAgendaP,
                 lambda p, s: [{"calendar.agenda": {"day": p.day}}], needs=("google",), confirm=False),
    TemplateSpec("tv", "TV remote", "📺", "Play/pause, power, volume on Android/Fire TV or Roku.",
                 TvP, lambda p, s: [{"tv": {"command": p.command.value, "target": p.target}}], needs=("devices",)),
    TemplateSpec("custom", "Custom steps", "🧩", "Advanced: write steps by hand (JSON).",
                 CustomP, lambda p, s: list(s.steps)),
    TemplateSpec("builtin", "Built-in", "⭐", "Part of the default gesture grammar.",
                 NoParams, lambda p, s: _builtin(s), confirm=False),
]}


class TemplateError(ValueError):
    pass


def compile_skill(skill: Skill) -> list[dict[str, Any]]:
    spec = TEMPLATES.get(skill.template)
    if spec is None:
        raise TemplateError(f"unknown template {skill.template!r}")
    try:
        params = spec.params.model_validate(skill.params)
    except ValidationError as e:
        raise TemplateError(f"skill {skill.id!r}: {e}") from None
    if skill.template == "custom" and not skill.steps:
        raise TemplateError(f"skill {skill.id!r}: custom skills need at least one step")
    return spec.compile(params, skill)


def should_confirm(skill: Skill) -> bool:
    spec = TEMPLATES.get(skill.template)
    return bool(spec and spec.confirm)


def describe() -> list[dict[str, Any]]:
    """Template catalog for the dashboard: forms are generated from these JSON schemas."""
    return [{"id": t.id, "name": t.name, "icon": t.icon, "description": t.description,
             "schema": t.params.model_json_schema(), "needs": list(t.needs)}
            for t in TEMPLATES.values() if t.id != "builtin"]
