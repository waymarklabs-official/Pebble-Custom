"""Long-lived services shared by actions: HTTP client, store, devices, notifier, scheduler."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from .actions.base import ActionError, Vars
from .actions.tesla import TeslaClient
from .catalog import Catalog, Mode
from .config import Config
from .context import Context
from .devices import DeviceRegistry
from .executor import Executor
from .feedback import Feedback, cap
from .links import LinkSigner
from .llm import LlmRouter
from .memory.notes import Notes
from .memory.reminders import Reminders
from .memory.service import Memory
from .modes import Resolution, binding, resolve
from .notifier import Notifier
from .phrases import PhraseIndex
from .store import Job, Store
from .templates import TemplateError, compile_skill, should_confirm
from .vehicle import Vehicle

log = logging.getLogger(__name__)

OVERRIDE_KEY = "mode_override"


class Scheduler:
    """Persistent delayed step lists (sleep timers, reminders, "remind me when I'm in the car")."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._executor: Executor | None = None
        self._live_vars: Callable[[], Awaitable[Vars]] | None = None

    def bind(self, executor: Executor, live_vars: Callable[[], Awaitable[Vars]]) -> None:
        """``live_vars`` supplies now/mode/state at fire time, so a job scheduled 25 minutes
        ago reports the state as it is *now*, not as it was when it was scheduled."""
        self._executor = executor
        self._live_vars = live_vars

    async def restore(self) -> None:
        for job in await self._store.jobs():
            self._spawn(job)
        if self._tasks:
            log.info("restored %d scheduled job(s)", len(self._tasks))

    async def schedule(self, steps: list[dict[str, Any]], delay: timedelta, variables: Vars,
                       job_id: str | None = None) -> Job:
        job = Job(
            id=job_id or uuid.uuid4().hex[:12],
            run_at=time.time() + delay.total_seconds(),
            steps=steps,
            vars={k: v for k, v in variables.items() if not k.startswith("_")},
        )
        self._cancel_task(job.id)
        await self._store.put_job(job)
        self._spawn(job)
        return job

    async def cancel(self, job_id: str) -> bool:
        self._cancel_task(job_id)
        return await self._store.delete_job(job_id)

    async def pending(self) -> list[Job]:
        return await self._store.jobs()

    def _cancel_task(self, job_id: str) -> None:
        task = self._tasks.pop(job_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _spawn(self, job: Job) -> None:
        self._tasks[job.id] = asyncio.get_running_loop().create_task(self._run(job), name=f"job:{job.id}")

    async def _run(self, job: Job) -> None:
        try:
            await asyncio.sleep(max(0.0, job.run_at - time.time()))
            await self._store.delete_job(job.id)
            self._tasks.pop(job.id, None)
            assert self._executor is not None and self._live_vars is not None
            variables = {**job.vars, **await self._live_vars(), "job": {"id": job.id}}
            await self._executor.run(job.steps, variables)
            await self._store.log_event(delivery_id=None, gesture=None, detail=f"job {job.id}",
                                        status="ok")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a failing job must never take the scheduler down
            log.exception("scheduled job %s failed", job.id)
            await self._store.log_event(delivery_id=None, gesture=None, detail=f"job {job.id}",
                                        status="error", error=str(e))

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()


class Runtime:
    """Everything an action can touch. Built once per process by the app lifespan."""

    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient, *, link_secret: bytes) -> None:
        self.config = config
        self.store = store
        self.http = client
        self.devices = DeviceRegistry(config, client, store)
        self.notifier = Notifier(config.ntfy, client)
        self.feedback = Feedback(self.notifier)
        self.links = LinkSigner(link_secret, config.server.public_url)
        self.scheduler = Scheduler(store)
        self.executor = Executor(self)
        self.scheduler.bind(self.executor, self.base_vars)
        self.audio_dir = config.server.data_dir / "audio"
        self.catalog = Catalog(store)
        self.context = Context(store)
        self.notes = Notes(store)
        self.reminders = Reminders(store)
        self.memory = Memory(self)
        self.context.on_place_event(self.memory.on_place_event)
        self.llm: LlmRouter | None = LlmRouter(config.llm) if config.llm else None
        self.tesla: TeslaClient | None = TeslaClient(config.tesla, store) if config.tesla else None
        self.vehicle: Vehicle | None = (
            Vehicle(self.tesla, config.tesla.state_ttl_seconds) if self.tesla and config.tesla else None)
        self._phrases: tuple[int, PhraseIndex] | None = None

    def now(self) -> datetime:
        return datetime.now(self.config.tz)

    # ------------------------------------------------------------------ modes

    async def resolution(self, ctx: dict[str, Any] | None = None, at: datetime | None = None) -> Resolution:
        modes = await self.catalog.modes()
        return resolve(modes, ctx if ctx is not None else await self.context.snapshot(),
                       at or self.now(), await self.store.get(OVERRIDE_KEY))

    async def set_override(self, mode_id: str | None, duration: timedelta | None = None) -> None:
        if mode_id is None:
            await self.store.delete(OVERRIDE_KEY)
            return
        mode = await self.catalog.mode(mode_id)
        if mode is None or not mode.enabled:
            raise ActionError(f"unknown or disabled mode {mode_id!r}")
        await self.store.set(OVERRIDE_KEY, {
            "mode": mode_id,
            "place": await self.context.get("place"),
            "expires_at": time.time() + duration.total_seconds() if duration else None,
        })

    async def cycle_override(self) -> Mode:
        modes = [m for m in await self.catalog.modes() if m.enabled]
        current = (await self.resolution()).mode
        idx = next((i for i, m in enumerate(modes) if m.id == current.id), -1)
        nxt = modes[(idx + 1) % len(modes)]
        await self.set_override(nxt.id)
        return nxt

    async def describe_mode(self, res: Resolution | None = None) -> str:
        res = res or await self.resolution()
        modes = await self.catalog.modes()
        lines = []
        for key, label in (("click", "click"), ("double-click", "double"), ("single-click-hold", "hold"),
                           ("double-click-hold", "click-hold")):
            sid, _ = binding(modes, res.mode, key)
            skill = await self.catalog.skill(sid) if sid else None
            lines.append(f"{label}: {skill.icon + ' ' + skill.name if skill else '—'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ skills

    async def phrase_index(self) -> PhraseIndex:
        version = self.catalog.version
        if self._phrases is None or self._phrases[0] != version:
            entries = [(s.id, ph) for s in await self.catalog.skills() for ph in s.phrases]
            self._phrases = (version, PhraseIndex(entries))
        return self._phrases[1]

    async def run_skill(self, skill_id: str, variables: Vars, *, via: str = "") -> Vars:
        skill = await self.catalog.skill(skill_id)
        if skill is None:
            raise ActionError(f"skill {skill_id!r} no longer exists")
        try:
            steps = compile_skill(skill)
        except TemplateError as e:
            raise ActionError(str(e)) from None
        variables = {**variables, "skill": {"id": skill.id, "name": skill.name, "via": via}}
        depth = int(variables.get("_depth", 0)) + 1
        try:
            result = await self.executor.run(steps, variables, depth=depth)
        except ActionError as e:
            raise ActionError(f"{skill.icon} {skill.name}: {e}") from e
        if should_confirm(skill):
            await self.feedback.say(f"{skill.icon} {skill.name}",
                                    level=cap("quiet", str(variables.get("feedback", "normal"))))
        return result

    # ------------------------------------------------------------------ vars

    async def base_vars(self) -> Vars:
        ctx = await self.context.snapshot()
        res = await self.resolution(ctx)
        return {
            "now": self.now(),
            "mode": res.mode.id,
            "mode_name": res.mode.name,
            "feedback": res.mode.feedback,
            "place": ctx.get("place"),
            "context": ctx,
            "state": await self.store.all_state(),
        }

    async def aclose(self) -> None:
        await self.scheduler.aclose()
        await self.devices.aclose()
        if self.tesla is not None:
            await self.tesla.aclose()
