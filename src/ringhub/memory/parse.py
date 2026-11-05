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
    if day == "today":
        return today, None
    if day == "tonight":
        return today, _PARTS_OF_DAY["night"]
    if day.startswith("this "):
        return today, _PARTS_OF_DAY[day.split()[1]]
    if day == "tomorrow":
        return today + timedelta(days=1), time(9)
    name = day.split()[-1]
    delta = (_WEEKDAYS.index(name) - now.weekday()) % 7
    if delta == 0 or day.startswith("next "):
        delta = delta or 7
    return today + timedelta(days=delta), time(9)


def _parse_time(text: str, now: datetime) -> tuple[datetime | None, list[tuple[int, int]]]:
    spans: list[tuple[int, int]] = []
    m = _IN_RE.search(text)
    if m:
        spans.append(m.span())
        return now + timedelta(seconds=float(m.group(1)) * _UNIT_SECONDS[m.group(2)]), spans

    day_m = _DAY_RE.search(text)
    clock_m = _CLOCK_RE.search(text)
    if not day_m and not clock_m:
        later = _LATER_RE.search(text)
        if later:
            spans.append(later.span())
            return now + timedelta(hours=3), spans
        return None, spans

    hour_min: tuple[int, int, str | None] | None = None
    if clock_m:
        spans.append(clock_m.span())
        word, h, mi, mer = clock_m.groups()
        if word in ("noon", "midday"):
            hour_min = (12, 0, "pm")
        elif word == "midnight":
            hour_min = (0, 0, "am")
        elif h is not None and int(h) <= 23 and int(mi or 0) <= 59:
            hour_min = (int(h), int(mi or 0), None if mer and "clock" in mer else mer)

    if day_m:
        spans.append(day_m.span())
        base, default = _day_base(now, day_m.group(1))
        part = day_m.group(2)
        if part:
            default = _PARTS_OF_DAY[part]
        if hour_min is not None:
            h, mi, mer = hour_min
            if mer in ("pm", "p m") and h < 12:
                h += 12
            elif mer is None and part in ("afternoon", "evening", "night") and h < 12:
                h += 12
            elif mer is None and day_m.group(1) == "tonight" and h < 12:
                h += 12
            at = base.replace(hour=h % 24, minute=mi)
        else:
            at = base.replace(hour=(default or time(9)).hour, minute=(default or time(9)).minute)
        if at <= now:
            if day_m.group(1) in ("today", "tonight") or day_m.group(1).startswith("this "):
                return None, spans  # already passed today; caller falls back to keeping the note
            at += timedelta(days=7)
        return at, spans

    assert hour_min is not None or clock_m is not None
    if hour_min is None:
        return None, spans
    return _next_clock(now, *hour_min), spans


def _strip(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    text = re.sub(r"^(?:(?:and|to|that|about|please|then|ok|okay|hey)\b ?)+", "", text)
    text = re.sub(r"(?:\b(?:to|and|please|then)\b ?)+$", "", text).strip(" ,.")
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
                return ParsedReminder(text=body, trigger=trig)

    if not cue:
        return None  # "meeting at 3" is a note, not a reminder, unless the user asked
    at, time_spans = _parse_time(text, now)
    body = _strip(text, spans + time_spans)
    if not body:
        return None
    if at is None:
        return ParsedReminder(text=body, trigger=Trigger(kind="none"))
    return ParsedReminder(text=body, trigger=Trigger(kind="time", at=at))
