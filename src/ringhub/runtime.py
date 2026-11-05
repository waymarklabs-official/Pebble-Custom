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
from .config import Config, NtfyConfig
from .devices import DeviceRegistry
from .executor import Executor
from .intents import IntentRouter
from .llm import LlmRouter
from .store import Job, Store

log = logging.getLogger(__name__)

MODE_KEY = "mode"


class Notifier:
    """Publishes to ntfy. Doubles as the hub's feedback channel (phone, Pebble watch, desktop)."""

    def __init__(self, cfg: NtfyConfig | None, client: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._cfg is not None

    async def publish(
        self,
        message: str,
        *,
        title: str | None = None,
        topic: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        click: str | None = None,
        actions: str | None = None,
    ) -> None:
        if self._cfg is None:
            raise ActionError("notify used but no 'ntfy' section is configured")
        headers: dict[str, str] = {}
        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = str(priority)
        if tags:
            headers["Tags"] = ",".join(tags)
        if click:
            headers["Click"] = click
        if actions:
            headers["Actions"] = actions
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token.get_secret_value()}"
        url = f"{self._cfg.url.rstrip('/')}/{topic or self._cfg.topic}"
        try:
            resp = await self._client.post(url, content=message.encode("utf-8"), headers=headers, timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ActionError(f"ntfy publish failed: {e!r}") from None


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
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient) -> None:
        self.config = config
        self.store = store
        self.http = client
        self.devices = DeviceRegistry(config, client, store)
        self.notifier = Notifier(config.ntfy, client)
        self.scheduler = Scheduler(store)
        self.executor = Executor(self)
        self.scheduler.bind(self.executor, self.base_vars)
        self.audio_dir = config.server.data_dir / "audio"
        self.intents = IntentRouter(config.intents)
        self.llm: LlmRouter | None = LlmRouter(config.llm, config.intents) if config.llm else None
        self.tesla: TeslaClient | None = TeslaClient(config.tesla, store) if config.tesla else None

    def now(self) -> datetime:
        return datetime.now(self.config.tz)

    async def mode(self) -> str:
        mode = await self.store.get(MODE_KEY)
        return mode if mode in self.config.modes else self.config.modes[0]

    async def base_vars(self) -> Vars:
        return {
            "now": self.now(),
            "mode": await self.mode(),
            "state": await self.store.all_state(),
        }

    async def aclose(self) -> None:
        await self.scheduler.aclose()
        await self.devices.aclose()
        if self.tesla is not None:
            await self.tesla.aclose()
