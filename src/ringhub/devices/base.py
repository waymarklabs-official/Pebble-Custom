"""Common TV interface. Each driver maps these verbs onto its own protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class TvCommand(StrEnum):
    PLAY_PAUSE = "play_pause"
    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    NEXT = "next"
    PREVIOUS = "previous"
    FAST_FORWARD = "fast_forward"
    REWIND = "rewind"
    REPLAY = "replay"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    POWER_TOGGLE = "power_toggle"
    MUTE = "mute"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    HOME = "home"
    BACK = "back"
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    SELECT = "select"
    CAPTIONS = "captions"


class DeviceError(Exception):
    """A device could not be reached or refused a command."""


class UnsupportedCommand(DeviceError):
    pass


class Tv(Protocol):
    name: str
    room: str | None

    async def command(self, cmd: TvCommand, repeat: int = 1) -> None: ...

    async def launch(self, app: str) -> None: ...

    async def open_url(self, url: str, mime: str | None = None) -> None: ...

    async def is_awake(self) -> bool: ...

    async def active_app(self) -> str | None: ...

    async def raw_key(self, key: str) -> None: ...

    async def aclose(self) -> None: ...
