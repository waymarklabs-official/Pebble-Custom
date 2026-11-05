# Recipes

Setups worth copying. Each one can be built in the dashboard. The JSON blocks are what the
dashboard saves (**Skills → Custom** or a mode's editor) and are validated in CI by
`tests/test_docs.py`.

## Memory

### "Did I…?": verification logs
The classic memory-aid problem is not remembering *whether* you did something. Give the
routine actions a one-click log, then ask later.

- Skill **Took meds ✓** (template *Log something*), bound to **click** in **Morning** (home,
  05:00–10:30).
- Skill **Locked up ✓**, bound to **double** in **Heading out** (10 min after leaving home).
- Later: *hold*, "did I take my meds?" → ✅ "Yes — 'Took meds ✓' at 8:04 am", or "No record
  today".

### Place reminders that don't need geofences
Say *"next time I'm at Target buy batteries"*. Then add a Wallet automation in Shortcuts:
**Transaction → Merchant is Target → Get Contents of URL**
`POST /api/v1/context/visit/target`. Paying there fires the reminder. Add "target" as a place
(or as an alias) so the hub recognises the name.

### Parking memory with zero presses
Shortcuts automations for car Bluetooth connect and disconnect (arrive/leave `car`) are enough.
When the car disconnects, the hub saves a "Parked" log with the car's GPS position (Tesla) or the
phone's last fix. *"Where did I park?"* pushes an Apple Maps link.

### Nothing is ever lost
Mis-heard capture? **Double within 30 s** undoes it. Unsure what you said? *"What did I say about
the dentist"* searches everything, with stemming ("dentists", "dental").

## Modes that pick themselves

### The default day
**Morning** (home, 05:00–10:30) → **Home** → **Heading out** (left home in the last 10 min) →
**In the car** (car Bluetooth) → **Work** (at work) → **Sleep** (Sleep Focus). The order is the
priority, so Morning wins over Home before 10:30.

### Focus-driven work mode
iOS Focus automations run silently. Turn on *Work Focus* → `POST /api/v1/context/focus/work`.
A mode with **Focus is work** binds click to a status push and double to "what's next":

```json
{"id": "deep_work", "name": "Deep work", "icon": "🎧", "feedback": "quiet",
 "when": {"focus": ["work"]},
 "bindings": {"click": "moment", "double-click": "list"}}
```

### Weekend chores, weekday commute
The same place can mean different things on different days:

```json
{"id": "weekend_home", "name": "Weekend at home", "icon": "🧺",
 "when": {"places": ["home"], "days": ["sat", "sun"]},
 "bindings": {"click": "moment"}}
```

### Guests over: quiet everything
Set a free-form context value from a Shortcut (`POST /api/v1/context` with
`{"set": {"guests": true}}`) and let a mode match it:

```json
{"id": "guests", "name": "Guests over", "icon": "🫖", "feedback": "silent",
 "when": {"context": {"guests": true}},
 "bindings": {"click": "moment", "double-click": "nothing"}}
```

## Car

### One button, two meanings
**Car: trunk or climate**: unlocked → trunk (you're at the car with your hands full); locked or
asleep → climate on. Bound by default to click in **Heading out** and **Home**, and to double in
**Morning**. Warming the car from bed is one double-click.

### Say it instead
Voice phrases on the built-in car skills: "warm up the car", "open the frunk". Add your own:

```json
{"id": "find_car", "name": "Flash the lights", "icon": "💡", "template": "car_command",
 "params": {"command": "flash_lights"},
 "phrases": ["flash the lights", "where's my car in this lot"]}
```

## Work & household

### Hand raise and bathroom requests
Your existing ntfy flows, as skills with the *Send a push* template, targeting the topics your
subscribers already listen to. Bound to click/double in **Work** by default. Say "hand raise"
from any mode.

### Running late, from any mode

```json
{"id": "late", "name": "Running late", "icon": "⏱️", "template": "notify",
 "params": {"topic": "team-status", "message": "Running ~10 min late, start without me", "priority": 3},
 "phrases": ["tell them i'm running late", "running late"]}
```

### Home Assistant, IFTTT, your Pi
A *Call a webhook* skill reaches anything. For example, a Home Assistant webhook trigger:

```json
{"id": "goodnight", "name": "Goodnight", "icon": "🌙", "template": "webhook",
 "params": {"url": "http://homeassistant.local:8123/api/webhook/ring-goodnight", "method": "POST"},
 "phrases": ["good night", "goodnight"]}
```

### Composite skills
Custom steps chain anything, using the context in templates:

```json
{"id": "leaving_work", "name": "Leaving work", "icon": "🏃", "template": "custom",
 "steps": [
   {"memory.log": {"text": "Left work"}},
   {"notify": {"topic": "family", "message": "Leaving work now — home in ~25 min"}},
   {"skill.run": "list"}
 ],
 "phrases": ["heading home"]}
```

## Other buttons, same brain

`ringhub send click --secret …` sends exactly what the ring would. A Stream Deck key, a Flic, a
cron job or an NFC Shortcut can drive the same modes and skills.
