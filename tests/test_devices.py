from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest
import respx

from ringhub.config import AndroidTvConfig, RokuConfig
from ringhub.devices import DeviceError, TvCommand, UnsupportedCommand
from ringhub.devices.androidtv import AndroidTv
from ringhub.devices.roku import Roku

ROKU = "http://10.0.0.9:8060"


def player(state: str) -> httpx.Response:
    return httpx.Response(200, text=f'<?xml version="1.0"?><player error="false" state="{state}"/>')


@pytest.fixture
async def roku() -> Roku:
    return Roku("bedroom", RokuConfig(type="roku", host="10.0.0.9"), httpx.AsyncClient())


@respx.mock
async def test_roku_pause_is_idempotent(roku: Roku) -> None:
    respx.get(f"{ROKU}/query/media-player").mock(side_effect=[player("play"), player("pause")])
    press = respx.post(f"{ROKU}/keypress/Play").mock(return_value=httpx.Response(200))
    await roku.command(TvCommand.PAUSE)
    await roku.command(TvCommand.PAUSE)  # already paused: no toggle
    assert press.call_count == 1


@respx.mock
async def test_roku_volume_repeat_and_unsupported(roku: Roku) -> None:
    up = respx.post(f"{ROKU}/keypress/VolumeUp").mock(return_value=httpx.Response(200))
    await roku.command(TvCommand.VOLUME_UP, repeat=3)
    assert up.call_count == 3
    with pytest.raises(UnsupportedCommand):
        await roku.command(TvCommand.NEXT)


@respx.mock
async def test_roku_open_url_uses_media_assistant(roku: Roku) -> None:
    launch = respx.post(url__startswith=f"{ROKU}/launch/782875").mock(return_value=httpx.Response(200))
    await roku.open_url("http://pi.local/media/a b.mp4")
    assert launch.calls[0].request.url.params["u"] == "http://pi.local/media/a b.mp4"
    assert launch.calls[0].request.url.params["t"] == "v"


@respx.mock
async def test_roku_launch_by_name_and_power_state(roku: Roku) -> None:
    respx.get(f"{ROKU}/query/apps").mock(return_value=httpx.Response(
        200, text='<apps><app id="12">Netflix</app><app id="837">YouTube</app></apps>'))
    launch = respx.post(f"{ROKU}/launch/837").mock(return_value=httpx.Response(200))
    await roku.launch("youtube")
    assert launch.called
    respx.get(f"{ROKU}/query/device-info").mock(return_value=httpx.Response(
        200, text="<device-info><power-mode>DisplayOff</power-mode></device-info>"))
    assert await roku.is_awake() is False


@respx.mock
async def test_roku_forbidden_explains_setting(roku: Roku) -> None:
    respx.post(f"{ROKU}/keypress/Home").mock(return_value=httpx.Response(403))
    with pytest.raises(DeviceError, match="Control by mobile apps"):
        await roku.command(TvCommand.HOME)


# ----------------------------------------------------------------- Android TV via a fake adb


@pytest.fixture
def fake_adb(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "adb.log"
    script = tmp_path / "adb"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1" in
  connect) echo "connected to $2" ;;
  -s)
    shift 3
    case "$1" in
      *mWakefulness*) echo "  mWakefulness=Asleep" ;;
      *mCurrentFocus*) echo "  mCurrentFocus=Window{{1 u0 com.netflix.ninja/com.netflix.ninja.MainActivity}}" ;;
      *LEANBACK_LAUNCHER*) echo "No activities found to run, monkey aborted." ;;
    esac ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def android(adb: Path) -> AndroidTv:
    return AndroidTv("living", AndroidTvConfig(type="androidtv", host="10.0.0.5", adb_path=str(adb)))


async def test_android_keyevents_batched(fake_adb: tuple[Path, Path]) -> None:
    adb, log = fake_adb
    tv = android(adb)
    await tv.command(TvCommand.VOLUME_DOWN, repeat=3)
    lines = log.read_text().splitlines()
    assert lines[0] == "connect 10.0.0.5:5555"
    assert lines[1] == "-s 10.0.0.5:5555 shell input keyevent 25 25 25"


async def test_android_open_url_is_shell_quoted(fake_adb: tuple[Path, Path]) -> None:
    adb, log = fake_adb
    await android(adb).open_url("http://pi/x.mp4; reboot", "video/*")
    assert "-d 'http://pi/x.mp4; reboot' -t 'video/*'" in log.read_text()


async def test_android_state_queries_and_launch_fallback(fake_adb: tuple[Path, Path]) -> None:
    adb, log = fake_adb
    tv = android(adb)
    assert await tv.is_awake() is False
    assert await tv.active_app() == "com.netflix.ninja"
    await tv.launch("com.amazon.firetv.youtube")
    assert "android.intent.category.LAUNCHER 1" in log.read_text()


async def test_android_rejects_bad_raw_key(fake_adb: tuple[Path, Path]) -> None:
    with pytest.raises(DeviceError):
        await android(fake_adb[0]).raw_key("3; rm -rf /")


async def test_android_missing_adb_binary() -> None:
    tv = android(Path(os.devnull).with_name("definitely-not-adb"))
    with pytest.raises(DeviceError, match="adb binary not found"):
        await tv.command(TvCommand.HOME)
