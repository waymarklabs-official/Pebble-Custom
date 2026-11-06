#!/usr/bin/env bash
# Publish ringhub and its ntfy on their OWN tailnet HTTPS ports. Never touches other mappings
# (Tstream on 443 stays exactly as it is) and never runs `tailscale serve reset`.
# Usage: sudo bash deploy/pi/serve.sh [ring_port] [ntfy_port]
# Undo exactly these two later:  sudo tailscale serve --https=<port> off   (always with the port!)
set -euo pipefail
RING_PORT="${1:-8443}"
NTFY_PORT="${2:-8444}"
[ "$RING_PORT" != "$NTFY_PORT" ] || { echo "ports must differ" >&2; exit 2; }
USED="$(tailscale serve status --json | python3 -c 'import json,sys
d=json.load(sys.stdin) or {}
print(" ".join((d.get("TCP") or {}).keys()))')"
for port in "$RING_PORT" "$NTFY_PORT"; do
  if printf ' %s ' "$USED" | grep -q " $port "; then
    echo "tailnet port $port is already served by something else; choose another (nothing changed)" >&2; exit 1
  fi
  # A program listening on this port (e.g. another app on 0.0.0.0:$port) would be hidden from the
  # tailnet once Serve claims the port. Refuse instead of breaking it.
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
    echo "port $port is already used by a program on this machine; choose another (nothing changed)" >&2; exit 1
  fi
done
echo "Before:"; tailscale serve status || true
tailscale serve --bg --yes --https="$RING_PORT" http://127.0.0.1:8787
tailscale serve --bg --yes --https="$NTFY_PORT" http://127.0.0.1:2586
echo; echo "After (your existing mappings should be unchanged):"; tailscale serve status
HOST="$(tailscale status --self --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower())')"
echo; echo "ringhub: https://$HOST:$RING_PORT    ntfy: https://$HOST:$NTFY_PORT"
