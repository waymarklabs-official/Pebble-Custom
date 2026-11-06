"""TV actions across Android TV / Fire TV (ADB) and Roku (ECP)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..devices import HERE_KEY, DeviceError, Tv, TvCommand, UnsupportedCommand
from .base import ActionError, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime


async def _fan_out(rt: Runtime, target: str, op: Callable[[Tv], Awaitable[Any]],
                   ignore_unsupported: bool = False) -> list[tuple[str, Any]]:
    """Run ``op`` on every resolved device concurrently; fail if any device fails."""
    try:
        devices = await rt.devices.resolve(target)
    except DeviceError as e:
        raise ActionError(str(e)) from None

    async def one(dev: Tv) -> tuple[str, Any]:
        try:
            return dev.name, await op(dev)
        except UnsupportedCommand:
            if ignore_unsupported:
                return dev.name, None
            raise

    results = await asyncio.gather(*(one(d) for d in devices), return_exceptions=True)
    errors = [str(r) for r in results if isinstance(r, BaseException)]
    if errors:
        raise ActionError("; ".join(errors))
    return [r for r in results if not isinstance(r, BaseException)]


class TvParams(Params):
    command: TvCommand
    target: str = "here"
    repeat: int = Field(1, ge=1, le=50)
    ignore_unsupported: bool = True
    """When targeting several TVs, skip ones that lack this key instead of failing."""


@action("tv", TvParams, primary="command")
async def tv(p: TvParams, rt: Runtime, v: Vars) -> Vars:
    done = await _fan_out(rt, p.target, lambda d: d.command(p.command, p.repeat), p.ignore_unsupported)
    return {"tv": {"targets": [name for name, _ in done]}}


class KeyParams(Params):
    key: str
    target: str = "here"


@action("tv.key", KeyParams, primary="key")
async def tv_key(p: KeyParams, rt: Runtime, v: Vars) -> None:
    """Send a platform-specific key (Android ``KEYCODE_*`` or Roku ECP key name)."""
    await _fan_out(rt, p.target, lambda d: d.raw_key(p.key))


class LaunchParams(Params):
    app: str
    """Android package name (``com.netflix.ninja``) or Roku channel id / name."""
    target: str = "here"


@action("tv.launch", LaunchParams, primary="app")
async def tv_launch(p: LaunchParams, rt: Runtime, v: Vars) -> None:
    await _fan_out(rt, p.target, lambda d: d.launch(p.app))


class OpenUrlParams(Params):
    url: str = Field(pattern=r"^(https?|rtsp|file)://")
    target: str = "here"
    mime: str | None = "video/*"


@action("tv.open_url", OpenUrlParams, primary="url")
async def tv_open_url(p: OpenUrlParams, rt: Runtime, v: Vars) -> None:
    await _fan_out(rt, p.target, lambda d: d.open_url(p.url, p.mime))


class TargetParams(Params):
    target: str = "here"


@action("tv.active_app", TargetParams, primary="target")
async def tv_active_app(p: TargetParams, rt: Runtime, v: Vars) -> Vars:
    """What is on screen right now. Exposes ``active_app`` (first device) and ``active_apps``."""
    done = await _fan_out(rt, p.target, lambda d: d.active_app())
    apps = dict(done)
    first = next(iter(apps.values()), None)
    return {"active_app": first, "active_apps": apps}


class HereSetParams(Params):
    target: str | None = None
    """Room or device; omit to forget and fall back to auto-detection."""


@action("here.set", HereSetParams, primary="target")
async def here_set(p: HereSetParams, rt: Runtime, v: Vars) -> Vars:
    if p.target is None:
        await rt.store.delete(HERE_KEY)
        return {"here": None}
    try:
        await rt.devices.resolve(p.target)  # validates and remembers it
    except DeviceError as e:
        raise ActionError(str(e)) from None
    return {"here": p.target}
