# ringhub

**A memory aid you wear: your Pebble Index ring, made context-aware.**

Hold the ring and talk. ringhub saves the thought, schedules the reminder, or answers the
question, and knows where you were when you said it. Click it and it does the one obvious thing
for where you are: log your meds in the morning, open the trunk when you're standing at the car,
raise your hand at work. Build your own skills and modes in a dashboard on your phone.

```
 ring ─▶ Index app ─signed webhook─▶ ringhub ─▶ ntfy push (phone + Pebble watch)
                                       ▲  │
 iPhone Shortcuts / OwnTracks ─context─┘  ├─▶ notes · reminders · recall   (SQLite + full-text)
 (arrive/leave, car BT, Focus, NFC, Wallet)├─▶ Tesla (state-aware, never wakes to look)
                                          └─▶ anything with a webhook
```

## What it feels like

| You | ringhub |
|---|---|
| *hold:* "the wifi password at the cabin is lakeside42" | 📝 Saved, filed under where you were. |
| *hold:* "when I get home take the chicken out" | 📍 Fires the moment your phone says you're home. |
| *hold:* "remind me at 7 to call mom" | ⏰ 7:00 pm. When it fires: **click** = done, **double** = snooze. |
| *hold:* "what did I say about the cabin?" | 🔎 "the wifi password at the cabin is lakeside42 — Tue 3:12 pm" |
| *hold:* "what did I note at the office?" | 📍 The last few things you said there. |
| *hold:* "did I take my meds?" | ✅ "Yes — 'Took meds ✓' at 8:04 am" |
| *hold:* "where did I park?" | 🚗 Saved automatically when the car's Bluetooth disconnected, with a Maps link. |
| *click, at home before 10:30* | 💊 Took meds ✓ (Morning mode) |
| *click, just left home* | 🚗 Unlocked and you're at it → trunk opens. Locked or asleep → climate on. |
| *click, at work* | ✋ Hand raise to your ntfy topic. |
| *triple* | 🧭 "Morning — at home, 05:00–10:30 · click: Took meds · double: What's on my list". Triple again to switch. |
| *double, right after a mis-heard capture* (or say "scratch that") | ↩️ Undone. |

Nothing you say is ever dropped. If it isn't a command, question or reminder, it's a note.

## Ideas that make it seamless

- **The ring answers what just happened.** For 2 minutes after a reminder fires, click means
  *done* and double means *snooze*, in any mode. Right after a capture, double means *undo*.
- **Modes pick themselves.** Each mode says when it applies: places, iOS Focus, time window,
  days, "within 10 min of leaving home", or any context value. The first match wins, and anything
  a mode doesn't define falls through to *Everywhere*. Triple-click tells you where you are and
  what the buttons do.
- **Context is a timeline.** The ring can store a recording offline and deliver it later. It's
  still filed under the place where you *said* it.
- **Place reminders without geofences.** A Wallet tap or NFC tag counts as a *visit*, so "next
  time I'm at Target…" fires when you pay there.
- **The car's lock state is proximity.** With Phone Key, unlocked ≈ standing at the car. State is
  read only on a press, never by waking the car, and trunk-opening is refused if your phone is
  elsewhere.
- **Pushes are designed for the wrist.** The useful text goes in the title, which is what the
  Pebble watch shows. Priority follows iOS: confirmations are passive, reminders are Time
  Sensitive.

## The dashboard

Served by the hub at `/`. It's mobile-first, works offline on your LAN, and needs no build step
or CDN.

- **Now**: active mode and why, what each gesture does right now, "try it" buttons, upcoming
  reminders, recent captures, override.
- **Modes**: order by priority, write conditions in plain language, pick a skill per gesture.
- **Skills**: create from templates (mark the moment, log something, push to a topic, webhook,
  car, TV…) or write custom steps. Add voice phrases such as `tell {who} i'm running late`.
- **Memory**: search every note, manage reminders.
- **Setup**: places, copy-ready iOS Shortcuts / OwnTracks setup with your URLs and
  token.

## Get started

```bash
pip install -e '.[llm]'            # Python 3.11+, or: docker compose up -d --build
cp config.example.yaml config.yaml && cp .env.example .env    # fill in secrets
ringhub check && ringhub serve
```

Then follow [docs/SETUP.md](docs/SETUP.md): point the Index app's webhooks at the hub and add
two or three Shortcuts automations (arrive/leave home, car Bluetooth). It takes about 30
minutes. The [design doc](docs/DESIGN.md) explains the model, and
[docs/RECIPES.md](docs/RECIPES.md) has ideas worth copying.

## Under the hood

- **Ring protocol:** a byte-exact implementation of the Index webhook v1, from the app's
  open-source code. Every request is HMAC-signed, with a replay window, delivery dedup, and a
  timeline for late deliveries. See [protocol notes](docs/INDEX_WEBHOOK_PROTOCOL.md).
- **Memory:** SQLite with FTS5 (porter stemming). Notes record what, when said, where, mode,
  audio and provenance. Reminders go pending → fired → done, with one nag if ignored for 15 min,
  plus an evening digest and an optional Obsidian Markdown mirror.
- **Reminder parser:** deterministic and offline, with 30+ phrasings tested. An optional Claude
  fallback extracts only what, when and where into a fixed schema.
- **Safety:**
  - Admin, context and per-link credentials are separate.
  - Dashboard sessions are HttpOnly, SameSite=Strict, with a CSRF header and a strict CSP.
  - Transcripts are never templates, `exec` is off by default, and geofence input is debounced.
- **Ops:** `ringhub check`, `/healthz`, an activity log, catalog export/import, Docker (non-root,
  multi-arch), a hardened systemd unit, and CI running ruff, `mypy --strict` and pytest.
