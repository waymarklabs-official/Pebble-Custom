"""Deterministic voice intent matching.

Regexes over a normalized transcript: fast, offline, predictable, and debuggable from the
event log. The optional LLM fallback only runs when nothing here matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import IntentConfig

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_NUMBER_RE = re.compile(
    r"\b(?:(" + "|".join(_TENS) + r")(?:[\s-](" + "|".join(k for k in _UNITS if _UNITS[k] < 10) + r"))?"
    r"|(" + "|".join(_UNITS) + r"))\b"
)
_PHRASES = [
    (re.compile(r"\bhalf an hour\b"), "30 minutes"),
    (re.compile(r"\ban hour\b"), "60 minutes"),
    (re.compile(r"\ba couple(?: of)?\b"), "2"),
    (re.compile(r"\b(a|an) (minute|second)\b"), r"1 \2"),
]


def _numberize(m: re.Match[str]) -> str:
    tens, unit, single = m.groups()
    if single:
        return str(_UNITS[single])
    return str(_TENS[tens] + (_UNITS[unit] if unit else 0))


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, turn spoken numbers into digits."""
    t = text.lower().replace("\u2019", "'")
    t = re.sub(r"[^\w\s':./-]", " ", t)
    t = re.sub(r"(?<!\d)[.:](?!\d)", " ", t)  # keep 7:30 and 1.5, drop sentence periods
    for pattern, repl in _PHRASES:
        t = pattern.sub(repl, t)
    t = _NUMBER_RE.sub(_numberize, t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True, slots=True)
class IntentMatch:
    intent: IntentConfig
    groups: dict[str, Any]
    text: str


class IntentRouter:
    def __init__(self, intents: list[IntentConfig]) -> None:
        self.intents = intents
        self._compiled = [
            (intent, [re.compile(p, re.IGNORECASE) for p in intent.match]) for intent in intents
        ]

    def match(self, transcript: str) -> IntentMatch | None:
        text = normalize(transcript)
        for intent, patterns in self._compiled:
            for pattern in patterns:
                m = pattern.search(text)
                if m:
                    groups = {k: v for k, v in m.groupdict().items() if v is not None}
                    return IntentMatch(intent=intent, groups=groups, text=text)
        return None

    def by_name(self, name: str) -> IntentConfig | None:
        return next((i for i in self.intents if i.name == name), None)
