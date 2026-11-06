#!/usr/bin/env bash
# End-to-end check of the production stack, run by CI on the exact image it is about to publish.
#
# It uses docker-compose.yml and config.example.yaml as shipped, the pinned ntfy image, a .env from
# ringhub.envfile, and creates the ntfy accounts with the same commands as docs/PI.md step 3. Then a
# signed voice note and a recall question go in through the webhook, as the ring app sends them,
# and the answers must come out of ntfy readable by the iPhone account, and only by it.
#
#   bash deploy/ci/e2e.sh <local ringhub image>
set -euo pipefail
IMAGE="${1:?usage: e2e.sh <local ringhub image>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/ringhub-e2e.XXXXXX")"
NTFY=http://127.0.0.1:2586
dc() { docker compose --project-directory "$WORK" --project-name ringhub-e2e "$@"; }
finish() {
  status=$?
  if [ "$status" != 0 ]; then echo "--- logs"; dc logs --no-color --tail 80 || true; fi
  dc down --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap finish EXIT
step() { printf '\n== %s\n' "$*"; }
expect_code() {  # expect_code <allowed codes regex> <description> <curl args...>
  local want="$1" what="$2"; shift 2
  local code; code="$(curl -sS -o /dev/null -w '%{http_code}' -m 10 "$@")"
  [[ "$code" =~ ^($want)$ ]] || { echo "FAIL: $what: HTTP $code (wanted $want)"; exit 1; }
  echo "ok: $what (HTTP $code)"
}

step "Stack from the shipped files"
cp "$ROOT/docker-compose.yml" "$WORK/"
cp "$ROOT/config.example.yaml" "$WORK/config.yaml"
mkdir -p "$WORK/data" "$WORK/ntfy/cache" "$WORK/ntfy/lib"
chmod -R 777 "$WORK/data" "$WORK/ntfy"          # the containers run as uid 1000; this runner may not be
PYTHONPATH="$ROOT/src" python3 -m ringhub.envfile --host e2e.example.ts.net --tag e2e --tz America/New_York \
  > "$WORK/.env"
docker tag "$IMAGE" ghcr.io/waymarklabs-official/pebble-custom:e2e
env_get() { PYTHONPATH="$ROOT/src" python3 -m ringhub.pitools env-get --env "$WORK/.env" "$1"; }
TOPIC="$(env_get NTFY_TOPIC)"; SECRET="$(env_get RINGHUB_SECRET)"
HUB_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
PHONE_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

step "ntfy, and its two accounts exactly as docs/PI.md step 3 creates them"
dc pull --quiet ntfy
dc up -d --wait ntfy
dc exec -T -e NTFY_PASSWORD="$HUB_PW" ntfy ntfy user add ringhub
dc exec -T ntfy ntfy access ringhub 'ring-*' write-only
TOKEN="$(dc exec -T ntfy ntfy token add --label=ringhub ringhub | grep -o 'tk_[A-Za-z0-9]*' | head -1)"
[ -n "$TOKEN" ] || { echo "FAIL: no token printed"; exit 1; }
dc exec -T -e NTFY_PASSWORD="$PHONE_PW" ntfy ntfy user add iphone
dc exec -T ntfy ntfy access iphone 'ring-*' read-only
sed -i "s/^NTFY_TOKEN=.*/NTFY_TOKEN=$TOKEN/" "$WORK/.env"

step "Access rules: nobody but the hub publishes, nobody but the iPhone reads"
expect_code '401|403' "anonymous can't publish" -d x "$NTFY/$TOPIC"
expect_code '401|403' "anonymous can't read" "$NTFY/$TOPIC/json?poll=1"
expect_code '401|403' "iPhone account can't publish" -u "iphone:$PHONE_PW" -d x "$NTFY/$TOPIC"
expect_code '401|403' "hub token can't read" -H "Authorization: Bearer $TOKEN" "$NTFY/$TOPIC/json?poll=1"
expect_code '401|403' "hub token can't publish outside ring-*" -H "Authorization: Bearer $TOKEN" -d x "$NTFY/other"
expect_code '200' "iPhone account can read" -u "iphone:$PHONE_PW" "$NTFY/$TOPIC/json?poll=1"

step "ringhub (the image under test, never pulled)"
dc up -d --pull never ringhub
for _ in $(seq 1 90); do curl -fsS -m 5 http://127.0.0.1:8787/healthz >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8787/healthz | grep -q '"ok":true'
curl -fsS -o /dev/null http://127.0.0.1:8787/                          # the dashboard ships in the image
curl -fsS -o /dev/null http://127.0.0.1:8787/static/views/modes.js
dc exec -T ringhub ringhub check -c /app/config.yaml
[ "$(dc exec -T ringhub id -u | tr -d '\r')" = 1000 ] || { echo "FAIL: ringhub is not running as uid 1000"; exit 1; }
expect_code '401' "unsigned webhook is refused" -H 'Content-Type: application/json' \
  -H 'X-Index-Webhook-Version: 1' -H 'X-Index-Trigger: click' -H "X-Index-Delivery: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -d '{"event":"button","gesture":"click","clicks":1,"pressedAt":0,"client":"ring"}' http://127.0.0.1:8787/ring

step "Ring → ringhub → ntfy → iPhone account"
MARKER="e2e$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
dc exec -T ringhub ringhub send hold --secret "$SECRET" --transcript "the parking spot code is $MARKER"
dc exec -T ringhub ringhub send hold --secret "$SECRET" --transcript "what did I say about the parking spot code"
python3 - "$NTFY/$TOPIC/json?poll=1&since=all" "iphone:$PHONE_PW" "$MARKER" "$(env_get NTFY_MIN_PRIORITY)" <<'PY'
import base64, json, sys, time, urllib.request
url, cred, marker, floor = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
auth = "Basic " + base64.b64encode(cred.encode()).decode()
deadline = time.monotonic() + 60
while True:
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=10) as r:
        msgs = [m for m in map(json.loads, r.read().decode().splitlines()) if m.get("event") == "message"]
    answers = [m for m in msgs if marker in m.get("message", "") + m.get("title", "")]
    if len(msgs) >= 2 and answers:
        break
    if time.monotonic() > deadline:
        sys.exit(f"FAIL: expected a capture confirmation and a recall answer containing {marker}; got {msgs}")
    time.sleep(1)
low = [m for m in msgs if m.get("priority", 3) < floor]
if low:
    sys.exit(f"FAIL: pushes below NTFY_MIN_PRIORITY={floor} (they would be silent on iPhone): {low}")
for m in msgs:
    print(f"ok: push p{m.get('priority', 3)}: {m.get('title', '')!r} {m.get('message', '')[:80]!r}")
PY

echo; echo "e2e passed"
