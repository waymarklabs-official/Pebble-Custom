"""Combo engine: turns the ring's five native gestures into an open-ended vocabulary.

The app already distinguishes click / double / triple. This layer adds *sequences* of those
(e.g. ``double-click`` then ``click``) without penalising plain gestures: we only hold an
event back when it is the prefix of a configured combo, and only for ``window`` seconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from .models import Gesture, RingEvent

log = logging.getLogger(__name__)

Emit = Callable[[RingEvent], Awaitable[None]]


class ComboEngine:
    def __init__(self, sequences: Iterable[Iterable[Gesture]], window_seconds: float, emit: Emit) -> None:
        self._sequences = {tuple(s) for s in sequences}
        self._prefixes = {s[:i] for s in self._sequences for i in range(1, len(s))}
        self._window = window_seconds
        self._emit = emit
        self._buffer: list[RingEvent] = []
        self._timer: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def _gestures(self, events: list[RingEvent]) -> tuple[Gesture, ...]:
        return tuple(e.gesture for e in events)

    async def feed(self, event: RingEvent) -> None:
        async with self._lock:
            self._cancel_timer()
            candidate = [*self._buffer, event]
            seq = self._gestures(candidate)

            if seq in self._prefixes:
                # Could still become a longer combo: wait for the next press.
                self._buffer = candidate
                self._arm_timer()
                return

            if seq in self._sequences:
                self._buffer = []
                await self._emit(event.with_sequence(seq))
                return

            # Not a combo: release what we held as individual gestures, then handle the new
            # event on its own (it may itself start a new combo).
            held, self._buffer = self._buffer, []
            for e in held:
                await self._emit(e.with_sequence((e.gesture,)))
            if (event.gesture,) in self._prefixes:
                self._buffer = [event]
                self._arm_timer()
            else:
                await self._emit(event.with_sequence((event.gesture,)))

    async def flush(self) -> None:
        """Resolve whatever is buffered right now (used on timeout and shutdown)."""
        async with self._lock:
            self._cancel_timer()
            held, self._buffer = self._buffer, []
            if not held:
                return
            seq = self._gestures(held)
            if seq in self._sequences:
                await self._emit(held[-1].with_sequence(seq))
            else:
                for e in held:
                    await self._emit(e.with_sequence((e.gesture,)))

    def _arm_timer(self) -> None:
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(self._window, self._on_timeout)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self) -> None:
        task = asyncio.get_running_loop().create_task(self.flush())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
