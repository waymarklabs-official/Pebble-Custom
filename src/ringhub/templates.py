"""Skill templates: a small form in the dashboard compiles to plain steps on the server.

Keeping the compiler here (not in the browser) means the dashboard can never produce steps the
engine can't run, and improving a template upgrades every skill built from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalog import Skill
from .devices import TvCommand


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_P):
    pass


class MomentP(_P):
    label: str | None = Field(None, max_length=80, description="Optional label, e.g. 'Great idea'")


class NoteP(_P):
    text: str = Field(min_length=1, max_length=120, description="What to log, e.g. 'Took meds ✓'")


class RecallP(_P):
    count: int = Field(3, ge=1, le=10)


class NotifyP(_P):
    message: str = Field(min_length=1, max_length=500)
    title: str | None = Field(None, max_length=80)
    topic: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$",
                              description="ntfy topic (blank = your own topic)")
    priority: int = Field(3, ge=1, le=5)
    tags: list[str] = Field(default_factory=list)


class WebhookP(_P):
    url: str = Field(pattern=r"^https?://")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None


class CarSmartP(_P):
    unlocked: Literal["trunk", "frunk", "nothing"] = "trunk"
    locked: Literal["precondition", "nothing"] = "precondition"


class CarCommandP(_P):
    command: str = Field(pattern=r"^[a-z_]{3,64}$")
    params: dict[str, Any] = Field(default_factory=dict)


class TvP(_P):
    command: TvCommand
    target: str = "here"


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
    TemplateSpec("recall", "Read back recent notes", "📝", "Pushes your last few captures.",
                 RecallP, lambda p, s: [{"memory.recall": {"count": p.count}}], confirm=False),
    TemplateSpec("notify", "Send a push", "📣",
                 "Push to an ntfy topic — hand raise, bathroom request, \"running late\".",
                 NotifyP, lambda p, s: [{"notify": {k: v for k, v in p.model_dump().items() if v not in (None, [])}}],
                 needs=("ntfy",)),
    TemplateSpec("webhook", "Call a webhook", "🔗", "HTTP request to anything: Home Assistant, IFTTT, your Pi.",
                 WebhookP, lambda p, s: [{"http": {"url": p.url, "method": p.method, "headers": p.headers,
                                                   **({"json": p.body} if p.body is not None else {})}}]),
    TemplateSpec("car_smart", "Car: trunk or climate", "🚗",
                 "Unlocked (you're at the car) → open the trunk. Locked/away → start climate.",
                 CarSmartP, lambda p, s: [{"car.smart": {"unlocked": p.unlocked, "locked": p.locked}}],
                 needs=("tesla",), confirm=False),
    TemplateSpec("car_command", "Car command", "🔋", "Any Fleet API command (lock, honk, charge port…).",
                 CarCommandP, lambda p, s: [{"tesla": {"command": p.command, "params": p.params}}], needs=("tesla",)),
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
