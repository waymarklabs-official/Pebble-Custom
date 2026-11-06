"""Recall questions answered from memory (place-aware)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ringhub.config import Config
from ringhub.runtime import Runtime
from ringhub.seed import seed_if_empty
from ringhub.store import Store


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[Runtime]:
    cfg = Config.model_validate({"ring": {"allow_unsigned": True}, "server": {"data_dir": str(tmp_path)}})
    store = Store(tmp_path / "db.sqlite3")
    async with httpx.AsyncClient() as client:
        runtime = Runtime(cfg, store, client, link_secret=b"k" * 32)
        await seed_if_empty(runtime.catalog, cfg)
        yield runtime
        await runtime.aclose()
    store.close()


async def test_what_did_i_note_at_a_place_uses_aliases(rt: Runtime) -> None:
    await rt.memory.capture("the squat rack bolt is loose", said_at=time.time(), place="work", mode=None)
    await rt.memory.capture("buy oat milk", said_at=time.time(), place="home", mode=None)
    answer = await rt.memory.answer("What did I note at the office?")
    assert answer is not None and "At Work" in answer and "squat rack" in answer and "oat milk" not in answer


async def test_unknown_place_and_empty_place(rt: Runtime) -> None:
    assert "don't know a place" in (await rt.memory.answer("what did I say at the gym") or "")
    assert "Nothing saved at Home" in (await rt.memory.answer("what did I save at home") or "")


async def test_question_is_not_saved_as_a_note(rt: Runtime) -> None:
    result = await rt.memory.capture("what did I write at work", said_at=time.time(), place="home", mode=None)
    assert result.kind == "answer"
    assert await rt.notes.recent(10) == []
