"""Sandboxed Jinja templating for action parameters.

Templates are rendered exactly once per execution. Transcript text is only ever passed in as
*data*, never parsed as a template, so saying "{{ ... }}" into the ring cannot inject code.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

_env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
_SINGLE_EXPR_RE = re.compile(r"^\{\{(?P<expr>(?:(?!\}\}|\{\{|\{%).)*)\}\}$", re.DOTALL)
_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?")


class TemplateRenderError(Exception):
    pass


def is_template(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, dict):
        return any(is_template(v) for v in value.values())
    if isinstance(value, list):
        return any(is_template(v) for v in value)
    return False


def render(value: Any, variables: dict[str, Any], *, skip_keys: frozenset[str] = frozenset()) -> Any:
    """Recursively render templated strings. Keys in ``skip_keys`` (top level) are left raw.

    A string that is exactly one ``{{ expression }}`` evaluates to the expression's native
    value; anything else (``"{{ n }}m"``, text with several tags) renders to a string.
    """
    if isinstance(value, str):
        if not is_template(value):
            return value
        try:
            single = _SINGLE_EXPR_RE.match(value.strip())
            if single:
                # A lone "{{ expr }}" keeps its native type (bool, int, list...) so that
                # e.g. state.set value "{{ on == 'on' }}" stores a real boolean. Values from
                # variables are returned as-is, never re-parsed, so transcripts stay strings.
                result = _env.compile_expression(single.group("expr"), undefined_to_none=False)(variables)
                if isinstance(result, Undefined):
                    str(result)  # StrictUndefined raises with a helpful message
                return result
            return _env.from_string(value).render(variables)
        except TemplateError as e:
            raise TemplateRenderError(f"template {value!r}: {e}") from None
    if isinstance(value, dict):
        return {k: v if k in skip_keys else render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    return value


def parse_duration(value: str | float | int) -> timedelta:
    """Parse ``90``, ``"90s"``, ``"30m"``, ``"1h30m"`` or ``"1.5h"``."""
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        text = value.strip().lower().replace(" ", "")
        try:
            seconds = float(text)
        except ValueError:
            m = _DURATION_RE.fullmatch(text)
            if not text or not m or not any(m.groups()):
                raise ValueError(f"invalid duration {value!r} (try '45s', '30m', '1h30m')") from None
            h, mi, s = (float(g) if g else 0.0 for g in m.groups())
            seconds = h * 3600 + mi * 60 + s
    if seconds < 0:
        raise ValueError("duration must not be negative")
    return timedelta(seconds=seconds)
