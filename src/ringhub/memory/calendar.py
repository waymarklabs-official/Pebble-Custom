"""Calendar by voice: "add dentist to my calendar Friday at 3", "what's on my calendar tomorrow".

Parsing is deterministic and offline, built on the reminder parser's time grammar:
- "for 90 minutes" / "for 2 hours" sets the length (otherwise the configured default);
- "from 2 to 4" sets both ends;
- a day with no time ("on Friday", "tomorrow") makes an all-day event.
A calendar request with no recognisable date is never dropped: the words are saved as a note and
you're told what to say instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..intents import normalize
from .parse import _CLOCK_RE, _DAY_RE, _IN_RE, _parse_time, _recase, _strip

_FILLER = re.compile(r"^(?:(?:um|uh|hey|ok|okay|so|please|can you|could you)\s+)+")
_ADD_TO = re.compile(
    r"^(?:add|put|schedule|create|make|book|set up|pencil in|stick)(?: an?| the)?"
    r"(?: (?:event|appointment|meeting|entry))?(?: for)? (?P<a>.+?) (?:to|on|in|into|onto) (?:my |the )?calendar"
    r"(?: for)?(?P<b>(?: .*)?)$")
_ADD_PREFIX = re.compile(r"^(?:add|put) (?:to|on|in|onto) (?:my |the )?calendar,? (?P<a>.+)$")
_CAL_PREFIX = re.compile(r"^(?:calendar|new event|new calendar event),? (?P<a>.+)$")
_SCHEDULE = re.compile(r"^schedule (?:an? |the )?(?P<a>.+)$")   # only with a time: "schedule" alone is ambiguous
_DURATION = re.compile(r"\bfor (\d+(?:\.\d+)?) ?(minute|min|hour|hr)s?\b")
_ALL_DAY = re.compile(r"\ball day\b")
_RANGE = re.compile(r"\bfrom (\d{1,2})(?::(\d{2}))?(?: ?(am|pm))? (?:to|until|till|-) "
                    r"(\d{1,2})(?::(\d{2}))?(?: ?(am|pm))?\b")

_AGENDA = re.compile(
    r"^(?:what(?:'s| is| do i have)|whats) (?:on )?(?:my |the )?(?:calendar|schedule|agenda)"
    r"(?: (?:for )?(?P<day>today|tomorrow|this week))?$"
    r"|^what do i have (?:on )?(?P<day2>today|tomorrow|this week)$"
    r"|^(?:do i have|any) (?:meetings|events|appointments)(?: (?P<day3>today|tomorrow|this week))?$")
_NEXT = re.compile(r"^(?:what(?:'s| is)|whats|when(?:'s| is)) my next (?:meeting|event|appointment|calendar event)$")


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    title: str
    start: datetime | date | None   # None: no date heard
    end: datetime | date | None

    @property
    def all_day(self) -> bool:
        return isinstance(self.start, date) and not isinstance(self.start, datetime)


def is_agenda_question(transcript: str) -> tuple[str, str] | None:
    """("day", "today|tomorrow|this week") or ("next", "") for agenda questions, else None."""
    q = _FILLER.sub("", normalize(transcript)).strip(" ?.!")
    if _NEXT.match(q):
        return ("next", "")
    m = _AGENDA.match(q)
    if m:
        return ("day", m.group("day") or m.group("day2") or m.group("day3") or "today")
    return None


def parse_event(transcript: str, now: datetime, default_minutes: int = 60) -> ParsedEvent | None:
    """An event if the sentence asks for one, else None (it is something else: let capture go on)."""
    text = _FILLER.sub("", normalize(transcript)).strip(" .!?")
    explicit = True
    m = _ADD_TO.match(text)
    if m:
        body = re.sub(r"\s+", " ", f"{m.group('a')} {m.group('b') or ''}").strip()
    elif (m := _ADD_PREFIX.match(text) or _CAL_PREFIX.match(text)):
        body = m.group("a")
    elif (m := _SCHEDULE.match(text)):
        body, explicit = m.group("a"), False
    else:
        return None

    end_clock: tuple[int, int, str | None] | None = None
    if (r := _RANGE.search(body)):
        h1, m1, mer1, h2, m2, mer2 = r.groups()
        mer_start = mer1 or (mer2 if mer2 and int(h1) <= int(h2) else None)
        start_text = f"at {h1}:{m1 or '00'}{' ' + mer_start if mer_start else ''}"
        body = body[:r.start()] + start_text + body[r.end():]
        end_clock = (int(h2), int(m2 or 0), mer2)

    spans: list[tuple[int, int]] = []
    minutes: float | None = None
    if (d := _DURATION.search(body)):
        spans.append(d.span())
        minutes = float(d.group(1)) * (60 if d.group(2) in ("hour", "hr") else 1)
    all_day = False
    if (ad := _ALL_DAY.search(body)):
        spans.append(ad.span())
        all_day = True

    at, time_spans = _parse_time(body, now)
    spans += time_spans
    title = _recase(_strip(body, spans), transcript)
    if not title:
        return None
    day_m = _DAY_RE.search(body)
    has_clock = bool(_CLOCK_RE.search(body) or _IN_RE.search(body) or (day_m and day_m.group(2)))
    if at is None:
        if not explicit:
            return None
        return ParsedEvent(title, None, None)
    if all_day or not has_clock:
        day = at.date()
        return ParsedEvent(title, day, day + timedelta(days=1))
    start = at
    if end_clock is not None:
        end = _end_at(start, *end_clock)
    else:
        end = start + timedelta(minutes=minutes or default_minutes)
    return ParsedEvent(title, start, end)


def _end_at(start: datetime, hour: int, minute: int, meridiem: str | None) -> datetime:
    h = hour % 12 + (12 if meridiem == "pm" else 0) if meridiem else hour
    end = start.replace(hour=h % 24, minute=minute, second=0, microsecond=0)
    if meridiem is None and end <= start and hour < 12:
        end += timedelta(hours=12)            # "from 11 to 1" means 1 pm
    if end <= start:
        end += timedelta(days=1)
    return end


def describe_when(ev: ParsedEvent, now: datetime) -> str:
    assert ev.start is not None
    if isinstance(ev.start, datetime):
        return f"{_day_word(ev.start.date(), now)} {_clock(ev.start)}-{_clock(ev.end)}"   # type: ignore[arg-type]
    return f"{_day_word(ev.start, now)} (all day)"


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").lower().replace(":00 ", " ")


def _day_word(d: date, now: datetime) -> str:
    if d == now.date():
        return "today"
    if d == now.date() + timedelta(days=1):
        return "tomorrow"
    if 0 < (d - now.date()).days < 7:
        return f"{d:%A}"
    return f"{d:%a %b %-d}"


def agenda_window(kind: str, day: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, label) for an agenda question."""
    midnight = datetime.combine(now.date(), time(0), now.tzinfo)
    if kind == "next":
        return now, now + timedelta(days=14), "Next"
    if day == "tomorrow":
        return midnight + timedelta(days=1), midnight + timedelta(days=2), "Tomorrow"
    if day == "this week":
        return now, midnight + timedelta(days=7), "This week"
    return now, midnight + timedelta(days=1), "Today"


def format_agenda(events: list[dict[str, object]], label: str, now: datetime) -> str:
    if not events:
        return f"📅 {label}: nothing on your calendar."
    lines = []
    for e in events:
        start = e.get("start") if isinstance(e.get("start"), dict) else {}
        summary = str(e.get("summary") or "(no title)")
        if isinstance(start, dict) and start.get("dateTime"):
            dt = datetime.fromisoformat(str(start["dateTime"])).astimezone(now.tzinfo)
            day = "" if label in ("Today", "Tomorrow") else f"{_day_word(dt.date(), now)} "
            lines.append(f"• {day}{_clock(dt)} {summary}")
        elif isinstance(start, dict) and start.get("date"):
            d = date.fromisoformat(str(start["date"]))
            day = "" if label in ("Today", "Tomorrow") else f"{_day_word(d, now)}, "
            lines.append(f"• {day}all day: {summary}")
    return f"📅 {label}:\n" + "\n".join(lines)
