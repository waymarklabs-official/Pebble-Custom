from __future__ import annotations

from pathlib import Path

import pytest

from ringhub.config import ConfigError, expand_env, load_config


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


