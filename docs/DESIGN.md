# ringhub v2 design: a memory aid that knows where you are

## Goals (from the owner, in priority order)

1. **Memory aid first.** Capturing notes and setting reminders by voice must be instant and
   reliable, and recall must be just as easy ("where did I park", "did I take my meds", "what's
   on my list").
2. **Context drives behaviour.** Location and proximity, reported by iOS Shortcuts automations
   and the car, choose what the button does. Changing modes by hand is the exception.
3. **A dashboard to build your own.** Create skills, put them in modes, and set when each mode
   applies, all without editing YAML.
4. **Simple and seamless.** One press does the obvious thing. Feedback is a short push, and
   nothing is lost.

TV control stays available as a skill template but is no longer what the product is about.

## Vocabulary

| Term | Meaning |
|---|---|
| **Gesture** | `click`, `double-click`, `triple-click` (instant) and `hold`, `click-hold` (voice). |
| **Skill** | Something the ring can do: "Mark this moment", "Hand raise", "Car: trunk or climate". Built from a template (a form) or as custom steps. Can have voice phrases. |
| **Mode** | A named set of gesture→skill bindings plus *when it applies* (places, Focus, time, days, context). The first matching mode in priority order is active. |
| **Place** | A named location (`home`, `work`, `car`, `gym`) with spoken aliases. The phone reports arriving at and leaving it. |
| **Context** | Live facts: `place`, `focus`, `location` and free-form keys, with a timeline. |

## The default gesture grammar (muscle memory)

These never change unless you rebind them:

- **hold** is *smart capture*. Just talk:
  - Commands ("hand raise", "warm up the car") run the skill whose phrase matches.
  - Questions ("where did I park", "what's on my list", "did I take my meds", "what did I say
    about the dentist") get an answer as a push.
  - Reminders ("remind me at 7 to…", "when I get home…", "when I leave work…") are scheduled.
  - **Everything else is saved as a note**, stamped with when and where it was *said*. If the
    ring delivers it late, the place is still resolved at the time it was recorded.
- **click-hold** is the *journal*: raw capture with audio and no interpretation.
- **click** and **double** are the active mode's two quick skills.
- **triple** says *where am I*: a push with the active mode, why it's active, and what click and
  double do. A second triple within 10 s cycles to the next mode as a manual override, which
  lasts until the place changes.

### Attention layer (seamless acknowledgement)

For 2 minutes after a reminder fires, **click = Done** and **double = Snooze 10 min**, whatever
the mode. The push shows the same two buttons, which use signed links so no admin token ever
reaches ntfy.

## Modes, resolved

```
manual override (until place changes, or for N minutes)
  ↓ else
first mode in priority order whose `when` matches the context
  ↓ else
the fallback mode "Everywhere" (always last, can't be deleted)
```

`when` conditions, all of which must hold (each list is any-of):

- `places`: you are at one of these places
- `not_places`
- `focus`: the iOS Focus is one of these (`sleep`, `work`, `driving`, …)
- `time`: `{after: "22:00", before: "06:00"}` (wraps midnight)
- `days`: `[mon, …]`
- `context`: `{key: value}` equality on free-form context
- `recently`: `{event: arrive|leave, place, within_minutes}`, for example "left home in the last
  15 minutes"

Gestures a mode doesn't bind fall through to **Everywhere**, so a mode only has to say what
differs. Each mode also sets a **feedback level**: `normal`, `quiet` (low-priority pushes, no
sound) or `silent` (failures only). Use `quiet` for driving and sleep.

## Smart car (locked state ≈ proximity)

With Phone Key and walk-up unlock, a Model Y **unlocks when you walk up to it and locks when you
walk away**, so `vehicle_state.locked == false` effectively means "I'm standing at the car". The
**Car: trunk or climate** skill:

| Car state (read on press, never wakes the car) | Action |
|---|---|
| asleep or offline | assume locked and away → **precondition** (this wakes it) |
| locked | **precondition** |
| unlocked, nobody inside | **open rear trunk** (configurable: frunk) |
| unlocked, user present or in gear | **nothing** (safety) and says so |
| unlocked, but the phone's fresh location is > 150 m away | **refuses**: walk-away lock is probably off |

State reads are cached for 30 s and only happen when a skill asks for them. The hub never polls
on a timer, which would keep the car awake and cost API credits.

**Parking memory for free:** when the phone reports *leave `car`* (car Bluetooth disconnects),
the hub saves "Parked" with the car's GPS position if Tesla is configured, or else the phone's
location. Asking "where did I park" pushes an Apple Maps link.

## Skills

A skill is `{id, name, icon, description, template, params, steps, phrases}`. The **template**
plus its **params** generate the steps server-side, so the dashboard edits a form while the engine
runs plain steps. The `custom` template exposes raw steps (JSON) for power users.

Built-in templates: `moment`, `note` (fixed text log, e.g. "Took meds ✓"), `list` (push open
reminders), `recall` (push recent notes), `notify` (push to an ntfy topic, used for hand raise
and bathroom requests), `webhook`, `car_smart`, `car_command`, `tv` (optional), `custom`.

Built-in skills that can't be deleted (they back the default grammar): `capture`, `journal`,
`where_am_i`, `nothing`.

Voice phrases use a simple pattern syntax, compiled to regex: `warm up the car`,
`tell {who} I'm running late`, `set focus timer for {minutes:number}`.

## Context feed (iOS Shortcuts)

One scoped **context token** (it can only write context), with endpoints that are easy to build
in Shortcuts:

```
POST /api/v1/context/arrive/{place}
POST /api/v1/context/leave/{place}
POST /api/v1/context/focus/{name}       (name "off" clears it)
POST /api/v1/context/visit/{place}      Wallet tap / NFC: fires "next time I'm at" reminders only
POST /api/v1/context                    {"set": {...}, "location": {"lat":..,"lon":..}}
POST /api/v1/owntracks                  OwnTracks HTTP mode (Basic auth, password = context token)
```

**Debounce, from the iOS research:** geofences fire about 200 m past the edge and jitter near
it. A leave within 4 minutes of arriving is ignored, and duplicate arrivals within 2 minutes
(Shortcuts plus OwnTracks plus Wi-Fi) collapse into one. A later re-arrival at the "same" place
still fires pending arrive reminders, because the leave may simply have been missed.

The dashboard's Places page generates the exact URL and header for each automation, with copy
buttons.

## Memory

- **Notes** are in SQLite with FTS5 full-text search (porter stemming). Kinds: note, journal,
  moment, log, reminder. Each note records time said, place, mode, audio path and provenance.
- **Reminders** have trigger `time`, `arrive`, `leave` or `none` (a list item), and status
  pending → fired → done/cancelled. Snooze re-arms a reminder as timed. If an unacknowledged
  reminder is ignored for 15 min it is pushed once more.
- **Daily digest** at a configured time: today's captures plus open reminders and list items.
- **Optional Markdown mirror** of notes into an Obsidian vault.

## Feedback, from the ntfy research

- Publish with ntfy's **JSON API**. HTTP headers must be ASCII and titles carry emoji.
- iOS maps priority 1–2 to passive (silent), 3 to active, and 4–5 to Time Sensitive.
  Confirmations use 2–3 (capped by the mode's feedback level), errors 3, reminders 4.
- The Pebble watch shows **title + first line** over ANCS and only offers *Dismiss*. So the
  reminder text goes in the title, and the ring itself is the acknowledgement path (attention
  layer).
- iOS only honors action buttons on the **newest** notification, and tapping one opens the ntfy
  app first. Done/Snooze buttons are a convenience; click/double on the ring is the primary path.
- Each reminder push uses `sequence_id = rem-<id>`. Done sends a `clear` (works on Android/web,
  harmless on iOS).

## Dashboard

A single-page app served by the hub at `/`, with no build step and no external CDN, so it works
offline on the LAN. Login uses the admin token and sets an HttpOnly, SameSite=Strict signed
session cookie. Mutations require JSON plus a CSRF header. It's mobile-first, since it's mostly
used from the phone.

Pages: **Now** (active mode and why, gesture map, context, next reminders, recent captures, test
buttons, override), **Modes** (priority order, conditions, bindings), **Skills** (library,
template forms, phrases, run now), **Memory** (notes search, reminders), **Places** (aliases,
generated Shortcut setups, context timeline), **Activity**.

## Config split

The YAML holds infrastructure only: secrets, ntfy, Tesla, TVs, LLM, timezone and digest time.
Skills, modes and places live in the database, are edited in the dashboard, are seeded with
sensible defaults on first boot, and can be exported or imported as JSON.
