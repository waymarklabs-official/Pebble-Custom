"""Event pipeline: combos -> ordered queue -> rule selection -> step execution -> feedback."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .actions.base import ActionError
from .combos import ComboEngine
from .models import RingEvent
from .rules import matches
from .runtime import Runtime

log = logging.getLogger(__name__)


class Engine:
    """Handles events strictly in arrival order so "triple-click (switch mode), click" is
    always evaluated in the new mode, even if the first rule takes a moment."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        sequences = [r.when.sequence for r in rt.config.rules if r.when.sequence]
        self.combos = ComboEngine(sequences, rt.config.ring.combo_window_ms / 1000, self._enqueue)
        self._queue: asyncio.Queue[RingEvent] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.rt.scheduler.restore()
        self._worker = asyncio.create_task(self._run(), name="ringhub-engine")

    async def stop(self) -> None:
        await self.combos.flush()
        await self._queue.join()
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def submit(self, event: RingEvent) -> None:
        if event.gesture.is_voice:
            # Voice arrives late (after transcription); resolve pending clicks first.
            await self.combos.flush()
            await self._enqueue(event.with_sequence((event.gesture,)))
        else:
            await self.combos.feed(event)

    async def _enqueue(self, event: RingEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error("event queue full; dropping %s", event.delivery_id)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.handle(event)
            except Exception:  # the worker must survive anything a rule does
                log.exception("unhandled error processing %s", event.delivery_id)
            finally:
                self._queue.task_done()

    async def handle(self, event: RingEvent) -> list[str]:
        """Run every matching rule (first match wins unless ``continue: true``)."""
        base = await self.rt.base_vars()
        label = "+".join(g.value for g in event.sequence) or event.gesture.value
        ran: list[str] = []
        for rule in self.rt.config.rules:
            if not matches(rule.when, event, mode=base["mode"], state=base["state"], now=base["now"]):
                continue
            ran.append(rule.name)
            variables: dict[str, Any] = {**base, "event": event.as_template_vars(), "rule": rule.name}
            try:
                await self.rt.executor.run(rule.do, variables)
            except Exception as e:
                await self._report_failure(event, label, rule.name, e)
            else:
                log.info("%s -> %s ok", label, rule.name)
                await self.rt.store.log_event(delivery_id=event.delivery_id, gesture=label,
                                              detail=rule.name, status="ok")
            if not rule.continue_:
                break
            base = await self.rt.base_vars()  # the rule may have changed mode/state
        if not ran:
            log.info("%s (mode=%s): no rule matched", label, base["mode"])
            await self.rt.store.log_event(delivery_id=event.delivery_id, gesture=label,
                                          detail=f"mode={base['mode']}", status="unmatched")
        return ran

    async def _report_failure(self, event: RingEvent, label: str, rule: str, err: Exception) -> None:
        expected = isinstance(err, ActionError)
        if expected:
            log.warning("%s -> %s failed: %s", label, rule, err)
        else:
            log.exception("%s -> %s crashed", label, rule)
        await self.rt.store.log_event(delivery_id=event.delivery_id, gesture=label, detail=rule,
                                      status="error", error=str(err))
        if self.rt.config.notify_on_error and self.rt.notifier.enabled:
            # Silent failure is the worst UX for a screenless button: always say something.
            try:
                await self.rt.notifier.publish(f"{rule}: {err}"[:500], title=f"Ring {label} failed",
                                               tags=["warning"], priority=3)
            except ActionError as notify_err:
                log.warning("could not send failure notification: %s", notify_err)
