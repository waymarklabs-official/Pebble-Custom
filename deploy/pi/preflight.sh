#!/usr/bin/env bash
# Read-only checks before installing or upgrading ringhub on a machine that already runs other
# apps (e.g. Tstream). Changes NOTHING, on this machine or in Tailscale.
#
#   bash deploy/pi/preflight.sh               ports from .env if it exists, otherwise 8445 and 8446
#   bash deploy/pi/preflight.sh 8447 8448     try two other ports before .env exists
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=1; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=1; }
info() { printf '    %s\n' "$*"; }
pit()  { PYTHONPATH=src python3 -m ringhub.pitools "$@"; }
WARN=0; FAIL=0
RING_TARGET=http://127.0.0.1:8787
NTFY_TARGET=http://127.0.0.1:2586
g() { git -c safe.directory="$PWD" "$@"; }   # also works when run with sudo in a user's checkout

REV="$(g rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
BRANCH="$(g rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
echo "ringhub preflight $REV${BRANCH:+ ($BRANCH)}"
echo "This checkout"
# Old scripts check old things (an older preflight looked at ports 8443/8444). Compare with GitHub
# first; `git ls-remote` only reads, it changes nothing here.
if [ -z "$BRANCH" ] || [ "$BRANCH" = HEAD ]; then
  ok "pinned to commit $REV (not on a branch, so not compared with GitHub)"
else
  HEAD_SHA="$(g rev-parse HEAD 2>/dev/null)"
  REMOTE_SHA="$(GIT_TERMINAL_PROMPT=0 timeout 20 git -c safe.directory="$PWD" ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1)"
  if [ -z "$REMOTE_SHA" ]; then
    warn "couldn't ask GitHub for the newest $BRANCH (offline, or not pushed); checking $REV as it is"
  elif [ "$REMOTE_SHA" = "$HEAD_SHA" ]; then
    ok "up to date with GitHub"
  elif g merge-base --is-ancestor "$REMOTE_SHA" HEAD 2>/dev/null; then
    ok "ahead of GitHub (local commits on top of ${REMOTE_SHA:0:7})"
  else
    fail "out of date: GitHub has ${REMOTE_SHA:0:7}, this is $REV. Run: git pull   then run this check again"
  fi
fi
# Your settings live in .env and config.yaml, which git ignores. A changed tracked file (say, a
# docker-compose.yml still naming an old self-built image) means this isn't the tested setup, and it
# can also make `git pull` refuse to update. --no-optional-locks: status must not rewrite .git/index
# (under sudo it would become root-owned and break your next git pull).
CHANGED="$(g --no-optional-locks status --porcelain --untracked-files=no 2>/dev/null | cut -c4-)"
if [ -n "$CHANGED" ]; then
  fail "files changed on this machine, so this isn't the tested setup (settings belong in .env and config.yaml):"
  while read -r f; do info "$f"; done <<<"$CHANGED"
  info "see the changes: git diff      put the files back: git checkout HEAD -- ${CHANGED//$'\n'/ }"
fi

echo "Machine"
ARCH=""
case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64;;
  x86_64|amd64)  ARCH=amd64;;
esac
# A 32-bit Raspberry Pi OS still boots a 64-bit kernel on a Pi 4/5, so uname alone is not enough:
# Docker follows the userland, and there is no 32-bit image.
if command -v dpkg >/dev/null; then
  case "$(dpkg --print-architecture)" in
    arm64) ARCH=arm64;; amd64) ARCH=amd64;;
    *) ARCH=""; fail "this OS is $(dpkg --print-architecture) (32-bit). Reinstall with 64-bit Raspberry Pi OS: dpkg --print-architecture must say arm64";;
  esac
fi
if [ -n "$ARCH" ]; then
  ok "linux/$ARCH, $(nproc) CPU, $(awk '/MemTotal/ {printf "%.1f GB RAM", $2/1048576}' /proc/meminfo)"
elif [ "$FAIL" = 0 ]; then
  fail "unsupported CPU $(uname -m): images exist for arm64 (Raspberry Pi 4/5) and amd64 only"
fi
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  fail "python3 3.9+ is needed once, to generate .env (it comes with Raspberry Pi OS)"
  echo; echo "Fix the ✗ items first."; exit 1
fi
ok "$(python3 --version)"
if [ "$(id -u)" = "1000" ]; then ok "you are uid 1000 (matches the containers)"
else warn "you are uid $(id -u); the containers run as 1000, so: sudo chown -R 1000:1000 data ntfy"; fi

if ! PORTS="$(pit ports --env .env "$@")"; then
  echo; echo "Fix the error above first."; exit 1
fi
read -r RING_PORT NTFY_PORT PORTS_FROM <<<"$PORTS"
ok "ports: ringhub $RING_PORT, ntfy $NTFY_PORT (from $PORTS_FROM)"

echo "Docker"
DOCKER=""
if ! command -v docker >/dev/null; then
  fail "docker not installed (curl -fsSL https://get.docker.com | sh)"
else
  ok "$(docker --version)"
  if docker info >/dev/null 2>&1; then DOCKER="docker"; ok "daemon reachable"
  elif sudo -n docker info >/dev/null 2>&1; then DOCKER="sudo docker"; ok "daemon reachable with sudo: put sudo in front of every docker command"
  else warn "couldn't reach the Docker daemon as $(id -un). Use sudo for docker commands, or: sudo usermod -aG docker $(id -un) and log in again"
  fi
  if [ -n "$DOCKER" ]; then
    SERVER_ARCH="$($DOCKER version --format '{{.Server.Arch}}' 2>/dev/null)"
    case "$SERVER_ARCH" in
      arm64|amd64) ok "Docker runs linux/$SERVER_ARCH images"; ARCH="$SERVER_ARCH";;
      *) fail "Docker runs ${SERVER_ARCH:-unknown} images; it must be arm64 or amd64 (64-bit OS)";;
    esac
  fi
  if docker compose version >/dev/null 2>&1; then ok "$(docker compose version | head -1)"
  else fail "docker compose plugin missing (it comes with get.docker.com)"; fi
fi

echo "Registry (this machine downloads tested images; it never builds)"
for url in https://ghcr.io/v2/ https://pkg-containers.githubusercontent.com/ \
           https://registry-1.docker.io/v2/ https://auth.docker.io/token https://production.cloudflare.docker.com/; do
  code="$(curl -sS -o /dev/null -m 15 -w '%{http_code}' "$url" 2>/dev/null || true)"
  case "$code" in
    2*|3*|400|401|403|404|405) ok "reachable: ${url#https://}";;
    *) fail "cannot reach ${url#https://} (HTTP '${code:-none}'): this network blocks image downloads";;
  esac
done
BRANCH_TAG=""
[ -n "$BRANCH" ] && [ "$BRANCH" != HEAD ] && BRANCH_TAG="$(pit branch-tag "$BRANCH" 2>/dev/null)"
TAG=""
if [ -f .env ]; then
  TAG="$(pit env-get --env .env RINGHUB_TAG)"
  if [ -z "$TAG" ]; then
    fail ".env has no RINGHUB_TAG, so compose would look for the ':main' image. Add this line to .env:"
    info "RINGHUB_TAG=${BRANCH_TAG:-main}"
    TAG="${BRANCH_TAG:-main}"
  fi
else
  TAG="${BRANCH_TAG:-main}"
fi
if [ -z "$ARCH" ]; then
  warn "skipping the image check (architecture unknown)"
else
  REFS="$(pit compose-images --env .env --tag "$TAG")"
  if ! grep -q '^ghcr\.io/waymarklabs-official/pebble-custom:' <<<"$REFS"; then
    fail "docker-compose.yml doesn't run the published ringhub image (it names $(head -1 <<<"$REFS")). Put it back: git checkout HEAD -- docker-compose.yml"
  fi
  while read -r ref; do
    [ -n "$ref" ] || continue
    if msg="$(pit image "$ref" --arch "$ARCH")"; then ok "$msg"; else fail "$msg"; fi
  done <<<"$REFS"
fi

echo "Tailscale"
HOST=""
if ! command -v tailscale >/dev/null; then fail "tailscale not installed"; else
  ok "tailscale $(tailscale version | head -1)"
  HOST="$(tailscale status --self --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower())' 2>/dev/null)"
  if [ -n "$HOST" ]; then ok "this machine's name: $HOST"; else fail "could not read the Tailscale name (is it logged in? sudo tailscale up)"; fi
  if STATUS="$(sudo -n tailscale serve status --json 2>/dev/null)" || STATUS="$(tailscale serve status --json 2>/dev/null)"; then
    ok "current tailscale serve mappings (left exactly as they are):"
    printf '%s' "$STATUS" | pit serve-show | while read -r line; do info "$line"; done
    for pair in "$RING_PORT ringhub $RING_TARGET" "$NTFY_PORT ntfy $NTFY_TARGET"; do
      read -r port name target <<<"$pair"
      case "$(printf '%s' "$STATUS" | pit serve-state "$port" "$target")" in
        free) ok "tailnet port $port is free for $name";;
        ours) ok "tailnet port $port already publishes $name (from an earlier install; serve.sh keeps it)";;
        ours-public) fail "tailnet port $port publishes $name to the PUBLIC internet (Funnel). Remove it with: sudo bash deploy/pi/serve.sh --off";;
        *) if [ "$PORTS_FROM" = ".env" ]; then
             fail "tailnet port $port (from your .env) is used by something else. Pick two free ports, then regenerate .env (docs/PI.md step 2)"
           else
             fail "tailnet port $port is used by something else. Try others: bash deploy/pi/preflight.sh <ringhub port> <ntfy port>"
           fi;;
      esac
      # A program already listening on a free port would be hidden from the tailnet once Serve
      # claims it.
      if [ "$(printf '%s' "$STATUS" | pit serve-state "$port" "$target")" = free ] \
         && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
        fail "a program on this machine listens on port $port; publishing $name there would hide it. Pick another"
      fi
    done
  else
    warn "couldn't read tailscale serve status; run this again with sudo to check the tailnet ports"
  fi
fi

echo "Local ports (both containers listen on 127.0.0.1 only)"
for port in 8787 2586; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
    warn "something already listens on :$port (fine if it's ringhub's own container from an earlier install)"
  else ok ":$port free"; fi
done

echo
if [ "$FAIL" = 1 ]; then echo "Fix the ✗ items first. Nothing was changed."; exit 1; fi
[ "$WARN" = 1 ] && echo "The ! items are worth a look but don't block anything."
D="${DOCKER:-docker}"
if [ -f .env ]; then
  echo "Ready to upgrade (from ~/ringhub):"
  echo "  $D compose pull && $D compose up -d"
  echo "  $D compose exec ringhub ringhub check -c /app/config.yaml"
  echo "  sudo bash deploy/pi/serve.sh"
else
  echo "Ready. Next (from ~/ringhub):"
  echo "  mkdir -p data ntfy/cache ntfy/lib"
  echo "  PYTHONPATH=src python3 -m ringhub.envfile --host ${HOST:-<name>} --ring-port $RING_PORT --ntfy-port $NTFY_PORT --tag $TAG --tz <Area/City> > .env"
  echo "  chmod 600 .env && cp config.example.yaml config.yaml"
fi
