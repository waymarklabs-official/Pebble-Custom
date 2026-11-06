"""deploy/pi/preflight.sh and serve.sh, run for real against fake tailscale/docker/git/etc.

The fakes reproduce one real Raspberry Pi: Tstream published to the internet on 8443 (Funnel),
ringhub on 8445 (plus an old duplicate on 443), ntfy on 8446, and another program on 8444. The
fake `tailscale` logs every change, so the tests can assert that nobody else's port is touched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = "tesla-stream.tail51b89d.ts.net"
BRANCH = "claude/pebble-ring-automation-kqoqn3"
HEAD = "8b75463a0f6466a84d4bef58362bdb11ba75c971"
NEWER = "ffff000000000000000000000000000000000000"
OLDER = "1c4626a12311a357cc2cf3ec3b8429d92496c86e"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs bash")


def _site(proxy: str) -> dict[str, Any]:
    return {"Handlers": {"/": {"Proxy": proxy}}}


PI_STATUS: dict[str, Any] = {
    "TCP": {p: {"HTTPS": True} for p in ("443", "8443", "8445", "8446")},
    "Web": {f"{HOST}:443": _site("http://127.0.0.1:8787"), f"{HOST}:8443": _site("http://127.0.0.1:8099"),
            f"{HOST}:8445": _site("http://127.0.0.1:8787"), f"{HOST}:8446": _site("http://127.0.0.1:2586")},
    "AllowFunnel": {f"{HOST}:8443": True},
}
ONLY_TSTREAM: dict[str, Any] = {
    "TCP": {"8443": {"HTTPS": True}}, "Web": {f"{HOST}:8443": _site("http://127.0.0.1:8099")},
    "AllowFunnel": {f"{HOST}:8443": True},
}
ENV_8445 = (f"RINGHUB_PUBLIC_URL=https://{HOST}:8445\nNTFY_PUBLIC_URL=https://{HOST}:8446\n"
            "NTFY_TOPIC=ring-abc\nNTFY_TOKEN=tk_x\nRINGHUB_TAG=sha-8b75463\n")

FAKE_TAILSCALE = r'''#!PYTHON
import json, os, sys
state, log, host = os.environ["FAKE_TS_STATE"], os.environ["FAKE_TS_LOG"], "HOST"
doc = json.load(open(state))
a = sys.argv[1:]
if a[:1] == ["version"]:
    print("1.104.5"); sys.exit()
if a[:3] == ["status", "--self", "--json"]:
    print(json.dumps({"Self": {"DNSName": host + "."}})); sys.exit()
if a[:1] == ["serve"] and a[1:2] == ["status"]:
    if os.environ.get("FAKE_TS_STATUS_DENIED"):
        sys.exit("access denied")
    print(json.dumps(doc) if "--json" in a else "\n".join(sorted(doc.get("Web", {})))); sys.exit()
if a[:1] == ["serve"]:
    open(log, "a").write(" ".join(a) + "\n")
    port = next(x.split("=", 1)[1] for x in a if x.startswith("--https="))
    if a[-1] == "off":
        doc["TCP"].pop(port, None); doc["Web"].pop(f"{host}:{port}", None)
        doc.get("AllowFunnel", {}).pop(f"{host}:{port}", None)
    else:
        doc.setdefault("TCP", {})[port] = {"HTTPS": True}
        doc.setdefault("Web", {})[f"{host}:{port}"] = {"Handlers": {"/": {"Proxy": a[-1]}}}
    json.dump(doc, open(state, "w")); sys.exit()
sys.exit(f"fake tailscale: unhandled {a}")
'''

FAKES = {
    "uname": 'echo aarch64',
    "dpkg": 'echo "${FAKE_DPKG:-arm64}"',
    "id": 'case "$1" in -u) echo "${FAKE_UID:-1000}";; -un) echo pi;; *) echo "uid=${FAKE_UID:-1000}";; esac',
    "sudo": '[ "$1" = -n ] && shift; [ -n "${FAKE_SUDO_DENIED:-}" ] && exit 1; FAKE_AS_ROOT=1 FAKE_UID=0 exec "$@"',
    "docker": '''case "$1" in
  --version) echo "Docker version 29.0.0, build fake";;
  info) [ -n "${FAKE_DOCKER_NEEDS_SUDO:-}" ] && [ "${FAKE_AS_ROOT:-}" != 1 ] && exit 1; exit 0;;
  version) echo "${FAKE_DOCKER_ARCH:-arm64}";;
  compose) echo "Docker Compose version v2.40.0";;
  *) exit 1;;
esac''',
    "curl": '''url="${@: -1}"
for h in ${FAKE_BLOCKED:-}; do case "$url" in *"$h"*) printf 000; exit 0;; esac; done
printf 401''',
    "ss": '''echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port"
for p in ${FAKE_LISTEN:-}; do echo "LISTEN 0 4096 0.0.0.0:$p 0.0.0.0:*"; done''',
    "git": '''while [ "$1" = -c ]; do shift 2; done
case "$1 $2" in
  "rev-parse --short=7") echo "${FAKE_HEAD:0:7}";;
  "rev-parse --abbrev-ref") echo "$FAKE_BRANCH";;
  "rev-parse HEAD") echo "$FAKE_HEAD";;
  "ls-remote origin") [ -n "${FAKE_REMOTE:-}" ] || exit 128; printf '%s\\t%s\\n' "$FAKE_REMOTE" "$3";;
  "merge-base --is-ancestor") case " ${FAKE_ANCESTORS:-} " in *" $3 "*) exit 0;; esac; exit 1;;
  *) echo "fake git: unhandled $*" >&2; exit 1;;
esac''',
    # Everything goes to the real Python except the registry lookup (tested in test_pitools.py).
    "python3": '''if [ "${1:-}" = -m ] && [ "${2:-}" = ringhub.pitools ] && [ "${3:-}" = image ]; then
  for m in ${FAKE_MISSING_IMAGES:-}; do case "$4" in *"$m"*) echo "$4: no such image"; exit 1;; esac; done
  echo "$4: found, includes linux/$6"; exit 0
fi
exec PYTHON "$@"''',
}


@dataclass
class Result:
    code: int
    out: str
    changes: list[str]
    state: dict[str, Any]

    def lines(self, mark: str) -> list[str]:
        return [line.strip()[2:] for line in self.out.splitlines() if line.strip().startswith(mark)]


@pytest.fixture
def pi(tmp_path: Path) -> Callable[..., Result]:
    repo, bin_ = tmp_path / "ringhub", tmp_path / "bin"
    (repo / "deploy" / "pi").mkdir(parents=True)
    bin_.mkdir()
    for script in ("preflight.sh", "serve.sh"):
        shutil.copy(ROOT / "deploy" / "pi" / script, repo / "deploy" / "pi" / script)
    shutil.copy(ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    (repo / "src").symlink_to(ROOT / "src")
    fakes = {**FAKES, "tailscale": FAKE_TAILSCALE.replace("HOST", HOST)}
    for name, body in fakes.items():
        f = bin_ / name
        text = body.replace("PYTHON", sys.executable)
        f.write_text(text if text.startswith("#!") else "#!/usr/bin/env bash\n" + text + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def run(script: str, *args: str, env_file: str | None = ENV_8445, status: dict[str, Any] | None = None,
            **fake: str) -> Result:
        env_path = repo / ".env"
        if env_file is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(env_file)
        state, log = tmp_path / "state.json", tmp_path / "changes.log"
        state.write_text(json.dumps(PI_STATUS if status is None else status))
        log.write_text("")
        env = {"PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(tmp_path), "FAKE_TS_STATE": str(state),
               "FAKE_TS_LOG": str(log), "FAKE_BRANCH": BRANCH, "FAKE_HEAD": HEAD, "FAKE_REMOTE": HEAD,
               "FAKE_LISTEN": "8444", **{k.upper(): v for k, v in fake.items()}}
        if script == "serve.sh":
            env.setdefault("FAKE_UID", "0")
        r = subprocess.run(["bash", str(repo / "deploy" / "pi" / script), *args], env=env,
                           capture_output=True, text=True, timeout=120, check=False)
        out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
        return Result(r.returncode, out, log.read_text().splitlines(), json.loads(state.read_text()))
    return run


# --- preflight --------------------------------------------------------------------------------


def test_preflight_on_the_real_pi_passes_and_never_looks_at_8444(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh")
    assert r.code == 0, r.out
    assert r.out.startswith(f"ringhub preflight {HEAD[:7]} ({BRANCH})")
    assert "up to date with GitHub" in r.lines("✓")
    assert "ports: ringhub 8445, ntfy 8446 (from .env)" in r.lines("✓")
    assert "tailnet port 8445 already publishes ringhub (from an earlier install; serve.sh keeps it)" in r.lines("✓")
    assert "tailnet port 8446 already publishes ntfy (from an earlier install; serve.sh keeps it)" in r.lines("✓")
    assert not r.lines("✗")
    assert "8444" not in r.out                     # the other program's port is none of our business
    assert ":8443 https [PUBLIC (Funnel)] / → http://127.0.0.1:8099" in r.out   # listed, not judged
    assert "Ready to upgrade" in r.out and r.changes == []


def test_preflight_catches_an_out_of_date_checkout(pi: Callable[..., Result]) -> None:
    """The confusion this prevents: an old preflight checking ports 8443/8444."""
    r = pi("preflight.sh", fake_remote=NEWER)
    assert r.code == 1
    assert f"out of date: GitHub has {NEWER[:7]}, this is {HEAD[:7]}. Run: git pull   then run this check again" \
        in r.lines("✗")


def test_preflight_ahead_of_github_or_offline_still_works(pi: Callable[..., Result]) -> None:
    ahead = pi("preflight.sh", fake_remote=OLDER, fake_ancestors=OLDER)
    assert ahead.code == 0 and any(line.startswith("ahead of GitHub") for line in ahead.lines("✓"))
    offline = pi("preflight.sh", fake_remote="")
    assert offline.code == 0 and any("couldn't ask GitHub" in line for line in offline.lines("!"))
    pinned = pi("preflight.sh", fake_branch="HEAD")
    assert pinned.code == 0 and any(line.startswith("pinned to commit") for line in pinned.lines("✓"))


def test_preflight_asks_for_a_missing_image_tag(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", env_file=ENV_8445.replace("RINGHUB_TAG=sha-8b75463\n", ""))
    assert r.code == 1
    assert any("no RINGHUB_TAG" in line for line in r.lines("✗"))
    assert "RINGHUB_TAG=claude-pebble-ring-automation-kqoqn3" in r.out


def test_preflight_refuses_a_port_that_belongs_to_tstream(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8445", ":8443"))
    assert r.code == 1
    assert any(line.startswith("tailnet port 8443 (from your .env) is used by something else") for line in r.lines("✗"))
    assert r.changes == []


def test_preflight_arguments_cannot_contradict_env(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", "8443", "8444")
    assert r.code == 1 and "already uses ports 8445 (ringhub) and 8446 (ntfy)" in r.out


def test_preflight_rejects_a_32_bit_os(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_dpkg="armhf")
    assert r.code == 1 and any("32-bit" in line for line in r.lines("✗"))


def test_preflight_reports_sudo_for_docker_and_uses_it_in_next_steps(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_docker_needs_sudo="1")
    assert r.code == 0
    assert any("with sudo" in line for line in r.lines("✓"))
    assert "sudo docker compose pull && sudo docker compose up -d" in r.out


def test_preflight_names_blocked_download_hosts_and_missing_images(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_blocked="pkg-containers.githubusercontent.com", fake_missing_images="sha-8b75463")
    assert r.code == 1
    fails = r.lines("✗")
    assert any("pkg-containers.githubusercontent.com" in line for line in fails)
    assert any("pebble-custom:sha-8b75463: no such image" in line for line in fails)


def test_preflight_without_serve_access_says_to_use_sudo(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_ts_status_denied="1", fake_sudo_denied="1")
    assert r.code == 0 and any("run this again with sudo" in line for line in r.lines("!"))


def test_preflight_fresh_install_prints_the_exact_next_commands(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", env_file=None, status=ONLY_TSTREAM)
    assert r.code == 0, r.out
    assert "tailnet port 8445 is free for ringhub" in r.lines("✓")
    assert (f"PYTHONPATH=src python3 -m ringhub.envfile --host {HOST} --ring-port 8445 --ntfy-port 8446 "
            f"--tag claude-pebble-ring-automation-kqoqn3 --tz <Area/City> > .env") in r.out


def test_preflight_fresh_install_refuses_a_port_a_local_program_uses(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", "8444", "8446", env_file=None, status=ONLY_TSTREAM)
    assert r.code == 1
    assert any("listens on port 8444" in line for line in r.lines("✗"))


# --- serve.sh ---------------------------------------------------------------------------------


def test_serve_on_the_real_pi_changes_nothing(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh")
    assert r.code == 0, r.out
    assert r.changes == []
    assert ":8445 already publishes ringhub; kept as is" in r.out
    assert r.state == PI_STATUS


def test_serve_off_removes_only_ringhubs_ports(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh", "--off")
    assert r.code == 0, r.out
    assert r.changes == ["serve --https=8445 off", "serve --https=8446 off"]
    assert sorted(r.state["TCP"]) == ["443", "8443"]
    assert r.state["AllowFunnel"] == {f"{HOST}:8443": True}          # Tstream still public


def test_serve_publishes_a_fresh_install_and_nothing_else(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh", status=ONLY_TSTREAM)
    assert r.code == 0, r.out
    assert r.changes == ["serve --bg --yes --https=8445 http://127.0.0.1:8787",
                         "serve --bg --yes --https=8446 http://127.0.0.1:2586"]
    assert r.state["AllowFunnel"] == {f"{HOST}:8443": True}
    assert r.state["Web"][f"{HOST}:8443"] == _site("http://127.0.0.1:8099")
    assert f"ringhub: https://{HOST}:8445    ntfy: https://{HOST}:8446" in r.out


def test_serve_never_touches_a_port_that_is_not_ringhubs(pi: Callable[..., Result]) -> None:
    env = ENV_8445.replace(":8445", ":8443")       # the mistake that once pointed ringhub at Tstream
    r = pi("serve.sh", env_file=env)
    assert r.code == 1 and r.changes == [] and "Nothing changed" in r.out
    off = pi("serve.sh", "--off", env_file=env)
    assert off.code == 0
    assert off.changes == ["serve --https=8446 off"]                 # ntfy's; 8443 is left alone
    assert ":8443 now serves something other than ringhub; left alone" in off.out


def test_serve_refuses_halfway_states(pi: Callable[..., Result]) -> None:
    # One port free, the other someone else's: refuse before publishing either.
    status = json.loads(json.dumps(ONLY_TSTREAM))
    r = pi("serve.sh", env_file=ENV_8445.replace(":8446", ":8443"), status=status)
    assert r.code == 1 and r.changes == []
    # A free port that a local program listens on would be hidden: refuse.
    r = pi("serve.sh", status=ONLY_TSTREAM, fake_listen="8446")
    assert r.code == 1 and r.changes == [] and "used by a program on this machine" in r.out


def test_serve_refuses_ringhub_on_the_public_internet(pi: Callable[..., Result]) -> None:
    status = json.loads(json.dumps(PI_STATUS))
    status["AllowFunnel"][f"{HOST}:8445"] = True
    r = pi("serve.sh", status=status)
    assert r.code == 1 and r.changes == [] and "PUBLIC" in r.out
    off = pi("serve.sh", "--off", status=status)
    assert "serve --https=8445 off" in off.changes and f"{HOST}:8443" in off.state["AllowFunnel"]


@pytest.mark.parametrize(("args", "fake", "message"), [
    ((), {"fake_uid": "1000"}, "run it with sudo"),
    (("--reset",), {}, "usage:"),
    (("8445", "8446"), {}, "usage:"),
])
def test_serve_rejects_bad_invocations(pi: Callable[..., Result], args: tuple[str, ...], fake: dict[str, str],
                                       message: str) -> None:
    r = pi("serve.sh", *args, **fake)
    assert r.code == 2 and message in r.out and r.changes == []


def test_serve_needs_env(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh", env_file=None)
    assert r.code == 2 and ".env not found" in r.out and r.changes == []
