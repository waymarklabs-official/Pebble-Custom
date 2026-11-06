# Setup

Five steps, about 30 minutes. At the end you have a ring that captures and recalls notes,
reminds you by time and place, and changes what its button does as you move through the day.

1. [Run the hub](#1-run-the-hub)
2. [Make it reachable from your phone](#2-make-it-reachable-from-your-phone)
3. [Point the ring at it](#3-point-the-ring-at-it)
4. [Tell it where you are (iOS Shortcuts or OwnTracks)](#4-tell-it-where-you-are)
5. [Optional: Tesla, voice fallback, TVs](#5-optional-integrations)

## 1. Run the hub

> **Raspberry Pi already running other apps on Tailscale?** Use [PI.md](PI.md) instead of steps 1–2:
> it sets up ringhub plus a private ntfy server and publishes both without touching your other apps.

Any small always-on Linux box works: a Pi 4/5, a thin client, or a container on a home server.
**With Docker**, follow [PI.md](PI.md) on any machine: it is not Pi-specific. **Without Docker:**

```bash
pip install -e '.[llm]'                                          # Python 3.11+
ringhub env --host <machine>.<tailnet>.ts.net --tz America/New_York > .env   # same as ringhub.envfile
echo "NTFY_URL=https://ntfy.sh" >> .env                          # or your own ntfy server
cp config.example.yaml config.yaml
set -a; . ./.env; set +a
ringhub check && ringhub serve
```

`ringhub check` prints what's on and what's missing. Open the dashboard and sign in with
`RINGHUB_ADMIN_TOKEN`. The **Now** page shows the active mode and what each gesture does.

Install the **ntfy** app on your iPhone and subscribe to `NTFY_TOPIC`. This is how the ring
talks back. The Pebble watch mirrors these notifications automatically. Allow Time Sensitive
notifications for ntfy so reminders can break through a Focus.

> Self-hosting ntfy? Set `upstream-base-url: "https://ntfy.sh"`, otherwise iOS delivers pushes
> late, which defeats a memory aid. Also set `cache-file`.

## 2. Make it reachable from your phone

Your phone POSTs ring events and context to the hub from wherever you are, so the hub needs an
HTTPS URL that works away from home.

- **Tailscale (recommended, private):** install it on the hub and the phone, then run
  `tailscale serve --bg 8787`. Put `https://<hub>.<tailnet>.ts.net` in `RINGHUB_PUBLIC_URL`.
- Cloudflare Tunnel or Caddy with your own domain also work. Signing and the tokens protect
  everything, but prefer not exposing the dashboard publicly.

`public_url` also turns on the **Done / Snooze** buttons on reminder pushes. They carry signed,
expiring links, never your admin token.

## 3. Point the ring at it

Index 01 app → **Settings → Webhook**. For each gesture (click, double-click, triple-click, hold
& talk, double-click & hold):

1. URL: `https://<public_url>/ring`
2. **Sign requests**: on, and paste `RINGHUB_SECRET`.
3. For the two voice gestures, set the payload to **Transcription only**, or **Both** to keep the
   audio.
4. **Send test event** should produce a "ringhub test OK" push.

Try it: hold and say *"remind me in two minutes to stretch"*. A confirmation arrives, then the
reminder. When it arrives, click the ring to mark it done.

> Click, double and triple webhooks need a recent Index app. If you only see the two voice
> gestures, update the app. Voice works either way.

## 4. Tell it where you are

The dashboard's **Setup** page generates every URL below with copy buttons. Create your
places there first (Home, Work and Car exist by default). The aliases are what you'd *say*
("the office").

### Option A: iOS Shortcuts automations (no extra app)

Shortcuts app → **Automation** → **+** → pick a trigger → **Run Immediately** → add **Get
Contents of URL**:

- URL: shown on the Setup page, for example `https://<public_url>/api/v1/context/arrive/home`
- Method: **POST**
- Headers: `Authorization` = `Bearer <RINGHUB_CONTEXT_TOKEN>`

| Trigger | URL | Notes |
|---|---|---|
| Arrive at Home | `/api/v1/context/arrive/home` | iOS shows a "Running your automation" banner for location triggers. |
| Leave Home | `/api/v1/context/leave/home` | Starts **Heading out** mode for 10 minutes. |
| Arrive/Leave Work | `/api/v1/context/arrive/work`, `/leave/work` | |
| Bluetooth: car connects | `/api/v1/context/arrive/car` | Switches to **In the car** (quiet feedback). |
| Bluetooth: car disconnects | `/api/v1/context/leave/car` | **Saves where you parked**, so "where did I park?" works. |
| Focus: Sleep turns on | `/api/v1/context/focus/sleep` | Runs silently. Turning off → `/api/v1/context/focus/off`. |
| Focus: Work turns on | `/api/v1/context/focus/work` | Use as a mode condition. |
| NFC tag (by the door) | `/api/v1/context/visit/home` | Silent. Good for "before I leave" checks. |
| Wallet: tap at a store | `/api/v1/context/visit/<store>` | "Next time I'm at Target buy batteries" fires when you pay at Target. |

What to expect:
- **Precision.** iOS geofences trigger about 200 m past the edge and are unreliable below a 100 m
  radius. The hub ignores a "leave" that comes within 4 minutes of "arrive" (jitter) and
  collapses duplicate arrivals.
- **Silence.** Focus, NFC, CarPlay, Alarm and Sleep automations run silently. Arrive/Leave,
  Wi-Fi and Bluetooth always show a banner.
- **Late reports.** If a leave is missed (battery, no signal), the next arrival elsewhere fixes
  the state. Pending "when I get home" reminders still fire on the next arrival home.

### Option B: OwnTracks (silent, continuous)

OwnTracks reports transitions silently and keeps working in the background.

1. Install OwnTracks → Settings → Mode **HTTP**.
2. URL `https://<public_url>/api/v1/owntracks`, Authentication on, username anything,
   password `RINGHUB_CONTEXT_TOKEN`.
3. Give your places latitude/longitude (**Setup → Places**). The hub sends them to OwnTracks as
   regions automatically (at most 20, the iOS limit), and OwnTracks enter/leave events become
   arrive/leave.

You can combine both options: OwnTracks for places, Shortcuts for Focus, the car and NFC.

## 5. Optional integrations

### Tesla: the "Car: trunk or climate" skill

With Phone Key and walk-up unlock, a Model Y unlocks as you walk up and locks as you walk
away, so **unlocked ≈ you're at the car**. One button then means "open the trunk" at the car and
"warm it up" anywhere else.

Setup (keys, the signing proxy, connecting your account, pairing the key to the car) is in
[INTEGRATIONS.md](INTEGRATIONS.md#tesla), along with every car voice command and Google Calendar.

Cost and battery: reading state on a press costs one free online check plus one data call
(about $0.002) only if the car is awake. A sleeping car is never woken just to read; it's treated
as locked and away. Commands cost about $0.001, and a wake (needed to precondition a sleeping
car) about $0.02. Nothing polls in the background. If your phone's last reported position is
more than 150 m from the car, the hub refuses to open the trunk even if the car reports unlocked.

### Voice fallback (Claude)

```yaml
llm:
  api_key: ${ANTHROPIC_API_KEY}
```

The built-in parser handles "in 20 minutes", "at 7", "tomorrow morning", "on Friday", "when I
get home/to work", "when I leave…" and "next time I'm at…". With `llm` set, sentences it doesn't
recognize as reminders go to Claude, which either extracts what/when/where into a fixed schema
or says it's just a note. It can't trigger anything else.

### TVs

Android/Fire TV (ADB) and Roku (ECP) still work as a **TV remote** skill template. See
`config.example.yaml` → `devices`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Ring app shows `401` | Secret mismatch, or the phone and hub clocks are more than 5 min apart. The response body says which. |
| Shortcut automation gets `401` | Header must be exactly `Authorization: Bearer <context token>`. |
| No pushes at all | Check `ntfy.topic` and that the app is subscribed. `ringhub check` shows the config. |
| Reminder buttons do nothing | Set `server.public_url`. The phone must reach that URL. iOS opens the ntfy app first. |
| Mode didn't change on arrival | **Setup → What your phone reported** shows each report and when it arrived. |
| "I don't know 'gym' yet" | Add a place called Gym, or add "gym" as an alias to an existing place. |
