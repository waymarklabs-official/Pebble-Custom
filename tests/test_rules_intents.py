from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ringhub.config import IntentConfig, When
from ringhub.intents import IntentRouter, normalize
from ringhub.models import Gesture, RingEvent
from ringhub.rules import matches

MON_23H = datetime(2026, 11, 2, 23, 30)  # a Monday


def ev(*seq: Gesture, transcript: str | None = None, payload: Any = None) -> RingEvent:
    now = datetime.now(UTC)
    return RingEvent(delivery_id="x", gesture=seq[-1], received_at=now, occurred_at=now,
                     transcript=transcript, payload=payload, sequence=tuple(seq))


def m(when: dict[str, Any], event: RingEvent, mode: str = "home", state: dict[str, Any] | None = None,
      now: datetime = MON_23H) -> bool:
    return matches(When.model_validate(when), event, mode=mode, state=state or {}, now=now)


def test_gesture_aliases_and_lists() -> None:
    assert m({"gesture": "hold"}, ev(Gesture.HOLD))
    assert m({"gesture": ["click", "double"]}, ev(Gesture.DOUBLE_CLICK))
    assert not m({"gesture": "click"}, ev(Gesture.DOUBLE_CLICK))


def test_combo_only_matches_sequence_rules() -> None:
    combo = ev(Gesture.DOUBLE_CLICK, Gesture.CLICK)
    assert m({"sequence": ["double", "click"]}, combo)
    assert not m({"gesture": "click"}, combo)
    assert not m({}, combo)


def test_mode_state_payload_transcript() -> None:
    assert m({"mode": "tv"}, ev(Gesture.CLICK), mode="tv")
    assert not m({"mode": "tv"}, ev(Gesture.CLICK), mode="home")
    assert m({"state": {"guest": True}}, ev(Gesture.CLICK), state={"guest": True})
    assert not m({"state": {"guest": True}}, ev(Gesture.CLICK))
    assert m({"payload": "lamp"}, ev(Gesture.CLICK, payload="lamp"))
    assert m({"transcript": "^remind"}, ev(Gesture.HOLD, transcript="Remind me to stretch"))


def test_time_window_wrapping_midnight_and_days() -> None:
    night = {"time": {"after": "22:00", "before": "06:00"}}
    assert m(night, ev(Gesture.CLICK))
    assert not m(night, ev(Gesture.CLICK), now=datetime(2026, 11, 2, 12, 0))
    assert m({"days": ["mon"]}, ev(Gesture.CLICK))
    assert not m({"days": ["sat", "sun"]}, ev(Gesture.CLICK))


def test_when_validation() -> None:
    with pytest.raises(ValueError, match="either"):
        When.model_validate({"gesture": "click", "sequence": ["click", "double"]})
    with pytest.raises(ValueError, match="voice"):
        When.model_validate({"sequence": ["click", "hold"]})
    with pytest.raises(ValueError, match="at least two"):
        When.model_validate({"sequence": ["click"]})


@pytest.mark.parametrize("raw,expected", [
    ("Sleep in thirty-five minutes.", "sleep in 35 minutes"),
    ("Turn it off in half an hour", "turn it off in 30 minutes"),
    ("Remind me at 7:30, please!", "remind me at 7:30 please"),
    ("Volume up twenty", "volume up 20"),
    ("I’m leaving", "i'm leaving"),  # noqa: RUF001 - smart quote is the point
])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_router_extracts_named_groups_first_match_wins() -> None:
    router = IntentRouter([
        IntentConfig(name="sleep", match=[r"sleep (?:in )?(?P<minutes>\d+)"], do=[{"noop": {}}]),
        IntentConfig(name="any", match=[r".*"], do=[{"noop": {}}]),
    ])
    hit = router.match("Sleep in forty five minutes")
    assert hit is not None and hit.intent.name == "sleep" and hit.groups == {"minutes": "45"}
    other = router.match("what")
    assert other is not None and other.intent.name == "any"
