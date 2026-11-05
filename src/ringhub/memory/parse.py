"""Turn a spoken sentence into a reminder: what, and when or where.

Deterministic on purpose: it runs offline in microseconds, behaves the same every time, and
every rule is covered by a test. Phrases it does not understand are still saved as notes
(nothing is ever lost), and the optional LLM can handle the long tail.

Examples (``now`` = Tue 14:10)::

    "remind me in 20 minutes to check the oven"  -> time  14:30  "Check the oven"
    "remind me at 7 to call mom"                 -> time  19:00  "Call mom"
    "tomorrow morning remind me to bring the charger" -> time Wed 09:00
    "when I get home take the chicken out"       -> arrive home   "Take the chicken out"
    "remind me when I leave work to buy milk"    -> leave work    "Buy milk"
    "remind me to renew the passport"            -> list (no trigger) "Renew the passport"
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from ..intents import normalize

TriggerKind = Literal["time", "arrive", "leave", "none"]

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PARTS_OF_DAY = {"morning": time(9), "afternoon": time(15), "evening": time(19), "night": time(20)}

_REMINDER_CUES = re.compile(
    r"\b(?:remind me|don'?t let me forget|don'?t forget|do not forget|remember to)\b(?: to| that| about)?"
)
_UNIT_SECONDS = {"second": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
_IN_RE = re.compile(r"\bin (\d+(?:\.\d+)?) ?(second|minute|min|hour|hr|day|week)s?\b")
_LATER_RE = re.compile(r"\blater(?: today| on)?\b")
_CLOCK_RE = re.compile(
    r"\b(?:at |by )(?:(noon|midday|midnight)|(\d{1,2})(?::(\d{2}))? ?(am|pm|a m|p m|o'?clock)?)(?![\d:])"
)
_DAY_RE = re.compile(
    r"\b(today|tonight|tomorrow|this (?:morning|afternoon|evening)|"
    r"(?:on |next |this )?(?:" + "|".join(_WEEKDAYS) + r"))(?: (morning|afternoon|evening|night))?\b"
)
_HOME_WORDS = {"home", "house", "back", "back home"}


def _place_regexes(aliases: list[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Arrive/leave patterns. Known aliases (longest first) win; otherwise one word is taken,
    because transcripts rarely have commas: "when I get to the gym call Bob" -> "gym"."""
    alt = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    place = rf"(?P<place>{alt + '|' if alt else ''}[a-z][\w']*)"
    arrive = re.compile(
        r"\b(?:when|once|as soon as|next time|whenever) (?:i|i'm|i am|we|we're)"
        r"(?: (?:get|arrive|am|are|come|walk|go|pull|make it))?"
        rf"(?: (?P<home>back home|home|back)\b| (?:to|at|in|into|by|inside) (?:the |my )?{place}\b)"
    )
    leave = re.compile(
        r"\b(?:when|once|as soon as|before|right before) (?:i|we) "
        r"(?:leave|get out of|head out of|go out of|exit|walk out of|step out of)"
        rf"(?: (?P<home>home)\b| (?:the |my )?{place}\b)"
    )
    return arrive, leave


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: TriggerKind
    at: datetime | None = None
    place: str | None = None
    place_known: bool = True


@dataclass(frozen=True, slots=True)
class ParsedReminder:
    text: str
    trigger: Trigger


def _resolve_place(raw: str, places: Mapping[str, str]) -> tuple[str, bool]:
    spoken = raw.strip().removeprefix("the ").removeprefix("my ").strip()
    if not spoken or spoken in _HOME_WORDS:
        spoken = "home"
    if spoken in places:
        return places[spoken], True
    return re.sub(r"[^a-z0-9]+", "_", spoken).strip("_"), False


def _next_clock(now: datetime, hour: int, minute: int, meridiem: str | None) -> datetime:
    """Soonest future occurrence; an unqualified "at 7" means 7:00 or 19:00, whichever is next."""
    candidates: list[int]
    if meridiem in ("am", "a m"):
        candidates = [hour % 12]
    elif meridiem in ("pm", "p m"):
        candidates = [hour % 12 + 12]
    elif hour >= 13 or hour == 0:
        candidates = [hour % 24]
    else:
        candidates = [hour % 12, hour % 12 + 12]
    best: datetime | None = None
    for h in candidates:
        dt = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        if best is None or dt < best:
            best = dt
    assert best is not None
    return best


def _day_base(now: datetime, day: str) -> tuple[datetime, time | None]:
    """Start-of-day for a day phrase plus that phrase's default time, if any."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    words = day.split()
    if day == "today":
        return today, None
    if day == "tonight":
        return today, _PARTS_OF_DAY["night"]
    if words[0] == "this" and words[-1] in _PARTS_OF_DAY:
        return today, _PARTS_OF_DAY[words[-1]]
    if day == "tomorrow":
        return today + timedelta(days=1), time(9)
    delta = (_WEEKDAYS.index(words[-1]) - now.weekday()) % 7
    if delta == 0 and words[0] != "this":
        delta = 7  # "on friday" said on a Friday means next week; "this friday" means today
    return today + timedelta(days=delta), time(9)


def _same_day(day: str) -> bool:
    return day in ("today", "tonight") or (day.startswith("this ") and day.split()[-1] in _PARTS_OF_DAY)


def _hour_on(base: datetime, now: datetime, hour: int, minute: int, meridiem: str | None,
             part: str | None, day: str) -> int:
    """24h hour for a clock time said together with a day word."""
    if meridiem in ("am", "a m"):
        return 0 if hour == 12 else hour
    if meridiem in ("pm", "p m"):
        return hour if hour == 12 else hour + 12
    if hour == 0 or hour >= 13:
        return hour
    if part in ("afternoon", "evening", "night") or day == "tonight":
        return hour if hour == 12 else hour + 12
    if base.date() == now.date():
        # "today at 7" at 14:10 means 19:00: the next occurrence of the clock face today.
        for h in (hour % 12, hour % 12 + 12):
            if base.replace(hour=h, minute=minute) > now:
                return h
        return hour % 12 + 12
    return hour + 12 if hour <= 6 else hour  # "friday at 3" is 3 pm; "friday at 8" is 8 am


def _after(now: datetime, seconds: float) -> datetime:
    """``now`` plus elapsed seconds (DST-safe, unlike aware wall-clock arithmetic)."""
    return datetime.fromtimestamp(now.timestamp() + seconds, now.tzinfo)


def _parse_time(text: str, now: datetime) -> tuple[datetime | None, list[tuple[int, int]]]:
    spans: list[tuple[int, int]] = []
    m = _IN_RE.search(text)
    if m:
        spans.append(m.span())
        amount, unit = float(m.group(1)), m.group(2)
        if unit in ("day", "week"):  # calendar units keep the wall-clock time of day
            return now + timedelta(days=amount * (7 if unit == "week" else 1)), spans
        return _after(now, amount * _UNIT_SECONDS[unit]), spans

    day_m = _DAY_RE.search(text)
    clock_m = _CLOCK_RE.search(text)
    if not day_m and not clock_m:
        later = _LATER_RE.search(text)
        if later:
            spans.append(later.span())
            return _after(now, 3 * 3600), spans
        return None, spans

    hour_min: tuple[int, int, str | None] | None = None
    if clock_m:
        spans.append(clock_m.span())
        word, h, mi, mer = clock_m.groups()
        if word in ("noon", "midday"):
            hour_min = (12, 0, "pm")
        elif word == "midnight":
            hour_min = (12, 0, "am")
        elif h is not None and int(h) <= 23 and int(mi or 0) <= 59:
            hour_min = (int(h), int(mi or 0), None if mer and "clock" in mer else mer)

    if day_m:
        spans.append(day_m.span())
        day = day_m.group(1).removeprefix("on ")
        base, default = _day_base(now, day)
        part = day_m.group(2)
        if part:
            default = _PARTS_OF_DAY[part]
        if hour_min is not None:
            h, mi, mer = hour_min
            at = base.replace(hour=_hour_on(base, now, h, mi, mer, part, day), minute=mi)
        else:
            at = base.replace(hour=(default or time(9)).hour, minute=(default or time(9)).minute)
        if at <= now:
            if _same_day(day) or day in ("today", "tomorrow"):
                return None, spans  # already passed; the caller keeps it as a list item
            at += timedelta(days=7)
        return at, spans

    if hour_min is None:
        return None, spans
    return _next_clock(now, *hour_min), spans


def _strip(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    text = re.sub(r"^(?:(?:and|to|that|about|please|then|ok|okay|hey)\b ?)+", "", text)
    return re.sub(r"(?:\b(?:to|and|please|then)\b ?)+$", "", text).strip(" ,.")


def _recase(body: str, original: str) -> str:
    """Restore the speaker's casing ("Bob", "NASA") lost by normalization, then capitalize.

    Walks the original words in order and reuses each one whose normalized form equals the next
    body word; words that normalization rewrote (spoken numbers) keep their normalized form.
    """
    orig = re.findall(r"[\w'\u2019]+", original)
    out: list[str] = []
    j = 0
    for word in body.split(" "):
        for k in range(j, len(orig)):
            if normalize(orig[k]) == word:
                out.append(orig[k].replace("\u2019", "'"))
                j = k + 1
                break
        else:
            out.append(word)
    text = " ".join(out)
    return text[:1].upper() + text[1:] if text else text


def parse_reminder(transcript: str, now: datetime, places: Mapping[str, str] | None = None) -> ParsedReminder | None:
    """Return a reminder if the sentence asks for one, else ``None`` (it is just a note).

    ``places`` maps spoken aliases ("home", "the office", "car") to place ids. Unknown places
    still produce a trigger with ``place_known=False`` so the user can be told to add it.
    """
    places = {re.sub(r"^(?:the|my) ", "", k.strip().lower()): v for k, v in (places or {}).items()}
    places.setdefault("home", "home")
    text = normalize(transcript)
    cue = _REMINDER_CUES.search(text)
    spans: list[tuple[int, int]] = [cue.span()] if cue else []

    arrive_re, leave_re = _place_regexes(list(places))
    for regex, kind in ((leave_re, "leave"), (arrive_re, "arrive")):
        m = regex.search(text)
        if m:
            place, known = _resolve_place(m.group("place") or m.group("home") or "home", places)
            spans.append(m.span())
            body = _strip(text, spans)
            if body:
                trig = Trigger(kind=kind, place=place, place_known=known)  # type: ignore[arg-type]
                return ParsedReminder(text=_recase(body, transcript), trigger=trig)

    if not cue:
        return None  # "meeting at 3" is a note, not a reminder, unless the user asked
    at, time_spans = _parse_time(text, now)
    body = _strip(text, spans + time_spans)
    if not body:
        return None
    body = _recase(body, transcript)
    if at is None:
        return ParsedReminder(text=body, trigger=Trigger(kind="none"))
    return ParsedReminder(text=body, trigger=Trigger(kind="time", at=at))
