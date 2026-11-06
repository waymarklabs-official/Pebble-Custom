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
# The real Pi: another program on 8444; both containers and Tstream bound to loopback.
PI_LISTEN = "*:8444 127.0.0.1:8787 127.0.0.1:2586 127.0.0.1:8099"
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
    root = os.environ.get("FAKE_AS_ROOT") == "1" or os.environ.get("FAKE_UID") == "0"
    operator = os.environ.get("FAKE_TS_OPERATOR") == "1"          # tailscale up --operator=$USER
    if os.environ.get("FAKE_TS_STATUS_DENIED") or not (root or operator):   # like tailscaled without an operator
        sys.exit("access denied")
    print(json.dumps(doc) if "--json" in a else "\n".join(sorted(doc.get("Web", {})))); sys.exit()
if a[:1] == ["serve"]:
    open(log, "a").write(" ".join(a) + "\n")
    port = next(x.split("=", 1)[1] for x in a if x.startswith("--https="))
    if a[-1] == "off":
        doc["TCP"].pop(port, None); doc["Web"].pop(f"{host}:{port}", None)
        doc.get("AllowFunnel", {}).pop(f"{host}:{port}", None)
    elif port == os.environ.get("FAKE_TS_FAIL_PORT"):
        sys.exit("fake tailscale: failed to publish " + port)
    else:
        doc.setdefault("TCP", {})[port] = {"HTTPS": True}
        doc.setdefault("Web", {})[f"{host}:{port}"] = {"Handlers": {"/": {"Proxy": a[-1]}}}
    json.dump(doc, open(state, "w")); sys.exit()
sys.exit(f"fake tailscale: unhandled {a}")
'''

# Each fake accepts exactly the invocations the scripts are meant to make and fails loudly on
# anything else, so a script that drifts from the real tool's interface fails here too.
FAKES = {
    "uname": '[ "$*" = -m ] || { echo "fake uname: $*" >&2; exit 1; }; echo aarch64',
    "dpkg": '[ "$*" = --print-architecture ] || exit 1; echo "${FAKE_DPKG:-arm64}"',
    "id": """case "$*" in
  -u) echo "${FAKE_UID:-1000}";;
  -un) if [ "${FAKE_UID:-1000}" = 0 ]; then echo root; else echo pi; fi;;
  *) echo "fake id: $*" >&2; exit 1;;
esac""",
    # Real sudo would ask for a password (and hang a script) without -n. Root never needs one.
    # `-u <user>` runs the command as that user (uid 1000 here), without root's access.
    "sudo": """[ "$1" = -n ] || { echo "sudo: a password is required" >&2; exit 1; }; shift
as_root=1
if [ "${1:-}" = -u ]; then [ -n "${2:-}" ] || exit 1; as_root=""; shift 2; fi
case "${1:-}" in -*|"") echo "fake sudo: unexpected: $*" >&2; exit 1;; esac
[ "${FAKE_UID:-1000}" != 0 ] && [ -n "${FAKE_SUDO_DENIED:-}" ] && exit 1
if [ -n "$as_root" ]; then FAKE_AS_ROOT=1 FAKE_UID=0 exec "$@"; fi
FAKE_AS_ROOT="" FAKE_UID=1000 exec "$@\"""",
    "docker": """case "$*" in
  --version) echo "Docker version 29.0.0, build fake";;
  info) [ -n "${FAKE_DOCKER_NEEDS_SUDO:-}" ] && [ "${FAKE_AS_ROOT:-}" != 1 ] && exit 1; exit 0;;
  "version --format {{.Server.Arch}}") echo "${FAKE_DOCKER_ARCH:-arm64}";;
  "compose version") echo "Docker Compose version v2.40.0";;
  *) echo "fake docker: unexpected: $*" >&2; exit 1;;
esac""",
    "curl": """case "$*" in
  "-sS -o /dev/null -m 15 -w %{http_code} https://"*) ;;
  *) echo "fake curl: $*" >&2; exit 2;;
esac
url="${@: -1}"
for h in ${FAKE_BLOCKED:-}; do case "$url" in *"$h"*) printf 000; exit 0;; esac; done
printf 401""",
    # FAKE_LISTEN holds real `ss -ltn` local-address forms: *:P, [::]:P, 0.0.0.0:P, 127.0.0.1:P, ...
    "ss": """[ "$*" = -ltn ] || { echo "fake ss: $*" >&2; exit 1; }
echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:PortProcess"
for a in ${FAKE_LISTEN:-}; do echo "LISTEN 0      4096   $a  0.0.0.0:*"; done""",
    # FAKE_UPSTREAM: what branch.<name>.remote/merge say ("" = no upstream). FAKE_REMOTE_BRANCH: the
    # branch GitHub has. FAKE_DIRTY / FAKE_DIFF ("S:path|..."): local changes; FAKE_COMMITTED: files
    # that local commits on top of FAKE_REMOTE change. ssh: git uses GIT_SSH_COMMAND, else GIT_SSH,
    # else core.sshCommand (FAKE_SSH_COMMAND), else plain ssh, which would prompt without BatchMode.
    "git": """[ "$1 $2" = "-c safe.directory=$(pwd -P)" ] \\
  || { echo "fake git: no safe.directory for $(pwd -P): $*" >&2; exit 129; }
shift 2
if [ "$1" = for-each-ref ]; then
  [ $# = 3 ] && [ "$2" = "--format=%(upstream:remotename) %(upstream:remoteref)" ] \\
    && [ "$3" = "refs/heads/$FAKE_BRANCH" ] || { echo "fake git: unexpected: $*" >&2; exit 129; }
  u="${FAKE_UPSTREAM-origin refs/heads/$FAKE_BRANCH}"; echo "${u:- }"; exit 0
fi
if [ "$1 $2 $3 $4 $5" = "--no-optional-locks diff --no-renames --name-status -z" ] && [ $# = 7 ] && [ "$7" = -- ]; then
  # Real `git diff` rewrites the index it reads, even with --no-optional-locks: only a copy is safe.
  [ -f "${GIT_INDEX_FILE:-}" ] && [ "$(cd "$(dirname "$GIT_INDEX_FILE")" && pwd -P)" != "$(pwd -P)/.git" ] \\
    || { echo "fake git: diff would rewrite .git/index" >&2; exit 129; }
  case "$6" in
    HEAD) ;;
    "$FAKE_REMOTE") for f in ${FAKE_COMMITTED:-}; do printf 'M\\0%s\\0' "$f"; done;;
    *) echo "fatal: bad revision '$6'" >&2; exit 128;;
  esac
  for f in ${FAKE_DIRTY:-}; do printf 'M\\0%s\\0' "$f"; done
  IFS='|' read -r -a extra <<<"${FAKE_DIFF:-}"
  for e in ${extra[@]+"${extra[@]}"}; do printf '%s\\0%s\\0' "${e%%:*}" "${e#*:}"; done
  exit 0
fi
case "$*" in
  "rev-parse HEAD") [ -n "${FAKE_HEAD:-}" ] || exit 128; echo "$FAKE_HEAD";;
  "rev-parse --git-path index") [ -n "${FAKE_HEAD:-}" ] || exit 128; echo .git/index;;
  "symbolic-ref --quiet --short HEAD") [ -n "${FAKE_BRANCH:-}" ] || exit 1; echo "$FAKE_BRANCH";;
  "config core.sshCommand") [ -n "${FAKE_SSH_COMMAND:-}" ] || exit 1; echo "$FAKE_SSH_COMMAND";;
  "ls-remote --exit-code "*)
    [ $# = 4 ] || { echo "fake git: unexpected: $*" >&2; exit 129; }
    [ -n "${FAKE_OFFLINE:-}" ] && exit 128
    case " ${FAKE_REMOTES:-origin} " in
      *" $3 "*) ;;
      *) echo "fatal: '$3' does not appear to be a git repository" >&2; exit 128;;
    esac
    want="${FAKE_NEEDS_SSH:-ssh -o BatchMode=yes -o ConnectTimeout=10}"
    used="${GIT_SSH_COMMAND:-${GIT_SSH:-${FAKE_SSH_COMMAND:-ssh}}}"
    [ "$used" = "$want" ] || { echo "fake git: ssh would run '$used', not '$want'" >&2; exit 128; }
    [ "$4" = "refs/heads/${FAKE_REMOTE_BRANCH-$FAKE_BRANCH}" ] || exit 2      # real git: a missing ref exits 2
    printf '%s\\t%s\\n' "$FAKE_REMOTE" "$4";;
  "merge-base --is-ancestor "*" HEAD") case " ${FAKE_ANCESTORS:-} " in *" $3 "*) exit 0;; esac; exit 1;;
  *) echo "fake git: unexpected: $*" >&2; exit 129;;
esac""",
    # Everything goes to the real Python except the registry lookup (tested in test_pitools.py).
    "python3": """if [ "${1:-}" = -m ] && [ "${2:-}" = ringhub.pitools ] && [ "${3:-}" = image ]; then
  for m in ${FAKE_MISSING_IMAGES:-}; do case "$4" in *"$m"*) echo "$4: no such image"; exit 1;; esac; done
  echo "$4: found, includes linux/$6"; exit 0
fi
exec PYTHON "$@\"""",
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
    repo, bin_, bin_git = tmp_path / "ringhub", tmp_path / "bin", tmp_path / "bin-git"
    (repo / "deploy" / "pi").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / "index").write_bytes(b"")
    bin_.mkdir()
    bin_git.mkdir()
    for script in ("preflight.sh", "serve.sh"):
        shutil.copy(ROOT / "deploy" / "pi" / script, repo / "deploy" / "pi" / script)
    shutil.copy(ROOT / "docker-compose.yml", repo / "docker-compose.yml")
    (repo / "src").symlink_to(ROOT / "src")
    link = tmp_path / "ringhub-link"
    link.symlink_to(repo, target_is_directory=True)
    fakes = {**FAKES, "tailscale": FAKE_TAILSCALE.replace("HOST", HOST)}
    for name, body in fakes.items():
        f = (bin_git if name == "git" else bin_) / name
        text = body.replace("PYTHON", sys.executable)
        f.write_text(text if text.startswith("#!") else "#!/usr/bin/env bash\n" + text + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def run(script: str, *args: str, env_file: str | None = ENV_8445, status: dict[str, Any] | None = None,
            compose: str | None = None, real_git: bool = False, **fake: str) -> Result:
        (repo / "docker-compose.yml").write_text(compose or (ROOT / "docker-compose.yml").read_text())
        env_path = repo / ".env"
        if env_file is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(env_file)
        state, log = tmp_path / "state.json", tmp_path / "changes.log"
        state.write_text(json.dumps(PI_STATUS if status is None else status))
        log.write_text("")
        path = f"{bin_}:{os.environ['PATH']}" if real_git else f"{bin_git}:{bin_}:{os.environ['PATH']}"
        env = {"PATH": path, "HOME": str(tmp_path), "FAKE_TS_STATE": str(state),
               "FAKE_TS_LOG": str(log), "FAKE_BRANCH": BRANCH, "FAKE_HEAD": HEAD, "FAKE_REMOTE": HEAD,
               "FAKE_LISTEN": PI_LISTEN, **{k.upper(): v for k, v in fake.items()}}
        if script == "serve.sh":
            env.setdefault("FAKE_UID", "0")
        # Run through a symlinked path, as from a symlinked ~/ringhub: git needs the physical path.
        r = subprocess.run(["bash", str(link / "deploy" / "pi" / script), *args], env=env,
                           capture_output=True, text=True, timeout=120, check=False)
        out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
        return Result(r.returncode, out, log.read_text().splitlines(), json.loads(state.read_text()))
    return run


# --- preflight --------------------------------------------------------------------------------


def test_preflight_on_the_real_pi_passes_and_never_looks_at_8444(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh")
    assert r.code == 0, r.out
    assert r.out.startswith(f"ringhub preflight {HEAD[:7]} ({BRANCH})")
    assert f"up to date with GitHub ({HEAD[:7]})" in r.lines("✓")
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
    assert ahead.code == 0 and f"ahead of GitHub (local commits on top of {OLDER[:7]})" in ahead.lines("✓")
    offline = pi("preflight.sh", fake_offline="1")
    assert offline.code == 0 and any("couldn't ask GitHub" in line for line in offline.lines("!"))


def test_preflight_says_when_the_branch_is_gone_from_github(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_remote_branch="something-else")       # merged and deleted
    assert r.code == 1
    assert f"GitHub no longer has the branch {BRANCH} (merged?). Switch to main: git checkout main && git pull" \
        in r.lines("✗")


def test_preflight_a_branch_that_was_never_pushed_is_not_called_merged(pi: Callable[..., Result]) -> None:
    """No upstream configured: missing on GitHub means 'not pushed', not 'deleted, switch to main'."""
    r = pi("preflight.sh", fake_upstream="", fake_remote_branch="something-else")
    assert r.code == 0, r.out
    assert not any("no longer has" in line for line in r.lines("✗"))
    assert any(line.startswith(f"{BRANCH} isn't on GitHub (never pushed?), so no tested image exists for it")
               for line in r.lines("!"))
    pushed = pi("preflight.sh", fake_upstream="")          # pushed without -u: still compared with GitHub
    assert f"up to date with GitHub ({HEAD[:7]})" in pushed.lines("✓")


def test_preflight_reads_the_upstream_from_config(pi: Callable[..., Result]) -> None:
    """A remote named fork/pi: splitting '@{u}' at the first '/' asked a remote called 'fork'."""
    r = pi("preflight.sh", fake_upstream=f"fork/pi refs/heads/{BRANCH}", fake_remotes="fork/pi", fake_remote=NEWER)
    assert r.code == 1
    assert f"out of date: GitHub has {NEWER[:7]}, this is {HEAD[:7]}. Run: git pull   then run this check again" \
        in r.lines("✗")
    local = pi("preflight.sh", fake_upstream=". refs/heads/main")       # follows a local branch
    assert any(line.startswith(f"{BRANCH} follows the local branch main, not one on GitHub")
               for line in local.lines("!"))
    assert not any("GitHub" in line for line in local.lines("✓"))


@pytest.mark.parametrize("ssh", [{"fake_ssh_command": "ssh -i ~/.ssh/deploy"},       # core.sshCommand
                                 {"git_ssh_command": "ssh -i ~/.ssh/deploy"},
                                 {"git_ssh": "/usr/local/bin/deploy-ssh"}])
def test_preflight_keeps_a_configured_ssh_command(pi: Callable[..., Result], ssh: dict[str, str]) -> None:
    """A deploy key in core.sshCommand/GIT_SSH(_COMMAND): overriding it hid an out-of-date checkout."""
    r = pi("preflight.sh", fake_needs_ssh=next(iter(ssh.values())), fake_remote=NEWER, **ssh)
    assert r.code == 1 and any(line.startswith("out of date: GitHub has") for line in r.lines("✗")), r.out


def test_preflight_suggests_the_tag_of_the_github_branch_it_follows(pi: Callable[..., Result]) -> None:
    """Local branch pi2 tracking the GitHub branch: CI only tags images by GitHub's branch name."""
    r = pi("preflight.sh", env_file=ENV_8445.replace("RINGHUB_TAG=sha-8b75463\n", ""), fake_branch="pi2",
           fake_upstream=f"origin refs/heads/{BRANCH}", fake_remote_branch=BRANCH, fake_missing_images=":pi2")
    assert f"up to date with GitHub ({HEAD[:7]})" in r.lines("✓")
    assert "RINGHUB_TAG=claude-pebble-ring-automation-kqoqn3" in r.out
    assert "RINGHUB_TAG=pi2" not in r.out and ":pi2" not in r.out


def test_preflight_out_of_date_with_changed_files_says_to_put_them_back_first(pi: Callable[..., Result]) -> None:
    """`git pull` refuses to overwrite a changed file; stashing or committing is how a Pi got stuck."""
    r = pi("preflight.sh", fake_remote=NEWER, fake_dirty="docker-compose.yml")
    assert r.code == 1
    fails = r.lines("✗")
    assert fails[0] == (f"out of date: GitHub has {NEWER[:7]}, this is {HEAD[:7]}. First put back the files "
                        "changed on this machine (below; don't stash or commit them), then: git pull   then run "
                        "this check again")
    assert fails[1].startswith("files changed on this machine")
    assert "put the files back: git checkout HEAD -- docker-compose.yml" in r.out


def test_preflight_ahead_of_github_with_committed_changes_is_not_a_tick(pi: Callable[..., Result]) -> None:
    """A committed compose edit: 'git checkout HEAD -- …' restores the edit itself, so name GitHub's commit."""
    old = (ROOT / "docker-compose.yml").read_text().replace(
        "image: ghcr.io/waymarklabs-official/pebble-custom:${RINGHUB_TAG:-main}", "image: ringhub:latest")
    r = pi("preflight.sh", compose=old, fake_remote=OLDER, fake_ancestors=OLDER, fake_committed="docker-compose.yml",
           fake_missing_images="ringhub:latest")
    assert r.code == 1
    assert not any(line.startswith("ahead of GitHub") for line in r.lines("✓"))
    assert (f"ahead of GitHub (local commits on top of {OLDER[:7]}), and these files differ from GitHub's, so "
            "this isn't the tested setup (settings belong in .env and config.yaml):") in r.lines("✗")
    assert (f"see the changes: git diff {OLDER[:7]}      put the files back: git checkout {OLDER[:7]} -- "
            "docker-compose.yml") in r.out
    assert ("docker-compose.yml doesn't run the published ringhub image (it names ringhub:latest). "
            f"Put it back: git checkout {OLDER[:7]} -- docker-compose.yml") in r.lines("✗")


def test_preflight_put_back_command_is_safe_to_paste(pi: Callable[..., Result]) -> None:
    """A staged rename once printed `git checkout HEAD -- a -> b`, which truncated b when pasted."""
    r = pi("preflight.sh", fake_diff="D:config.example.yaml|A:my config.yaml|M:a;b $(x)")
    assert r.code == 1
    listed = r.out[r.out.index("files changed on this machine"):].splitlines()[1:4]
    assert [line.strip() for line in listed] == ["config.example.yaml", "my config.yaml", "a;b $(x)"]
    assert ("put the files back: git checkout HEAD -- config.example.yaml a\\;b\\ \\$\\(x\\) "
            "&& git reset -q HEAD -- my\\ config.yaml") in r.out
    assert "->" not in r.out


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to chown")
def test_preflight_root_owned_git_hint_works_for_any_owner(pi: Callable[..., Result], tmp_path: Path) -> None:
    repo = tmp_path / "ringhub"
    os.chown(repo, 4242, 4242)                      # a uid with no passwd entry: `find ! -user` can't use it
    os.chown(repo / ".git", 4242, 4242)             # .git/index stays root's, as after `sudo git ...`
    r = pi("preflight.sh", fake_remote=NEWER)
    assert "Fix: sudo chown -R 4242:4242 .git   then git pull (without sudo)" in r.out
    os.chown(repo, 0, 0)
    os.chown(repo / ".git" / "index", 4242, 4242)
    r = pi("preflight.sh", fake_remote=NEWER)
    assert "Fix: sudo chown -R root: .git   then git pull (without sudo)" in r.out


def test_preflight_never_gives_a_green_tick_it_cannot_back_up(pi: Callable[..., Result]) -> None:
    detached = pi("preflight.sh", fake_branch="", env_file=ENV_8445.replace("RINGHUB_TAG=sha-8b75463\n", ""))
    assert any(line.startswith(f"not on a branch (detached at {HEAD[:7]})") for line in detached.lines("!"))
    assert f"RINGHUB_TAG=sha-{HEAD[:7]}" in detached.out        # the image of exactly this commit
    not_git = pi("preflight.sh", fake_head="", fake_branch="")
    assert not_git.out.startswith("ringhub preflight (not a git checkout)")
    assert any("isn't a git checkout" in line for line in not_git.lines("!"))
    assert not any("GitHub" in line for line in not_git.lines("✓"))


def test_preflight_flags_files_changed_on_the_pi(pi: Callable[..., Result]) -> None:
    """Settings belong in .env/config.yaml; an edited tracked file isn't the tested setup."""
    r = pi("preflight.sh", fake_dirty="docker-compose.yml deploy/pi/serve.sh")
    assert r.code == 1
    assert any(line.startswith("files changed on this machine") for line in r.lines("✗"))
    assert "put the files back: git checkout HEAD -- docker-compose.yml deploy/pi/serve.sh" in r.out


def test_preflight_explains_a_compose_file_naming_the_old_self_built_image(pi: Callable[..., Result]) -> None:
    """What a Pi showed: docker-compose.yml still said `image: ringhub:latest`."""
    old = (ROOT / "docker-compose.yml").read_text().replace(
        "image: ghcr.io/waymarklabs-official/pebble-custom:${RINGHUB_TAG:-main}", "build: .\n    image: ringhub:latest")
    assert "ringhub:latest" in old
    r = pi("preflight.sh", compose=old, fake_dirty="docker-compose.yml",
           fake_missing_images="ringhub:latest")
    assert r.code == 1
    assert ("docker-compose.yml doesn't run the published ringhub image (it names ringhub:latest). "
            "Put it back: git checkout HEAD -- docker-compose.yml") in r.lines("✗")


def test_preflight_asks_for_a_missing_image_tag(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", env_file=ENV_8445.replace("RINGHUB_TAG=sha-8b75463\n", ""))
    assert r.code == 1
    assert any("no RINGHUB_TAG" in line for line in r.lines("✗"))
    assert "RINGHUB_TAG=claude-pebble-ring-automation-kqoqn3" in r.out


def test_preflight_refuses_a_port_that_belongs_to_tstream(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8445", ":8443"))
    assert r.code == 1
    (msg,) = r.lines("✗")
    assert msg.startswith("tailnet port 8443 is already published by something else (see the list above). "
                          f"ringhub is already published on 8445: in .env set RINGHUB_PUBLIC_URL=https://{HOST}:8445")
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


def test_preflight_without_serve_access_is_not_ready_when_env_exists(pi: Callable[..., Result]) -> None:
    """sudo needs a password, so the ports in .env can't be checked: never 'Ready' (a clash would
    only surface at serve.sh, after the containers were already recreated with the wrong URL)."""
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8446", ":8444"), fake_sudo_denied="1")
    assert r.code == 1 and "Ready" not in r.out
    assert ("couldn't read tailscale serve status, so the ports in .env weren't checked against your tailnet. "
            "Run it with sudo: sudo bash deploy/pi/preflight.sh") in r.lines("✗")
    fresh = pi("preflight.sh", env_file=None, fake_sudo_denied="1")         # nothing to protect yet
    assert fresh.code == 0 and any("run this again with sudo" in line for line in fresh.lines("!"))


def test_preflight_prints_sudo_docker_when_docker_is_unreachable(pi: Callable[..., Result]) -> None:
    """sudo needs a password, so `sudo -n docker` fails too: the printed commands still need sudo."""
    r = pi("preflight.sh", fake_docker_needs_sudo="1", fake_sudo_denied="1", fake_ts_operator="1")
    assert r.code == 0, r.out
    assert any(line.startswith("couldn't reach the Docker daemon as pi. Use sudo") for line in r.lines("!"))
    assert "  sudo docker compose pull && sudo docker compose up -d" in r.out.splitlines()


def test_preflight_run_with_sudo_judges_docker_and_uid_for_the_user(pi: Callable[..., Result]) -> None:
    """`sudo bash deploy/pi/preflight.sh` (as it asks): root reaches Docker, the user doesn't."""
    as_sudo = {"fake_uid": "0", "fake_as_root": "1", "sudo_user": "pi", "sudo_uid": "1000"}
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8446", ":8444"), fake_docker_needs_sudo="1", **as_sudo)
    assert r.code == 1
    assert "you are uid 1000 (matches the containers)" in r.lines("✓")
    assert not any("you are uid 0" in line for line in r.lines("!"))
    assert "daemon reachable with sudo: put sudo in front of every docker command" in r.lines("✓")
    (msg,) = r.lines("✗")
    assert f"in .env set NTFY_PUBLIC_URL=https://{HOST}:8446, then: sudo docker compose up -d." in msg
    member = pi("preflight.sh", **as_sudo)                                  # the user is in the docker group
    assert member.code == 0 and "daemon reachable" in member.lines("✓")
    assert "  docker compose pull && docker compose up -d" in member.out.splitlines()


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


def test_preflight_as_the_real_pi_will_see_it_today(pi: Callable[..., Result]) -> None:
    """The Pi as it is: no RINGHUB_TAG yet, docker only with sudo, containers running."""
    r = pi("preflight.sh", env_file=ENV_8445.replace("RINGHUB_TAG=sha-8b75463\n", ""), fake_docker_needs_sudo="1")
    assert r.code == 1
    assert r.lines("✗") == [
        ".env has no RINGHUB_TAG, so compose would look for the ':main' image. Add this line to .env:"]
    assert "RINGHUB_TAG=claude-pebble-ring-automation-kqoqn3" in r.out
    assert r.lines("!") == [
        "something already listens on :8787 (fine if it's ringhub's own container from an earlier install)",
        "something already listens on :2586 (fine if it's ringhub's own container from an earlier install)"]


def test_preflight_points_an_env_port_at_where_ntfy_really_is(pi: Callable[..., Result]) -> None:
    """The likely cause of a '8444' report: NTFY_PUBLIC_URL still on the old default 8444."""
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8446", ":8444"), fake_docker_needs_sudo="1")
    assert r.code == 1
    assert "tailnet port 8444 is free for ntfy" not in r.lines("✓")     # no ✓ followed by a ✗
    assert r.lines("✗") == [
        "a program on this machine listens on port 8444 (*:8444); publishing ntfy there would hide it. "
        f"ntfy is already published on 8446: in .env set NTFY_PUBLIC_URL=https://{HOST}:8446, then: "
        "sudo docker compose up -d. It must match the server URL in the iPhone ntfy app exactly, "
        "so re-add the server there if it changes."]


def test_preflight_never_suggests_regenerating_an_existing_env(pi: Callable[..., Result]) -> None:
    """Regenerating .env would replace the secrets the ring app, Shortcuts and ntfy already use."""
    r = pi("preflight.sh", env_file=ENV_8445.replace(":8445", ":8443"), status=ONLY_TSTREAM)
    assert r.code == 1
    (msg,) = r.lines("✗")
    assert msg.startswith("tailnet port 8443 is already published by something else")
    assert "Change only RINGHUB_PUBLIC_URL in .env to a free port" in msg and "don't regenerate .env" in msg


@pytest.mark.parametrize("listen", ["127.0.0.1:8445 [::1]:8446", "127.0.0.53%lo:8445",
                                    "0.0.0.0%lo:8445 [::1]%lo:8446", "[::ffff:127.0.0.1]:8446"])
def test_loopback_only_listeners_do_not_block(pi: Callable[..., Result], listen: str) -> None:
    assert pi("preflight.sh", env_file=None, status=ONLY_TSTREAM, fake_listen=listen).code == 0
    assert pi("serve.sh", status=ONLY_TSTREAM, fake_listen=listen).code == 0


@pytest.mark.parametrize("listen", ["*:8445", "[::]:8446", "0.0.0.0:8445", "100.75.135.17:8446", "192.168.1.9:8445"])
def test_reachable_listeners_block(pi: Callable[..., Result], listen: str) -> None:
    r = pi("preflight.sh", env_file=None, status=ONLY_TSTREAM, fake_listen=listen)
    assert r.code == 1 and any("listens on port" in line for line in r.lines("✗"))
    s = pi("serve.sh", status=ONLY_TSTREAM, fake_listen=listen)
    assert s.code == 1 and s.changes == []


def test_preflight_flags_ringhub_on_the_public_internet(pi: Callable[..., Result]) -> None:
    status = json.loads(json.dumps(PI_STATUS))
    status["AllowFunnel"][f"{HOST}:8445"] = True
    r = pi("preflight.sh", status=status)
    assert r.code == 1 and any("PUBLIC internet (Funnel)" in line for line in r.lines("✗"))


def test_preflight_ready_lines_are_exact(pi: Callable[..., Result]) -> None:
    r = pi("preflight.sh", fake_docker_needs_sudo="1")
    assert r.code == 0
    tail = r.out[r.out.index("Ready to upgrade"):].splitlines()
    assert tail == ["Ready to upgrade (from ~/ringhub):",
                    "  sudo docker compose pull && sudo docker compose up -d",
                    "  sudo docker compose exec ringhub ringhub check -c /app/config.yaml",
                    "  sudo bash deploy/pi/serve.sh"]


# --- preflight with real git ------------------------------------------------------------------
# The fake git can't show what real git does with a pruned upstream, with the index, or with a
# pasted command, so these use a real clone of a real bare "origin".

OLD_COMPOSE = (ROOT / "docker-compose.yml").read_text().replace(
    "image: ghcr.io/waymarklabs-official/pebble-custom:${RINGHUB_TAG:-main}", "build: .\n    image: ringhub:latest")


def _git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd.parent), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


def _paste(r: Result, repo: Path) -> None:
    """Run the 'put the files back' command exactly as printed, as a user pasting it would."""
    m = re.search(r"put the files back: (.*)", r.out)
    assert m, r.out
    subprocess.run(["bash", "-c", m.group(1)], cwd=repo, env={"PATH": os.environ["PATH"], "HOME": str(repo.parent)},
                   check=True, capture_output=True, timeout=60)


@pytest.fixture
def real_repo(pi: Callable[..., Result], tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("needs git")
    repo, origin = tmp_path / "ringhub", tmp_path / "origin.git"
    shutil.rmtree(repo / ".git")
    shutil.copy(ROOT / "config.example.yaml", repo / "config.example.yaml")
    (repo / ".gitignore").write_text(".env\nsrc\ndeploy/\n")
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "init", "-q", "-b", BRANCH)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", BRANCH)
    return repo


def test_real_git_a_merged_and_pruned_branch_is_reported(pi: Callable[..., Result], real_repo: Path,
                                                         tmp_path: Path) -> None:
    """GitHub deleted the branch after the merge and `git pull` pruned the tracking ref: @{u} is gone."""
    _git(tmp_path / "origin.git", "branch", "-D", BRANCH)
    _git(real_repo, "fetch", "-q", "--prune")
    r = pi("preflight.sh", real_git=True)
    assert r.code == 1
    assert f"GitHub no longer has the branch {BRANCH} (merged?). Switch to main: git checkout main && git pull" \
        in r.lines("✗")


def test_real_git_leaves_git_untouched_and_its_put_back_command_works(pi: Callable[..., Result],
                                                                      real_repo: Path) -> None:
    """A staged rename (with a space) plus a changed compose file. .gitignore is touched but not
    changed, so the index is stale and `git diff` would rewrite it (root's, under sudo)."""
    _git(real_repo, "mv", "config.example.yaml", "my config.yaml")
    os.utime(real_repo / ".gitignore", ns=(10**18, 10**18))
    index = real_repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_uid)
    r = pi("preflight.sh", real_git=True, compose=OLD_COMPOSE, fake_missing_images="ringhub:latest")
    assert r.code == 1
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_uid) == before
    assert ".gitignore" not in r.out                                   # touched, not changed
    _paste(r, real_repo)
    assert _git(real_repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (real_repo / "config.example.yaml").read_text() == (ROOT / "config.example.yaml").read_text()
    assert (real_repo / "my config.yaml").read_text() == (ROOT / "config.example.yaml").read_text()  # kept
    again = pi("preflight.sh", real_git=True)
    assert again.code == 0, again.out
    assert f"up to date with GitHub ({_git(real_repo, 'rev-parse', '--short=7', 'HEAD')})" in again.lines("✓")


def test_real_git_a_committed_edit_is_caught_and_its_fix_does_not_loop(pi: Callable[..., Result],
                                                                       real_repo: Path) -> None:
    """The compose edit was committed (as git's own hint suggests): HEAD has it, GitHub doesn't."""
    upstream = _git(real_repo, "rev-parse", "--short=7", "HEAD")
    (real_repo / "docker-compose.yml").write_text(OLD_COMPOSE)
    _git(real_repo, "commit", "-qam", "run my local image")
    r = pi("preflight.sh", real_git=True, compose=OLD_COMPOSE, fake_missing_images="ringhub:latest")
    assert r.code == 1
    assert not any(line.startswith("ahead of GitHub") for line in r.lines("✓"))
    assert f"Put it back: git checkout {upstream} -- docker-compose.yml" in r.out
    _paste(r, real_repo)
    again = pi("preflight.sh", real_git=True)                         # the file as it now is (GitHub's)
    assert again.code == 0, again.out
    assert f"ahead of GitHub (local commits on top of {upstream})" in again.lines("✓")


def test_pi_docs_upgrade_covers_failed_pulls_sudo_and_the_iphone() -> None:
    """The upgrade steps a stuck Pi follows: what to do when `git pull` stops, docker needing sudo,
    and the iPhone only when NTFY_PUBLIC_URL changed."""
    doc = (ROOT / "docs" / "PI.md").read_text()
    upgrade = doc[doc.index("## Upgrading an existing install"):doc.index("## Everyday operations")]
    step1 = upgrade[upgrade.index("1. **Get the new scripts"):upgrade.index("2. **Fix what it flags")]
    for text in ("unmerged files", "would be overwritten", "git checkout HEAD -- docker-compose.yml",
                 "Don't `git stash` or `git commit`"):
        assert text in step1, text
    assert "put `sudo` in front of every\n`docker` command below" in upgrade[:upgrade.index("1. **")]
    assert "nothing to redo on the\niPhone." not in upgrade
    assert "If `NTFY_PUBLIC_URL` is the same as before,\nthere's nothing to redo on the iPhone" in upgrade
    troubleshooting = doc[doc.index("## Troubleshooting"):]
    assert "Pulling is not possible because you have unmerged files" in troubleshooting
    assert "Your local changes to the following files would be overwritten" in troubleshooting


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
    r = pi("serve.sh", status=ONLY_TSTREAM, fake_listen="[::]:8446")
    assert r.code == 1 and r.changes == [] and "used by a program on this machine" in r.out


def test_serve_refuses_ringhub_on_the_public_internet(pi: Callable[..., Result]) -> None:
    status = json.loads(json.dumps(PI_STATUS))
    status["AllowFunnel"][f"{HOST}:8445"] = True
    r = pi("serve.sh", status=status)
    assert r.code == 1 and r.changes == [] and "PUBLIC" in r.out
    off = pi("serve.sh", "--off", status=status)
    assert "serve --https=8445 off" in off.changes and f"{HOST}:8443" in off.state["AllowFunnel"]


def test_serve_undoes_its_own_changes_when_publishing_fails_halfway(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh", status=ONLY_TSTREAM, fake_ts_fail_port="8446")
    assert r.code == 1
    assert r.changes == ["serve --bg --yes --https=8445 http://127.0.0.1:8787",
                         "serve --bg --yes --https=8446 http://127.0.0.1:2586",
                         "serve --https=8445 off"]
    assert r.state == ONLY_TSTREAM


def test_serve_never_suggests_regenerating_env(pi: Callable[..., Result]) -> None:
    r = pi("serve.sh", env_file=ENV_8445.replace(":8445", ":8443"), status=ONLY_TSTREAM)
    assert r.code == 1 and "Change only RINGHUB_PUBLIC_URL in .env" in r.out and "Don't regenerate .env" in r.out


def test_serve_points_a_clashing_port_at_where_ntfy_already_is(pi: Callable[..., Result]) -> None:
    """Same advice as the preflight: following 'pick a free port' instead left ntfy's base URL on
    the old port while the phone was given a new one, which silently delivers nothing."""
    r = pi("serve.sh", env_file=ENV_8445.replace(":8446", ":8444"))         # *:8444 is another program's
    assert r.code == 1 and r.changes == [] and r.state == PI_STATUS
    assert ("port 8444 is used by a program on this machine (*:8444); publishing ntfy there would hide it.\n"
            f"ntfy is already published on 8446: in .env set NTFY_PUBLIC_URL=https://{HOST}:8446, then: "
            "sudo docker compose up -d, and run this again. It must match the server URL in the iPhone ntfy "
            "app exactly, so re-add the server there if it changes.\nNothing changed.") in r.out


@pytest.mark.parametrize(("env", "status", "listen", "expect"), [
    (ENV_8445, ONLY_TSTREAM, "[::]:8446",
     "Change only NTFY_PUBLIC_URL in .env to a free port (deploy/pi/preflight.sh checks it), then: sudo docker "
     "compose up -d, and run this again. Don't regenerate .env: that would replace your secrets. It must match "
     "the server URL in the iPhone ntfy app exactly"),
    (ENV_8445.replace(":8445", ":8443"), PI_STATUS, PI_LISTEN,
     f"ringhub is already published on 8445: in .env set RINGHUB_PUBLIC_URL=https://{HOST}:8445, then: sudo "
     "docker compose up -d, and run this again. If it changes, update the ring app's webhook URLs"),
])
def test_serve_refusals_say_what_has_to_follow_a_changed_url(pi: Callable[..., Result], env: str,
                                                              status: dict[str, Any], listen: str,
                                                              expect: str) -> None:
    r = pi("serve.sh", env_file=env, status=status, fake_listen=listen)
    assert r.code == 1 and r.changes == [] and expect in r.out and "Nothing changed." in r.out


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
