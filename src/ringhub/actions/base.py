"""Action registry.

An action is an async function plus a pydantic model for its parameters. Steps in the config
look like ``- notify: {message: hi}`` or, for actions with a primary field, ``- notify: hi``.

Actions may return a dict; it is merged into the template variables of later steps in the
same run, so ``tv.active_app`` followed by ``notes.append`` can write down what was playing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..runtime import Runtime

Vars = dict[str, Any]
P = TypeVar("P", bound=BaseModel)
ActionFn = Callable[[Any, "Runtime", Vars], Awaitable[Vars | None]]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


class ActionError(Exception):
    """An action failed in an expected way (bad target, device offline, HTTP error...)."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    params: type[BaseModel]
    fn: ActionFn
    primary: str | None = None
    """Field that receives a scalar shorthand, e.g. ``- wait: 5s``."""
    raw_fields: frozenset[str] = field(default_factory=frozenset)
    """Fields left unrendered (nested step lists, rendered later when they run)."""
    step_fields: frozenset[str] = field(default_factory=frozenset)
    """Fields holding nested step lists, validated recursively at load time."""

    def normalize(self, raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if self.primary is None:
            raise ActionError(f"action {self.name!r} needs a mapping of parameters")
        return {self.primary: raw}


REGISTRY: dict[str, ActionSpec] = {}


def action(
    name: str,
    params: type[P],
    *,
    primary: str | None = None,
    steps: tuple[str, ...] = (),
) -> Callable[[Callable[[P, Runtime, Vars], Awaitable[Vars | None]]], ActionFn]:
    def deco(fn: Callable[[P, Runtime, Vars], Awaitable[Vars | None]]) -> ActionFn:
        if name in REGISTRY:
            raise RuntimeError(f"duplicate action {name!r}")
        REGISTRY[name] = ActionSpec(
            name=name, params=params, fn=fn, primary=primary,
            raw_fields=frozenset(steps), step_fields=frozenset(steps),
        )
        return fn

    return deco
