"""What production installs is pinned, hashed and consistent with pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _lock(name: str) -> dict[str, tuple[str, int, str | None]]:
    """{package: (version, number of hashes, marker)} from a uv/pip-compile hashed lock file."""
    text = (ROOT / name).read_text()
    out: dict[str, tuple[str, int, str | None]] = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        first = block.splitlines()[0]
        if first.startswith("#") or not first.strip():
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$", first)
        assert m, f"{name}: not an exact pin: {first!r}"
        out[canonicalize_name(m.group(1))] = (m.group(2), block.count("--hash=sha256:"), m.group(3))
    return out


RUNTIME = _lock("requirements.txt")
DEV = _lock("requirements-dev.txt")
BUILD = _lock("requirements-build.txt")


@pytest.mark.parametrize("lock", ["requirements.txt", "requirements-dev.txt", "requirements-build.txt"])
def test_every_pin_is_exact_and_hashed(lock: str) -> None:
    pins = _lock(lock)
    assert pins
    assert all(hashes >= 1 for _, hashes, _ in pins.values()), lock


def _requirements(*groups: str) -> list[Requirement]:
    reqs = [Requirement(r) for r in PYPROJECT["project"]["dependencies"]]
    for g in groups:
        reqs += [Requirement(r) for r in PYPROJECT["project"]["optional-dependencies"][g]]
    return reqs


@pytest.mark.parametrize(("lock", "groups"), [(RUNTIME, ("llm",)), (DEV, ("llm", "dev"))],
                         ids=["runtime", "dev"])
def test_lock_satisfies_pyproject(lock: dict[str, tuple[str, int, str | None]], groups: tuple[str, ...]) -> None:
    """Changing a dependency in pyproject.toml without re-locking (deploy/ci/lock.sh) fails here."""
    for req in _requirements(*groups):
        name = canonicalize_name(req.name)
        assert name in lock, f"{req} is not locked; run: bash deploy/ci/lock.sh"
        assert req.specifier.contains(lock[name][0], prereleases=True), \
            f"locked {name}=={lock[name][0]} doesn't satisfy {req}; run: bash deploy/ci/lock.sh"


def test_runtime_lock_covers_linux_on_every_supported_python() -> None:
    for py in ("3.11", "3.12", "3.13"):
        for machine in ("aarch64", "x86_64"):
            env = {"sys_platform": "linux", "platform_machine": machine, "python_version": py,
                   "python_full_version": py + ".0", "platform_python_implementation": "CPython",
                   "implementation_name": "cpython", "os_name": "posix", "platform_system": "Linux"}
            active = {n for n, (_, _, marker) in RUNTIME.items() if marker is None or Marker(marker).evaluate(env)}
            assert {"fastapi", "uvicorn", "uvloop", "pydantic-core", "anthropic"} <= active, (py, machine)


def test_dev_lock_ships_the_runtime_versions_ci_tests() -> None:
    for name, (version, _, _) in RUNTIME.items():
        assert DEV.get(name, (None,))[0] == version, f"{name}: dev lock differs from runtime lock"


def test_build_lock_matches_build_system() -> None:
    backend = [Requirement(r) for r in PYPROJECT["build-system"]["requires"]]
    for req in backend:
        name = canonicalize_name(req.name)
        assert name in BUILD and req.specifier.contains(BUILD[name][0])
    for name, (version, _, _) in BUILD.items():
        if name in DEV:
            assert DEV[name][0] == version, f"{name}: build and dev locks disagree (CI installs both)"


def test_images_are_pinned_by_digest() -> None:
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text(), re.MULTILINE)
    assert len(froms) == 2 and len(set(froms)) == 1, froms          # one base, both stages
    assert re.fullmatch(r"python:[\w.-]+@sha256:[0-9a-f]{64}", froms[0])
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"image:\s*binwiederhier/ntfy:v[\d.]+@sha256:[0-9a-f]{64}", compose)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for ref in re.findall(r"(?:image[:=]\s*)((?:docker\.io/)?[a-z]+/[a-z]+:\S+)", workflow):
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"unpinned image in ci.yml: {ref}"


def test_actions_are_pinned_to_commits() -> None:
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*([^\s#]+)", line)
            if m and not m.group(1).startswith("./"):
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", m.group(1)), f"{wf.name}: {line.strip()}"


def test_dockerfile_never_installs_unverified_python_packages() -> None:
    text = (ROOT / "Dockerfile").read_text()
    runs = re.findall(r"^RUN (.*?)(?<!\\)$", text, re.MULTILINE | re.DOTALL)
    pip_cmds = [c for r in runs for c in re.split(r"&&", r) if re.search(r"\bpip (install|download|wheel)\b", c)]
    assert pip_cmds
    for cmd in pip_cmds:
        local = "--no-index" in cmd
        assert local or "--require-hashes" in cmd, f"network pip without hashes: {cmd.strip()}"
        assert "--no-deps" in cmd or "--only-binary=:all:" in cmd, f"may build from source: {cmd.strip()}"
    assert "# syntax=" not in text      # no frontend image pulled at build time
