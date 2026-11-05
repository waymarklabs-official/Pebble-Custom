from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ringhub.combos import ComboEngine
from ringhub.models import Gesture, RingEvent

C, D, T = Gesture.CLICK, Gesture.DOUBLE_CLICK, Gesture.TRIPLE_CLICK


def ev(g: Gesture, n: int = 0) -> RingEvent:
    now = datetime.now(UTC)
    return RingEvent(delivery_id=f"{g}-{n}", gesture=g, received_at=now, occurred_at=now)


def collector() -> tuple[list[tuple[Gesture, ...]], ComboEngine]:
    out: list[tuple[Gesture, ...]] = []

    async def emit(e: RingEvent) -> None:
        out.append(e.sequence)

    return out, ComboEngine([(D, C), (D, D, C)], 0.05, emit)


async def test_non_prefix_gesture_fires_immediately() -> None:
    out, eng = collector()
    await eng.feed(ev(C))
    assert out == [(C,)]


async def test_prefix_waits_then_falls_back_to_single() -> None:
    out, eng = collector()
    await eng.feed(ev(D))
    assert out == []
    await asyncio.sleep(0.1)
    assert out == [(D,)]


async def test_complete_combo_fires_without_waiting() -> None:
    out, eng = collector()
    await eng.feed(ev(D))
    await eng.feed(ev(C))
    assert out == [(D, C)]


async def test_longer_combo_supersedes_shorter_prefix() -> None:
    out, eng = collector()
    for g in (D, D, C):
        await eng.feed(ev(g))
    assert out == [(D, D, C)]


async def test_broken_combo_releases_held_events_in_order() -> None:
    out, eng = collector()
    await eng.feed(ev(D))
    await eng.feed(ev(T))
    assert out == [(D,), (T,)]


async def test_broken_combo_new_event_can_start_new_combo() -> None:
    out, eng = collector()
    await eng.feed(ev(D))
    await eng.feed(ev(D))  # (D, D) is a prefix of (D, D, C) -> still waiting
    await eng.feed(ev(D))  # (D, D, D) is nothing: release D, D; third D starts again
    assert out == [(D,), (D,)]
    await eng.feed(ev(C))
    assert out == [(D,), (D,), (D, C)]


async def test_flush_resolves_buffer() -> None:
    out, eng = collector()
    await eng.feed(ev(D))
    await eng.flush()
    assert out == [(D,)]
