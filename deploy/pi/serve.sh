#!/usr/bin/env bash
# Publish ringhub and its ntfy on your tailnet, on exactly the two ports in .env
# (RINGHUB_PUBLIC_URL and NTFY_PUBLIC_URL), and change nothing else. Other mappings (Tstream's,
# for one) are never touched, Funnel is never turned on, and `tailscale serve reset` is never used.
#
#   sudo bash deploy/pi/serve.sh          publish; a port that already points at ringhub/ntfy is kept
#   sudo bash deploy/pi/serve.sh --off    unpublish: removes those two ports, and only if they still
#                                         point at ringhub and ntfy
set -euo pipefail
cd "$(dirname "$0")/../.."
pit() { PYTHONPATH=src python3 -m ringhub.pitools "$@"; }
MODE=on
case "${1:-}" in
  "") ;;
  --off) MODE=off;;
  *) echo "usage: sudo bash deploy/pi/serve.sh [--off]   (ports come from .env)" >&2; exit 2;;
esac
[ "$(id -u)" = 0 ] || { echo "run it with sudo: sudo bash deploy/pi/serve.sh $*" >&2; exit 2; }
[ -f .env ] || { echo ".env not found here; generate it first (docs/PI.md step 2). Nothing changed." >&2; exit 2; }

PORTS="$(pit ports --env .env)"
read -r RING_PORT NTFY_PORT _ <<<"$PORTS"
STATUS="$(tailscale serve status --json)"
state() { printf '%s' "$STATUS" | pit serve-state "$1" "$2"; }
PLAN=("$RING_PORT ringhub http://127.0.0.1:8787" "$NTFY_PORT ntfy http://127.0.0.1:2586")

echo "Before:"; tailscale serve status || true; echo

if [ "$MODE" = off ]; then
  for item in "${PLAN[@]}"; do
    read -r port name target <<<"$item"
    case "$(state "$port" "$target")" in
      ours|ours-public) tailscale serve --https="$port" off; echo "removed :$port ($name)";;
      free) echo ":$port is not published; nothing to remove";;
      *) echo ":$port now serves something other than $name; left alone";;
    esac
  done
else
  # Check both ports before changing anything, so a refusal never leaves a half-published setup.
  TODO=()
  for item in "${PLAN[@]}"; do
    read -r port name target <<<"$item"
    case "$(state "$port" "$target")" in
      free)
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
          echo "port $port is used by a program on this machine; publishing $name there would hide it. Nothing changed." >&2
          exit 1
        fi
        TODO+=("$item");;
      ours) echo ":$port already publishes $name; kept as is";;
      ours-public)
        echo ":$port publishes $name to the PUBLIC internet (Funnel). Run 'sudo bash deploy/pi/serve.sh --off' first. Nothing changed." >&2
        exit 1;;
      *)
        echo "tailnet port $port (from .env) is used by something else; see 'Before' above." >&2
        echo "Pick two free ports with deploy/pi/preflight.sh, then regenerate .env. Nothing changed." >&2
        exit 1;;
    esac
  done
  for item in ${TODO[@]+"${TODO[@]}"}; do
    read -r port name target <<<"$item"
    tailscale serve --bg --yes --https="$port" "$target"
  done
fi

echo; echo "After (your other mappings are unchanged):"; tailscale serve status || true
HOST="$(tailscale status --self --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower())')"
[ "$MODE" = on ] && { echo; echo "ringhub: https://$HOST:$RING_PORT    ntfy: https://$HOST:$NTFY_PORT"; }
exit 0
