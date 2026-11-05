"""Load-time validation and run-time execution of step lists."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .actions.base import REGISTRY, ActionError, ActionSpec, Vars
from .config import normalize_steps
from .templating import TemplateRenderError, is_template, render

if TYPE_CHECKING:
    from .config import Config
    from .runtime import Runtime

log = logging.getLogger(__name__)

MAX_DEPTH = 8


class StepError(Exception):
    def __init__(self, where: str, message: str) -> None:
        super().__init__(f"{where}: {message}")
        self.where = where


def _split(step: dict[str, Any]) -> tuple[str, Any]:
    ((name, params),) = step.items()
    return name, params


def _spec(name: str, where: str) -> ActionSpec:
    spec = REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(REGISTRY))
        raise StepError(where, f"unknown action {name!r}. Known actions: {known}")
    return spec


def _preflight(spec: ActionSpec, params: dict[str, Any], where: str) -> None:
    """Validate as much as possible before templates are rendered."""
    model = spec.params
    static = {k: v for k, v in params.items() if k in spec.raw_fields or not is_template(v)}
    allowed = set(model.model_fields) | {f.alias for f in model.model_fields.values() if f.alias}
    unknown = set(params) - allowed
    if unknown:
        raise StepError(where, f"{spec.name}: unknown parameter(s) {sorted(unknown)}")
    if len(static) == len(params):
        try:
            model.model_validate(params)
        except ValidationError as e:
            raise StepError(where, f"{spec.name}: {e}") from None
        return
    missing = [n for n, f in model.model_fields.items()
               if f.is_required() and n not in params and (f.alias or n) not in params]
    if missing:
        raise StepError(where, f"{spec.name}: missing parameter(s) {missing}")


def validate_steps(steps: list[dict[str, Any]], config: Config, where: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise StepError(where, "steps nested too deeply")
    for i, step in enumerate(steps):
        name, raw = _split(step)
        loc = f"{where}[{i}] {name}"
        spec = _spec(name, loc)
        try:
            params = spec.normalize(raw)
        except ActionError as e:
            raise StepError(loc, str(e)) from None
        _preflight(spec, params, loc)
        for f in spec.step_fields:
            nested = params.get(f)
            if nested is not None:
                if not isinstance(nested, list):
                    raise StepError(loc, f"{f!r} must be a list of steps")
                validate_steps(normalize_steps(nested), config, f"{loc}.{f}", depth + 1)
        if name == "run":
            macro = params.get("macro")
            if isinstance(macro, str) and not is_template(macro) and macro not in config.macros:
                raise StepError(loc, f"unknown macro {macro!r}")
        if name == "exec" and not config.allow_exec:
            raise StepError(loc, "exec is disabled; set allow_exec: true to enable it")


def validate_config(config: Config) -> None:
    """Validate every step list in the config. Raises StepError on the first problem."""
    for macro, steps in config.macros.items():
        validate_steps(steps, config, f"macros.{macro}")
    for rule in config.rules:
        validate_steps(rule.do, config, f"rule {rule.name!r}")
    for intent in config.intents:
        validate_steps(intent.do, config, f"intent {intent.name!r}")


class Executor:
    def __init__(self, runtime: Runtime) -> None:
        self._rt = runtime

    async def run(self, steps: list[dict[str, Any]], variables: Vars, *, depth: int = 0) -> Vars:
        """Run steps sequentially; the first failure aborts the list and propagates."""
        if depth > MAX_DEPTH:
            raise ActionError("steps nested too deeply (macro recursion?)")
        for step in normalize_steps(steps):
            name, raw = _split(step)
            spec = _spec(name, name)
            try:
                rendered = render(spec.normalize(raw), variables, skip_keys=spec.raw_fields)
                params = spec.params.model_validate(rendered)
            except (TemplateRenderError, ValidationError) as e:
                raise ActionError(f"{name}: {e}") from None
            variables["_depth"] = depth
            log.debug("run %s %s", name, rendered)
            result = await spec.fn(params, self._rt, variables)
            if result:
                variables.update(result)
        return variables
