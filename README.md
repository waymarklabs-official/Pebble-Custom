# ringhub

**Turn a Pebble Index 01 ring into a remote for your whole house.** One button and a mic on your
finger become: per-room TV control, a sleep timer you set by talking, "play what my Pi just
downloaded on the bedroom TV", car preconditioning timed to when you leave, a focus timer that
counts your interruptions, a voice inbox that drops into Obsidian, and anything else you can
reach over HTTP, ADB or a shell.

```
 ring ──BLE──▶ Index app ──signed webhook──▶ ringhub ──▶ TVs (ADB / Roku ECP)
                                               │  ├──▶ ntfy  (feedback on phone / watch / desktop)
  5 gestures × modes × combos                  │  ├──▶ Pi media server, any HTTP API
  + free-form voice → intents                  │  ├──▶ Tesla Fleet API
                                               │  └──▶ Markdown notes, shell commands
                                               └── SQLite: state, timers, dedup, event log
```

## Why this works better than it should

- **The ring sends five gestures, not one.** The current Index app can POST `click`,
  `double-click` and `triple-click` as instant JSON events, and `hold` / `double-click-and-hold`
  as voice recordings with a transcript. ringhub implements that protocol from the app's open
  source code, including the HMAC signing. ([protocol notes](docs/INDEX_WEBHOOK_PROTOCOL.md))
- **Modes multiply them.** Triple-click cycles `home → tv → focus`; click means "hand raise" in
  one mode, "play/pause" in another and "log an interruption" in a third. Each mode change sends
  you a push explaining what the buttons now do.
- **Combos add more.** `double` then `click` within 650 ms is its own gesture ("pause every
  screen in the house"). Gestures that don't start a combo still fire instantly.
- **"here" knows which room you're in.** TV commands default to the TV that's on, or the one you
  used last, so one click does the obvious thing in every room.
- **Voice is deterministic first.** Regex intents with spoken-number normalization ("forty-five" →
  45) run offline and predictably. Optionally, Claude maps phrasing you didn't anticipate onto your
  *existing* intents. It cannot invent actions.
- **It never fails silently.** A screenless button that fails silently is useless, so every
  failure pushes an ntfy alert naming the rule and the reason, and every event lands in a log you
  can query.

## Quick start

```bash
pip install -e '.[llm]'          # Python 3.11+; also: apt install adb (for Android/Fire TV)
cp config.example.yaml config.yaml
cp .env.example .env && echo "RINGHUB_SECRET=$(ringhub secret)" >> .env
set -a; . ./.env; set +a
ringhub check                    # validates every rule, step and template reference
ringhub serve
```

Then, in the Index app: **Settings → Webhook**, configure each gesture with your hub's HTTPS URL,
enable **Sign requests** with the same secret, choose **Transcription only** (or **Both** to keep
audio) for the two voice gestures, and tap **Send test event**. You should get a "ringhub test OK"
push. The full walkthrough, including how to reach the hub from anywhere (Tailscale), is in
[docs/SETUP.md](docs/SETUP.md).

Without the ring, you can send exactly what the app would:

```bash
ringhub send triple-click --secret "$RINGHUB_SECRET"
ringhub send hold --transcript "sleep in 30 minutes" --secret "$RINGHUB_SECRET"
```

`ringhub send` is also how you plug **other buttons** (Stream Deck / Stream-Pi, a Flic, cron jobs,
an NFC tag shortcut) into the same rules.

## The recipes

[docs/RECIPES.md](docs/RECIPES.md) is the playbook: what's in the example config, why it works,
and the less obvious tricks. Highlights:

| Gesture | Does |
|---|---|
| hold: *"sleep in 45"* | TV in this room powers off in 45 min. The timer survives reboots, and saying it again replaces it. |
| hold: *"play the latest on the bedroom TV"* | Asks your Pi downloader for its newest file and plays it there: Android TV via a VIEW intent, Roku via Media Assistant. |
| tv mode, double | **"What did they say?"** Rewinds a few seconds and turns subtitles on, then turns them off 20 s later. |
| double → click | Pauses every TV in the house at once. |
| hold: *"I'm leaving in 25"* | Car climate starts at T−10, and the house pauses when you walk out. |
| hold: *"bookmark this"* | Logs the app on screen plus a timestamp to `bookmarks.md`. |
| focus mode | Double starts a 25 min block. Click silently tallies interruptions, and the end-of-block push reports the count. |
| click-hold | Raw journal entry with audio, no interpretation. |
| anything unmatched | Lands as a checkbox in today's inbox note, so nothing you say is lost. |

## Configuration model

```yaml
rules:                     # evaluated in order; first match wins unless `continue: true`
  - name: tv play/pause
    when: {gesture: click, mode: tv}          # also: sequence, time, days, state, payload, transcript
    do: [{tv: play_pause}]                    # shorthand for {tv: {command: play_pause, target: here}}
intents:
  - name: sleep timer
    match: ['sleep (?:in )?(?P<minutes>\d+)']
    do:
      - schedule: {id: sleep_timer, after: "{{ minutes }}m", do: [{tv: power_off}]}
```

Actions: `notify`, `http`, `tv`, `tv.key`, `tv.launch`, `tv.open_url`, `tv.active_app`, `here.set`,
`media.play_latest`, `notes.append`, `intent.route`, `mode.set`, `mode.cycle`, `state.set`,
`state.toggle`, `state.increment`, `state.delete`, `schedule`, `schedule.cancel`, `wait`, `run`,
`parallel`, `tesla`, `exec` (opt-in), `log`, `noop`. Each one is a small, typed function in
`src/ringhub/actions/`, so adding your own is one decorator.

## Production notes

- **Security:** HMAC-SHA256 on every request (version, timestamp, delivery id, trigger and raw
  body), a ±5 min replay window, constant-time comparison, delivery-id dedup persisted for 7 days,
  a 25 MB body cap, a sandboxed Jinja environment (transcripts are data, never templates), notes
  writes confined to `notes_dir`, ADB arguments shell-quoted, `exec` off by default and run without
  a shell, and the admin API off unless a token is set.
- **Ordering:** events are processed one at a time in arrival order, so "switch mode, then click"
  always sees the new mode. Long delays belong in `schedule` (persistent), not `wait` (capped at 60 s).
- **Ops:** `GET /healthz`, plus `GET /api/events|state|jobs` and `POST /api/simulate` with a bearer
  token. There's a Docker image (multi-arch base, non-root, tini), a hardened systemd unit, and CI
  running ruff, `mypy --strict` and pytest on Python 3.11–3.13 plus a Docker build.

## Development

```bash
pip install -e '.[dev,llm]'
ruff check src tests && mypy src && pytest -q
```

Tests replay byte-exact copies of the app's requests through the real HTTP stack. They drive a
fake `adb` binary and mocked Roku ECP, and run every recipe in `config.example.yaml` against fake
TVs.
