# Running ringhub on your Pi, with its own ntfy

This puts ringhub and a **private ntfy server just for the ring** on a Raspberry Pi that already
runs other things on your tailnet (like Tstream). Nothing about the existing apps changes.

```
iPhone (Tailscale on)                         Raspberry Pi
 ├─ Index app ─ webhooks ─┐
 ├─ Shortcuts ─ context ──┼─▶ https://pi.<tailnet>.ts.net:8443 ─▶ ringhub  127.0.0.1:8787
 ├─ dashboard (Safari) ───┘                                          │ publishes
 └─ ntfy app ◀── fetch ─────── https://pi.<tailnet>.ts.net:8444 ─▶ ntfy  127.0.0.1:2586
          ▲                                                          │ "wake up" (no content)
          └──── Apple push ◀── ntfy.sh relay ◀───────────────────────┘
                              https://pi.<tailnet>.ts.net:443  ─▶ Tstream (untouched)
```

**Resources:** ringhub idles at about 46 MB RAM and 0.2% CPU (measured). ntfy is similarly small.
Traffic is a few KB per press, or under 50 KB for a voice note with audio, so it won't compete
with streaming. If you outgrow the Pi, see [Moving to a mini PC](#moving-to-a-mini-pc).

> ⚠️ **Two commands that would break Tstream:** never run `tailscale serve reset`, and never run a
> bare `tailscale serve off` (with no port, it removes everything on 443). To remove something of
> ringhub's, always name its port: `sudo tailscale serve --https=8443 off`.

---

## 1. Check the Pi (changes nothing)

```bash
git clone https://github.com/waymarklabs-official/Pebble-Custom.git ~/ringhub && cd ~/ringhub
git checkout claude/pebble-ring-automation-kqoqn3
bash deploy/pi/preflight.sh
```

It prints your machine's Tailscale name (e.g. `pi.tail1234.ts.net`) and confirms that ports 8443
and 8444 are free on the tailnet. If Tstream already uses one of them, pick two others and pass
them to every step below: `bash deploy/pi/preflight.sh 8445 8446`.

In the Tailscale admin console (**DNS**), make sure **MagicDNS** and **HTTPS Certificates** are
on. They probably already are if Tstream uses `https://…ts.net`.

## 2. Build and configure

```bash
mkdir -p data ntfy/cache ntfy/lib            # must exist before the first start (else Docker makes them root-owned)
docker build -t ringhub:latest .             # 3-5 min on a Pi
docker run --rm ringhub:latest env --host pi.tail1234.ts.net --tz America/New_York > .env
chmod 600 .env
cp config.example.yaml config.yaml
```

Replace `pi.tail1234.ts.net` with the name from step 1. `.env` now holds fresh secrets, a random
ntfy topic and both URLs. The defaults in `config.yaml` are fine.

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
server denies everything by default.

## 4. Start ringhub and publish both to your tailnet

```bash
docker compose up -d
docker compose exec ringhub ringhub check -c /app/config.yaml        # everything should say "on"
sudo bash deploy/pi/serve.sh                                         # adds :8443 and :8444 only
```

`serve.sh` shows your Tailscale mappings before and after, so you can see Tstream's entry is
unchanged. The mappings survive reboots, and the containers restart on their own.

## 5. iPhone

1. **Tailscale app → your profile → VPN On Demand:** set it to always connect, on Wi-Fi and
   cellular. The ring, Shortcuts and ntfy reach the Pi through Tailscale, so it must be on.
2. **ntfy app → Settings → add a server:** type the `NTFY_PUBLIC_URL` from `.env` *exactly*
   (`https://pi.tail1234.ts.net:8444`, no trailing slash). Add the user `iphone` with its password.
   Then subscribe to the `NTFY_TOPIC` from `.env` on that server. Allow **Time Sensitive**
   notifications for ntfy (iOS Settings → Notifications → ntfy).
3. **Dashboard:** open `RINGHUB_PUBLIC_URL` (`https://pi.tail1234.ts.net:8443`) in Safari, sign in
   with `RINGHUB_ADMIN_TOKEN`, and add it to your Home Screen.

## 6. The ring

Index app → **Settings → Webhook**. For each gesture:
- URL: `https://pi.tail1234.ts.net:8443/ring`
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

## Everyday operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f ringhub` (or `ntfy`) |
| Update | `git pull && docker build -t ringhub:latest . && docker compose up -d` |
| Back up | Copy `~/ringhub/data`, `~/ringhub/ntfy`, `.env` and `config.yaml` (or Dashboard → Setup → Backup for modes/skills) |
| Stop serving | `sudo tailscale serve --https=8443 off` and `sudo tailscale serve --https=8444 off` |

## Moving to a mini PC

Everything lives in `~/ringhub`. Stop the containers, copy the whole folder (including `data/`,
`ntfy/` and `.env`), install Docker and Tailscale on the new machine, then run
`docker compose up -d` and `sudo bash deploy/pi/serve.sh` there.

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
| `https://…:8443` times out from the phone | `sudo tailscale serve status` should list 8443. If your tailnet uses custom ACLs, allow the phone TCP 8443 and 8444. |
| The first request after setup takes a minute | Tailscale is issuing the HTTPS certificate. This happens once. |
| `ntfy` won't start: permission denied | `data/` or `ntfy/` was created by Docker as root: `sudo chown -R 1000:1000 data ntfy`. |

**Privacy note:** the ntfy.sh relay only ever sees a hash of your topic URL and a message ID,
never the content. The Tailscale HTTPS certificate makes your machine name public in certificate
transparency logs, which is why the topic name is random.
