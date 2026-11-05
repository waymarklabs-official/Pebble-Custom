"""Executes the recipes in config.example.yaml against fake TVs and a mocked ntfy/Pi."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ringhub.config import load_config
from ringhub.devices import TvCommand
from ringhub.engine import Engine
from ringhub.models import Gesture, RingEvent
from ringhub.runtime import Runtime
from ringhub.store import Store

ROOT = Path(__file__).resolve().parents[1]


async def wait_until(cond: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not cond():  # noqa: ASYNC110 - polling a mock, no event to wait on
            await asyncio.sleep(0.01)


@dataclass
class FakeTv:
    name: str
    room: str | None
    awake: bool = False
    app: str | None = "com.google.android.youtube.tv"
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def command(self, cmd: TvCommand, repeat: int = 1) -> None:
        self.calls.append(("cmd", cmd.value, repeat))

    async def launch(self, app: str) -> None:
        self.calls.append(("launch", app))

    async def open_url(self, url: str, mime: str | None = None) -> None:
        self.calls.append(("open", url))

    async def is_awake(self) -> bool:
        return self.awake

    async def active_app(self) -> str | None:
        return self.app

    async def raw_key(self, key: str) -> None:
        self.calls.append(("key", key))

    async def aclose(self) -> None:
        return None


@dataclass
class Harness:
    rt: Runtime
    engine: Engine
    tvs: dict[str, FakeTv]
    ntfy: respx.Route
    notes: Path

    async def say(self, text: str, gesture: Gesture = Gesture.HOLD) -> list[str]:
        return await self.press(gesture, transcript=text)

    async def press(self, gesture: Gesture, transcript: str | None = None,
                    sequence: tuple[Gesture, ...] = ()) -> list[str]:
        now = datetime.now(UTC)
        ev = RingEvent(delivery_id=f"t{time.monotonic_ns()}", gesture=gesture, received_at=now,
                       occurred_at=now, transcript=transcript, sequence=sequence or (gesture,))
        return await self.engine.handle(ev)

    def said(self) -> list[str]:
        return [c.request.content.decode() for c in self.ntfy.calls]


@pytest.fixture
async def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setenv("RINGHUB_SECRET", "s" * 40)
    monkeypatch.chdir(tmp_path)  # data_dir/notes_dir are relative in the example
    cfg = load_config(ROOT / "config.example.yaml")
    with respx.mock(assert_all_called=False) as mock:
        ntfy = mock.post(url__regex=r"https://ntfy\.sh/.*").mock(return_value=httpx.Response(200))
        mock.get("http://pi.local:8000/api/latest").mock(return_value=httpx.Response(
            200, json={"url": "http://pi.local:8000/media/ep1.mp4", "title": "Episode 1"}))
        store = Store(tmp_path / "db.sqlite3")
        async with httpx.AsyncClient() as client:
            rt = Runtime(cfg, store, client)
            tvs = {n: FakeTv(n, d.room) for n, d in rt.devices.devices.items()}
            rt.devices.devices = dict(tvs)  # type: ignore[arg-type]
            engine = Engine(rt)
            yield Harness(rt, engine, tvs, ntfy, tmp_path / "data" / "notes")
            await rt.aclose()
        store.close()


async def test_triple_cycles_mode_and_explains_it(h: Harness) -> None:
    assert await h.press(Gesture.TRIPLE_CLICK) == ["cycle mode"]
    assert await h.rt.mode() == "tv"
    assert "play/pause" in h.said()[0]


async def test_tv_mode_click_targets_awake_tv(h: Harness) -> None:
    await h.rt.store.set("mode", "tv")
    h.tvs["bedroom_tv"].awake = True
    await h.press(Gesture.CLICK)
    assert h.tvs["bedroom_tv"].calls == [("cmd", "play_pause", 1)]
    assert h.tvs["living_tv"].calls == []


async def test_house_pause_combo_hits_every_tv(h: Harness) -> None:
    await h.press(Gesture.CLICK, sequence=(Gesture.DOUBLE_CLICK, Gesture.CLICK))
    assert all(tv.calls == [("cmd", "pause", 1)] for tv in h.tvs.values())


async def test_what_did_they_say_schedules_captions_off(h: Harness) -> None:
    await h.rt.store.set("mode", "tv")
    await h.press(Gesture.DOUBLE_CLICK)
    assert h.tvs["living_tv"].calls == [("cmd", "replay", 1), ("cmd", "captions", 1)]
    assert [j.id for j in await h.rt.scheduler.pending()] == ["captions_off"]


async def test_sleep_timer_and_cancel(h: Harness) -> None:
    await h.say("Sleep in forty-five minutes")
    assert h.said() == ["TV off in 45 min"]
    assert [j.id for j in await h.rt.scheduler.pending()] == ["sleep_timer"]
    await h.say("cancel the sleep timer")
    assert await h.rt.scheduler.pending() == []


async def test_play_latest_on_named_room(h: Harness) -> None:
    await h.say("Play the latest download on the bedroom TV")
    assert h.tvs["bedroom_tv"].calls == [("open", "http://pi.local:8000/media/ep1.mp4")]
    assert h.said() == ["▶ Episode 1"]
    # "here" now remembers the bedroom
    await h.rt.store.set("mode", "tv")
    await h.press(Gesture.CLICK)
    assert h.tvs["bedroom_tv"].calls[-1] == ("cmd", "play_pause", 1)


async def test_leaving_soon_math(h: Harness) -> None:
    await h.say("I'm leaving in 25 minutes")
    jobs = {j.id: j.run_at - time.time() for j in await h.rt.scheduler.pending()}
    assert set(jobs) == {"precondition", "departure"}
    assert 14 * 60 < jobs["precondition"] <= 15 * 60
    assert h.said() == ["Car climate in 15 min, house pauses in 25"]


async def test_tally_and_goodnight(h: Harness) -> None:
    await h.say("Water")
    await h.say("water.")
    await h.say("coffee")
    await h.say("Good night")
    assert h.said()[:3] == ["water #1 today", "water #2 today", "coffee #1 today"]
    assert h.said()[3] == "Night. Water: 2, coffee: 1"
    assert all(("cmd", "power_off", 1) in tv.calls for tv in h.tvs.values())


async def test_volume_template(h: Harness) -> None:
    await h.say("volume down 7")
    assert h.tvs["living_tv"].calls == [("cmd", "volume_down", 7)]


async def test_bookmark_writes_what_was_playing(h: Harness) -> None:
    await h.say("bookmark this")
    text = (h.notes / "bookmarks.md").read_text()
    assert "com.google.android.youtube.tv" in text and "bookmark this" in text


async def test_unmatched_voice_lands_in_inbox(h: Harness) -> None:
    await h.say("call the dentist about the crown")
    inbox = next((h.notes / "inbox").glob("*.md")).read_text()
    assert inbox.startswith("# Inbox ") and "- [ ] call the dentist about the crown" in inbox


async def test_focus_mode_counts_and_summarises(h: Harness) -> None:
    await h.rt.store.set("mode", "focus")
    await h.press(Gesture.DOUBLE_CLICK)
    for _ in range(3):
        await h.press(Gesture.CLICK)
    job = (await h.rt.scheduler.pending())[0]
    assert job.id == "focus"
    # Fire the persisted job through the real scheduler path, as if 25 minutes passed.
    await h.rt.scheduler.schedule(job.steps, timedelta(seconds=0), job.vars, job.id)
    await wait_until(lambda: len(h.said()) >= 2)
    assert h.said()[-1] == "3 interruptions logged. Stand up."


async def test_tesla_without_config_fails_loudly(h: Harness) -> None:
    await h.say("open the frunk")
    assert any("intent 'frunk'" in m and "no 'tesla' section" in m for m in h.said())
