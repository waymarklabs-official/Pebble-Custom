"""General-purpose actions: feedback, HTTP, flow control, modes and state."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import ConfigDict, Field, field_validator

from ..config import normalize_steps
from ..templating import parse_duration
from .base import ActionError, NoParams, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- feedback


class NotifyParams(Params):
    message: str
    title: str | None = None
    topic: str | None = None
    priority: int | None = Field(None, ge=1, le=5)
    tags: list[str] | None = None
    click: str | None = None
    actions: list[dict[str, Any]] | None = Field(None, max_length=3)
    """ntfy action objects, e.g. [{"action": "view", "label": "Open", "url": "https://..."}]."""


@action("notify", NotifyParams, primary="message")
async def notify(p: NotifyParams, rt: Runtime, v: Vars) -> None:
    await rt.notifier.publish(p.message, title=p.title, topic=p.topic, priority=p.priority,
                              tags=p.tags, click=p.click, actions=p.actions)


class LogParams(Params):
    message: str


@action("log", LogParams, primary="message")
async def log_(p: LogParams, rt: Runtime, v: Vars) -> None:
    log.info("rule log: %s", p.message)


# ---------------------------------------------------------------------------- http


class HttpParams(Params):
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = Field(None, alias="json")
    body: str | None = None
    timeout: float = Field(10, gt=0, le=120)
    save_as: str | None = None
    """Store the response (parsed JSON if possible) in this variable for later steps."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, coerce_numbers_to_str=True)


@action("http", HttpParams, primary="url")
async def http(p: HttpParams, rt: Runtime, v: Vars) -> Vars | None:
    kwargs: dict[str, Any] = {"headers": p.headers, "timeout": p.timeout}
    if p.json_body is not None:
        kwargs["json"] = p.json_body
    elif p.body is not None:
        kwargs["content"] = p.body.encode()
    try:
        resp = await rt.http.request(p.method, p.url, **kwargs)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ActionError(f"http {p.method} {p.url}: {e!r}") from None
    if not p.save_as:
        return None
    try:
        data: Any = resp.json()
    except ValueError:
        data = resp.text
    return {p.save_as: data}


# ---------------------------------------------------------------------------- flow


class WaitParams(Params):
    duration: str | float

    @field_validator("duration")
    @classmethod
    def _limit(cls, v: str | float) -> str | float:
        if parse_duration(v).total_seconds() > 60:
            # Events are handled in order, one at a time; long waits would stall the ring.
            raise ValueError("wait is capped at 60s; use 'schedule' for longer delays")
        return v


@action("wait", WaitParams, primary="duration")
async def wait(p: WaitParams, rt: Runtime, v: Vars) -> None:
    await asyncio.sleep(parse_duration(p.duration).total_seconds())


class ParallelParams(Params):
    do: list[Any]


@action("parallel", ParallelParams, primary="do", steps=("do",))
async def parallel(p: ParallelParams, rt: Runtime, v: Vars) -> None:
    """Run each step concurrently, e.g. pause every TV at once instead of one by one."""
    depth = v.get("_depth", 0) + 1
    results = await asyncio.gather(
        *(rt.executor.run([s], dict(v), depth=depth) for s in normalize_steps(p.do)),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise ActionError("; ".join(str(e) for e in errors))


class ScheduleParams(Params):
    after: str | float
    do: list[Any]
    id: str | None = None
    """Re-scheduling with the same id replaces the pending job (e.g. one sleep timer)."""

    @field_validator("after")
    @classmethod
    def _valid(cls, v: str | float) -> str | float:
        parse_duration(v)
        return v


@action("schedule", ScheduleParams, steps=("do",))
async def schedule(p: ScheduleParams, rt: Runtime, v: Vars) -> Vars:
    job = await rt.scheduler.schedule(normalize_steps(p.do), parse_duration(p.after), v, p.id)
    return {"job": {"id": job.id, "run_at": job.run_at}}


class CancelParams(Params):
    id: str


@action("schedule.cancel", CancelParams, primary="id")
async def schedule_cancel(p: CancelParams, rt: Runtime, v: Vars) -> Vars:
    return {"cancelled": await rt.scheduler.cancel(p.id)}


# ---------------------------------------------------------------------------- state


class StateSetParams(Params):
    key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    value: Any


@action("state.set", StateSetParams)
async def state_set(p: StateSetParams, rt: Runtime, v: Vars) -> Vars:
    if p.key in ("mode_override", "attention", "where_am_i_at") or p.key.startswith("ctx."):
        raise ActionError(f"state key {p.key!r} is managed by the hub")
    await rt.store.set(p.key, p.value)
    state = dict(v.get("state", {}))
    state[p.key] = p.value
    return {"state": state}


class StateKeyParams(Params):
    key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")


@action("state.toggle", StateKeyParams, primary="key")
async def state_toggle(p: StateKeyParams, rt: Runtime, v: Vars) -> Vars:
    value = not bool(await rt.store.get(p.key, False))
    await rt.store.set(p.key, value)
    state = dict(v.get("state", {}))
    state[p.key] = value
    return {"state": state, "toggled": value}


@action("state.delete", StateKeyParams, primary="key")
async def state_delete(p: StateKeyParams, rt: Runtime, v: Vars) -> None:
    await rt.store.delete(p.key)


class CounterParams(Params):
    key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    by: int = 1


@action("state.increment", CounterParams, primary="key")
async def state_increment(p: CounterParams, rt: Runtime, v: Vars) -> Vars:
    """Tally anything with a click: water glasses, interruptions, coffees, baby feeds."""
    value = int(await rt.store.get(p.key, 0) or 0) + p.by
    await rt.store.set(p.key, value)
    state = dict(v.get("state", {}))
    state[p.key] = value
    return {"state": state, "count": value}


# ---------------------------------------------------------------------------- exec


class ExecParams(Params):
    argv: list[str] = Field(min_length=1)
    timeout: float = Field(30, gt=0, le=600)
    save_as: str | None = None


@action("exec", ExecParams)
async def exec_(p: ExecParams, rt: Runtime, v: Vars) -> Vars | None:
    """Run a local program (no shell). Disabled unless ``allow_exec: true``."""
    if not rt.config.allow_exec:
        raise ActionError("exec is disabled (allow_exec: false)")
    proc = await asyncio.create_subprocess_exec(
        *p.argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), p.timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ActionError(f"exec {p.argv[0]} timed out") from None
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise ActionError(f"exec {p.argv[0]} exited {proc.returncode}: {text[-300:]}")
    return {p.save_as: text} if p.save_as else None


@action("noop", NoParams)
async def noop(p: NoParams, rt: Runtime, v: Vars) -> None:
    """Swallow a gesture on purpose (e.g. disable click in 'sleep' mode)."""
