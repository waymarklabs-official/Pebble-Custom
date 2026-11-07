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
HOST="$(tailscale status --self --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip(".").lower())')"
state() { printf '%s' "$STATUS" | pit serve-state "$1" "$2"; }
PLAN=("$RING_PORT ringhub http://127.0.0.1:8787 RINGHUB_PUBLIC_URL" "$NTFY_PORT ntfy http://127.0.0.1:2586 NTFY_PUBLIC_URL")

# Refuse a port, with the same advice as preflight.sh: where the service already is if it's
# published elsewhere, and what else has to follow a changed URL (the running containers and the
# phone). Changes nothing.
refuse() {
  local why="$1" name="$2" target="$3" key="$4" also elsewhere
  case "$name" in
    ntfy) also="It must match the server URL in the iPhone ntfy app exactly, so re-add the server there if it changes.";;
    *) also="If it changes, update the ring app's webhook URLs and your Shortcuts too.";;
  esac
  elsewhere="$(printf '%s' "$STATUS" | pit serve-ports "$target")"
  echo "$why." >&2
  if [ -n "$elsewhere" ]; then
    echo "$name is already published on ${elsewhere%% *}: in .env set $key=https://$HOST:${elsewhere%% *}, then: sudo docker compose up -d, and run this again. $also" >&2
  else
    echo "Change only $key in .env to a free port (deploy/pi/preflight.sh checks it), then: sudo docker compose up -d, and run this again. Don't regenerate .env: that would replace your secrets. $also" >&2
  fi
  echo "Nothing changed." >&2
  exit 1
}

echo "Before:"; tailscale serve status || true; echo

if [ "$MODE" = off ]; then
  for item in "${PLAN[@]}"; do
    read -r port name target _ <<<"$item"
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
    read -r port name target key <<<"$item"
    case "$(state "$port" "$target")" in
      free)
        # Loopback-only listeners don't count: the tailnet can't reach them, so nothing is hidden.
        if who="$(ss -ltn 2>/dev/null | pit listeners "$port")"; then
          refuse "port $port is used by a program on this machine ($who); publishing $name there would hide it" \
            "$name" "$target" "$key"
        fi
        TODO+=("$item");;
      ours) echo ":$port already publishes $name; kept as is";;
      ours-public)
        echo ":$port publishes $name to the PUBLIC internet (Funnel). Run 'sudo bash deploy/pi/serve.sh --off' first. Nothing changed." >&2
        exit 1;;
      *) refuse "tailnet port $port ($key in .env) is used by something else; see 'Before' above" "$name" "$target" "$key";;
    esac
  done
  ADDED=()
  for item in ${TODO[@]+"${TODO[@]}"}; do
    read -r port name target _ <<<"$item"
    if ! tailscale serve --bg --yes --https="$port" "$target"; then
      echo "publishing $name on :$port failed; undoing what this run added" >&2
      for port in ${ADDED[@]+"${ADDED[@]}"}; do tailscale serve --https="$port" off || true; done
      exit 1
    fi
    ADDED+=("$port")
  done
fi

echo; echo "After (your other mappings are unchanged):"; tailscale serve status || true
[ "$MODE" = on ] && { echo; echo "ringhub: https://$HOST:$RING_PORT    ntfy: https://$HOST:$NTFY_PORT"; }
exit 0
