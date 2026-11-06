"""Android TV / Google TV / Fire TV over network ADB.

Uses the stock ``adb`` binary (``apt install adb``) rather than a Python ADB reimplementation:
it handles key auth, reconnects and protocol quirks across vendors far better.

One-time setup per TV: enable Developer options > Network/ADB debugging, then accept the
"Allow debugging from this computer?" prompt the first time the hub connects (tick
"Always allow"). The hub's ADB key lives in ``~/.android`` - persist it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex

from ..config import AndroidTvConfig
from .base import DeviceError, TvCommand

log = logging.getLogger(__name__)

# https://developer.android.com/reference/android/view/KeyEvent
KEYCODES: dict[TvCommand, int] = {
    TvCommand.PLAY_PAUSE: 85,
    TvCommand.PLAY: 126,
    TvCommand.PAUSE: 127,
    TvCommand.STOP: 86,
    TvCommand.NEXT: 87,
    TvCommand.PREVIOUS: 88,
    TvCommand.FAST_FORWARD: 90,
    TvCommand.REWIND: 89,
    TvCommand.REPLAY: 89,
    TvCommand.POWER_TOGGLE: 26,
    TvCommand.POWER_ON: 224,  # KEYCODE_WAKEUP: idempotent, unlike POWER
    TvCommand.POWER_OFF: 223,  # KEYCODE_SLEEP
    TvCommand.MUTE: 164,
    TvCommand.VOLUME_UP: 24,
    TvCommand.VOLUME_DOWN: 25,
    TvCommand.HOME: 3,
    TvCommand.BACK: 4,
    TvCommand.UP: 19,
    TvCommand.DOWN: 20,
    TvCommand.LEFT: 21,
    TvCommand.RIGHT: 22,
    TvCommand.SELECT: 23,
    TvCommand.CAPTIONS: 175,
}

_PKG_RE = re.compile(r"\b([a-zA-Z][\w]*(?:\.[\w]+)+)/")
_RAW_KEY_RE = re.compile(r"^(KEYCODE_[A-Z0-9_]+|\d{1,3})$")


class AndroidTv:
    def __init__(self, name: str, cfg: AndroidTvConfig) -> None:
        self.name = name
        self.room = cfg.room
        self._cfg = cfg
        self._serial = f"{cfg.host}:{cfg.port}"
        self._connected = False
        self._lock = asyncio.Lock()

    async def _exec(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cfg.adb_path, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            raise DeviceError(f"adb binary not found ({self._cfg.adb_path!r}); install android-tools") from None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self._cfg.timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DeviceError(f"{self.name}: adb {args[0]} timed out") from None
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise DeviceError(f"{self.name}: adb {args[0]} failed: {text[:200]}")
        return text

    async def _connect(self) -> None:
        out = await self._exec("connect", self._serial)
        if "connected to" not in out:
            raise DeviceError(f"{self.name}: cannot connect to {self._serial}: {out[:200]}")
        self._connected = True

    async def shell(self, command: str) -> str:
        """Run a remote shell command, (re)connecting once if the device dropped."""
        async with self._lock:
            for attempt in (1, 2):
                if not self._connected:
                    await self._connect()
                try:
                    return await self._exec("-s", self._serial, "shell", command)
                except DeviceError as e:
                    msg = str(e)
                    transient = any(s in msg for s in ("offline", "not found", "unauthorized", "closed"))
                    self._connected = False
                    if attempt == 2 or not transient:
                        raise
                    log.info("%s: reconnecting after: %s", self.name, msg)
        raise AssertionError("unreachable")

    async def command(self, cmd: TvCommand, repeat: int = 1) -> None:
        code = KEYCODES[cmd]
        # `input keyevent` accepts several codes in one call: one round-trip for volume +5.
        await self.shell("input keyevent " + " ".join([str(code)] * max(1, repeat)))

    async def raw_key(self, key: str) -> None:
        if not _RAW_KEY_RE.match(key):
            raise DeviceError(f"invalid Android keycode {key!r}")
        await self.shell(f"input keyevent {key}")

    async def launch(self, app: str) -> None:
        pkg = shlex.quote(app)
        out = await self.shell(f"monkey -p {pkg} -c android.intent.category.LEANBACK_LAUNCHER 1")
        if "No activities found" in out:  # phone-style apps / some Fire TV builds
            out = await self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in out or "monkey aborted" in out:
            raise DeviceError(f"{self.name}: no launchable activity for {app!r}")

    async def open_url(self, url: str, mime: str | None = None) -> None:
        cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
        if mime:
            cmd += f" -t {shlex.quote(mime)}"
        out = await self.shell(cmd)
        if "Error" in out:
            raise DeviceError(f"{self.name}: could not open URL: {out[:200]}")

    async def is_awake(self) -> bool:
        out = await self.shell("dumpsys power | grep -m1 mWakefulness=")
        return "Awake" in out

    async def active_app(self) -> str | None:
        out = await self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -n 2")
        m = _PKG_RE.search(out)
        return m.group(1) if m else None

    async def aclose(self) -> None:
        return None
