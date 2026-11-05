# Setup

The target is a small always-on Linux box: a Pi 4/5, a thin client, or a container on your home
server. ringhub idles at roughly 60 MB RAM and does no work between button presses.

## 1. Install

**Docker (recommended):**

```bash
cp config.example.yaml config.yaml
cp .env.example .env            # fill in RINGHUB_SECRET=$(ringhub secret) etc.
docker compose up -d --build
docker compose logs -f
```

**Bare metal:** see the header of [`deploy/ringhub.service`](../deploy/ringhub.service).

## 2. Make it reachable from your phone, anywhere

The ring talks to your phone, and your phone POSTs to the hub. Your phone is not always on your
LAN, so the hub needs an HTTPS URL that works from anywhere. The app retries failed deliveries
every minute, up to 10 times.

- **Tailscale (simplest, private):** install Tailscale on the hub and the phone, then
  `tailscale serve --bg 8787` to get `https://<hub>.<tailnet>.ts.net/ring`. The URL is only
  reachable from your own devices.
- **Cloudflare Tunnel / Caddy with a domain:** works too. Keep request signing on, which it is by
  default.

## 3. Configure the Index app

Index 01 Settings → **Webhook**. For **each** gesture (click, double-click, triple-click,
hold & talk, double-click & hold):

1. URL: `https://<your-hub>/ring`
2. **Sign requests**: on, and paste `RINGHUB_SECRET`. You can use a different secret per gesture
   by adding `ring.secrets.<gesture>` entries.
3. Voice gestures: payload **Transcription only**, or **Both** if you want audio archived in
   `data/audio/`.
4. Click gestures: leave **payload** empty, or put a JSON or text tag in it and match on it with
   `when: {payload: ...}`.
5. **Send test event**. Expect a 200 in *Recent runs* and a "ringhub test OK" push.

Check that the hub saw it: `curl -H "Authorization: Bearer $RINGHUB_ADMIN_TOKEN" localhost:8787/api/events`.

> Button-event webhooks (click/double/triple) are in recent builds of the Core Devices app. If
> your app only lists the two recording gestures, update it. Everything voice-driven works either
> way, and modes can also be switched by voice ("tv mode").

## 4. TVs

**Android TV / Google TV / Shield / Fire TV (ADB):**

1. Enable Developer options: Settings → About → click *Build* 7 times. On Fire TV it's
   Settings → My Fire TV → About → click the device name 7 times.
2. Turn on **USB/ADB debugging** (some models call it *Network debugging*).
3. Give the TV a DHCP reservation and put its IP in `devices:`.
4. Trigger any TV action once and accept **"Allow USB debugging? — Always allow"** on the TV.
   The key is stored in the `adb-keys` volume (Docker) or `~/.android` (bare metal). Keep it, or
   you'll have to approve every TV again.

Useful package names for `tv.launch`: YouTube `com.google.android.youtube.tv`, Netflix
`com.netflix.ninja`, Plex `com.plexapp.android`, Kodi `org.xbmc.kodi`, VLC `org.videolan.vlc`.
To find any app's package name, open it and run `adb shell dumpsys window | grep mCurrentFocus`.

**Roku (ECP):**

1. Settings → System → Advanced system settings → **Control by mobile apps** → *Network access*:
   **Permissive**. Roku OS 13+ blocks ECP by default; a 403 in the logs means this setting is off.
2. For `tv.open_url` / `media.play_latest`, install the free **Media Assistant** channel
   (id 782875). ringhub launches it with the media URL.
3. `power_on` from deep sleep needs *Fast TV start* enabled on Roku TVs.

## 5. Your Pi downloader

`media.play_latest` needs one endpoint that returns the newest item as JSON, for example:

```json
{"url": "http://pi.local:8000/media/2026-11-05-some-video.mp4", "title": "Some video"}
```

Adjust `url_field` / `title_field` (dotted paths such as `items.0.url`) to match what your app
already returns. If you only have a static file server, a 10-line handler that sorts by mtime
does the job.

## 6. Tesla (optional)

Model Y needs **signed** commands:

1. Register a Fleet API app at developer.tesla.com and complete the partner and virtual-key
   steps (pair the key via `https://tesla.com/_ak/<your-domain>`).
2. Run Tesla's [`tesla-http-proxy`](https://github.com/teslamotors/vehicle-command) next to
   ringhub. It signs commands with your key.
3. Fill in the `tesla:` section and point `base_url` at the proxy. ringhub refreshes the access
   token and persists each rotated refresh token in its state store.

Command names are the standard Fleet API ones, for example `auto_conditioning_start`,
`actuate_trunk` (`which_trunk: front|rear`), `door_lock`, `door_unlock`, `flash_lights`,
`honk_horn` and `charge_port_door_open`.

## 7. Voice fallback with Claude (optional)

```yaml
llm:
  api_key: ${ANTHROPIC_API_KEY}
```

When no regex matches, the transcript and your intent catalog (names, `description`s and slot
names) go to Claude, which either picks one intent and fills its slots or answers "none". It only
runs after the regexes miss, so most commands never leave the box. Add `description:` to the
intents you want it to choose reliably.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App shows `401` | Secret mismatch, or the phone/hub clocks are more than 5 min apart. The response body says which. |
| App shows `400 unsupported webhook version` | Very old app build. Update it. |
| Push "Ring click failed: … cannot connect" | TV asleep with network standby off, the IP changed, or ADB debugging was reset by a TV update. |
| Click does nothing, no push | Check `/api/events`: `unmatched` means no rule exists for that gesture in the current mode. |
| Combo fires as two separate gestures | You were slower than `combo_window_ms`. Raise it to 800–900. |
