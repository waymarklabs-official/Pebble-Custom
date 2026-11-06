"""`ringhub env` writes a complete, valid .env for the Pi stack."""

from __future__ import annotations

import re

import pytest

from ringhub.__main__ import main


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["env", *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_env_has_fresh_secrets_random_topic_and_separate_ports(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--host", "Pi.Tail1234.ts.net.", "--tz", "America/New_York")
    assert code == 0
    env = dict(line.split("=", 1) for line in out.splitlines() if line and not line.startswith("#"))
    assert env["RINGHUB_PUBLIC_URL"] == "https://pi.tail1234.ts.net:8443"
    assert env["NTFY_PUBLIC_URL"] == "https://pi.tail1234.ts.net:8444"  # must match the iPhone app exactly
    assert re.fullmatch(r"ring-[a-z2-9]{12}", env["NTFY_TOPIC"])          # random: matches ACL 'ring-*'
    assert env["TZ"] == "America/New_York" and env["NTFY_TOKEN"] == ""
    assert env["RINGHUB_TAG"] == "main"  # follows the default branch's published image
    secrets = [env["RINGHUB_SECRET"], env["RINGHUB_ADMIN_TOKEN"], env["RINGHUB_CONTEXT_TOKEN"]]
    assert len(set(secrets)) == 3 and all(len(s) >= 40 for s in secrets)
    _, again, _ = run(capsys, "--host", "pi.tail1234.ts.net")
    assert "RINGHUB_SECRET=" + env["RINGHUB_SECRET"] not in again  # never reused


def test_env_port_443_omits_the_port(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "--host", "ring.tail1234.ts.net", "--ring-port", "443", "--ntfy-port", "8443")
    assert "RINGHUB_PUBLIC_URL=https://ring.tail1234.ts.net\n" in out


@pytest.mark.parametrize("args", [
    ("--host", "https://pi.tail1234.ts.net"),
    ("--host", "pi"),
    ("--host", "100.101.102.103"),
    ("--host", "pi.tail1234.ts.net", "--ring-port", "8443", "--ntfy-port", "8443"),
    ("--host", "pi.tail1234.ts.net", "--tag", "main; rm -rf /"),
])
def test_env_rejects_bad_input(capsys: pytest.CaptureFixture[str], args: tuple[str, ...]) -> None:
    code, out, err = run(capsys, *args)
    assert code == 2 and out == "" and "error:" in err


def test_envfile_runs_without_any_dependencies_installed() -> None:
    """On a fresh Pi, .env is generated with the system Python before anything is built."""
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    code = ("import sys; import ringhub.envfile as e; "
            "heavy = [m for m in ('httpx', 'pydantic', 'fastapi', 'yaml', 'jinja2') if m in sys.modules]; "
            "assert not heavy, heavy; sys.exit(e.main(['--host', 'pi.tail1234.ts.net']))")
    r = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(src)},
                       capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    assert "NTFY_PUBLIC_URL=https://pi.tail1234.ts.net:8444" in r.stdout


async def test_min_priority_raises_every_push(make_config, ntfy) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from ringhub.notifier import Notifier

    cfg = make_config(ntfy={"url": "https://ntfy.test", "topic": "ring", "min_priority": 4})
    async with httpx.AsyncClient() as client:
        n = Notifier(cfg.ntfy, client)
        await n.publish("quiet confirmation", priority=2)
        await n.publish("no priority given")
        await n.publish("urgent", priority=5)
    assert [b["priority"] for b in ntfy.bodies] == [4, 4, 5]


def test_env_pins_a_specific_build(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "--host", "pi.tail1234.ts.net", "--tag", "sha-1a2b3c4")
    assert "RINGHUB_TAG=sha-1a2b3c4\n" in out
