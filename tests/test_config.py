from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ringhub.config import Config, ConfigError, expand_env, load_config
from ringhub.executor import StepError, validate_config


def test_env_expansion() -> None:
    env = {"A": "1"}
    assert expand_env({"x": ["${A}", "${B:-two}"]}, env) == {"x": ["1", "two"]}
    with pytest.raises(ConfigError, match="MISSING"):
        expand_env("${MISSING}", env)


def test_signing_required_unless_opted_out(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("ring: {}\n")
    with pytest.raises(ConfigError, match="signing secret"):
        load_config(p)
    p.write_text("ring: {allow_unsigned: true}\n")
    assert load_config(p).ring.allow_unsigned


def test_short_secret_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("ring: {secrets: {default: short}}\n")
    with pytest.raises(ConfigError, match="too short"):
        load_config(p)


@pytest.mark.parametrize("rules,error", [
    ([{"name": "r", "do": [{"nope": {}}]}], "unknown action 'nope'"),
    ([{"name": "r", "do": [{"notify": {"msg": "x"}}]}], "unknown parameter"),
    ([{"name": "r", "do": [{"tv": "explode"}]}], "tv"),
    ([{"name": "r", "do": [{"run": "missing"}]}], "unknown macro"),
    ([{"name": "r", "do": [{"exec": {"argv": ["ls"]}}]}], "allow_exec"),
    ([{"name": "r", "do": [{"schedule": {"after": "5m", "do": [{"bogus": {}}]}}]}], "bogus"),
    ([{"name": "r", "do": [{"wait": "2h"}]}], "60s"),
])
def test_step_validation_fails_fast(make_config: Callable[..., Config], rules: list[dict[str, Any]],
                                    error: str) -> None:
    cfg = make_config(rules=rules)
    with pytest.raises(StepError, match=error):
        validate_config(cfg)


def test_templated_params_validated_for_shape_only(make_config: Callable[..., Config]) -> None:
    cfg = make_config(rules=[{"name": "r", "do": [{"tv": {"command": "volume_up", "repeat": "{{ n }}"}}]}])
    validate_config(cfg)


def test_unknown_mode_in_rule(make_config: Callable[..., Config]) -> None:
    with pytest.raises(ConfigError, match="unknown mode"):
        make_config(rules=[{"name": "r", "when": {"mode": "party"}, "do": ["noop"]}])


def test_yaml_sexagesimal_time_is_handled(tmp_path: Path) -> None:
    # Unquoted 22:00 is the integer 1320 in YAML 1.1.
    p = tmp_path / "c.yaml"
    p.write_text("ring: {allow_unsigned: true}\nrules:\n- name: r\n  when: {time: {after: 22:00, before: 06:30}}\n"
                 "  do: [noop]\n")
    w = load_config(p).rules[0].when.time
    assert w is not None and (w.after.hour, w.before.hour, w.before.minute) == (22, 6, 30)


def test_example_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    for var in ("RINGHUB_SECRET", "NTFY_TOKEN", "RINGHUB_ADMIN_TOKEN"):
        monkeypatch.setenv(var, "x" * 40)
    validate_config(load_config(root / "config.example.yaml"))
