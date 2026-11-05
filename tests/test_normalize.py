from __future__ import annotations

import pytest

from ringhub.intents import normalize


@pytest.mark.parametrize("raw,expected", [
    ("Sleep in thirty-five minutes.", "sleep in 35 minutes"),
    ("Turn it off in half an hour", "turn it off in 30 minutes"),
    ("Remind me at 7:30, please!", "remind me at 7:30 please"),
    ("Volume up twenty", "volume up 20"),
    ("I’m leaving", "i'm leaving"),  # noqa: RUF001 - smart quote is the point
])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected
