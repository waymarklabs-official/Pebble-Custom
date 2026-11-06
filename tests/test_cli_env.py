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
])
def test_env_rejects_bad_input(capsys: pytest.CaptureFixture[str], args: tuple[str, ...]) -> None:
    code, out, err = run(capsys, *args)
    assert code == 2 and out == "" and "error:" in err
