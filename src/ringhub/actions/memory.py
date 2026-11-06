"""Memory-aid, mode and smart-car actions (the building blocks of the default skills)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..feedback import cap
from ..geo import distance_m, fresh_fix
from ..templating import parse_duration
from .base import ActionError, NoParams, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime


def _event(v: Vars) -> dict[str, Any]:
    ev = v.get("event")
    return ev if isinstance(ev, dict) else {}


def _said_at(v: Vars) -> float:
    at: Any = _event(v).get("at")
    if isinstance(at, datetime):
        return at.timestamp()
    if isinstance(at, int | float):
        return float(at)
    if isinstance(at, str):
        try:
            return datetime.fromisoformat(at).timestamp()
        except ValueError:
            pass
    return time.time()


async def _say(rt: Runtime, v: Vars, message: str, level: str = "normal", **kw: Any) -> None:
    await rt.feedback.say(message, level=cap(level, str(v.get("feedback", "normal"))), **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- capture


class CaptureParams(Params):
    text: str | None = None
    """Defaults to the voice transcript."""


@action("memory.capture", CaptureParams, primary="text")
async def capture(p: CaptureParams, rt: Runtime, v: Vars) -> Vars:
    """Smart capture: skill phrase -> question -> reminder -> note."""
    ev = _event(v)
    text = p.text if p.text is not None else str(ev.get("transcript") or "")
    match = (await rt.phrase_index()).match(text) if text.strip() else None
    if match is not None:
        v.update(match.slots)
        await rt.run_skill(match.skill_id, v, via=f"phrase “{match.phrase}”")
        return {"capture": {"kind": "skill", "skill": match.skill_id}}
    result = await rt.memory.capture(text, said_at=_said_at(v), place=v.get("place"), mode=v.get("mode"),
                                     audio=ev.get("audio_path"), source=ev.get("id"))
    level = "normal" if result.kind != "empty" else "important"
    await _say(rt, v, result.message, level)
    return {"capture": {"kind": result.kind, "note": result.note.id if result.note else None,
                        "reminder": result.reminder.id if result.reminder else None}}


@action("memory.journal", CaptureParams, primary="text")
async def journal(p: CaptureParams, rt: Runtime, v: Vars) -> Vars:
    ev = _event(v)
    text = p.text if p.text is not None else str(ev.get("transcript") or "")
    result = await rt.memory.journal(text, said_at=_said_at(v), place=v.get("place"), mode=v.get("mode"),
                                     audio=ev.get("audio_path"), source=ev.get("id"))
    await _say(rt, v, result.message, "quiet")
    return {"note": result.note.id if result.note else None}


class MomentParams(Params):
    label: str | None = Field(None, max_length=120)


@action("memory.moment", MomentParams, primary="label")
async def moment(p: MomentParams, rt: Runtime, v: Vars) -> Vars:
    note = await rt.memory.moment(p.label, at=_said_at(v), place=v.get("place"), mode=v.get("mode"))
    await _say(rt, v, f"📍 {note.text} · {rt.now().strftime('%-I:%M %p').lower()}", "quiet")
    return {"note": note.id}


class LogParams(Params):
    text: str = Field(min_length=1, max_length=200)


@action("memory.log", LogParams, primary="text")
async def log_entry(p: LogParams, rt: Runtime, v: Vars) -> Vars:
    note = await rt.memory.log(p.text, at=_said_at(v), place=v.get("place"), mode=v.get("mode"))
    await _say(rt, v, f"✔ {p.text}", "quiet")
    return {"note": note.id}


@action("memory.list", NoParams)
async def list_(p: NoParams, rt: Runtime, v: Vars) -> None:
    await _say(rt, v, await rt.memory.list_summary(), "normal")


@action("memory.next", NoParams)
async def next_(p: NoParams, rt: Runtime, v: Vars) -> None:
    await _say(rt, v, await rt.memory.next_summary(), "normal")


class RecallParams(Params):
    count: int = Field(3, ge=1, le=10)
    query: str | None = None


@action("memory.recall", RecallParams, primary="query")
async def recall(p: RecallParams, rt: Runtime, v: Vars) -> None:
    msg = await rt.memory.search_summary(p.query, p.count) if p.query else await rt.memory.recent_summary(p.count)
    await _say(rt, v, msg, "normal")


class RemindParams(Params):
    text: str = Field(min_length=1, max_length=300)
    after: str | None = None
    """Duration like "20m"; or use arrive/leave for place reminders; neither = list item."""
    arrive: str | None = None
    leave: str | None = None


@action("memory.remind", RemindParams, primary="text")
async def remind(p: RemindParams, rt: Runtime, v: Vars) -> Vars:
    """Explicit reminder for custom skills (voice reminders are parsed by memory.capture)."""
    if sum(x is not None for x in (p.after, p.arrive, p.leave)) > 1:
        raise ActionError("memory.remind takes at most one of after / arrive / leave")
    if p.after is not None:
        due = time.time() + parse_duration(p.after).total_seconds()
        r = await rt.reminders.add(p.text, "time", due_at=due)
        await rt.memory.schedule(r)
    elif p.arrive or p.leave:
        r = await rt.reminders.add(p.text, "arrive" if p.arrive else "leave", place=p.arrive or p.leave)
    else:
        r = await rt.reminders.add(p.text, "none")
    return {"reminder": r.id}


@action("memory.digest", NoParams)
async def digest(p: NoParams, rt: Runtime, v: Vars) -> None:
    await rt.memory.digest()


# ---------------------------------------------------------------------------- reminders


class ReminderRef(Params):
    id: str = Field(pattern=r"^[0-9a-f]{6,32}$")


@action("reminder.fire", ReminderRef, primary="id")
async def reminder_fire(p: ReminderRef, rt: Runtime, v: Vars) -> None:
    await rt.memory.fire(p.id)


@action("reminder.nag", ReminderRef, primary="id")
async def reminder_nag(p: ReminderRef, rt: Runtime, v: Vars) -> None:
    await rt.memory.nag(p.id)


# ---------------------------------------------------------------------------- modes


@action("mode.where_am_i", NoParams)
async def where_am_i(p: NoParams, rt: Runtime, v: Vars) -> Vars:
    """Triple-click: say the active mode and what the buttons do. Again within 10 s: next mode."""
    last = await rt.store.get("where_am_i_at")
    now = time.time()
    if isinstance(last, int | float) and now - float(last) < 10:
        mode = await rt.cycle_override()
        await rt.store.delete("where_am_i_at")
        await rt.feedback.say(await rt.describe_mode(), title=f"{mode.icon} Switched to {mode.name}",
                              level="normal", tags=["arrows_counterclockwise"])
        return {"mode": mode.id}
    await rt.store.set("where_am_i_at", now)
    res = await rt.resolution()
    await rt.feedback.say(await rt.describe_mode() + "\nTriple again within 10 s to switch.",
                          title=f"{res.mode.icon} {res.mode.name} — {res.reason}", level="normal")
    return {}


class OverrideParams(Params):
    mode: str | None = None
    """Mode id; omit to clear the override and go back to automatic."""
    minutes: int | None = Field(None, ge=1, le=24 * 60)


@action("mode.override", OverrideParams, primary="mode")
async def mode_override(p: OverrideParams, rt: Runtime, v: Vars) -> Vars:
    await rt.set_override(p.mode, timedelta(minutes=p.minutes) if p.minutes else None)
    return {"mode": p.mode}


class SkillRunParams(Params):
    skill: str


@action("skill.run", SkillRunParams, primary="skill")
async def skill_run(p: SkillRunParams, rt: Runtime, v: Vars) -> None:
    await rt.run_skill(p.skill, v, via="skill.run")


# ---------------------------------------------------------------------------- car


class CarSmartParams(Params):
    unlocked: str = Field("trunk", pattern="^(trunk|frunk|nothing)$")
    locked: str = Field("precondition", pattern="^(precondition|nothing)$")


NEAR_CAR_METRES = 150


@action("car.smart", CarSmartParams)
async def car_smart(p: CarSmartParams, rt: Runtime, v: Vars) -> Vars:
    """Unlocked (phone key says you're at the car) -> trunk; locked or asleep -> climate."""
    if rt.tesla is None or rt.vehicle is None:
        raise ActionError("car skill needs the 'tesla' section in config")
    state = await rt.vehicle.state()
    if state is None:
        raise ActionError("couldn't reach the car (Fleet API error) — nothing done")
    phone = fresh_fix(await rt.context.get("location"))
    far = None
    if phone and state.lat is not None and state.lon is not None:
        far = distance_m(phone[0], phone[1], state.lat, state.lon) > NEAR_CAR_METRES
    if state.online and state.locked is False:
        if far:
            # Unlocked but your phone is elsewhere (walk-away lock off?): never open it remotely.
            await _say(rt, v, "🚗 Car is unlocked but you're not near it — not opening anything. "
                              "Lock it from the Tesla app.", "important")
            return {"car": {"did": "nothing", "state": state.summary, "far": True}}
        if state.user_present or (state.shift_state not in (None, "P")):
            await _say(rt, v, "🚗 You're in the car — not opening anything.", "normal")
            return {"car": {"did": "nothing", "state": state.summary}}
        if p.unlocked == "nothing":
            await _say(rt, v, "🚗 Car is unlocked.", "normal")
            return {"car": {"did": "nothing", "state": state.summary}}
        which = "rear" if p.unlocked == "trunk" else "front"
        await rt.tesla.command("actuate_trunk", {"which_trunk": which}, wake=False)
        rt.vehicle.invalidate()
        await _say(rt, v, f"🚗 {'Trunk' if which == 'rear' else 'Frunk'} opening", "normal")
        return {"car": {"did": f"{which}_trunk", "state": state.summary}}
    if p.locked == "nothing":
        await _say(rt, v, f"🚗 Car is {state.summary}.", "normal")
        return {"car": {"did": "nothing", "state": state.summary}}
    await rt.tesla.command("auto_conditioning_start", {}, wake=not state.online)
    rt.vehicle.invalidate()
    await _say(rt, v, f"🚗 Climate on (car was {state.summary})", "normal")
    return {"car": {"did": "precondition", "state": state.summary}}


@action("car.where", NoParams)
async def car_where(p: NoParams, rt: Runtime, v: Vars) -> None:
    await _say(rt, v, await rt.memory.where_parked(), "normal")
