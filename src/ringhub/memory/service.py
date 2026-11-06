"""The memory aid: smart capture, recall, and reminder delivery (time, place, nag, digest)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..actions.base import ActionError
from ..intents import normalize
from .calendar import agenda_window, describe_when, format_agenda, is_agenda_question, parse_event
from .notes import Note
from .parse import ParsedReminder, parse_reminder
from .reminders import Reminder

if TYPE_CHECKING:
    from ..context import PlaceEvent
    from ..runtime import Runtime

log = logging.getLogger(__name__)

ATTENTION_REMINDER = "attention.reminder"
ATTENTION_CAPTURE = "attention.capture"
CAPTURE_UNDO_SECONDS = 30
NAG_AFTER = timedelta(minutes=15)
SNOOZE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    kind: str  # "note" | "reminder" | "answer" | "skill" | "empty" | "calendar"
    message: str
    note: Note | None = None
    reminder: Reminder | None = None


# Built-in questions, matched against the WHOLE utterance (after filler) so statements that merely
# contain "where I parked" are saved as notes, not answered. Checked after user skill phrases.
_FILLER = re.compile(r"^(?:(?:um|uh|hey|ok|okay|so|ring|please)\s+)+|(?:\s+(?:please|again|now))+$")
_Q_PARKED = re.compile(r"^(?:where (?:did|have) i (?:park|parked)(?: the car| my car)?(?: today)?|"
                       r"where(?: is|'s) (?:the |my )?car|where (?:did|have) i (?:leave|left) (?:the |my )?car)$")
_Q_WHERE = re.compile(r"^where (?:did|have) i (?:leave|left|put) (?:the |my )?(?P<q>.+)$|"
                      r"^where(?: are| is|'s|'re) (?:the |my )?(?P<q2>.+)$")
_Q_LIST = re.compile(r"^(?:what(?:'s| is) on my (?:list|plate)(?: today)?|what are my reminders|"
                     r"what do i (?:have|need) to do(?: today)?|(?:my )?(?:list|reminders|to ?do(?: list)?))$")
_Q_NEXT = re.compile(r"^(?:what(?:'s| is) next|(?:what(?:'s| is) )?(?:my )?next reminder)$")
_Q_DID = re.compile(r"^(?:did|have) i (?:already )?(?P<what>.+?)(?: today| yet| already)?$")
_Q_ABOUT = re.compile(r"^(?:what did i (?:say|note|write|record) (?:about|on|regarding)|"
                      r"find (?:my )?(?:notes? )?(?:about|on|for)|search (?:my notes )?for|"
                      r"what was (?:the|that) (?:thing|idea|note) (?:about|for))\s+(?P<q>.+)$")
_Q_AT = re.compile(r"^what did i (?:say|note|save|write|capture|record|jot down)(?: while i was)? (?:at|in) "
                   r"(?:the |my )?(?P<place>[\w' ]+?)(?: today| yesterday| recently)?$")
_Q_LAST = re.compile(r"^(?:what (?:was|were) my last|read (?:me )?my (?:last|latest|recent))"
                     r"(?: (?P<n>\d+))?(?: (?:notes?|thoughts?|captures?))?$")
_UNDO = re.compile(r"^(?:never ?mind|cancel(?: that)?|scratch that|undo(?: that)?|forget (?:it|that)|delete that)$")
_NOISE = re.compile(r"^(?:ok|okay|um+|uh+|hmm+|hm|test(?:ing)?|hello|hi|yes|no|nothing|thanks|thank you)$")
LATE_REFERENCE_LIMIT = 7 * 86400
"""Voice can arrive late (ring offline); trust its recorded time up to a week back."""


def _clip(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"



def _push_id(r: Reminder, *, nag: bool) -> str:
    """The ntfy sequence ID of one reminder alert.

    Every alert needs its own: ntfy clients *replace* a notification that has the same sequence ID
    instead of alerting again, so a snoozed reminder or a nag that reused the first alert's ID
    arrived silently.
    """
    return f"rem-{r.id}-{r.snoozes}" + ("-nag" if nag else "")


def _round_push_ids(r: Reminder, snoozes: int) -> list[str]:
    """Every alert that can be showing for snooze round `snoozes`, plus the old single ID."""
    at = replace(r, snoozes=snoozes)
    return [_push_id(at, nag=False), _push_id(at, nag=True), f"rem-{r.id}"]


class Memory:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    # ------------------------------------------------------------------ helpers

    def _when(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, self.rt.config.tz)
        now = self.rt.now()
        clock = dt.strftime("%-I:%M %p").lower()
        if dt.date() == now.date():
            return clock
        if dt.date() == (now - timedelta(days=1)).date():
            return f"yesterday {clock}"
        if abs((dt.date() - now.date()).days) < 7:
            return f"{dt:%a} {clock}"
        return f"{dt:%b %-d} {clock}"

    async def place_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for place in await self.rt.catalog.places():
            for spoken in place.spoken():
                aliases[spoken] = place.id
        return aliases

    async def place_label(self, place_id: str | None) -> str:
        if not place_id:
            return ""
        place = await self.rt.catalog.place(place_id)
        return place.name if place else place_id.replace("_", " ")

    async def set_attention(self, kind: str, ref: Any) -> None:
        """Remember what the ring is "about" right now: a just-fired reminder, or a just-saved capture.

        Separate slots, so saving a note never steals click=Done from a reminder that just fired.
        """
        key = ATTENTION_REMINDER if kind == "reminder" else ATTENTION_CAPTURE
        await self.rt.store.set(key, {"kind": kind, "ref": ref, "at": time.time()})

    async def attention_slots(self) -> dict[str, dict[str, Any]]:
        window = self.rt.config.memory.ack_window_seconds
        if window <= 0:
            return {}
        out: dict[str, dict[str, Any]] = {}
        now = time.time()
        for kind, key, limit in (("reminder", ATTENTION_REMINDER, window),
                                 ("capture", ATTENTION_CAPTURE, min(window, CAPTURE_UNDO_SECONDS))):
            att = await self.rt.store.get(key)
            if isinstance(att, dict) and now - float(att.get("at", 0)) <= limit:
                out[kind] = att
        return out

    async def attention(self) -> dict[str, Any] | None:
        """The most recent active attention item (for display)."""
        slots = await self.attention_slots()
        return max(slots.values(), key=lambda a: float(a.get("at", 0))) if slots else None

    async def clear_attention(self, kind: str | None = None, ref: Any = None) -> None:
        """Clear one slot (only if it still refers to ``ref``, when given) or both."""
        for k, key in (("reminder", ATTENTION_REMINDER), ("capture", ATTENTION_CAPTURE)):
            if kind not in (None, k):
                continue
            if ref is not None:
                att = await self.rt.store.get(key)
                if not isinstance(att, dict) or att.get("ref") != ref:
                    continue
            await self.rt.store.delete(key)

    # ------------------------------------------------------------------ capture

    async def capture(self, transcript: str, *, said_at: float, place: str | None, mode: str | None,
                      audio: str | None = None, source: str | None = None) -> CaptureResult:
        """Smart capture: question -> reminder -> note. (Skill phrases are tried by the caller.)"""
        text = transcript.strip()
        if not text and audio:
            # "Recording only" payload mode (or STT failed): keep the audio as a voice memo.
            note = await self.rt.notes.add("(voice memo)", kind="note", created_at=said_at, place=place,
                                           mode=mode, audio=audio, source=source)
            await self.mirror(note)
            return CaptureResult("note", "🎙️ Voice memo saved (no transcript)", note=note)
        if not text:
            return CaptureResult("empty", "🤔 I didn't catch anything — nothing saved.")
        spoken = _FILLER.sub("", normalize(text)).strip(" .!?")
        if _UNDO.match(spoken):
            undo = (await self.attention_slots()).get("capture")
            if undo and isinstance(undo.get("ref"), dict):
                return CaptureResult("answer", await self.undo_last_capture(undo["ref"]))
            return CaptureResult("answer", "🤷 Nothing recent to undo.")
        if not spoken or _NOISE.match(spoken):
            # An accidental hold ("ok", "um", "test") shouldn't litter your notes.
            return CaptureResult("empty", "👍 Nothing saved.")
        try:
            answer = await self.answer(text)
            if answer is not None:
                return CaptureResult("answer", answer)
            # Resolve "at 7" / "tomorrow" against when it was SAID: the ring may deliver late.
            current = time.time()
            ref = said_at if current - LATE_REFERENCE_LIMIT <= said_at <= current + 300 else current
            now = datetime.fromtimestamp(ref, self.rt.config.tz)
            if self.rt.gcal is not None:
                event = await self._calendar_capture(text, now, said_at=said_at, place=place, mode=mode,
                                                     audio=audio, source=source)
                if event is not None:
                    return event
            parsed = parse_reminder(text, now, await self.place_aliases())
            if parsed is None and self.rt.llm is not None:
                parsed = await self.rt.llm.extract_reminder(text, now, await self.place_aliases())
            if parsed is not None:
                return await self._create_reminder(parsed, text, said_at=said_at, place=place, mode=mode,
                                                   audio=audio, source=source)
        except Exception:  # parsing must never cost the user their words
            log.exception("capture interpretation failed; saving as a plain note")

        note = await self.rt.notes.add(text, kind="note", created_at=said_at, place=place, mode=mode,
                                       audio=audio, source=source)
        await self.mirror(note)
        await self.set_attention("capture", {"note": note.id})
        where = f" · {await self.place_label(place)}" if place else ""
        return CaptureResult("note", f"📝 {_clip(text)}{where}", note=note)

    async def _calendar_capture(self, text: str, now: datetime, *, said_at: float, place: str | None,
                                mode: str | None, audio: str | None, source: str | None) -> CaptureResult | None:
        """"add dentist to my calendar Friday at 3" -> a Google Calendar event (None: not a calendar request)."""
        assert self.rt.gcal is not None
        ev = parse_event(text, now, self.rt.gcal.config.default_minutes)
        if ev is None:
            return None

        async def keep_as_note(why: str) -> CaptureResult:
            note = await self.rt.notes.add(text, kind="note", created_at=said_at, place=place, mode=mode,
                                           audio=audio, source=source)
            await self.mirror(note)
            await self.set_attention("capture", {"note": note.id})
            return CaptureResult("note", f"📅 {why} — saved as a note: {_clip(text, 60)}", note=note)

        if ev.start is None or ev.end is None:
            return await keep_as_note("No date heard (say e.g. “add dentist to my calendar Friday at 3”)")
        if not await self.rt.gcal.connected():
            return await keep_as_note("Google Calendar isn't connected (Setup → Integrations)")
        try:
            event = await self.rt.gcal.add_event(ev.title, ev.start, ev.end,
                                                 description=f"Added by ringhub from: “{text}”")
        except ActionError as e:
            log.warning("calendar add failed: %s", e)
            return await keep_as_note(f"Couldn't add it to your calendar ({e})")
        await self.set_attention("capture", {"calendar": event["id"], "title": ev.title})
        return CaptureResult("calendar", f"📅 {_clip(ev.title, 60)} · {describe_when(ev, now)}")

    async def agenda(self, kind: str, day: str) -> str:
        assert self.rt.gcal is not None
        now = self.rt.now()
        start, end, label = agenda_window(kind, day, now)
        try:
            events = await self.rt.gcal.events(start, end, limit=1 if kind == "next" else 10)
        except ActionError as e:
            return f"📅 {e}"
        return format_agenda(events, label, now)

    async def _create_reminder(self, parsed: ParsedReminder, original: str, *, said_at: float,
                               place: str | None, mode: str | None, audio: str | None,
                               source: str | None) -> CaptureResult:
        note = await self.rt.notes.add(original, kind="reminder", created_at=said_at, place=place, mode=mode,
                                       audio=audio, source=source)
        trig = parsed.trigger
        due = trig.at.timestamp() if trig.at else None
        reminder = await self.rt.reminders.add(parsed.text, trig.kind, due_at=due, place=trig.place,
                                               note_id=note.id, created_at=said_at)
        if trig.kind == "time":
            await self.schedule(reminder)
            if due is not None and due < time.time():
                message = f"⏰ Overdue (said {self._when(said_at)}) · {_clip(parsed.text)}"
            else:
                message = f"⏰ {self._when(due or time.time())} · {_clip(parsed.text)}"
        elif trig.kind in ("arrive", "leave"):
            label = await self.place_label(trig.place)
            verb = "arrive at" if trig.kind == "arrive" else "leave"
            message = f"📍 When you {verb} {label} · {_clip(parsed.text)}"
            if not trig.place_known:
                message += f"\n(I don't know “{label}” yet — add it under Places, or say it differently.)"
        else:
            message = f"📋 On your list · {_clip(parsed.text)}"
        await self.mirror(note, text=message.split("\n")[0].split(" ", 1)[1])
        await self.set_attention("capture", {"note": note.id, "reminder": reminder.id})
        return CaptureResult("reminder", message, note=note, reminder=reminder)

    async def undo_last_capture(self, ref: dict[str, Any]) -> str:
        parts = []
        if ref.get("calendar") and self.rt.gcal is not None:
            try:
                await self.rt.gcal.delete_event(str(ref["calendar"]))
                parts.append("calendar event")
            except ActionError as e:
                await self.clear_attention("capture")
                return f"⚠️ Couldn't remove “{_clip(str(ref.get('title') or 'the event'), 40)}” from your calendar: {e}"
        if ref.get("reminder"):
            await self.cancel(str(ref["reminder"]))
            parts.append("reminder")
        if ref.get("note"):
            note = await self.rt.notes.get(int(ref["note"]))
            if note and await self.rt.notes.delete(note.id):
                parts.append("note")
        await self.clear_attention("capture")
        return f"↩️ Undone ({' + '.join(parts) or 'nothing to undo'})"

    async def journal(self, transcript: str, *, said_at: float, place: str | None, mode: str | None,
                      audio: str | None, source: str | None) -> CaptureResult:
        text = transcript.strip() or "(audio only)"
        note = await self.rt.notes.add(text, kind="journal", created_at=said_at, place=place, mode=mode,
                                       audio=audio, source=source)
        await self.mirror(note)
        await self.set_attention("capture", {"note": note.id})
        return CaptureResult("note", f"📓 Journaled · {_clip(text, 60)}", note=note)

    async def moment(self, label: str | None, *, at: float, place: str | None, mode: str | None) -> Note:
        loc = await self.rt.context.get("location")
        meta: dict[str, Any] = {}
        if isinstance(loc, dict) and time.time() - float(loc.get("at", 0)) < 600:
            meta["location"] = {k: loc[k] for k in ("lat", "lon") if k in loc}
        where = await self.place_label(place)
        text = label or f"Moment{' at ' + where if where else ''}"
        note = await self.rt.notes.add(text, kind="moment", created_at=at, place=place, mode=mode, meta=meta)
        await self.mirror(note)
        return note

    async def log(self, text: str, *, at: float, place: str | None, mode: str | None) -> Note:
        note = await self.rt.notes.add(text, kind="log", created_at=at, place=place, mode=mode)
        await self.mirror(note)
        return note

    # ------------------------------------------------------------------ recall

    async def answer(self, text: str) -> str | None:
        if self.rt.gcal is not None and (agenda := is_agenda_question(text)) is not None:
            return await self.agenda(*agenda)
        q = _FILLER.sub("", normalize(text)).strip()
        if _Q_PARKED.match(q):
            return await self.where_parked()
        if _Q_LIST.match(q):
            return await self.list_summary()
        if _Q_NEXT.match(q):
            return await self.next_summary()
        m = _Q_ABOUT.match(q)
        if m:
            return await self.search_summary(m.group("q"))
        m = _Q_AT.match(q)
        if m:
            return await self.place_summary(m.group("place"))
        m = _Q_LAST.match(q)
        if m:
            return await self.recent_summary(int(m.group("n") or 3))
        m = _Q_WHERE.match(q)
        if m:
            return await self.search_summary(m.group("q") or m.group("q2"))
        m = _Q_DID.match(q)
        if m:
            return await self.did_i(m.group("what"))
        return None

    async def where_parked(self) -> str:
        """The automatic "Parked" log (car Bluetooth disconnect) and/or what you *said* about
        parking in the last day, most recent first; with a Maps link when a position is known."""
        logged = await self.rt.notes.latest_matching("log", "Parked")
        recent = await self.rt.notes.search("parked park parking", 5, since=time.time() - 86400,
                                            exclude_kinds=["log", "reminder"])
        said = recent[0] if recent else None
        if logged is None and said is None:
            return "🚗 I don't have a parking spot saved yet. Hold and say where you parked next time."
        link = ""
        if logged is not None:
            loc = logged.meta.get("location") or {}
            if isinstance(loc.get("lat"), int | float) and isinstance(loc.get("lon"), int | float):
                link = f"\nhttps://maps.apple.com/?q=Car&ll={float(loc['lat'])!r},{float(loc['lon'])!r}"
        if said is not None and (logged is None or said.created_at >= logged.created_at - 600):
            return f"🚗 You said: “{_clip(said.text, 90)}” · {self._when(said.created_at)}{link}"
        assert logged is not None
        where = f" at {await self.place_label(logged.place)}" if logged.place else ""
        return f"🚗 {logged.text}{where} · {self._when(logged.created_at)}{link}"

    async def list_summary(self, limit: int = 8) -> str:
        items = await self.rt.reminders.open()
        if not items:
            return "✅ Nothing on your list."
        lines = []
        for r in items[:limit]:
            if r.trigger == "time" and r.due_at:
                when = self._when(r.due_at)
            elif r.trigger in ("arrive", "leave"):
                when = f"{'@' if r.trigger == 'arrive' else 'leaving'} {await self.place_label(r.place)}"
            else:
                when = "list"
            lines.append(f"• {_clip(r.text, 60)} — {when}")
        more = f"\n…and {len(items) - limit} more" if len(items) > limit else ""
        return "📋 " + "\n".join(lines) + more

    async def next_summary(self) -> str:
        timed = [r for r in await self.rt.reminders.open(trigger="time") if r.due_at]
        if not timed:
            return "✅ No timed reminders coming up."
        r = min(timed, key=lambda x: x.due_at or 0)
        return f"⏰ Next: {_clip(r.text)} · {self._when(r.due_at or 0)}"

    async def search_summary(self, query: str, limit: int = 3) -> str:
        hits = await self.rt.notes.search(query, limit)
        if not hits:
            return f"🔎 Nothing found for “{_clip(query, 40)}”."
        lines = [f"• {_clip(n.text, 70)} — {self._when(n.created_at)}" for n in hits]
        return "🔎 " + "\n".join(lines)

    async def place_summary(self, spoken: str, limit: int = 3) -> str:
        """"What did I note at the gym?" -> the latest notes said there."""
        aliases = await self.place_aliases()
        key = spoken.strip().removeprefix("the ").removeprefix("my ")
        place = aliases.get(key) or aliases.get(spoken.strip())
        if place is None:
            return f"📍 I don't know a place called “{_clip(spoken, 30)}”."
        notes = await self.rt.notes.recent(limit, kinds=["note", "journal", "moment", "reminder"], place=place)
        label = await self.place_label(place)
        if not notes:
            return f"📍 Nothing saved at {label} yet."
        return f"📍 At {label}:\n" + "\n".join(f"• {_clip(n.text, 70)} — {self._when(n.created_at)}" for n in notes)

    async def recent_summary(self, count: int = 3) -> str:
        notes = await self.rt.notes.recent(max(1, min(count, 10)), kinds=["note", "journal", "moment", "reminder"])
        if not notes:
            return "📝 No notes yet."
        return "📝 " + "\n".join(f"• {_clip(n.text, 70)} — {self._when(n.created_at)}" for n in notes)

    async def did_i(self, what: str) -> str:
        """Yes only when every meaningful word matches something logged today."""
        start = self.rt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        kw = {"since": start, "exclude_kinds": ["reminder"]}
        hits = await self.rt.notes.search(what, 1, all_terms=True, **kw)  # type: ignore[arg-type]
        if hits:
            n = hits[0]
            return f"✅ Yes — “{_clip(n.text, 50)}” at {self._when(n.created_at)}"
        close = await self.rt.notes.search(what, 1, **kw)  # type: ignore[arg-type]
        if close:
            n = close[0]
            return f"🤔 Not sure. Closest today: “{_clip(n.text, 50)}” at {self._when(n.created_at)}"
        return f"🤷 No record of “{_clip(what, 40)}” today."

    # ------------------------------------------------------------------ reminders

    async def schedule(self, r: Reminder) -> None:
        if r.trigger != "time" or r.due_at is None:
            return
        delay = timedelta(seconds=max(0.0, r.due_at - time.time()))
        await self.rt.scheduler.schedule([{"reminder.fire": {"id": r.id}}], delay, {}, f"reminder:{r.id}")

    async def fire(self, rid: str) -> Reminder | None:
        r = await self.rt.reminders.mark_fired(rid)
        if r is None:
            return None  # already fired/done/cancelled: idempotent
        # Schedule the follow-up before pushing: if the push hangs or the hub restarts now, the
        # nag still delivers it.
        await self.rt.scheduler.schedule([{"reminder.nag": {"id": r.id}}], NAG_AFTER, {}, f"nag:{r.id}")
        await self.set_attention("reminder", r.id)
        await self._push_reminder(r, nag=False)
        return r

    async def reconcile(self) -> int:
        """On boot: re-arm any pending timed reminder that lost its job. Returns how many."""
        jobs = {j.id for j in await self.rt.scheduler.pending()}
        missing = [r for r in await self.rt.reminders.pending_timed() if f"reminder:{r.id}" not in jobs]
        for r in missing:
            await self.schedule(r)
        if missing:
            log.warning("re-armed %d reminder(s) that had no scheduled job", len(missing))
        return len(missing)

    async def nag(self, rid: str) -> None:
        r = await self.rt.reminders.get(rid)
        if r is not None and r.status == "fired":
            # The nag is a new alert; the first one is now stale (dismissed on Android/web).
            await self._clear_pushes([_push_id(r, nag=False)])
            await self._push_reminder(r, nag=True)
            await self.set_attention("reminder", r.id)

    async def _clear_pushes(self, ids: list[str]) -> None:
        topic = self.rt.notifier.topic_for("reminder")
        for seq in ids:
            await self.rt.notifier.clear(seq, topic)

    async def _push_reminder(self, r: Reminder, *, nag: bool) -> None:
        actions: list[dict[str, Any]] = []
        for label, action in (("Done", "done"), ("Snooze 10m", "snooze")):
            url = self.rt.links.sign(f"/r/{r.id}/{action}")
            if url:
                actions.append({"action": "http", "label": label, "url": url, "method": "POST", "clear": True})
        detail = "Ring: click = done · double = snooze"
        if r.trigger in ("arrive", "leave") and r.place:
            verb = "Arrived at" if r.trigger == "arrive" else "Left"
            detail = f"{verb} {await self.place_label(r.place)} · {detail}"
        # The watch shows the title + first line: the reminder itself goes in the title.
        await self.rt.feedback.say(
            detail, title=f"{'⏰ Still: ' if nag else '⏰ '}{_clip(r.text, 70)}", level="important",
            tags=["alarm_clock"], actions=actions or None, kind="reminder", sequence_id=_push_id(r, nag=nag),
            click=f"{self.rt.links.public_url}/#/memory" if self.rt.links.public_url else None,
        )

    async def done(self, rid: str) -> Reminder | None:
        r = await self.rt.reminders.mark_done(rid)
        await self._drop_jobs(rid)
        if r is not None:
            await self._clear_pushes(_round_push_ids(r, r.snoozes))
        await self.clear_attention("reminder", rid)
        return r

    async def snooze(self, rid: str, by: timedelta = SNOOZE) -> Reminder | None:
        r = await self.rt.reminders.snooze(rid, time.time() + by.total_seconds())
        if r is not None:
            await self.rt.scheduler.cancel(f"nag:{rid}")
            await self.schedule(r)
            await self.clear_attention("reminder", rid)
            await self._clear_pushes(_round_push_ids(r, r.snoozes - 1))   # the alerts just snoozed
        return r

    async def cancel(self, rid: str) -> Reminder | None:
        r = await self.rt.reminders.cancel(rid)
        await self._drop_jobs(rid)
        if r is not None:
            await self._clear_pushes(_round_push_ids(r, r.snoozes))
            await self.clear_attention("reminder", rid)
        return r

    async def _drop_jobs(self, rid: str) -> None:
        await self.rt.scheduler.cancel(f"reminder:{rid}")
        await self.rt.scheduler.cancel(f"nag:{rid}")

    async def on_place_event(self, event: PlaceEvent) -> None:
        trigger = "arrive" if event.kind in ("arrive", "visit") else "leave"
        for r in await self.rt.reminders.pending_place(trigger, event.place):  # type: ignore[arg-type]
            await self.fire(r.id)
        if event.kind == "leave" and event.place == "car":
            await self.record_parking(event.at)

    async def record_parking(self, at: float) -> Note:
        """Leaving the car = parked. Save where, from the car's GPS or the phone's last fix."""
        meta: dict[str, Any] = {}
        if self.rt.vehicle is not None:
            state = await self.rt.vehicle.state(allow_stale=True)
            if state and state.lat is not None and state.lon is not None:
                meta["location"] = {"lat": state.lat, "lon": state.lon, "source": "car"}
        if "location" not in meta:
            loc = await self.rt.context.get("location")
            if isinstance(loc, dict) and "lat" in loc and at - float(loc.get("at", 0)) < 900:
                meta["location"] = {"lat": loc["lat"], "lon": loc["lon"], "source": "phone"}
        place = await self.rt.context.value_at("place", at - 1)
        if place == "car":
            place = None
        note = await self.rt.notes.add("Parked", kind="log", created_at=at, place=place, meta=meta)
        await self.mirror(note)
        return note

    # ------------------------------------------------------------------ digest & mirror

    async def schedule_digest(self) -> None:
        at = self.rt.config.memory.digest_time
        if at is None:
            await self.rt.scheduler.cancel("digest")
            return
        now = self.rt.now()
        target = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        # Elapsed seconds, not aware wall-clock subtraction (which is off by an hour across DST).
        delay = timedelta(seconds=max(0.0, target.timestamp() - time.time()))
        await self.rt.scheduler.schedule([{"memory.digest": {}}], delay, {}, "digest")

    async def digest(self) -> None:
        start = self.rt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = await self.rt.notes.recent(200, since=start)
        captured = [n for n in today if n.kind in ("note", "journal", "moment", "reminder")]
        open_items = await self.rt.reminders.open()
        lines = [f"Captured today: {len(captured)}"]
        lines += [f"• {_clip(n.text, 60)}" for n in captured[:5]]
        if open_items:
            lines.append(f"Open: {len(open_items)}")
            lines += [f"• {_clip(r.text, 60)}" for r in open_items[:5]]
        await self.rt.feedback.say("\n".join(lines), title="🌙 Today", level="normal", tags=["crescent_moon"])
        await self.schedule_digest()

    async def mirror(self, note: Note, *, text: str | None = None) -> None:
        """Append to a daily Markdown file (Obsidian/Logseq) when ``memory.markdown_dir`` is set."""
        base = self.rt.config.memory.markdown_dir
        if base is None:
            return

        dt = datetime.fromtimestamp(note.created_at, self.rt.config.tz)
        icons = {"note": "-", "journal": "- 📓", "moment": "- 📍", "log": "- ✔", "reminder": "- [ ] ⏰"}
        icon = icons.get(note.kind, "-")
        where = f" _({await self.place_label(note.place)})_" if note.place else ""
        line = f"{icon} {dt:%H:%M} {text or note.text}{where}\n"
        path = base / f"{dt:%Y-%m-%d}.md"

        def write() -> None:
            base.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if new:
                    f.write(f"# {dt:%A, %B %-d}\n\n")
                f.write(line)

        await asyncio.to_thread(write)
