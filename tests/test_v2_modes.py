"""modes.resolve() and binding fallthrough: pure functions over catalog + context + clock."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ringhub.catalog import Mode
from ringhub.modes import binding, resolve

TZ = ZoneInfo("America/Chicago")
# Wednesday 2026-11-04 14:00 local
WED_1400 = datetime(2026, 11, 4, 14, 0, tzinfo=TZ)


def mode(mid: str, when: dict[str, Any] | None = None, bindings: dict[str, str] | None = None,
         **kw: Any) -> Mode:
    return Mode.model_validate({"id": mid, "name": mid.title(), "when": when or {}, "bindings": bindings or {},
                                **kw})


EVERYWHERE = mode("everywhere", bindings={"click": "moment", "double": "list", "triple": "where_am_i",
                                          "hold": "capture", "click-hold": "journal"})


def pick(modes: list[Mode], ctx: dict[str, Any], now: datetime = WED_1400,
         override: dict[str, Any] | None = None) -> tuple[str, str, bool]:
    r = resolve([*modes, EVERYWHERE], ctx, now, override)
    return r.mode.id, r.reason, r.manual


# ----------------------------------------------------------------------------- conditions


def test_places_condition() -> None:
    m = mode("work", {"places": ["work", "office2"]})
    assert pick([m], {"place": "work"}) == ("work", "at work", False)
    assert pick([m], {"place": "office2"})[0] == "work"
    assert pick([m], {"place": "home"})[0] == "everywhere"
    assert pick([m], {"place": None})[0] == "everywhere"


def test_not_places_condition() -> None:
    m = mode("away", {"not_places": ["home"]})
    assert pick([m], {"place": "work"})[0] == "away"
    assert pick([m], {"place": None})[0] == "away"
    assert pick([m], {"place": "home"})[0] == "everywhere"


def test_focus_condition_case_insensitive() -> None:
    m = mode("sleep", {"focus": ["Sleep", " DND "]})
    assert m.when.focus == ["sleep", "dnd"]
    assert pick([m], {"focus": "Sleep"}) == ("sleep", "sleep focus", False)
    assert pick([m], {"focus": "dnd"})[0] == "sleep"
    assert pick([m], {"focus": None})[0] == "everywhere"
    assert pick([m], {"focus": "work"})[0] == "everywhere"


@pytest.mark.parametrize("hhmm,expected", [
    ((21, 59), "everywhere"), ((22, 0), "night"), ((23, 59), "night"), ((0, 0), "night"), ((5, 59), "night"),
    ((6, 0), "everywhere"), ((12, 0), "everywhere"),
])
def test_time_window_wraps_midnight(hhmm: tuple[int, int], expected: str) -> None:
    m = mode("night", {"time": {"after": "22:00", "before": "06:00"}})
    now = WED_1400.replace(hour=hhmm[0], minute=hhmm[1])
    assert pick([m], {}, now)[0] == expected


def test_time_window_same_day_and_reason() -> None:
    m = mode("lunch", {"time": {"after": "12:00", "before": "14:00"}})
    assert pick([m], {}, WED_1400.replace(hour=12))[1] == "12:00-14:00"
    assert pick([m], {}, WED_1400)[0] == "everywhere"  # 'before' is exclusive


def test_days_condition() -> None:
    m = mode("weekday", {"days": ["mon", "tue", "wed", "thu", "fri"]})
    assert pick([m], {}, WED_1400)[0] == "weekday"
    assert pick([m], {}, WED_1400.replace(day=7))[0] == "everywhere"  # Saturday
    assert pick([m], {}, WED_1400.replace(day=8))[0] == "everywhere"  # Sunday


def test_days_reject_unknown_names() -> None:
    with pytest.raises(ValueError):
        mode("bad", {"days": ["monday"]})


def test_context_condition_is_equality() -> None:
    m = mode("guests", {"context": {"guests": True, "tv": "on"}})
    assert pick([m], {"guests": True, "tv": "on"}) == ("guests", "guests=True, tv=on", False)
    assert pick([m], {"guests": True})[0] == "everywhere"
    assert pick([m], {"guests": "true", "tv": "on"})[0] == "everywhere"  # no truthiness coercion


def test_recently_left() -> None:
    m = mode("leaving", {"recently": {"event": "leave", "place": "home", "within_minutes": 10}})
    now = WED_1400.timestamp()
    assert pick([m], {"last_left": {"place": "home", "at": now - 300}})[0:2] == ("leaving", "left home recently")
    assert pick([m], {"last_left": {"place": "home", "at": now - 601}})[0] == "everywhere"
    assert pick([m], {"last_left": {"place": "work", "at": now - 60}})[0] == "everywhere"
    assert pick([m], {})[0] == "everywhere"


def test_recently_arrived() -> None:
    m = mode("just_home", {"recently": {"event": "arrive", "place": "home", "within_minutes": 15}})
    now = WED_1400.timestamp()
    assert pick([m], {"place": "home", "place_since": now - 60})[1] == "arrived at home recently"
    assert pick([m], {"place": "home", "place_since": now - 16 * 60})[0] == "everywhere"
    assert pick([m], {"place": "work", "place_since": now - 60})[0] == "everywhere"
    assert pick([m], {"place": "home"})[0] == "everywhere"  # unknown since


def test_all_conditions_must_hold_and_reasons_join() -> None:
    m = mode("morning", {"places": ["home"], "time": {"after": "05:00", "before": "10:30"}, "days": ["wed"]})
    at_8 = WED_1400.replace(hour=8)
    assert pick([m], {"place": "home"}, at_8) == ("morning", "at home, 05:00-10:30, wed", False)
    assert pick([m], {"place": "home"}, WED_1400)[0] == "everywhere"
    assert pick([m], {"place": "work"}, at_8)[0] == "everywhere"


def test_empty_when_always_matches() -> None:
    assert pick([mode("always")], {}) == ("always", "always", False)


# ----------------------------------------------------------------------------- priority & fallback


def test_first_matching_mode_in_priority_order_wins() -> None:
    a = mode("sleep", {"focus": ["sleep"]})
    b = mode("home", {"places": ["home"]})
    ctx = {"place": "home", "focus": "sleep"}
    assert pick([a, b], ctx)[0] == "sleep"
    assert pick([b, a], ctx)[0] == "home"


def test_disabled_modes_are_skipped() -> None:
    off = mode("home", {"places": ["home"]}, enabled=False)
    assert pick([off], {"place": "home"}) == ("everywhere", "no other mode applies", False)


def test_fallback_is_never_matched_early_even_if_listed_first() -> None:
    work = mode("work", {"places": ["work"]})
    r = resolve([EVERYWHERE, work], {"place": "work"}, WED_1400)
    assert r.mode.id == "work"


def test_missing_fallback_is_an_error() -> None:
    with pytest.raises(LookupError):
        resolve([mode("work", {"places": ["work"]})], {}, WED_1400)


# ----------------------------------------------------------------------------- manual override


def test_override_wins_until_it_expires() -> None:
    work = mode("work", {"places": ["work"]})
    sleep = mode("sleep", {"focus": ["sleep"]})
    ov = {"mode": "sleep", "place": "work", "expires_at": WED_1400.timestamp() + 60}
    assert pick([work, sleep], {"place": "work"}, override=ov) == ("sleep", "set by hand", True)
    expired = {**ov, "expires_at": WED_1400.timestamp() - 1}
    assert pick([work, sleep], {"place": "work"}, override=expired)[0] == "work"
    forever = {**ov, "expires_at": None}
    assert pick([work, sleep], {"place": "work"}, override=forever)[2] is True


def test_override_ends_when_place_changes() -> None:
    work = mode("work", {"places": ["work"]})
    sleep = mode("sleep", {"focus": ["sleep"]})
    ov = {"mode": "sleep", "place": "home", "expires_at": None}
    assert pick([work, sleep], {"place": "home"}, override=ov)[0] == "sleep"
    assert pick([work, sleep], {"place": "work"}, override=ov)[0] == "work"
    nowhere = {"mode": "sleep", "place": None, "expires_at": None}
    assert pick([work, sleep], {"place": "work"}, override=nowhere)[0] == "work"
    assert pick([work, sleep], {"place": None}, override=nowhere)[0] == "sleep"


def test_override_ignored_for_disabled_or_unknown_mode() -> None:
    off = mode("sleep", {"focus": ["sleep"]}, enabled=False)
    assert pick([off], {}, override={"mode": "sleep", "expires_at": None})[0] == "everywhere"
    assert pick([], {}, override={"mode": "ghost", "expires_at": None})[0] == "everywhere"
    assert pick([], {}, override={})[0] == "everywhere"


def test_override_to_fallback_is_allowed() -> None:
    work = mode("work", {"places": ["work"]})
    r = resolve([work, EVERYWHERE], {"place": "work"}, WED_1400, {"mode": "everywhere", "place": "work"})
    assert (r.mode.id, r.manual) == ("everywhere", True)


# ----------------------------------------------------------------------------- bindings


def test_binding_falls_through_to_everywhere() -> None:
    work = mode("work", {"places": ["work"]}, bindings={"click": "hand_raise"})
    modes = [work, EVERYWHERE]
    assert binding(modes, work, "click") == ("hand_raise", work)
    assert binding(modes, work, "double-click") == ("list", EVERYWHERE)
    assert binding(modes, work, "single-click-hold") == ("capture", EVERYWHERE)
    assert binding(modes, work, "double-click+click") == (None, None)
    assert binding(modes, EVERYWHERE, "click") == ("moment", EVERYWHERE)


def test_binding_without_fallback_mode() -> None:
    work = mode("work", bindings={"click": "x"})
    assert binding([work], work, "double-click") == (None, None)


def test_mode_can_block_a_gesture_with_nothing() -> None:
    sleep = mode("sleep", {"focus": ["sleep"]}, bindings={"double": "nothing"})
    assert binding([sleep, EVERYWHERE], sleep, "double-click") == ("nothing", sleep)
