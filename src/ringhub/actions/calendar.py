"""Google Calendar actions for skills: hear your agenda, or add an event from a sentence."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from .base import ActionError, Params, Vars, action
from .memory import _event, _said_at, _say

if TYPE_CHECKING:
    from ..runtime import Runtime


class AgendaParams(Params):
    day: Literal["today", "tomorrow", "this week", "next"] = "today"


@action("calendar.agenda", AgendaParams, primary="day")
async def agenda(p: AgendaParams, rt: Runtime, v: Vars) -> Vars:
    if rt.gcal is None:
        raise ActionError("Google Calendar is off (set GOOGLE_CLIENT_ID in .env)")
    kind, day = ("next", "") if p.day == "next" else ("day", p.day)
    text = await rt.memory.agenda(kind, day)
    await _say(rt, v, text, "normal")
    return {"calendar": {"agenda": p.day}}


class AddParams(Params):
    text: str | None = Field(None, max_length=500)
    """A sentence like “dentist Friday at 3”; defaults to the voice transcript."""


@action("calendar.add", AddParams, primary="text")
async def add(p: AddParams, rt: Runtime, v: Vars) -> Vars:
    if rt.gcal is None:
        raise ActionError("Google Calendar is off (set GOOGLE_CLIENT_ID in .env)")
    sentence = p.text if p.text is not None else str(_event(v).get("transcript") or "")
    if not sentence.strip():
        raise ActionError("nothing to add: say what and when, e.g. “dentist Friday at 3”")
    # Reuse the voice path so the rules (and "scratch that") are identical.
    if not sentence.lower().lstrip().startswith(("add ", "put ", "schedule ", "calendar")):
        sentence = f"add {sentence} to my calendar"
    said_at = _said_at(v)
    result = await rt.memory._calendar_capture(sentence, datetime.fromtimestamp(said_at, rt.config.tz),
                                               said_at=said_at, place=v.get("place"), mode=v.get("mode"),
                                               audio=None, source=_event(v).get("id"))
    if result is None:
        raise ActionError("that didn't read as an event; say what and when, e.g. “dentist Friday at 3”")
    await _say(rt, v, result.message, "normal")
    return {"calendar": {"kind": result.kind}}
