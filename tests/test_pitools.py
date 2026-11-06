"""The decisions behind deploy/pi/*.sh: ports, Tailscale mappings, and published images."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ringhub import pitools
from ringhub.pitools import (
    NTFY_TARGET,
    RING_TARGET,
    PiError,
    branch_tag,
    check_image,
    compose_images,
    describe_mappings,
    parse_env,
    parse_image_ref,
    parse_serve_status,
    resolve_ports,
    serve_state,
)

ROOT = Path(__file__).resolve().parents[1]
HOST = "tesla-stream.tail51b89d.ts.net"

# The real layout this was written for: Tstream on public 8443, ringhub on 8445 (plus an old
# duplicate on 443), ntfy on 8446.
PI_STATUS: dict[str, Any] = {
    "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}, "8445": {"HTTPS": True}, "8446": {"HTTPS": True}},
    "Web": {
        f"{HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}},
        f"{HOST}:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8099"}}},
        f"{HOST}:8445": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}},
        f"{HOST}:8446": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:2586"}}},
    },
    "AllowFunnel": {f"{HOST}:8443": True},
}


# --- .env and ports ---------------------------------------------------------------------------


def test_parse_env_follows_compose_rules() -> None:
    env = parse_env(
        "# comment\n"
        "RINGHUB_PUBLIC_URL=https://pi.t.ts.net:8445\n"
        "NTFY_TOKEN=            # filled in later\n"
        "QUOTED='a # not a comment'\n"
        'DQ="x"\n'
        "export TZ=America/New_York\n"
        "HASH=abc#def\n"
        "not a line\n")
    assert env == {"RINGHUB_PUBLIC_URL": "https://pi.t.ts.net:8445", "NTFY_TOKEN": "",
                   "QUOTED": "a # not a comment", "DQ": "x", "TZ": "America/New_York", "HASH": "abc#def"}


def _env(ring: str = "https://pi.t.ts.net:8445", ntfy: str = "https://pi.t.ts.net:8446") -> dict[str, str]:
    return {"RINGHUB_PUBLIC_URL": ring, "NTFY_PUBLIC_URL": ntfy}


def test_ports_come_from_env_when_it_exists() -> None:
    assert resolve_ports(_env()) == (8445, 8446, ".env")
    assert resolve_ports(_env(ring="https://pi.t.ts.net")) == (443, 8446, ".env")
    assert resolve_ports(_env(), "8445", "8446") == (8445, 8446, "arguments")  # agreeing args are fine


def test_ports_arguments_that_disagree_with_env_are_refused() -> None:
    """The bug this prevents: .env said 8443 while serve.sh published 8445."""
    with pytest.raises(PiError, match="already uses ports 8445"):
        resolve_ports(_env(), "8443", "8444")


def test_ports_defaults_avoid_funnel_ports() -> None:
    ring, ntfy, source = resolve_ports(None)
    assert source == "defaults" and {ring, ntfy}.isdisjoint({443, 8443, 10000})
    assert resolve_ports(None, "9001", "9002") == (9001, 9002, "arguments")


@pytest.mark.parametrize(("env", "ring", "ntfy", "match"), [
    (None, "8445", None, "both ports"),
    (None, "9000", "9000", "different ports"),
    (None, "0", "9000", "out of range"),
    (None, "abc", "9000", "not a port"),
    ({"RINGHUB_PUBLIC_URL": "https://pi.t.ts.net:8445"}, None, None, "NTFY_PUBLIC_URL"),
    (_env(ring="http://pi.t.ts.net:8445"), None, None, "must look like https"),
    (_env(ring="https://pi.t.ts.net:99999"), None, None, "invalid port"),
    (_env(ntfy="https://pi.t.ts.net:8445"), None, None, "different ports"),
])
def test_ports_rejects_bad_input(env: dict[str, str] | None, ring: str | None, ntfy: str | None,
                                 match: str) -> None:
    with pytest.raises(PiError, match=match):
        resolve_ports(env, ring, ntfy)


def test_envfile_and_pitools_agree_on_ports(tmp_path: Path) -> None:
    from ringhub.envfile import generate

    env_file = tmp_path / ".env"
    env_file.write_text(generate("pi.tail1234.ts.net", tz="UTC"))
    assert pitools.resolve_ports(pitools.read_env(env_file))[:2] == (8445, 8446)
    assert pitools.read_env(tmp_path / "missing") is None


# --- tailscale serve --------------------------------------------------------------------------


def test_real_pi_layout_is_classified_correctly() -> None:
    m = parse_serve_status(PI_STATUS)
    assert serve_state(m, 8445, RING_TARGET) == "ours"
    assert serve_state(m, 8446, NTFY_TARGET) == "ours"
    assert serve_state(m, 443, RING_TARGET) == "ours"          # old duplicate: recognised, not "other"
    assert serve_state(m, 8443, RING_TARGET) == "other"        # Tstream: never ours
    assert serve_state(m, 8443, NTFY_TARGET) == "other"
    assert serve_state(m, 8445, NTFY_TARGET) == "other"        # ringhub's port is not ntfy's
    assert serve_state(m, 8444, RING_TARGET) == "free"
    lines = describe_mappings(m)
    assert any(":8443" in line and "PUBLIC" in line and "8099" in line and "←" not in line for line in lines)
    assert any(":8445" in line and "tailnet only" in line and line.endswith("← ringhub") for line in lines)
    assert any(":8446" in line and line.endswith("← ntfy") for line in lines)


def test_ringhub_published_with_funnel_is_flagged() -> None:
    status = json.loads(json.dumps(PI_STATUS))
    status["AllowFunnel"][f"{HOST}:8445"] = True
    assert serve_state(parse_serve_status(status), 8445, RING_TARGET) == "ours-public"


@pytest.mark.parametrize("handlers", [
    {"/": {"Proxy": "http://127.0.0.1:8787"}, "/api": {"Proxy": "http://127.0.0.1:9000"}},  # shared port
    {"/ring": {"Proxy": "http://127.0.0.1:8787"}},                                           # sub-path
    {"/": {"Proxy": "http://192.168.1.5:8787"}},                                             # not local
    {"/": {"Proxy": "https+insecure://127.0.0.1:8787"}},
    {"/": {"Path": "/srv/www"}},
    {"/": {"Text": "hi"}},
])
def test_anything_but_an_exact_local_proxy_is_someone_elses(handlers: dict[str, Any]) -> None:
    status = {"TCP": {"8445": {"HTTPS": True}}, "Web": {f"{HOST}:8445": {"Handlers": handlers}}}
    assert serve_state(parse_serve_status(status), 8445, RING_TARGET) == "other"


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:8787", "http://localhost:8787/", "127.0.0.1:8787"])
def test_equivalent_spellings_of_ringhubs_target_are_ours(proxy: str) -> None:
    status = {"TCP": {"8445": {"HTTPS": True}}, "Web": {f"{HOST}:8445": {"Handlers": {"/": {"Proxy": proxy}}}}}
    assert serve_state(parse_serve_status(status), 8445, RING_TARGET) == "ours"


def test_tcp_forwards_and_foreground_sessions_count_as_used() -> None:
    status = {
        "TCP": {"8445": {"TCPForward": "127.0.0.1:8787"}},
        "Foreground": {"sess1": {"TCP": {"8446": {"HTTPS": True}},
                                 "Web": {f"{HOST}:8446": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:2586"}}}}}},
    }
    m = parse_serve_status(status)
    assert serve_state(m, 8445, RING_TARGET) == "other"   # raw TCP, not ringhub's HTTPS proxy
    assert serve_state(m, 8446, NTFY_TARGET) == "ours"


@pytest.mark.parametrize("doc", [{}, None, [], {"TCP": None}, {"TCP": {"x": {}}}, {"TCP": {"80": "bad"}}])
def test_empty_or_odd_status_means_nothing_is_served(doc: Any) -> None:
    assert parse_serve_status(doc) == {}


def test_cli_serve_state_reads_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(PI_STATUS)))
    assert pitools.main(["serve-state", "8445", RING_TARGET]) == 0
    assert capsys.readouterr().out.strip() == "ours"
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert pitools.main(["serve-state", "8445", RING_TARGET]) == 0
    assert capsys.readouterr().out.strip() == "free"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert pitools.main(["serve-show"]) == 2


SS = """State  Recv-Q Send-Q Local Address:Port  Peer Address:PortProcess
LISTEN 0      4096       127.0.0.1:8787       0.0.0.0:*
LISTEN 0      4096           [::1]:8446          [::]:*
LISTEN 0      4096   127.0.0.53%lo:8445       0.0.0.0:*
LISTEN 0      4096               *:8444             *:*
LISTEN 0      4096            [::]:9000          [::]:*
LISTEN 0      4096   100.75.135.17:9001       0.0.0.0:*
LISTEN 0      4096   [fe80::1%eth0]:9002        [::]:*
"""


@pytest.mark.parametrize(("port", "expect"), [
    (8787, []), (8446, []), (8445, []),                          # loopback only: Serve hides nothing
    (8444, ["*:8444"]), (9000, ["[::]:9000"]), (9001, ["100.75.135.17:9001"]), (9002, ["[fe80::1%eth0]:9002"]),
    (1234, []),
])
def test_listeners_ignore_loopback_only_sockets(port: int, expect: list[str]) -> None:
    assert pitools.listeners(SS, port) == expect


def test_ports_serving_finds_where_a_target_really_is() -> None:
    m = parse_serve_status(PI_STATUS)
    assert pitools.ports_serving(m, RING_TARGET) == [8445, 443]       # Funnel-capable 443 last
    assert pitools.ports_serving(m, NTFY_TARGET) == [8446]
    status = json.loads(json.dumps(PI_STATUS))
    status["AllowFunnel"][f"{HOST}:8446"] = True
    assert pitools.ports_serving(parse_serve_status(status), NTFY_TARGET) == []   # public: never suggested


def test_cli_listeners_and_serve_ports(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(SS))
    assert pitools.main(["listeners", "8444"]) == 0 and capsys.readouterr().out == "*:8444\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(SS))
    assert pitools.main(["listeners", "8787"]) == 1
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(PI_STATUS)))
    assert pitools.main(["serve-ports", NTFY_TARGET]) == 0 and capsys.readouterr().out == "8446\n"


# --- images -----------------------------------------------------------------------------------


def test_compose_images_resolve_the_tag_from_env() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    ring, ntfy = compose_images(compose, {"RINGHUB_TAG": "sha-1c4626a"})
    assert ring == "ghcr.io/waymarklabs-official/pebble-custom:sha-1c4626a"
    assert ntfy.startswith("binwiederhier/ntfy:v") and "@sha256:" in ntfy   # pinned by digest
    assert compose_images(compose, {})[0].endswith(":main")
    assert compose_images(compose, {"RINGHUB_TAG": ""})[0].endswith(":main")  # ${VAR:-x}: empty → default


@pytest.mark.parametrize(("branch", "tag"), [
    ("claude/pebble-ring-automation-kqoqn3", "claude-pebble-ring-automation-kqoqn3"),
    ("refs/heads/main", "main"),
    ("feat/ünïcode+x", "feat--n-code-x"),
    ("-.odd", "odd"),
])
def test_branch_tag_matches_ci(branch: str, tag: str) -> None:
    assert branch_tag(branch) == tag


def test_branch_tag_rejects_nothing_left() -> None:
    with pytest.raises(PiError):
        branch_tag("---")


@pytest.mark.parametrize(("ref", "registry", "repo", "reference"), [
    ("ghcr.io/waymarklabs-official/pebble-custom:main", "ghcr.io", "waymarklabs-official/pebble-custom", "main"),
    ("python:3.12-slim", "registry-1.docker.io", "library/python", "3.12-slim"),
    ("binwiederhier/ntfy:v2.37.2@sha256:" + "a" * 64, "registry-1.docker.io", "binwiederhier/ntfy",
     "sha256:" + "a" * 64),
    ("docker.io/library/alpine", "registry-1.docker.io", "library/alpine", "latest"),
    ("localhost:5000/x/y:1", "localhost:5000", "x/y", "1"),
])
def test_parse_image_ref(ref: str, registry: str, repo: str, reference: str) -> None:
    r = parse_image_ref(ref)
    assert (r.registry, r.repository, r.reference) == (registry, repo, reference)


@pytest.mark.parametrize("ref", ["UPPER/case:1", "x@sha256:short", "ghcr.io/:tag"])
def test_parse_image_ref_rejects_garbage(ref: str) -> None:
    with pytest.raises(PiError):
        parse_image_ref(ref)


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _http_error(url: str, code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "err", msg, io.BytesIO(b"{}"))


def _index(*platforms: tuple[str, str]) -> bytes:
    return json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": "sha256:" + "0" * 64, "platform": {"os": os_, "architecture": arch}} for os_, arch in platforms
    ] + [{"digest": "sha256:" + "1" * 64, "platform": {"os": "unknown", "architecture": "unknown"}}]}).encode()


def _registry(body: bytes | None = None, *, manifest_error: int | None = None,
              token_error: int | None = None) -> tuple[Callable[..., Any], list[Any]]:
    """A fake anonymous registry: 401 + challenge, then token, then the manifest."""
    calls: list[Any] = []

    def opener(req: Any, timeout: float) -> _Resp:
        url = req if isinstance(req, str) else req.full_url
        calls.append(req)
        if "/token?" in url:
            assert "scope=repository%3Awaymarklabs-official%2Fpebble-custom%3Apull" in url
            if token_error:
                raise _http_error(url, token_error)
            return _Resp(json.dumps({"token": "t0k"}).encode())
        if req.get_header("Authorization") != "Bearer t0k":
            raise _http_error(url, 401, {"WWW-Authenticate":
                                         'Bearer realm="https://ghcr.io/token",service="ghcr.io"'})
        if manifest_error:
            raise _http_error(url, manifest_error)
        assert body is not None
        return _Resp(body)
    return opener, calls


REF = "ghcr.io/waymarklabs-official/pebble-custom:claude-pebble-ring-automation-kqoqn3"


def test_check_image_finds_the_pi_build() -> None:
    opener, calls = _registry(_index(("linux", "amd64"), ("linux", "arm64")))
    ok, msg = check_image(REF, "arm64", opener)
    assert ok and "linux/arm64" in msg and len(calls) == 3
    assert "application/vnd.oci.image.index.v1+json" in calls[-1].get_header("Accept")


def test_check_image_reports_a_missing_architecture() -> None:
    opener, _ = _registry(_index(("linux", "amd64")))
    ok, msg = check_image(REF, "arm64", opener)
    assert not ok and "no linux/arm64" in msg and "linux/amd64" in msg


@pytest.mark.parametrize(("kwargs", "expect"), [
    ({"manifest_error": 404}, "no such image"),
    ({"manifest_error": 401}, "access denied"),
    ({"token_error": 403}, "access denied"),
    ({"manifest_error": 500}, "HTTP 500"),
    ({"body": b"<html>captive portal</html>"}, "isn't a manifest"),
])
def test_check_image_explains_failures(kwargs: dict[str, Any], expect: str) -> None:
    opener, _ = _registry(**kwargs)
    ok, msg = check_image(REF, "arm64", opener)
    assert not ok and expect in msg


def test_check_image_network_blocked() -> None:
    def opener(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError("timed out")
    ok, msg = check_image(REF, "arm64", opener)
    assert not ok and "can't reach ghcr.io" in msg


def test_check_image_verifies_a_pinned_digest() -> None:
    body = _index(("linux", "arm64"))
    good = "sha256:" + hashlib.sha256(body).hexdigest()
    opener, _ = _registry(body)
    assert check_image(f"ghcr.io/waymarklabs-official/pebble-custom@{good}", "arm64", opener)[0]
    opener, _ = _registry(body)
    ok, msg = check_image("ghcr.io/waymarklabs-official/pebble-custom@sha256:" + "f" * 64, "arm64", opener)
    assert not ok and "doesn't match" in msg


def test_check_image_single_platform_is_accepted() -> None:
    opener, _ = _registry(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json"}).encode())
    ok, msg = check_image(REF, "arm64", opener)
    assert ok and "single-platform" in msg


# --- the scripts themselves -------------------------------------------------------------------


def test_cli_ports_and_env_get(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    env.write_text("RINGHUB_PUBLIC_URL=https://pi.t.ts.net:8445\nNTFY_PUBLIC_URL=https://pi.t.ts.net:8446\n"
                   "RINGHUB_TAG=sha-1a2b3c4\n")
    assert pitools.main(["ports", "--env", str(env)]) == 0
    assert capsys.readouterr().out == "8445 8446 .env\n"
    assert pitools.main(["ports", "--env", str(env), "8443", "8444"]) == 2
    assert "already uses ports" in capsys.readouterr().err
    assert pitools.main(["env-get", "--env", str(env), "RINGHUB_TAG"]) == 0
    assert capsys.readouterr().out == "sha-1a2b3c4\n"
    assert pitools.main(["env-get", "--env", str(tmp_path / "none"), "RINGHUB_TAG"]) == 0
    assert capsys.readouterr().out == "\n"


@pytest.mark.parametrize("script", sorted((ROOT / "deploy").rglob("*.sh")), ids=lambda p: p.name)
def test_shell_scripts_parse(script: Path) -> None:
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_scripts_never_reset_or_remove_unverified_ports() -> None:
    for script in (ROOT / "deploy" / "pi").glob("*.sh"):
        code = "\n".join(line for line in script.read_text().splitlines() if not line.lstrip().startswith("#"))
        assert "serve reset" not in code, script
        assert "tailscale funnel" not in code, script   # never changes what is public
        for line in code.splitlines():
            if " off" in line and "tailscale serve" in line:
                assert '--https="$port" off' in line, f"{script.name}: {line.strip()}"


def test_pitools_runs_without_any_dependencies_installed() -> None:
    src = ROOT / "src"
    code = ("import sys; import ringhub.pitools as p; "
            "heavy = [m for m in ('httpx', 'pydantic', 'fastapi', 'yaml', 'jinja2') if m in sys.modules]; "
            "assert not heavy, heavy; sys.exit(p.main(['branch-tag', 'claude/x']))")
    r = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": str(src)},
                       capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == "claude-x\n", r.stderr
