"""End-to-end: signed HTTP requests in, observable effects (ntfy posts, files, state) out."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ringhub.app import create_app
from ringhub.config import Config

from .conftest import ADMIN, signed_request

AUTH = {"Authorization": f"Bearer {ADMIN}"}

RULES: dict[str, Any] = {
    "macros": {"ping": [{"notify": "macro {{ event.gesture }} in {{ mode }}"}]},
    "rules": [
        {"name": "cycle", "when": {"gesture": "triple"}, "do": ["mode.cycle", {"notify": "mode={{ mode }}"}]},
        {"name": "combo", "when": {"sequence": ["double", "click"]}, "do": [{"notify": "combo!"}]},
        {"name": "tv click", "when": {"gesture": "click", "mode": "tv"}, "do": [{"notify": "tv click"}]},
        {"name": "click", "when": {"gesture": "click"},
         "do": [{"state.increment": "water"}, {"notify": "water {{ count }}"}]},
        {"name": "double", "when": {"gesture": "double"}, "do": [{"run": "ping"}]},
        {"name": "voice", "when": {"gesture": "hold"},
         "do": [{"intent.route": {"fallback": [
             {"notes.append": {"file": "inbox.md", "text": "- {{ event.transcript }}"}},
             {"notify": "saved"},
         ]}}]},
        {"name": "capture", "when": {"gesture": "click-hold"}, "do": [{"http": "https://fail.test/x"}]},
    ],
    "intents": [
        {"name": "sleep", "match": [r"sleep (?:in )?(?P<minutes>\d+)"],
         "do": [{"schedule": {"id": "sleep", "after": "{{ minutes }}m", "do": [{"notify": "zzz"}]}},
                {"notify": "sleep in {{ minutes }}"}]},
    ],
}


@pytest.fixture
def ntfy() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://ntfy.test/ring").mock(return_value=httpx.Response(200))
        mock.post("https://fail.test/x").mock(return_value=httpx.Response(500))
        yield route


@pytest.fixture
def client(make_config: Callable[..., Config]) -> Iterator[TestClient]:
    with TestClient(create_app(make_config(RULES))) as c:
        yield c


def messages(route: respx.Route) -> list[str]:
    return [call.request.content.decode() for call in route.calls]


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def post(client: TestClient, trigger: str, **kw: Any) -> httpx.Response:
    body, headers = signed_request(trigger, **kw)
    return client.post("/ring", content=body, headers=headers)


def test_click_runs_rule_and_counts(client: TestClient, ntfy: respx.Route) -> None:
    assert post(client, "click").json() == {"ok": True}
    assert post(client, "click").status_code == 200
    wait_for(lambda: len(ntfy.calls) == 2)
    assert messages(ntfy) == ["water 1", "water 2"]


def test_duplicate_delivery_is_acked_but_not_rerun(client: TestClient, ntfy: respx.Route) -> None:
    body, headers = signed_request("click", delivery="same-recording")
    assert client.post("/ring", content=body, headers=headers).json() == {"ok": True}
    body, headers = signed_request("click", delivery="same-recording")
    assert client.post("/ring", content=body, headers=headers).json() == {"ok": True, "duplicate": True}
    wait_for(lambda: len(ntfy.calls) == 1)
    time.sleep(0.1)
    assert len(ntfy.calls) == 1


def test_forged_request_rejected(client: TestClient, ntfy: respx.Route) -> None:
    resp = post(client, "click", secret="wrong-secret-0123456789abcdef")
    assert resp.status_code == 401
    assert post(client, "click", secret=None).status_code == 401
    assert not ntfy.calls


def test_mode_cycle_changes_click_meaning(client: TestClient, ntfy: respx.Route) -> None:
    post(client, "triple-click")
    wait_for(lambda: len(ntfy.calls) == 1)
    post(client, "click")
    wait_for(lambda: len(ntfy.calls) == 2)
    assert messages(ntfy) == ["mode=tv", "tv click"]
    assert client.get("/api/state", headers=AUTH).json()["mode"] == "tv"


def test_combo(client: TestClient, ntfy: respx.Route) -> None:
    post(client, "double-click")
    post(client, "click")
    wait_for(lambda: len(ntfy.calls) == 1)
    assert messages(ntfy) == ["combo!"]


def test_double_alone_after_window_runs_macro(client: TestClient, ntfy: respx.Route) -> None:
    post(client, "double-click")
    wait_for(lambda: len(ntfy.calls) == 1)
    assert messages(ntfy) == ["macro double-click in home"]


def test_voice_intent_schedules_persistent_job(client: TestClient, ntfy: respx.Route) -> None:
    post(client, "single-click-hold", transcript="Sleep in forty minutes.")
    wait_for(lambda: len(ntfy.calls) == 1)
    assert messages(ntfy) == ["sleep in 40"]
    jobs = client.get("/api/jobs", headers=AUTH).json()
    assert [j["id"] for j in jobs] == ["sleep"]
    assert 39 * 60 < jobs[0]["run_at"] - time.time() <= 40 * 60
    # Re-arming the same timer replaces it rather than stacking a second one.
    post(client, "single-click-hold", transcript="sleep 5")
    wait_for(lambda: len(ntfy.calls) == 2)
    assert len(client.get("/api/jobs", headers=AUTH).json()) == 1


def test_voice_fallback_captures_to_inbox(client: TestClient, ntfy: respx.Route, tmp_path: Path) -> None:
    post(client, "single-click-hold", transcript="buy more {{ 7*7 }} filters", audio=b"\x00\x01fake-m4a")
    wait_for(lambda: len(ntfy.calls) == 1)
    # Transcripts are data, never templates.
    assert (tmp_path / "notes" / "inbox.md").read_text() == "- buy more {{ 7*7 }} filters\n"
    assert list((tmp_path / "data" / "audio").glob("*.m4a"))


def test_failures_notify_and_are_logged(client: TestClient, ntfy: respx.Route) -> None:
    post(client, "double-click-hold", transcript="anything")
    wait_for(lambda: len(ntfy.calls) == 1)
    assert "failed" in ntfy.calls[0].request.headers["Title"]
    events = client.get("/api/events", headers=AUTH).json()
    assert events[0]["status"] == "error" and "500" in events[0]["error"]


def test_test_event_confirms_loop_without_running_rules(client: TestClient, ntfy: respx.Route) -> None:
    resp = post(client, "test-event", test=True)
    assert resp.json() == {"ok": True, "test": True}
    assert messages(ntfy) == ["Ring -> hub -> ntfy works."]


def test_admin_api_requires_token(client: TestClient, ntfy: respx.Route) -> None:
    assert client.get("/api/events").status_code == 401
    resp = client.post("/api/simulate", json={"gesture": "click"}, headers=AUTH)
    assert resp.status_code == 200
    wait_for(lambda: len(ntfy.calls) == 1)


def test_oversized_body_rejected(client: TestClient) -> None:
    resp = client.post("/ring", content=b"x" * (26 * 1024 * 1024),
                       headers={"X-Index-Webhook-Version": "1"})
    assert resp.status_code == 413


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["ok"] is True
