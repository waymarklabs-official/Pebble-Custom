"""Device registry and target resolution (``all``, a device, a room, or ``here``)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import AndroidTvConfig, Config
from ..store import Store
from .androidtv import AndroidTv
from .base import DeviceError, Tv
from .roku import Roku

log = logging.getLogger(__name__)

HERE_KEY = "here"


class DeviceRegistry:
    def __init__(self, config: Config, client: httpx.AsyncClient, store: Store) -> None:
        self._config = config
        self._store = store
        self.devices: dict[str, Tv] = {}
        for name, cfg in config.devices.items():
            if isinstance(cfg, AndroidTvConfig):
                self.devices[name] = AndroidTv(name, cfg)
            else:
                self.devices[name] = Roku(name, cfg, client)

    def _expand(self, name: str) -> list[Tv]:
        if name == "all":
            return list(self.devices.values())
        if name in self.devices:
            return [self.devices[name]]
        in_room = [d for d in self.devices.values() if d.room == name]
        if in_room:
            return in_room
        raise DeviceError(f"unknown target {name!r} (not a device, room, 'all' or 'here')")

    async def resolve(self, selector: str) -> list[Tv]:
        """Resolve a target selector to devices.

        ``here`` resolves, in order, to: the room/device remembered in state (set explicitly
        or by the last targeted command), the TVs that are currently on, then
        ``default_target``. It is designed so that one gesture "does the obvious thing"
        in whichever room you are in.
        """
        if selector != "here":
            targets = self._expand(selector)
            if selector != "all":
                await self._store.set(HERE_KEY, selector)
            return targets

        remembered = await self._store.get(HERE_KEY)
        if isinstance(remembered, str):
            try:
                return self._expand(remembered)
            except DeviceError:
                await self._store.delete(HERE_KEY)

        awake = await self.awake_devices()
        if awake:
            return awake
        if self._config.default_target:
            return self._expand(self._config.default_target)
        raise DeviceError("cannot resolve 'here': nothing remembered, no TV is on, no default_target")

    async def awake_devices(self) -> list[Tv]:
        async def probe(dev: Tv) -> bool:
            try:
                return await dev.is_awake()
            except DeviceError as e:
                log.debug("awake probe failed for %s: %s", dev.name, e)
                return False

        devs = list(self.devices.values())
        results = await asyncio.gather(*(probe(d) for d in devs))
        return [d for d, on in zip(devs, results, strict=True) if on]

    async def aclose(self) -> None:
        for dev in self.devices.values():
            await dev.aclose()
