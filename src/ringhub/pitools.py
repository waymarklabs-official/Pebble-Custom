"""Decisions for the Raspberry Pi scripts in deploy/pi. Standard library only.

    PYTHONPATH=src python3 -m ringhub.pitools <command> ...

The shell scripts handle what only a shell can (sudo, docker, ss). Everything they decide on
(which tailnet ports ringhub owns, whether a Tailscale mapping is ringhub's or someone else's,
whether the image for this machine exists) is decided here, where it is unit-tested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .envfile import DEFAULT_NTFY_PORT, DEFAULT_RING_PORT

RING_TARGET = "http://127.0.0.1:8787"
NTFY_TARGET = "http://127.0.0.1:2586"
TARGET_NAMES = {RING_TARGET: "ringhub", NTFY_TARGET: "ntfy"}

_ENV_KEY = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")
_INVALID_TAG_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_TIMEOUT = 15
_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class PiError(ValueError):
    pass


# --- .env -------------------------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """Parse a .env the way docker compose does for the cases ringhub writes and documents."""
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_KEY.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            # Unquoted: "#" at the start or after whitespace begins a comment (compose semantics);
            # a "#" inside a value (abc#def) does not.
            value = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()
        env[key] = value
    return env


def read_env(path: Path) -> dict[str, str] | None:
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def url_port(url: str, key: str) -> int:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PiError(f"{key} must look like https://<machine>.<tailnet>.ts.net:<port> (it is {url!r})")
    try:
        port = parts.port
    except ValueError as e:
        raise PiError(f"{key} has an invalid port ({url!r})") from e
    return port or 443


def _valid_port(value: str | int, what: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as e:
        raise PiError(f"{what} {value!r} is not a port number") from e
    if not 1 <= port <= 65535:
        raise PiError(f"{what} {port} is out of range")
    return port


def resolve_ports(env: Mapping[str, str] | None, ring: str | None = None,
                  ntfy: str | None = None) -> tuple[int, int, str]:
    """Ports to use, and where they came from: explicit arguments, then .env, then the defaults.

    When a .env exists its URLs are the truth (the ring app, Shortcuts and the iPhone ntfy app all
    use them), so arguments that disagree with it are refused rather than silently preferred.
    """
    from_env: tuple[int, int] | None = None
    if env is not None:
        missing = [k for k in ("RINGHUB_PUBLIC_URL", "NTFY_PUBLIC_URL") if not env.get(k)]
        if missing:
            raise PiError(f".env has no {' or '.join(missing)}; regenerate it (docs/PI.md step 2)")
        from_env = (url_port(env["RINGHUB_PUBLIC_URL"], "RINGHUB_PUBLIC_URL"),
                    url_port(env["NTFY_PUBLIC_URL"], "NTFY_PUBLIC_URL"))
    if (ring is None) != (ntfy is None):
        raise PiError("give both ports (ringhub, then ntfy) or neither")
    if ring is not None and ntfy is not None:
        chosen = (_valid_port(ring, "ringhub port"), _valid_port(ntfy, "ntfy port"))
        if from_env is not None and chosen != from_env:
            raise PiError(f".env already uses ports {from_env[0]} (ringhub) and {from_env[1]} (ntfy). "
                          "Run without port arguments, or edit both URLs in .env first")
        ports, source = chosen, "arguments"
    elif from_env is not None:
        ports, source = from_env, ".env"
    else:
        ports, source = (DEFAULT_RING_PORT, DEFAULT_NTFY_PORT), "defaults"
    if ports[0] == ports[1]:
        raise PiError(f"ringhub and ntfy need different ports (both are {ports[0]})")
    return ports[0], ports[1], source


# --- tailscale serve --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServeMapping:
    port: int
    kind: str                     # https | http | tcp | tls-terminated-tcp
    funnel: bool                  # reachable from the public internet
    handlers: tuple[tuple[str, str], ...]   # (path, what it serves)

    @property
    def single_proxy(self) -> str | None:
        """The proxy target if this mapping is exactly `/ -> http://127.0.0.1:<port>`."""
        if self.kind == "https" and len(self.handlers) == 1 and self.handlers[0][0] == "/":
            return normalize_target(self.handlers[0][1])
        return None


def normalize_target(target: str) -> str | None:
    """Canonical form of a local proxy target, or None if it isn't a plain local HTTP proxy."""
    t = target.strip()
    if "://" not in t:
        t = "http://" + t
    parts = urllib.parse.urlsplit(t)
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or not port:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return f"http://127.0.0.1:{port}"


def _describe_handler(h: Mapping[str, Any]) -> str:
    if h.get("Proxy"):
        return str(h["Proxy"])
    if h.get("Path"):
        return f"files {h['Path']}"
    if "Text" in h:
        return "text"
    if h.get("Redirect"):
        return f"redirect {h['Redirect']}"
    return "?"


def _port_of(hostport: str) -> int | None:
    _, _, port = hostport.rpartition(":")
    return int(port) if port.isdigit() else None


def parse_serve_status(doc: Any) -> dict[int, ServeMapping]:
    """Turn `tailscale serve status --json` into one entry per port (background and foreground)."""
    if not isinstance(doc, dict):
        return {}
    configs = [doc, *[c for c in (doc.get("Foreground") or {}).values() if isinstance(c, dict)]]
    out: dict[int, ServeMapping] = {}
    for cfg in configs:
        tcp = cfg.get("TCP") or {}
        web = cfg.get("Web") or {}
        funnel = {p for hp, on in (cfg.get("AllowFunnel") or {}).items() if on and (p := _port_of(hp))}
        for port_s, handler in tcp.items():
            if not str(port_s).isdigit() or not isinstance(handler, dict):
                continue
            port = int(port_s)
            if handler.get("HTTPS"):
                kind = "https"
            elif handler.get("HTTP"):
                kind = "http"
            elif handler.get("TerminateTLS"):
                kind = "tls-terminated-tcp"
            else:
                kind = "tcp"
            handlers: list[tuple[str, str]] = []
            if kind in ("https", "http"):
                for hostport, site in web.items():
                    if _port_of(hostport) == port and isinstance(site, dict):
                        for path, h in sorted((site.get("Handlers") or {}).items()):
                            if isinstance(h, dict):
                                handlers.append((path, _describe_handler(h)))
            else:
                handlers.append(("", str(handler.get("TCPForward") or handler.get("TerminateTLS") or "?")))
            out[port] = ServeMapping(port, kind, port in funnel, tuple(handlers))
    return out


def serve_state(mappings: Mapping[int, ServeMapping], port: int, target: str) -> str:
    """free | ours | ours-public | other, for publishing `target` on tailnet `port`."""
    m = mappings.get(port)
    if m is None:
        return "free"
    if m.single_proxy == normalize_target(target):
        return "ours-public" if m.funnel else "ours"
    return "other"


def describe_mappings(mappings: Mapping[int, ServeMapping]) -> list[str]:
    lines = []
    for port in sorted(mappings):
        m = mappings[port]
        reach = "PUBLIC (Funnel)" if m.funnel else "tailnet only"
        what = ", ".join(f"{p} → {t}" if p else t for p, t in m.handlers) or "(no handlers)"
        owner = TARGET_NAMES.get(m.single_proxy or "")
        lines.append(f":{port} {m.kind} [{reach}] {what}" + (f"   ← {owner}" if owner else ""))
    return lines


# --- images -----------------------------------------------------------------------------------


def compose_images(compose_text: str, env: Mapping[str, str]) -> list[str]:
    """The `image:` references in a compose file, with ${VAR:-default} filled in from .env."""
    def sub(m: re.Match[str]) -> str:
        value = env.get(m.group(1))
        return value if value else (m.group(2) or "")
    refs = []
    for line in compose_text.splitlines():
        m = re.match(r"^\s*image:\s*([^\s#]+)", line)
        if m:
            refs.append(_COMPOSE_VAR.sub(sub, m.group(1).strip("'\"")))
    return refs


def branch_tag(branch: str) -> str:
    """The image tag CI publishes for a branch (same rule as docker/metadata-action)."""
    tag = _INVALID_TAG_CHARS.sub("-", branch.removeprefix("refs/heads/"))
    tag = tag.lstrip(".-")[:128]
    if not tag:
        raise PiError(f"branch {branch!r} has no usable image tag")
    return tag


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    reference: str        # tag or sha256:digest

    @property
    def is_digest(self) -> bool:
        return self.reference.startswith("sha256:")


def parse_image_ref(ref: str) -> ImageRef:
    name, _, digest = ref.partition("@")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "registry-1.docker.io", name
        if "/" not in path:
            path = "library/" + path
    repo, _, tag = path.partition(":") if ":" in path.rsplit("/", 1)[-1] else (path, "", "")
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise PiError(f"invalid digest in {ref!r}")
    if not repo or not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repo):
        raise PiError(f"invalid image name {ref!r}")
    if registry == "docker.io":
        registry = "registry-1.docker.io"
    return ImageRef(registry, repo, digest or tag or "latest")


Opener = Callable[..., Any]


def _bearer_token(challenge: str, repository: str, opener: Opener) -> str:
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.get("realm")
    if not challenge.lower().startswith("bearer") or not realm:
        raise PiError("the registry asked for a login this check can't do")
    query = {"scope": f"repository:{repository}:pull"}
    if params.get("service"):
        query["service"] = params["service"]
    with opener(f"{realm}?{urllib.parse.urlencode(query)}", timeout=_TIMEOUT) as resp:
        data = json.load(resp)
    token = data.get("token") or data.get("access_token")
    if not token:
        raise PiError("the registry gave no access token")
    return str(token)


def fetch_manifest(ref: ImageRef, opener: Opener = urllib.request.urlopen) -> tuple[dict[str, Any], bytes]:
    url = f"https://{ref.registry}/v2/{ref.repository}/manifests/{ref.reference}"
    headers = {"Accept": _ACCEPT}
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener(req, timeout=_TIMEOUT) as resp:
                body: bytes = resp.read()
            return json.loads(body), body
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                token = _bearer_token(e.headers.get("WWW-Authenticate", ""), ref.repository, opener)
                headers = {**headers, "Authorization": f"Bearer {token}"}
                continue
            raise
    raise AssertionError("unreachable")


def check_image(ref_text: str, arch: str, opener: Opener = urllib.request.urlopen) -> tuple[bool, str]:
    """(ok, message): does `ref_text` exist and include a linux/<arch> image?"""
    try:
        ref = parse_image_ref(ref_text)
    except PiError as e:
        return False, f"{ref_text}: {e}"
    try:
        doc, body = fetch_manifest(ref, opener)
    except PiError as e:
        return False, f"{ref_text}: {e}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, (f"{ref_text}: no such image. Check the repository's Actions tab for a green run "
                           "on that branch, or use a tag it published")
        if e.code in (401, 403):
            return False, f"{ref_text}: access denied (HTTP {e.code}): no such image there, or it is private"
        return False, f"{ref_text}: registry answered HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"{ref_text}: can't reach {ref.registry} ({reason}); this network may block it"
    except ValueError:
        return False, f"{ref_text}: the registry sent something that isn't a manifest"
    if ref.is_digest and "sha256:" + hashlib.sha256(body).hexdigest() != ref.reference:
        return False, f"{ref_text}: the manifest doesn't match its pinned digest"
    manifests = doc.get("manifests")
    if not isinstance(manifests, list):
        return True, f"{ref_text}: found (single-platform image; architecture not listed)"
    platforms = sorted({f"{p.get('os')}/{p.get('architecture')}" + (f"/{p['variant']}" if p.get("variant") else "")
                        for m in manifests if isinstance(p := m.get("platform"), dict)
                        and p.get("os") not in (None, "unknown")})
    if any(p == f"linux/{arch}" or p.startswith(f"linux/{arch}/") for p in platforms):
        return True, f"{ref_text}: found, includes linux/{arch}"
    return False, f"{ref_text}: exists but has no linux/{arch} build (has: {', '.join(platforms) or 'none'})"


# --- CLI --------------------------------------------------------------------------------------


def _read_status(stream: Iterable[str]) -> dict[int, ServeMapping]:
    text = "".join(stream).strip()
    try:
        return parse_serve_status(json.loads(text) if text else {})
    except json.JSONDecodeError as e:
        raise PiError("could not read `tailscale serve status --json`") from e


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m ringhub.pitools", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("ports", help="print: <ringhub port> <ntfy port> <source>")
    c.add_argument("--env", type=Path, default=Path(".env"))
    c.add_argument("ring", nargs="?")
    c.add_argument("ntfy", nargs="?")

    c = sub.add_parser("env-get", help="print one value from .env (empty if unset)")
    c.add_argument("--env", type=Path, default=Path(".env"))
    c.add_argument("key")

    c = sub.add_parser("serve-state", help="free|ours|ours-public|other (reads serve status JSON on stdin)")
    c.add_argument("port", type=int)
    c.add_argument("target")

    sub.add_parser("serve-show", help="list tailscale serve mappings (reads serve status JSON on stdin)")

    c = sub.add_parser("compose-images", help="image references in docker-compose.yml, one per line")
    c.add_argument("--compose", type=Path, default=Path("docker-compose.yml"))
    c.add_argument("--env", type=Path, default=Path(".env"))
    c.add_argument("--tag", help="use this RINGHUB_TAG instead of the one in .env")

    c = sub.add_parser("image", help="check that an image exists for linux/<arch>")
    c.add_argument("ref")
    c.add_argument("--arch", required=True)

    c = sub.add_parser("branch-tag", help="the image tag CI publishes for a branch name")
    c.add_argument("branch")

    args = p.parse_args(argv)
    try:
        if args.cmd == "ports":
            ring, ntfy, source = resolve_ports(read_env(args.env), args.ring, args.ntfy)
            print(ring, ntfy, source)
        elif args.cmd == "env-get":
            print((read_env(args.env) or {}).get(args.key, ""))
        elif args.cmd == "serve-state":
            print(serve_state(_read_status(sys.stdin), args.port, args.target))
        elif args.cmd == "serve-show":
            print("\n".join(describe_mappings(_read_status(sys.stdin))) or "(none)")
        elif args.cmd == "compose-images":
            env = dict(read_env(args.env) or {})
            if args.tag:
                env["RINGHUB_TAG"] = args.tag
            print("\n".join(compose_images(args.compose.read_text(encoding="utf-8"), env)))
        elif args.cmd == "image":
            ok, message = check_image(args.ref, args.arch)
            print(message)
            return 0 if ok else 1
        elif args.cmd == "branch-tag":
            print(branch_tag(args.branch))
    except PiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
