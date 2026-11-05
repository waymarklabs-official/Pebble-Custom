"""The memory aid: smart capture, recall, and reminder delivery (time, place, nag, digest)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..intents import normalize
from .notes import Note
from .parse import ParsedReminder, parse_reminder
from .reminders import Reminder

if TYPE_CHECKING:
    from ..context import PlaceEvent
    from ..runtime import Runtime

log = logging.getLogger(__name__)

ATTENTION_KEY = "attention"
NAG_AFTER = timedelta(minutes=15)
SNOOZE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    kind: str  # "note" | "reminder" | "answer" | "skill" | "empty"
    message: str
    note: Note | None = None
    reminder: Reminder | None = None


# Built-in questions. Checked after user skill phrases, before reminders and notes.
_Q_PARKED = re.compile(r"\bwhere(?:'s| is| did i| have i)? ?(?:i )?(?:park|parked|leave|left)\b|"
                       r"\bwhere(?:'s| is) (?:the |my )?car\b")
_Q_LIST = re.compile(r"\bwhat(?:'s| is) on my (?:list|plate)\b|\bwhat are my reminders\b|"
                     r"\bwhat do i (?:have|need) to do\b|^(?:my )?(?:list|reminders|to ?do(?: list)?)$")
_Q_NEXT = re.compile(r"\bwhat(?:'s| is) next\b|\bnext reminder\b")
_Q_DID = re.compile(r"^(?:did|have) i (?:already )?(?P<what>.+?)(?: today)?$")
_Q_ABOUT = re.compile(r"\b(?:what did i (?:say|note|write|record) (?:about|on|regarding)|"
                      r"find (?:my )?(?:notes? )?(?:about|on|for)|search (?:my notes )?for|"
                      r"what was (?:the|that) (?:thing|idea|note) (?:about|for))\s+(?P<q>.+)$")
_Q_AT = re.compile(r"\bwhat did i (?:say|note|save|write|capture|record|jot down)(?: while i was)? (?:at|in) "
                   r"(?:the |my )?(?P<place>[\w' ]+?)(?: today| yesterday| recently)?$")
_Q_LAST = re.compile(r"\b(?:what (?:was|were) my last|read (?:me )?my (?:last|latest|recent))\b"
                     r"(?: (?P<n>\d+))? ?(?:notes?|thoughts?|captures?)?")


def _clip(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


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
        await self.rt.store.set(ATTENTION_KEY, {"kind": kind, "ref": ref, "at": time.time()})

    async def attention(self) -> dict[str, Any] | None:
        att = await self.rt.store.get(ATTENTION_KEY)
        window = self.rt.config.memory.ack_window_seconds
        if not isinstance(att, dict) or window <= 0:
            return None
        limit = window if att.get("kind") == "reminder" else min(window, 30)
        if time.time() - float(att.get("at", 0)) > limit:
            return None
        return att

    async def clear_attention(self) -> None:
        await self.rt.store.delete(ATTENTION_KEY)

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
        answer = await self.answer(text)
        if answer is not None:
            return CaptureResult("answer", answer)

        now = datetime.fromtimestamp(max(said_at, time.time() - 3600), self.rt.config.tz)
        parsed = parse_reminder(text, now, await self.place_aliases())
        if parsed is None and self.rt.llm is not None:
            parsed = await self.rt.llm.extract_reminder(text, now, await self.place_aliases())
        if parsed is not None:
            return await self._create_reminder(parsed, text, said_at=said_at, place=place, mode=mode,
                                               audio=audio, source=source)

        note = await self.rt.notes.add(text, kind="note", created_at=said_at, place=place, mode=mode,
                                       audio=audio, source=source)
        await self.mirror(note)
        await self.set_attention("capture", {"note": note.id})
        where = f" · {await self.place_label(place)}" if place else ""
        return CaptureResult("note", f"📝 {_clip(text)}{where}", note=note)

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
        if ref.get("reminder"):
            await self.cancel(str(ref["reminder"]))
            parts.append("reminder")
        if ref.get("note"):
            note = await self.rt.notes.get(int(ref["note"]))
            if note and await self.rt.notes.delete(note.id):
                parts.append("note")
        await self.clear_attention()
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
        q = normalize(text)
        if _Q_PARKED.search(q):
            return await self.where_parked()
        if _Q_LIST.search(q):
            return await self.list_summary()
        if _Q_NEXT.search(q):
            return await self.next_summary()
        m = _Q_ABOUT.search(q)
        if m:
            return await self.search_summary(m.group("q"))
        m = _Q_AT.search(q)
        if m:
            return await self.place_summary(m.group("place"))
        m = _Q_LAST.search(q)
        if m and ("note" in q or "thought" in q or "capture" in q or "last" in q):
            return await self.recent_summary(int(m.group("n") or 3))
        m = _Q_DID.match(q)
        if m:
            return await self.did_i(m.group("what"))
        return None

    async def where_parked(self) -> str:
        note = await self.rt.notes.latest_matching("log", "Parked")
        if note is None:
            return "🚗 I don't have a parking spot saved yet."
        loc = note.meta.get("location") or {}
        link = f"\nhttps://maps.apple.com/?q=Car&ll={loc['lat']},{loc['lon']}" if "lat" in loc else ""
        where = f" at {await self.place_label(note.place)}" if note.place else ""
        return f"🚗 {note.text}{where} · {self._when(note.created_at)}{link}"

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
        start = self.rt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        hits = [n for n in await self.rt.notes.search(what, 10) if n.created_at >= start and n.kind != "reminder"]
        if not hits:
            return f"🤷 No record of “{_clip(what, 40)}” today."
        n = hits[0]
        return f"✅ Yes — “{_clip(n.text, 50)}” at {self._when(n.created_at)}"

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
        await self._push_reminder(r, nag=False)
        await self.set_attention("reminder", r.id)
        await self.rt.scheduler.schedule([{"reminder.nag": {"id": r.id}}], NAG_AFTER, {}, f"nag:{r.id}")
        return r

    async def nag(self, rid: str) -> None:
        r = await self.rt.reminders.get(rid)
        if r is not None and r.status == "fired":
            await self._push_reminder(r, nag=True)
            await self.set_attention("reminder", r.id)

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
            tags=["alarm_clock"], actions=actions or None, kind="reminder", sequence_id=f"rem-{r.id}",
            click=f"{self.rt.links.public_url}/#/memory" if self.rt.links.public_url else None,
        )

    async def done(self, rid: str) -> Reminder | None:
        r = await self.rt.reminders.mark_done(rid)
        await self._drop_jobs(rid)
        if r is not None:
            await self.rt.notifier.clear(f"rem-{rid}", self.rt.notifier.topic_for("reminder"))
        att = await self.rt.store.get(ATTENTION_KEY)
        if isinstance(att, dict) and att.get("ref") == rid:
            await self.clear_attention()
        return r

    async def snooze(self, rid: str, by: timedelta = SNOOZE) -> Reminder | None:
        r = await self.rt.reminders.snooze(rid, time.time() + by.total_seconds())
        if r is not None:
            await self.rt.scheduler.cancel(f"nag:{rid}")
            await self.schedule(r)
            await self.clear_attention()
        return r

    async def cancel(self, rid: str) -> Reminder | None:
        r = await self.rt.reminders.cancel(rid)
        await self._drop_jobs(rid)
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
        await self.rt.scheduler.schedule([{"memory.digest": {}}], target - now, {}, "digest")

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
