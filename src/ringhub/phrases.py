"""Voice phrase patterns for skills, simple enough to type into the dashboard on a phone.

    warm up the car
    tell {who} i'm running late
    focus for {minutes:number} minutes
    re:^(?:open|pop) the (?P<which>trunk|frunk)$      (raw regex escape hatch)

Literal words are normalized the same way transcripts are (lowercase, spoken numbers to
digits, punctuation dropped), and the *whole* utterance must match apart from polite filler,
so "remind me to warm up the car at 7" is a reminder, not the "warm up the car" command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .intents import normalize

_SLOT_RE = re.compile(r"\{([a-z][a-z0-9_]{0,30})(?::(number|word|text))?\}")
_KINDS = {"number": r"\d+(?:\.\d+)?", "word": r"[\w']+", "text": r".+?"}
_FILLER_PRE = r"(?:(?:hey|ok|okay|please|can you|could you|would you|i want to|i'd like to|go ahead and) )*"
_FILLER_POST = r"(?: (?:please|now|thanks|thank you))*"


def compile_phrase(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip()
    if pattern.startswith("re:"):
        try:
            return re.compile(pattern[3:], re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"bad regex in phrase {pattern!r}: {e}") from None
    out: list[str] = []
    pos = 0
    seen: set[str] = set()
    for m in _SLOT_RE.finditer(pattern):
        literal = normalize(pattern[pos:m.start()])
        if literal:
            out.append(re.escape(literal))
        name, kind = m.group(1), m.group(2) or "text"
        if name in seen:
            raise ValueError(f"phrase {pattern!r} uses slot {{{name}}} twice")
        seen.add(name)
        out.append(f"(?P<{name}>{_KINDS[kind]})")
        pos = m.end()
    tail = normalize(pattern[pos:])
    if tail:
        out.append(re.escape(tail))
    if not out:
        raise ValueError("empty phrase")
    if "{" in _SLOT_RE.sub("", pattern) or "}" in _SLOT_RE.sub("", pattern):
        raise ValueError(f"phrase {pattern!r} has a malformed {{slot}} (use {{name}} or {{name:number}})")
    body = " ".join(out)
    return re.compile(rf"^{_FILLER_PRE}{body}{_FILLER_POST}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PhraseMatch:
    skill_id: str
    slots: dict[str, Any]
    phrase: str


class PhraseIndex:
    def __init__(self, entries: list[tuple[str, str]]) -> None:
        """``entries`` is (skill_id, phrase) in priority order."""
        self._compiled = [(sid, p, compile_phrase(p)) for sid, p in entries]

    def match(self, transcript: str) -> PhraseMatch | None:
        text = normalize(transcript)
        for skill_id, phrase, rx in self._compiled:
            m = rx.search(text)
            if m:
                return PhraseMatch(skill_id, {k: v for k, v in m.groupdict().items() if v is not None}, phrase)
        return None
