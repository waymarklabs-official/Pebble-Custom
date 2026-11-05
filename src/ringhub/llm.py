"""Optional Claude fallback: map a free-form transcript onto one of *your* intents.

The model never acts on its own. It can only choose an intent you defined (or ``none``) and
fill named slots; the chosen intent's steps then run exactly as if a regex had matched.
Requires ``pip install 'ringhub[llm]'``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import IntentConfig, LlmConfig

log = logging.getLogger(__name__)

_SYSTEM = (
    "You route short voice commands, spoken into a smart ring, to a fixed list of intents for a "
    "home automation hub. Pick the single intent whose description best matches what the speaker "
    "wants, or 'none' if no intent clearly fits; never guess. Extract slot values as short plain "
    "strings using the slot names listed for that intent (numbers as digits). Transcripts come "
    "from speech recognition and may contain recognition errors."
)


@dataclass(frozen=True, slots=True)
class LlmChoice:
    intent: str
    slots: dict[str, str]


def _slot_names(intent: IntentConfig) -> list[str]:
    names: list[str] = []
    for pattern in intent.match:
        names.extend(n for n in re.compile(pattern).groupindex if n not in names)
    return names


class LlmRouter:
    def __init__(self, cfg: LlmConfig, intents: list[IntentConfig]) -> None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("llm configured but the 'anthropic' package is missing: "
                               "pip install 'ringhub[llm]'") from None
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=cfg.api_key.get_secret_value(), timeout=cfg.timeout_seconds, max_retries=1)
        self._model = cfg.model
        self._intents = intents
        catalog = [
            {"name": i.name, "description": i.description or i.name, "slots": _slot_names(i)}
            for i in intents
        ]
        # Stable system prompt (catalog only changes on config reload) keeps it cacheable.
        self._system = f"{_SYSTEM}\n\nIntents:\n{json.dumps(catalog, indent=1)}"
        self._schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": [i.name for i in intents] + ["none"]},
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                        "required": ["name", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["intent", "slots"],
            "additionalProperties": False,
        }

    async def classify(self, transcript: str) -> LlmChoice | None:
        a = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=1024,
                system=self._system,
                messages=[{"role": "user", "content": transcript}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": self._schema}},
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
            log.info("llm declined to classify transcript")
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("llm returned non-JSON output (stop_reason=%s)", response.stop_reason)
            return None
        intent = data.get("intent")
        if not isinstance(intent, str) or intent == "none":
            return None
        valid = {i.name: set(_slot_names(i)) for i in self._intents}
        if intent not in valid:
            return None
        slots = {
            s["name"]: s["value"] for s in data.get("slots", [])
            if isinstance(s, dict) and s.get("name") in valid[intent] and isinstance(s.get("value"), str)
        }
        return LlmChoice(intent=intent, slots=slots)
