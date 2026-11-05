"""Every YAML block in docs/RECIPES.md must load and validate against the real schema."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from ringhub.config import Config
from ringhub.executor import validate_config

DOC = Path(__file__).resolve().parents[1] / "docs" / "RECIPES.md"
BLOCKS = re.findall(r"```yaml\n(.*?)```", DOC.read_text(), re.DOTALL)


def test_doc_has_blocks() -> None:
    assert len(BLOCKS) >= 8


@pytest.mark.parametrize("block", BLOCKS, ids=[f"block{i}" for i in range(len(BLOCKS))])
def test_recipe_block_validates(block: str, make_config: Callable[..., Config]) -> None:
    fragment: dict[str, Any] = yaml.safe_load(block)
    extra: dict[str, Any] = {
        "devices": {"bedroom_tv": {"type": "roku", "host": "10.0.0.2", "room": "bedroom"}},
        "tesla": {"base_url": "https://localhost:4443", "vin": "5YJ", "client_id": "c", "refresh_token": "r"},
        **fragment,
    }
    validate_config(make_config(extra))
