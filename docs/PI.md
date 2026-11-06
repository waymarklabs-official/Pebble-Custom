# Running ringhub on your Pi, with its own ntfy

This puts ringhub and a **private ntfy server just for the ring** on a Raspberry Pi that already
runs other things on your tailnet (like Tstream). Nothing about the existing apps changes.

```
iPhone (Tailscale on)                         Raspberry Pi
 ├─ Index app ─ webhooks ─┐
 ├─ Shortcuts ─ context ──┼─▶ https://pi.<tailnet>.ts.net:8445 ─▶ ringhub  127.0.0.1:8787
 ├─ dashboard (Safari) ───┘                                          │ publishes
 └─ ntfy app ◀── fetch ─────── https://pi.<tailnet>.ts.net:8446 ─▶ ntfy  127.0.0.1:2586
          ▲                                                          │ "wake up" (no content)
          └──── Apple push ◀── ntfy.sh relay ◀───────────────────────┘
                     your other apps' ports (e.g. Tstream on 8443) ─▶ untouched
```

ringhub uses tailnet ports **8445** and **8446** by default. It avoids 443, 8443 and 10000 because
those are the only ports Tailscale Funnel can use, so other apps are likely to be on them already.

**Resources:** ringhub idles at about 46 MB RAM and 0.2% CPU (measured), and ntfy is similarly
small. Traffic is a few KB per press, or under 50 KB for a voice note with audio, so it won't
compete with streaming. If you outgrow the Pi, see [Moving to a mini PC](#moving-to-a-mini-pc).

> ⚠️ **Never remove Tailscale mappings by hand.** On a Pi with Tstream, port 8443 is Tstream's
> public Funnel, and `tailscale serve reset` or a bare `tailscale serve off` would take it down. To
> unpublish ringhub, run `sudo bash deploy/pi/serve.sh --off`. It removes only the two ports in
> your `.env`, and only after checking that they still point at ringhub and ntfy.

**Already running ringhub on this Pi?** Skip to [Upgrading an existing install](#upgrading-an-existing-install).

---

## 1. Check the Pi (changes nothing)

```bash
git clone https://github.com/waymarklabs-official/Pebble-Custom.git ~/ringhub && cd ~/ringhub
git checkout claude/pebble-ring-automation-kqoqn3
bash deploy/pi/preflight.sh
```

The script checks, without changing anything:
- **The OS.** It must be 64-bit; there is no 32-bit image.
- **Docker.** It must be installed, and the script reports whether you need `sudo` for it.
- **Downloads.** The Pi must be able to reach GitHub's image registry and Docker Hub, and the
  image for your Pi must exist.
- **Tailscale.** It lists your current mappings (Tstream's included) and confirms that 8445 and
  8446 are free.

If a port is taken, try two others: `bash deploy/pi/preflight.sh 8447 8448`. The script ends by
printing the exact commands for step 2, with your machine name, ports and image tag filled in.

**If it says Docker needs sudo,** put `sudo` in front of every `docker` command below. If it says it
couldn't read the Tailscale mappings, run it again as `sudo bash deploy/pi/preflight.sh`.

In the Tailscale admin console (**DNS**), make sure **MagicDNS** and **HTTPS Certificates** are
on. They probably already are if Tstream uses `https://…ts.net`.

## 2. Configure

Run the commands preflight printed. They look like this:

```bash
mkdir -p data ntfy/cache ntfy/lib            # must exist before the first start (else Docker makes them root-owned)
PYTHONPATH=src python3 -m ringhub.envfile --host pi.tail1234.ts.net --ring-port 8445 --ntfy-port 8446 \
  --tag claude-pebble-ring-automation-kqoqn3 --tz America/New_York > .env
chmod 600 .env && cp config.example.yaml config.yaml
```

This uses the Pi's built-in Python, with nothing to install first. `.env` now holds fresh
secrets, a random ntfy topic and both URLs. The defaults in `config.yaml` are fine.

`--tag` chooses which tested image to run (see [Which image runs](#which-image-runs)). It should
match the branch you checked out, with `/` written as `-`: here
`claude-pebble-ring-automation-kqoqn3`, or `main` once this work is merged.

## 3. Start ntfy and create its two accounts

```bash
docker compose up -d ntfy
docker compose exec ntfy ntfy user add ringhub                       # any long password; never used again
docker compose exec ntfy ntfy access ringhub 'ring-*' write-only
docker compose exec ntfy ntfy token add --label=ringhub ringhub      # prints tk_...
nano .env                                                            # paste it after NTFY_TOKEN=
docker compose exec ntfy ntfy user add iphone                        # choose a password: you'll type it into the iPhone
docker compose exec ntfy ntfy access iphone 'ring-*' read-only
```

The hub can only publish and your phone can only read. Nobody else can do either, because the
server denies everything by default. CI checks each of these rules on every build.

## 4. Start ringhub, then publish both to your tailnet

```bash
docker compose pull                                                  # downloads the tested image
docker compose up -d
docker compose exec ringhub ringhub check -c /app/config.yaml        # everything should say "on"
sudo bash deploy/pi/serve.sh                                         # publishes the two ports in .env, nothing else
```

`serve.sh` reads its ports from `.env`, so they always match the URLs your phone uses. It shows
your Tailscale mappings before and after, so you can see that Tstream's entry is unchanged. It
refuses to change anything if a port belongs to something else. The mappings survive reboots,
and the containers restart on their own.

## 5. iPhone

1. **Tailscale app → your profile → VPN On Demand:** set it to always connect, on Wi-Fi and
   cellular. The ring, Shortcuts and ntfy reach the Pi through Tailscale, so it must be on.
2. **ntfy app → Settings → add a server:** type the `NTFY_PUBLIC_URL` from `.env` *exactly*
   (`https://pi.tail1234.ts.net:8446`, no trailing slash). Add the user `iphone` with its password.
   Then subscribe to the `NTFY_TOPIC` from `.env` on that server. Allow **Time Sensitive**
   notifications for ntfy (iOS Settings → Notifications → ntfy).
3. **Dashboard:** open `RINGHUB_PUBLIC_URL` (`https://pi.tail1234.ts.net:8445`) in Safari, sign in
   with `RINGHUB_ADMIN_TOKEN`, and add it to your Home Screen.

## 6. The ring

Index app → **Settings → Webhook**. For each gesture:
- URL: `RINGHUB_PUBLIC_URL` followed by `/ring` (`https://pi.tail1234.ts.net:8445/ring`)
- **Sign requests**: on, and paste `RINGHUB_SECRET`.
- For the two voice gestures, set the payload to **Transcription only**.

Then tap **Send test event**. A "ringhub test OK" notification should arrive within seconds.

## 7. Tell it where you are

The dashboard's **Setup** page lists copy-ready Shortcuts automations with your URL and token
filled in. Start with Arrive/Leave Home, car Bluetooth connect/disconnect, and Sleep Focus.
Add Tailscale's **Connect** action at the top of each automation, so it works even if the VPN
was asleep. Background details are in [SETUP.md](SETUP.md#4-tell-it-where-you-are).

## 8. Try it

- Hold: *"remind me in two minutes to stretch."* When it fires, click to mark it done.
- Hold: *"the wifi password at the cabin is lakeside42"*, then *"what did I say about the cabin?"*
- Triple-click: hear which mode you're in and what each gesture does.

---

## Which image runs

The Pi never builds anything. For every commit, GitHub Actions first runs the tests. It then
builds the arm64 image and runs this whole stack under emulation: the compose file, ntfy, the
accounts from step 3, and a signed voice note that must arrive on the `iphone` account. Only after
that does it publish amd64 + arm64 images to `ghcr.io/waymarklabs-official/pebble-custom`. Every
Python package in the image is pinned by version and checksum, and so is the base image.

`RINGHUB_TAG` in `.env` chooses the image:

| Tag | Runs | Changes when |
|---|---|---|
| `main` | the default branch | something is merged |
| `claude-pebble-ring-automation-kqoqn3` | that branch (`/` written as `-`) | that branch gets a commit |
| `sha-1a2b3c4` | one exact commit (from the Actions run or the package page) | never: published once, never overwritten |

For the steadiest production install, pin a `sha-` tag and change it on purpose.

## Upgrading an existing install

For a Pi that already runs ringhub (for example, one that built its own image before CI existed).
From `~/ringhub`:

1. **Get the new scripts and check (changes nothing).**
   ```bash
   git pull
   bash deploy/pi/preflight.sh
   ```
   It reads your ports from `.env`. Mappings you already published show as ✓ "already publishes
   ringhub", and Tstream's show as left alone.
2. **Fix what it flags.** If it says `.env` has no `RINGHUB_TAG`, add the line it prints. Also add
   this line if it's missing:
   ```
   NTFY_MIN_PRIORITY=4
   ```
   If you deleted the `min_priority:` line from `config.yaml`, put it back under `ntfy:`, or take
   a fresh copy with `cp config.example.yaml config.yaml` (only if you never customized it):
   ```yaml
     min_priority: ${NTFY_MIN_PRIORITY:-4}
   ```
3. **Switch to the tested image.**
   ```bash
   docker compose pull && docker compose up -d
   docker compose exec ringhub ringhub check -c /app/config.yaml
   sudo bash deploy/pi/serve.sh            # keeps your existing ringhub/ntfy mappings as they are
   docker image rm ringhub:latest          # optional: the old self-built image is no longer used
   ```

Your data, `.env`, ntfy accounts and phone setup are unchanged, so there's nothing to redo on the
iPhone.

## Everyday operations

| Task | Command |
|---|---|
| Check everything | `bash deploy/pi/preflight.sh` (read-only, safe any time) |
| Logs | `docker compose logs -f ringhub` (or `ntfy`) |
| Update | `git pull && docker compose pull && docker compose up -d` |
| Roll back | Set `RINGHUB_TAG=sha-<previous>` in `.env`, then `docker compose up -d` |
| Back up | Copy `~/ringhub/data`, `~/ringhub/ntfy`, `.env` and `config.yaml` (or Dashboard → Setup → Backup for modes/skills) |
| Stop serving | `sudo bash deploy/pi/serve.sh --off` (removes ringhub's two ports only) |

## Moving to a mini PC

Everything lives in `~/ringhub`. Stop the containers, copy the whole folder (including `data/`,
`ntfy/` and `.env`), install Docker and Tailscale on the new machine, then run
`bash deploy/pi/preflight.sh`, `docker compose up -d` and `sudo bash deploy/pi/serve.sh` there.
The same image runs on amd64.

**Keep the same Tailscale machine name and every URL stays valid:** the ring's webhooks, your
Shortcuts and the ntfy subscription all point at it. Rename the old Pi first, then give the new
machine the Pi's old name in the Tailscale admin console. If the name does change, edit
`RINGHUB_PUBLIC_URL` and `NTFY_PUBLIC_URL` in `.env`, update the ring app and your Shortcuts, and
in the ntfy app remove and re-add the server.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No notifications at all, no errors anywhere | The ntfy server URL in the iPhone app doesn't *exactly* match `NTFY_PUBLIC_URL`. Delete it in the app and re-add it, copied from `.env`. |
| Notifications say only "New message" | Apple's wake-up arrived, but the phone couldn't fetch the message: Tailscale was off on the phone, or the `iphone` user lacks read access (`docker compose exec ntfy ntfy access`). |
| Ring app shows 401 | `RINGHUB_SECRET` in the app doesn't match `.env`, or the phone's clock is off by more than 5 minutes. |
| Shortcut fails with "could not connect" | Tailscale on the phone was disconnected. Add Tailscale's **Connect** action first, and keep VPN On Demand on. |
| `https://…:8445` times out from the phone | Run `bash deploy/pi/preflight.sh`: both ports should show "already publishes". If your tailnet uses custom ACLs, allow the phone TCP to both ports. |
| The first request after setup takes a minute | Tailscale is issuing the HTTPS certificate. This happens once. |
| `ntfy` won't start: permission denied | `data/` or `ntfy/` was created by Docker as root: `sudo chown -R 1000:1000 data ntfy`. |
| `no matching manifest for linux/arm/v7` | The OS is 32-bit, even if `uname -m` says `aarch64`: `dpkg --print-architecture` must say `arm64`. Reinstall with Raspberry Pi OS (64-bit). |
| `manifest unknown` pulling the image | No image exists for that `RINGHUB_TAG`. `preflight.sh` says which tag it checked; use one from a green run in the repo's **Actions** tab. |
| `denied` pulling the image | The package was made private on GitHub (it's public by default for this repo). Org → **Packages** → **pebble-custom** → **Package settings** → **Change visibility** → **Public**. |
| Pull times out | Run `bash deploy/pi/preflight.sh`: its Registry section shows which download host this network blocks. |

**Privacy note:** the ntfy.sh relay only ever sees a hash of your topic URL and a message ID,
never the content. The Tailscale HTTPS certificate makes your machine name public in certificate
transparency logs, which is why the topic name is random.
