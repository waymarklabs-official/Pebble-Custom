"""Roku over External Control Protocol (ECP), plain HTTP on port 8060.

Roku OS 13+ ships with ECP restricted: set Settings > System > Advanced system settings >
Control by mobile apps > Network access to "Permissive" (or "Default" on the same subnet).
"""

from __future__ import annotations

import re
from urllib.parse import quote
from xml.etree.ElementTree import Element  # type only; parsing goes through defusedxml

import httpx
from defusedxml import ElementTree

from ..config import RokuConfig
from .base import DeviceError, TvCommand, UnsupportedCommand

KEYS: dict[TvCommand, str] = {
    TvCommand.PLAY_PAUSE: "Play",
    TvCommand.FAST_FORWARD: "Fwd",
    TvCommand.REWIND: "Rev",
    TvCommand.REPLAY: "InstantReplay",
    TvCommand.POWER_ON: "PowerOn",
    TvCommand.POWER_OFF: "PowerOff",
    TvCommand.POWER_TOGGLE: "Power",
    TvCommand.MUTE: "VolumeMute",
    TvCommand.VOLUME_UP: "VolumeUp",
    TvCommand.VOLUME_DOWN: "VolumeDown",
    TvCommand.HOME: "Home",
    TvCommand.BACK: "Back",
    TvCommand.UP: "Up",
    TvCommand.DOWN: "Down",
    TvCommand.LEFT: "Left",
    TvCommand.RIGHT: "Right",
    TvCommand.SELECT: "Select",
}

# "Media Assistant" channel: plays an arbitrary URL passed via ECP launch params.
MEDIA_ASSISTANT_APP_ID = "782875"
_RAW_KEY_RE = re.compile(r"^[A-Za-z]+(?:_[A-Za-z0-9%]+)?$")


class Roku:
    def __init__(self, name: str, cfg: RokuConfig, client: httpx.AsyncClient) -> None:
        self.name = name
        self.room = cfg.room
        self._base = f"http://{cfg.host}:{cfg.port}"
        self._timeout = cfg.timeout_seconds
        self._client = client

    async def _request(self, method: str, path: str) -> httpx.Response:
        try:
            resp = await self._client.request(method, self._base + path, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise DeviceError(f"{self.name}: {method} {path} failed: {e!r}") from None
        if resp.status_code == 403:
            raise DeviceError(f"{self.name}: ECP forbidden - enable 'Control by mobile apps'")
        if resp.status_code >= 400:
            raise DeviceError(f"{self.name}: {method} {path} -> HTTP {resp.status_code}")
        return resp

    async def _xml(self, path: str) -> Element:
        resp = await self._request("GET", path)
        try:
            return ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as e:
            raise DeviceError(f"{self.name}: bad XML from {path}: {e}") from None

    async def keypress(self, key: str) -> None:
        await self._request("POST", f"/keypress/{key}")

    async def player_state(self) -> str | None:
        root = await self._xml("/query/media-player")
        state = root.get("state")
        return str(state) if state is not None else None

    async def command(self, cmd: TvCommand, repeat: int = 1) -> None:
        # Roku only has a Play toggle; make play/pause idempotent by checking player state.
        if cmd in (TvCommand.PLAY, TvCommand.PAUSE):
            state = await self.player_state()
            playing = state == "play"
            if (cmd is TvCommand.PAUSE and playing) or (cmd is TvCommand.PLAY and state == "pause"):
                await self.keypress("Play")
            return
        key = KEYS.get(cmd)
        if key is None:
            raise UnsupportedCommand(f"{self.name}: Roku has no {cmd.value} key")
        for _ in range(max(1, repeat)):
            await self.keypress(key)

    async def raw_key(self, key: str) -> None:
        if not _RAW_KEY_RE.match(key):
            raise DeviceError(f"invalid Roku key {key!r}")
        await self.keypress(key)

    async def _resolve_app(self, app: str) -> str:
        if app.isdigit():
            return app
        root = await self._xml("/query/apps")
        for el in root.iter("app"):
            if (el.text or "").strip().lower() == app.strip().lower():
                app_id = el.get("id")
                if app_id:
                    return str(app_id)
        raise DeviceError(f"{self.name}: no installed channel named {app!r}")

    async def launch(self, app: str) -> None:
        await self._request("POST", f"/launch/{await self._resolve_app(app)}")

    async def open_url(self, url: str, mime: str | None = None) -> None:
        kind = "a" if mime and mime.startswith("audio") else "v"
        await self._request(
            "POST", f"/launch/{MEDIA_ASSISTANT_APP_ID}?u={quote(url, safe='')}&t={kind}"
        )

    async def is_awake(self) -> bool:
        root = await self._xml("/query/device-info")
        return (root.findtext("power-mode") or "").strip() == "PowerOn"

    async def active_app(self) -> str | None:
        root = await self._xml("/query/active-app")
        app = root.find("app")
        return (app.text or "").strip() or None if app is not None else None

    async def aclose(self) -> None:
        return None
