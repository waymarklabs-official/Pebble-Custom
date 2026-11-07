# Integrations: Tesla and Google Calendar

Both are optional and turn on from `.env`, and both connect from the dashboard (**Setup →
Integrations**). If your `config.yaml` predates them and you never customized it, take a fresh
copy: `cp config.example.yaml config.yaml` (it has the `tesla:` and `google:` sections, fed from
`.env`). After changing `.env`, run `docker compose up -d` (with `sudo` if you need it for Docker).

---

## Google Calendar

What you get:
- **"Add dentist to my calendar Friday at 3"**, "put lunch with Sam on my calendar tomorrow at
  noon", "schedule a haircut Tuesday at 4pm".
- "**… for 30 minutes**" or "**from 2 to 4**" sets the length (otherwise 1 hour). A day with no time
  ("on Sunday") makes an all-day event.
- **"What's on my calendar (today / tomorrow / this week)?"**, "what's my next meeting?"
- **"Scratch that"** (or a double-click) right after adding removes the event again.
- If no date was heard, or Google can't be reached, your words are saved as a note. Nothing is lost.
- The **What's on my calendar** skill template binds your agenda to a gesture.

### 1. Create the OAuth client (once, about 10 minutes)

In [Google Cloud Console](https://console.cloud.google.com/), signed in as the account whose
calendar you want:

1. Create a project, e.g. "ringhub".
2. **APIs & Services → Library →** Google Calendar API **→ Enable**.
3. **OAuth consent screen** (Google Auth Platform → Branding/Audience): User type **External**, app
   name "ringhub", your email as support and developer contact.
   - **Data Access**: add the scope `https://www.googleapis.com/auth/calendar.events`.
   - **Audience → Publish app** ("In production"). **Don't skip this:** while an app is in
     "Testing", Google expires its refresh tokens after 7 days and the calendar stops working.
     You don't need Google's verification for your own account. You'll see a "Google hasn't
     verified this app" screen when connecting; choose **Advanced → Go to ringhub**.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   - **Authorized redirect URI:** copy it from the dashboard (Setup → Integrations → Google
     Calendar). It's your `RINGHUB_PUBLIC_URL` + `/oauth/callback`, for example
     `https://tesla-stream.tail51b89d.ts.net:8445/oauth/callback`. Only the browser on your
     phone needs to reach it, so a tailnet address works.
   - Copy the **Client ID** and **Client secret**.

### 2. Turn it on

```
GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
# GOOGLE_CALENDAR_ID=primary          # or a calendar's ID from its settings in Google Calendar
```
Add these to `.env`, then `docker compose up -d`.

### 3. Connect

Dashboard → Setup → Integrations → **Connect Google Calendar**. Sign in, allow access, and you're
back on the Setup page showing "Connected as you@gmail.com". Try: hold the ring and say "add test
event to my calendar tomorrow at 9".

To disconnect: the dashboard's **Disconnect** removes the hub's token and revokes it at Google.

---

## Tesla

What you get, by ring gesture or voice:

| Say | Does |
|---|---|
| "how's the car" / "car battery" / "is the car locked" | 🔋 78% (241 mi) · locked · 61°F inside. Never wakes the car. |
| "wake up the car" | Wakes it so the Tesla app and the next command answer at once. Leaves an awake car alone. |
| "warm up the car" / "start the car" · "stop the car climate" | Climate on / off |
| "set the car to 70" | Driver and passenger 70°F (or "set the car to 21" for °C) |
| "defrost the car" | Max defrost |
| "lock the car" · "unlock the car" | Locks |
| "open the frunk" · "open the trunk" | Frunk / power liftgate |
| "honk" · "flash the lights" / "find my car" | Horn / lights |
| "open/close the charge port" · "start/stop charging" · "charge the car to 80" | Charging |
| "turn on/off sentry mode" | Sentry |
| "vent the windows" · "close the windows" | Windows |
| "heat my seat" · "heat the steering wheel" | Driver seat (high) / wheel heater |
| "open the garage" | HomeLink (the car must be near the door) |
| click (Home/Morning/Heading out modes) | **Car: trunk or climate**: at the car and unlocked → trunk; away → climate on |

**Safety.** Unlock, trunk, frunk, vent and garage run only when your phone's recent location puts
you within 150 m of the car, so a misheard command can't open a car across town. Use the Tesla app
for deliberate remote unlocking. Each of these skills has an **Only when I'm at the car** switch
you can turn off.

All of these are ordinary skills, added the first time you connect. Rename them, change phrases,
bind them to gestures, or build your own from the **Car** template.

### How it works

```
ring ─▶ ringhub ──commands──▶ tesla-proxy (on the Pi, signs with your key) ──▶ Fleet API ──▶ car
            └────state reads (never wake the car)──────────────────────────▶ Fleet API
```

A Model Y only accepts commands signed by a key the car trusts. That key is yours: it's created
on the Pi and never leaves it. Tesla's official `tesla-http-proxy` signs with it. You pair the key
to the car once, from the Tesla app.

**Cost.** Tesla bills Fleet API use by the request (commands, data, and especially wake-ups), with
a monthly credit for personal use. Check current rates at
[developer.tesla.com](https://developer.tesla.com). ringhub calls the API only when you ask, never
polls, never wakes the car to check its state, and caches what it reads for 30 seconds.

### 1. Keys (on the Pi)

```bash
cd ~/ringhub
bash deploy/pi/tesla-keys.sh
```
This makes:
- `data/tesla/private-key.pem`: your signing key. Back it up with `data/` and never share it.
- `public-key.pem` and `callback.html`: the two files for your public site in step 2.
- The proxy's TLS certificate.

Running it again never replaces a key.

### 2. Put two files on a public site you control (not the Pi)

Tesla checks the app's domain and sign-in address against these rules, as its form states them:
- they must not contain "tesla";
- they need a certificate from a public CA;
- they must not sit behind a reverse proxy.

Your Pi fails all three: its name is `tesla-stream…`, and Tailscale Serve is a reverse proxy. So
the Tesla side lives on a small static site of yours. GitHub Pages is free, and many Fleet API
users host their key there. The Pi stays exactly as it is, with nothing exposed and no
networking changes.

1. On GitHub, create a public repository named **`<your-username>.github.io`** (for this account:
   `waymarklabs-official.github.io`). Your domain is then `waymarklabs-official.github.io`.
2. Add three files:
   - `.well-known/appspecific/com.tesla.3p.public-key.pem`: the contents of
     `data/tesla/public-key.pem`. Tesla reads your key here.
   - `callback.html`: the contents of `data/tesla/callback.html` (step 1 makes it from
     `RINGHUB_PUBLIC_URL` in `.env`; the dashboard also offers it under Setup → Integrations →
     Tesla → **Download callback.html**). After you sign in, Tesla sends your browser here, and the
     page passes it straight on to ringhub over Tailscale. It's static, contains no secrets, and
     only ever forwards to your own ringhub address. If `RINGHUB_PUBLIC_URL` ever changes, run
     step 1 again and replace this file.
   - `.nojekyll`: empty. Without it, GitHub Pages hides folders that start with a dot.
3. **Settings → Pages →** deploy from the main branch. Then check in a browser that
   `https://waymarklabs-official.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem`
   shows the key.

A domain you own works the same way: host the same three files.

### 3. Create the app at developer.tesla.com

Sign in with your Tesla account, then **Dashboard → Create application**:
- **Allowed origin:** `https://<domain>` (e.g. `https://waymarklabs-official.github.io`).
- **Allowed redirect URI:** `https://<domain>/callback.html` (e.g.
  `https://waymarklabs-official.github.io/callback.html`). The dashboard shows it too. It must
  match character for character.
- **Scopes:** Vehicle Information, Vehicle Location, Vehicle Commands, Vehicle Charging
  Management.
- Copy the **Client ID** and **Client Secret**. Set up billing if Tesla asks.

### 4. Turn it on and start the proxy

```
TESLA_CLIENT_ID=...
TESLA_CLIENT_SECRET=...
TESLA_DOMAIN=waymarklabs-official.github.io
```
Add these to `.env`, then:
```bash
docker compose --profile tesla up -d      # starts tesla-proxy next to ringhub and ntfy
```
From now on, keep `--profile tesla` on `docker compose pull` and `up -d`, or put
`COMPOSE_PROFILES=tesla` in `.env` so plain `docker compose up -d` includes it.

### 5. Connect, register, pair (dashboard → Setup → Integrations → Tesla)

1. **Connect Tesla:** sign in at Tesla and allow access. Tesla sends you to your `callback.html`,
   which takes you back to the dashboard. With one car on the account, it's picked automatically;
   otherwise tap **Show my cars** and choose. If you end up stuck on the callback page (Tailscale
   was off on the phone), copy that page's full address and paste it under **Finish connecting by
   pasting the address**.
2. **Register** (once): tells Tesla your domain and key, and checks that the key Tesla downloaded
   matches this hub's.
3. **Pair the key** (once): on your phone, with the Tesla app signed in and the car nearby, open
   the link shown (`https://tesla.com/_ak/<domain>`) and approve. The car lists your domain under
   **Controls → Locks**.
4. **Check the car:** reads its state without waking it. Then try "flash the lights".

### Troubleshooting

| Symptom | Fix |
|---|---|
| "the car doesn't know this hub's key yet" | Do step 5.3 (pairing). The car must be online and near your phone. |
| "the command proxy isn't running" | `docker compose --profile tesla up -d`, then `docker compose logs tesla-proxy`. |
| Tesla's form rejects the origin or redirect URI | Use your GitHub Pages (or own) domain, never the Pi's address. Neither may contain "tesla". `ringhub check` and the dashboard flag addresses Tesla would refuse. |
| "redirect_uri mismatch" after signing in | The redirect URI at developer.tesla.com must be exactly what the dashboard shows (`https://<domain>/callback.html`). Use `TESLA_REDIRECT_URI` only if you host the page elsewhere. |
| Stuck on the callback page | Turn Tailscale on and reload it, or paste its address into the dashboard (Finish connecting). |
| Register fails with a domain error | The allowed origin at developer.tesla.com must be `https://<TESLA_DOMAIN>`, and the key URL must load in a browser. |
| "registered, but with a different public key" | The hosted file isn't `data/tesla/public-key.pem`. Replace it, then Register again. |
| "access was revoked or expired" | Connect Tesla again (refresh tokens are single-use, and changing your Tesla password revokes them). |
| "Not done: I can't tell you're at the car" | Unlock, trunk, vent and garage need a recent location from your phone. Use OwnTracks, or the Shortcuts location automations. |
| The car is asleep | State reads say so (and never wake it). Commands wake it first, which takes up to ~45 s. Bind **Wake the car** to a gesture to wake it ahead of time. |
