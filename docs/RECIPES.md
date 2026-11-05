# Recipes

Everything in `config.example.yaml`, the reasoning behind it, and more niche setups you can paste
in. Every YAML block in this file is validated in CI (`tests/test_docs.py`), so they stay
correct as the code changes.

## Principles that make a one-button UI pleasant

1. **Feedback on every state change.** Changing modes pushes a cheat sheet of what each gesture
   now does. Pebble watches and phones show ntfy pushes, so you're never guessing.
2. **Silent where it matters.** Tally clicks (interruptions, water) make no noise. You get the
   summary later.
3. **Idempotent verbs.** `pause` pauses whatever is playing and leaves paused things alone, even
   on Roku, which only has a toggle key (ringhub checks the player state first). `power_on` uses
   Android's WAKEUP key, not POWER, so it never turns a TV *off*.
4. **One gesture, one meaning per mode.** Keep the vocabulary small and use voice for the long
   tail.
5. **Never lose a thought.** Unmatched voice goes to an inbox, and failures push the reason.

---

## TV & media

### "What did they say?" (tv mode, double-click)
Rewinds a few seconds, turns captions on, and turns them off again 20 s later. Android exposes a
captions key; on Roku the step is skipped and only the replay happens.

### Room-aware "here"
`target: here` resolves to the room you last targeted explicitly, then the TVs that are currently
on, then `default_target`. To pin it explicitly, for example from an NFC sticker by the bed
running `ringhub send` with a payload:

```yaml
rules:
  - name: nfc bedside sticker
    when: {gesture: click, payload: bedroom}
    do: [{here.set: bedroom}, {notify: "Ring now controls the bedroom"}]
```

### Pi downloader → TV in one sentence
*"Play the latest on the office TV."* `media.play_latest` asks your downloader for its newest
item, then Android/Fire TV get a `VIEW` intent (Kodi/VLC/MX Player pick it up) and Roku gets the
Media Assistant channel.

### App launcher with a fallback to search

```yaml
intents:
  - name: open app
    match: ['open (?P<app>youtube|netflix|plex)']
    do:
      - tv.launch: "{{ {'youtube': 'com.google.android.youtube.tv', 'netflix': 'com.netflix.ninja', 'plex': 'com.plexapp.android'}[app] }}"
```

---

## Voice inbox, journal, memory

### Never-lose-it inbox
Unmatched voice becomes `- [ ] <transcript>` in `inbox/YYYY-MM-DD.md`. Point `notes_dir` at an
Obsidian vault synced with Syncthing and it shows up in your task view.

### Parking spot / "where did I put it" memory

```yaml
intents:
  - name: remember location
    match: ['(?:i )?(?:parked|put (?:the |my )?(?P<thing>\w+)) (?P<where>(?:on|in|at|under) .+)']
    do:
      - state.set: {key: "where.{{ thing | default('car') }}", value: "{{ where }} ({{ now.strftime('%a %H:%M') }})"}
      - notify: {message: "Got it: {{ thing | default('car') }} {{ where }}", priority: 1}
  - name: recall location
    match: ['where (?:did i (?:park|put)|is) (?:the |my )?(?P<thing>\w+)?']
    do:
      - notify: "{{ state.get('where.' ~ (thing | default('car')), 'No idea, you never told me') }}"
```

### Baby log (two buttons, zero thinking at 3 a.m.)

```yaml
modes: [home, baby]
rules:
  - name: feed
    when: {gesture: click, mode: baby}
    do:
      - state.increment: feeds
      - state.set: {key: last_feed, value: "{{ now.strftime('%H:%M') }}"}
      - notes.append: {file: "baby/{{ now.strftime('%Y-%m-%d') }}.md", text: "- {{ now.strftime('%H:%M') }} feed"}
  - name: diaper
    when: {gesture: double, mode: baby}
    do:
      - state.increment: diapers
      - notes.append: {file: "baby/{{ now.strftime('%Y-%m-%d') }}.md", text: "- {{ now.strftime('%H:%M') }} diaper"}
intents:
  - name: last feed
    match: ['when was the last feed|last feed']
    do: [{notify: "Last feed {{ state.last_feed | default('never') }} · {{ state.feeds | default(0) }} today"}]
```

---

## Work & focus

### Interruption-counting focus block (focus mode)
Double starts 25 minutes, each click silently logs an interruption, and the closing push reports
the count. It's useful data for a conversation with your manager.

### Covert meeting signals
Click while in a meeting, without opening anything:

```yaml
modes: [home, meeting]
rules:
  - name: running late
    when: {gesture: click, mode: meeting}
    do: [{notify: {topic: team-status, message: "Running ~5 min late, start without me", priority: 3}}]
  - name: rescue call
    when: {gesture: double, mode: meeting}
    do:
      - schedule: {after: 45s, do: [{notify: {message: "📞 Urgent: call back now", priority: 5, tags: [telephone]}}]}
```

### Slide clicker for a laptop
Run a tiny HTTP agent on the presenting machine (or a Stream-Pi action server) and:

```yaml
modes: [home, present]
rules:
  - name: next slide
    when: {gesture: click, mode: present}
    do: [{http: {url: "http://laptop.local:9000/key/Right", method: POST, timeout: 2}}]
  - name: previous slide
    when: {gesture: double, mode: present}
    do: [{http: {url: "http://laptop.local:9000/key/Left", method: POST, timeout: 2}}]
```

---

## Car

### Leave-in-N (voice)
*"I'm leaving in 25."* The climate starts at T−10 so the cabin is ready when you get there, and
every TV pauses at T−0. Say it again to reschedule; the job ids are fixed, so nothing stacks.

### Frunk for groceries
*"Open the frunk."* That's `actuate_trunk` with `which_trunk: front`. Pair it with a time window
so it only works when you're likely to be at the car:

```yaml
rules:
  - name: frunk on triple at the car
    when: {gesture: triple, state: {at_car: true}}
    do: [{tesla: {command: actuate_trunk, params: {which_trunk: front}}}]
```

`at_car` can be set by a phone automation (iOS Shortcuts / Tasker "connected to car Bluetooth")
calling `ringhub send` or `/api/simulate` with a payload rule that runs `state.set`.

---

## Household

### Hand raise & bathroom requests
You already do these through ntfy. Target the topics your existing subscribers listen to:

```yaml
rules:
  - name: bathroom request
    when: {gesture: double, mode: home, time: {after: "08:00", before: "15:30"}, days: [mon, tue, wed, thu, fri]}
    do: [{notify: {topic: bathroom-requests, message: "🚻 Request at {{ now.strftime('%H:%M') }}", priority: 4}}]
```

### Guest mode: disable the risky stuff
Rules are first-match, so put this at the top:

```yaml
rules:
  - name: guest lockout
    when: {gesture: [triple, click-hold], state: {guest: true}}
    do: [noop]
intents:
  - name: guest mode
    match: ['guest mode (?P<on>on|off)']
    do: [{state.set: {key: guest, value: "{{ on == 'on' }}"}}, {notify: "Guest mode {{ on }}"}]
```

### Quiet hours
Between 23:00 and 06:00 a click only pauses, with no pushes and nothing else:

```yaml
rules:
  - name: night click
    when: {gesture: click, time: {after: "23:00", before: "06:00"}}
    do: [{tv: {command: pause, target: all}}]
```

---

## Glue

- **Home Assistant:** `http` to an HA webhook trigger (`http://ha.local:8123/api/webhook/<id>`)
  gives the ring access to every HA entity without ringhub knowing about HA.
- **Other buttons → same brain:** `ringhub send click --payload desk-button --secret …` from a
  Stream Deck, Flic, cron job or NFC Shortcut, matched with `when: {payload: desk-button}`.
- **Your own action:** add a function with `@action("name", ParamsModel)` in
  `src/ringhub/actions/`. It's validated at load time and documented by its pydantic model.
