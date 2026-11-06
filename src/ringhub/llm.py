"""Optional Claude fallback for reminders the deterministic parser didn't understand.

"ping me after lunch about the invoice" or "nudge me Thursday before the standup" are easy for a
model and tedious for regexes. The model only ever *extracts* (what, when, where) into a fixed
schema; the hub validates the result and then treats it exactly like a parsed reminder. When the
model says it isn't a reminder, the sentence is saved as a note as usual.
Requires ``pip install 'ringhub[llm]'``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .config import LlmConfig
from .memory.parse import ParsedReminder, Trigger

log = logging.getLogger(__name__)

_SYSTEM = (
    "You read one short sentence that someone spoke into a smart ring (speech recognition, may contain "
    "errors) and decide whether they are asking to be reminded of something later. If they are NOT asking "
    "for a reminder (a fact, an idea, a journal thought), answer is_reminder=false. If they are, extract the "
    "reminder text as a short imperative (e.g. 'Call the bank'), and when or where it should fire: either a "
    "local date-time, a number of minutes from now, arriving at a place, leaving a place, or no trigger "
    "(a to-do list item). Only use place ids from the provided list; if the place is not in the list, use "
    "its lowercase name. Never invent details that were not said."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_reminder": {"type": "boolean"},
        "text": {"type": "string"},
        "trigger": {"type": "string", "enum": ["datetime", "minutes", "arrive", "leave", "none"]},
        "local_datetime": {"type": "string", "description": "YYYY-MM-DDTHH:MM in the user's local time, or ''"},
        "minutes": {"type": "integer"},
        "place": {"type": "string"},
    },
    "required": ["is_reminder", "text", "trigger", "local_datetime", "minutes", "place"],
    "additionalProperties": False,
}


class LlmRouter:
    def __init__(self, cfg: LlmConfig) -> None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("llm configured but the 'anthropic' package is missing: "
                               "pip install 'ringhub[llm]'") from None
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key.get_secret_value(), timeout=cfg.timeout_seconds, max_retries=1)
        self._model = cfg.model

    async def _ask(self, system: str, user: str) -> dict[str, Any] | None:
        a = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError:
            log.warning("llm rate limited; skipping fallback")
            return None
        except a.APIStatusError as e:
            log.warning("llm request failed (%s): %s", e.status_code, e.message)
            return None
        except a.APIConnectionError as e:
            log.warning("llm unreachable: %s", e)
            return None
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("llm returned non-JSON output (stop_reason=%s)", response.stop_reason)
            return None
        return data if isinstance(data, dict) else None

    async def extract_reminder(self, transcript: str, now: datetime,
                               places: Mapping[str, str]) -> ParsedReminder | None:
        place_ids = sorted(set(places.values()))
        user = (f"Now: {now:%A %Y-%m-%d %H:%M} (local time)\nKnown places: {', '.join(place_ids) or 'none'}\n"
                f"Sentence: {transcript}")
        data = await self._ask(_SYSTEM, user)
        return interpret(data, now, places) if data else None


def interpret(data: dict[str, Any], now: datetime, places: Mapping[str, str]) -> ParsedReminder | None:
    """Validate the model's JSON into a reminder (pure, unit-tested)."""
    if not data.get("is_reminder"):
        return None
    text = str(data.get("text") or "").strip()[:300]
    if not text:
        return None
    kind = data.get("trigger")
    if kind == "minutes":
        minutes = data.get("minutes")
        if not isinstance(minutes, int) or not 0 < minutes <= 60 * 24 * 366:
            return ParsedReminder(text, Trigger("none"))
        return ParsedReminder(text, Trigger("time", at=now + timedelta(minutes=minutes)))
    if kind == "datetime":
        try:
            at = datetime.strptime(str(data.get("local_datetime")), "%Y-%m-%dT%H:%M").replace(tzinfo=now.tzinfo)
        except ValueError:
            return ParsedReminder(text, Trigger("none"))
        if at <= now:
            return ParsedReminder(text, Trigger("none"))
        return ParsedReminder(text, Trigger("time", at=at))
    if kind in ("arrive", "leave"):
        raw = str(data.get("place") or "").strip().lower()
        known = set(places.values())
        place = raw if raw in known else places.get(raw, raw.replace(" ", "_"))
        if not place:
            return ParsedReminder(text, Trigger("none"))
        return ParsedReminder(text, Trigger(kind, place=place, place_known=place in known))
    return ParsedReminder(text, Trigger("none"))
