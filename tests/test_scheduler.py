from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from ringhub.actions.base import Vars
from ringhub.executor import Executor
from ringhub.runtime import Scheduler
from ringhub.store import Store


class Recorder(Executor):
    def __init__(self) -> None:
        self.runs: list[tuple[list[dict[str, object]], Vars]] = []

    async def run(self, steps, variables, *, depth=0):  # type: ignore[no-untyped-def,override]
        self.runs.append((steps, variables))
        return variables


async def test_job_persists_across_restart_and_sees_live_state(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    live = {"state": {"n": 1}}

    async def live_vars() -> Vars:
        return dict(live)

    store = Store(db)
    first = Scheduler(store)
    first.bind(Recorder(), live_vars)
    await first.schedule([{"noop": {}}], timedelta(seconds=0.2), {"minutes": "5", "state": {"n": 0}}, "sleep")
    await first.aclose()  # simulate the process dying before it fires
    store.close()

    store = Store(db)
    rec = Recorder()
    second = Scheduler(store)
    second.bind(rec, live_vars)
    live["state"] = {"n": 42}
    await second.restore()
    await asyncio.sleep(0.4)
    assert len(rec.runs) == 1
    steps, variables = rec.runs[0]
    assert steps == [{"noop": {}}]
    assert variables["minutes"] == "5"          # captured at schedule time
    assert variables["state"] == {"n": 42}       # refreshed at fire time
    assert await store.jobs() == []
    await second.aclose()
    store.close()


async def test_same_id_replaces_and_cancel_removes(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    rec = Recorder()
    sched = Scheduler(store)

    async def live_vars() -> Vars:
        return {}

    sched.bind(rec, live_vars)
    await sched.schedule([{"noop": {}}], timedelta(minutes=5), {}, "t")
    await sched.schedule([{"noop": {}}], timedelta(minutes=9), {}, "t")
    assert len(await sched.pending()) == 1
    assert await sched.cancel("t") is True
    assert await sched.cancel("t") is False
    await sched.aclose()
    store.close()
