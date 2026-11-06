from __future__ import annotations

from datetime import timedelta

import pytest

from ringhub.templating import TemplateRenderError, parse_duration, render


def test_single_expression_keeps_native_type() -> None:
    v = {"on": "on", "n": 3, "items": [1, 2]}
    assert render("{{ on == 'on' }}", v) is True
    assert render("{{ n + 1 }}", v) == 4
    assert render("{{ items }}", v) == [1, 2]
    assert render("{{ missing | default(3) }}", v) == 3


def test_mixed_templates_render_to_strings() -> None:
    assert render("{{ n }}m", {"n": 5}) == "5m"
    assert render("{{ a }} and {{ b }}", {"a": 1, "b": 2}) == "1 and 2"


def test_variable_content_is_never_reparsed() -> None:
    # A transcript that looks like a literal or a template stays an inert string.
    for evil in ("123", "[1, 2]", "None", "{{ 7*7 }}", "{% for x in range(9**9) %}{% endfor %}"):
        assert render("{{ t }}", {"t": evil}) == evil
        assert render("said: {{ t }}", {"t": evil}) == f"said: {evil}"


def test_undefined_is_an_error() -> None:
    with pytest.raises(TemplateRenderError):
        render("{{ nope }}", {})
    with pytest.raises(TemplateRenderError):
        render("x {{ nope }}", {})


def test_sandbox_blocks_internals() -> None:
    with pytest.raises(TemplateRenderError):
        render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_skip_keys_left_raw() -> None:
    out = render({"do": [{"notify": "{{ x }}"}], "after": "{{ x }}m"}, {"x": 1}, skip_keys=frozenset({"do"}))
    assert out == {"do": [{"notify": "{{ x }}"}], "after": "1m"}


@pytest.mark.parametrize("raw,seconds", [(90, 90), ("90", 90), ("45s", 45), ("30m", 1800), ("1h30m", 5400),
                                         ("1.5h", 5400), ("0m", 0)])
def test_parse_duration(raw: str | int, seconds: float) -> None:
    assert parse_duration(raw) == timedelta(seconds=seconds)


@pytest.mark.parametrize("raw", ["", "soon", "5x", "-5"])
def test_parse_duration_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(raw)
