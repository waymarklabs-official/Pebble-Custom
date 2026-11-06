#!/usr/bin/env bash
# Read-only checks before installing ringhub on a machine that already runs other apps
# (e.g. Tstream). Changes NOTHING. Usage: bash deploy/pi/preflight.sh [ring_port] [ntfy_port]
set -uo pipefail
RING_PORT="${1:-8443}"
NTFY_PORT="${2:-8444}"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=1; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=1; }
WARN=0; FAIL=0

echo "Machine"
ok "arch $(uname -m), $(nproc) CPU, $(awk '/MemTotal/ {printf "%.1f GB RAM", $2/1048576}' /proc/meminfo)"
[ "$(id -u)" = "1000" ] && ok "you are uid 1000 (matches the containers)" \
  || warn "you are uid $(id -u); the containers run as 1000 — chown data/ and ntfy/ to 1000:1000"

echo "Docker"
if command -v docker >/dev/null; then ok "$(docker --version)"; else fail "docker not installed (curl -fsSL https://get.docker.com | sh)"; fi
docker compose version >/dev/null 2>&1 && ok "$(docker compose version | head -1)" || fail "docker compose plugin missing"

echo "Tailscale"
if ! command -v tailscale >/dev/null; then fail "tailscale not installed"; else
  ok "tailscale $(tailscale version | head -1)"
  HOST="$(tailscale status --self --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower())' 2>/dev/null)"
  if [ -n "$HOST" ]; then ok "this machine's name: $HOST"; else fail "could not read the Tailscale name (is it logged in?)"; fi
  SERVE_JSON="$(sudo -n tailscale serve status --json 2>/dev/null || tailscale serve status --json 2>/dev/null || echo '{}')"
  USED="$(printf '%s' "$SERVE_JSON" | python3 -c 'import json,sys
try: d=json.load(sys.stdin) or {}
except Exception: d={}
print(" ".join(sorted((d.get("TCP") or {}).keys(), key=int)))' 2>/dev/null)"
  ok "tailscale serve ports already in use: ${USED:-none}  (these are left alone)"
  for port in "$RING_PORT" "$NTFY_PORT"; do
    if printf ' %s ' "$USED" | grep -q " $port "; then fail "tailnet port $port is already served — pick others: preflight.sh <ring> <ntfy>"
    else ok "tailnet port $port is free"; fi
  done
fi

echo "Local ports (must be free; both services bind 127.0.0.1)"
for port in 8787 2586 "$RING_PORT" "$NTFY_PORT"; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then warn "something already listens on :$port ($(ss -ltnp 2>/dev/null | grep -E "[:.]${port} " | head -1 | awk '{print $NF}'))"
  else ok ":$port free"; fi
done

echo
if [ "$FAIL" = 1 ]; then echo "Fix the ✗ items first."; exit 1; fi
echo "Ready. Next:  ringhub env --host ${HOST:-<name>} --ring-port $RING_PORT --ntfy-port $NTFY_PORT --tz <Zone/City> > .env"
