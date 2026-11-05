"""Markdown file and media playback actions."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..devices import DeviceError
from .base import ActionError, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- notes


class NotesParams(Params):
    file: str
    """Path relative to ``memory.markdown_dir``, e.g. ``lists/groceries.md``."""
    text: str
    ensure_header: str | None = None
    """Written once when the file is created (e.g. ``# Inbox {{ now.date() }}``)."""


def _safe_path(base: Path, relative: str) -> Path:
    root = base.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ActionError(f"notes path escapes notes_dir: {relative!r}")
    return target


@action("notes.append", NotesParams)
async def notes_append(p: NotesParams, rt: Runtime, v: Vars) -> Vars:
    """Append a line to any Markdown file under ``memory.markdown_dir`` (custom skills)."""
    if rt.config.memory.markdown_dir is None:
        raise ActionError("notes.append requires memory.markdown_dir in config")
    path = _safe_path(rt.config.memory.markdown_dir, p.file)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new and p.ensure_header:
                f.write(p.ensure_header.rstrip() + "\n\n")
            f.write(p.text.rstrip("\n") + "\n")

    await asyncio.to_thread(write)
    return {"note_file": str(path)}


# ---------------------------------------------------------------------------- media


def dig(data: Any, dotted: str) -> Any:
    """``dig({'items': [{'url': 'x'}]}, 'items.0.url') == 'x'``"""
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, list) and part.lstrip("-").isdigit():
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(dotted)
    return cur


class PlayLatestParams(Params):
    source: str
    target: str = "here"
    mime: str | None = "video/*"


@action("media.play_latest", PlayLatestParams, primary="source")
async def media_play_latest(p: PlayLatestParams, rt: Runtime, v: Vars) -> Vars:
    """Ask a media source (your Pi downloader) for its newest item and play it on a TV."""
    src = rt.config.media_sources.get(p.source)
    if src is None:
        raise ActionError(f"unknown media source {p.source!r}")
    try:
        resp = await rt.http.get(src.latest_url, headers=src.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ActionError(f"media source {p.source}: {e!r}") from None
    try:
        url = str(dig(data, src.url_field))
    except (KeyError, IndexError):
        raise ActionError(f"media source {p.source}: no {src.url_field!r} in response") from None
    title = None
    if src.title_field:
        try:
            title = str(dig(data, src.title_field))
        except (KeyError, IndexError):
            title = None
    try:
        for dev in await rt.devices.resolve(p.target):
            await dev.open_url(url, p.mime)
    except DeviceError as e:
        raise ActionError(str(e)) from None
    return {"media": {"url": url, "title": title or url.rsplit("/", 1)[-1]}}

