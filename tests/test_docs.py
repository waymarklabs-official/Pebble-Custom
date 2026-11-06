"""Every JSON block in docs/RECIPES.md must validate against the real catalog models."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from ringhub.catalog import Mode, Skill
from ringhub.config import Config
from ringhub.executor import validate_skill_steps
from ringhub.seed import default_catalog
from ringhub.templates import compile_skill

DOC = Path(__file__).resolve().parents[1] / "docs" / "RECIPES.md"
BLOCKS = re.findall(r"```json\n(.*?)```", DOC.read_text(), re.DOTALL)


def test_doc_has_blocks() -> None:
    assert len(BLOCKS) >= 6


@pytest.mark.parametrize("block", BLOCKS, ids=[f"block{i}" for i in range(len(BLOCKS))])
def test_recipe_block_validates(block: str, make_config: Callable[..., Config]) -> None:
    cfg = make_config({"tesla": {"base_url": "https://localhost:4443", "vin": "5YJYGDEE1MF000001", "client_id": "c",
                                 "refresh_token": "r"}})
    doc = json.loads(block)
    seeded = default_catalog(cfg)
    skill_ids = {s["id"] for s in seeded["skill"]}
    place_ids = {p["id"] for p in seeded["place"]}
    if "bindings" in doc:
        mode = Mode.model_validate(doc)
        assert set(mode.bindings.values()) <= skill_ids, "recipe binds a skill that doesn't exist by default"
        assert set(mode.when.places) <= place_ids
    else:
        skill = Skill.model_validate(doc)
        validate_skill_steps(compile_skill(skill), cfg, f"recipe {skill.id}")
