"""Event pipeline: combos -> ordered queue -> attention -> mode -> binding -> skill -> feedback."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from .actions.base import ActionError
from .combos import ComboEngine
from .feedback import cap
from .models import Gesture, RingEvent
from .modes import binding
from .runtime import Runtime

log = logging.getLogger(__name__)

LATE_DELIVERY_SECONDS = 60
"""Voice older than this is filed under the place it was *said*, not where it was received."""

_LABEL = {
    "click": "click", "double-click": "double", "triple-click": "triple",
    "single-click-hold": "hold", "double-click-hold": "click-hold",
}


def label(key: str) -> str:
    return "+".join(_LABEL.get(p, p) for p in key.split("+"))


class Engine:
    """Handles events strictly in arrival order, so "triple (switch mode), click" always sees
    the new mode even if the first skill takes a moment."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._window = rt.config.ring.combo_window_ms / 1000
        self.combos = ComboEngine([], self._window, self._enqueue)
        self._combo_version = -1
        self._queue: asyncio.Queue[RingEvent] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.rt.scheduler.restore()
        await self.rt.memory.schedule_digest()
        self._worker = asyncio.create_task(self._run(), name="ringhub-engine")

    async def stop(self) -> None:
        await self.combos.flush()
        await self._queue.join()
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def _sync_combos(self) -> None:
        if self._combo_version == self.rt.catalog.version:
            return
        sequences = {tuple(Gesture(g) for g in key.split("+"))
                     for m in await self.rt.catalog.modes() for key in m.bindings if "+" in key}
        await self.combos.flush()
        self.combos = ComboEngine(sequences, self._window, self._enqueue)
        self._combo_version = self.rt.catalog.version

    async def submit(self, event: RingEvent) -> None:
        await self._sync_combos()
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
            except Exception:  # the worker must survive anything a skill does
                log.exception("unhandled error processing %s", event.delivery_id)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ handling

    async def handle(self, event: RingEvent) -> str:
        """Process one (combo-resolved) event. Returns a short outcome string for logs/tests."""
        rt = self.rt
        key = "+".join(g.value for g in (event.sequence or (event.gesture,)))
        said_at = event.occurred_at.timestamp()
        ctx = await rt.context.snapshot()
        if event.gesture.is_voice and time.time() - said_at > LATE_DELIVERY_SECONDS:
            for k in ("place", "focus"):
                ctx[k] = await rt.context.value_at(k, said_at)

        if key in ("click", "double-click"):
            handled = await self._attention(key, event)
            if handled is not None:
                return handled

        res = await rt.resolution(ctx)
        modes = await rt.catalog.modes()
        skill_id, _ = binding(modes, res.mode, key)
        if skill_id is None and event.gesture.is_voice:
            skill_id = "capture" if event.gesture is Gesture.HOLD else "journal"  # voice is never dropped
        if skill_id is None:
            await rt.store.log_event(delivery_id=event.delivery_id, gesture=label(key),
                                     detail=f"{res.mode.id}: unbound", status="unmatched")
            await rt.feedback.say(f"{res.mode.icon} {res.mode.name}: nothing on {label(key)}",
                                  level=cap("quiet", res.mode.feedback))
            return "unbound"

        variables: dict[str, Any] = {
            "now": rt.now(),
            "event": event.as_template_vars(),
            "mode": res.mode.id,
            "mode_name": res.mode.name,
            "feedback": res.mode.feedback,
            "place": ctx.get("place"),
            "context": ctx,
            "state": await rt.store.all_state(),
        }
        detail = f"{res.mode.id}: {skill_id}"
        try:
            await rt.run_skill(skill_id, variables, via=f"{label(key)} in {res.mode.name}")
        except Exception as e:
            await self._report_failure(event, key, detail, e)
            return f"error: {e}"
        log.info("%s -> %s ok", label(key), detail)
        await rt.store.log_event(delivery_id=event.delivery_id, gesture=label(key), detail=detail, status="ok")
        return skill_id

    async def _attention(self, key: str, event: RingEvent) -> str | None:
        """Right after a reminder fires (or a capture is saved), the ring answers *that*."""
        rt = self.rt
        att = await rt.memory.attention()
        if att is None:
            return None
        if att.get("kind") == "reminder":
            rid = str(att.get("ref"))
            if key == "click":
                r = await rt.memory.done(rid)
                if r is not None:
                    await rt.feedback.say(f"✅ Done · {r.text}", level="quiet")
                    await rt.store.log_event(delivery_id=event.delivery_id, gesture="click",
                                             detail=f"reminder {rid} done", status="ok")
                    return "reminder-done"
            else:
                r = await rt.memory.snooze(rid)
                if r is not None:
                    await rt.feedback.say(f"😴 Snoozed 10 min · {r.text}", level="quiet")
                    await rt.store.log_event(delivery_id=event.delivery_id, gesture="double",
                                             detail=f"reminder {rid} snoozed", status="ok")
                    return "reminder-snoozed"
            await rt.memory.clear_attention()
            return None
        if att.get("kind") == "capture" and key == "double-click" and isinstance(att.get("ref"), dict):
            msg = await rt.memory.undo_last_capture(att["ref"])
            await rt.feedback.say(msg, level="quiet")
            await rt.store.log_event(delivery_id=event.delivery_id, gesture="double", detail="undo capture",
                                     status="ok")
            return "undo"
        return None

    async def _report_failure(self, event: RingEvent, key: str, detail: str, err: Exception) -> None:
        if isinstance(err, ActionError):
            log.warning("%s -> %s failed: %s", label(key), detail, err)
        else:
            log.exception("%s -> %s crashed", label(key), detail)
        await self.rt.store.log_event(delivery_id=event.delivery_id, gesture=label(key), detail=detail,
                                      status="error", error=str(err))
        if self.rt.config.notify_on_error:
            # Silent failure is the worst UX for a screenless button: always say something.
            await self.rt.feedback.say(str(err)[:500], title=f"⚠️ {label(key)} failed", level="important",
                                       tags=["warning"])


