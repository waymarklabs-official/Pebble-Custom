"""Integrations over HTTP: status, Connect (OAuth state), the callback, car selection, registration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from ringhub.config import Config
from ringhub.oauth import STATE_TTL_SECONDS, new_state, take_state

from .conftest import ADMIN_H, TESLA_AUTH, TESLA_CONFIG, TESLA_VIN, NtfyMock

API = "/api/v1"
GOOGLE = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "gsecret"}
TESLA = {**TESLA_CONFIG["tesla"], "client_secret": "tsecret", "domain": "me.github.io", "refresh_token": None,
         "vin": None}


@pytest.fixture
def client(make_client: Callable[..., TestClient], make_config: Callable[..., Config]) -> TestClient:
    return make_client(make_config({"tesla": TESLA, "google": GOOGLE}), follow_redirects=False)


def services(c: TestClient) -> Any:
    return c.app.state.services  # type: ignore[attr-defined]


def connect(c: TestClient, provider: str) -> tuple[str, str]:
    r = c.post(f"{API}/integrations/{provider}/connect", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    q = parse_qs(urlsplit(url).query)
    assert q["redirect_uri"] == [f"https://hub.test/oauth/{provider}/callback"]
    return url, q["state"][0]


def test_status_before_connecting(client: TestClient) -> None:
    data = client.get(f"{API}/integrations", headers=ADMIN_H).json()
    t, g = data["tesla"], data["google"]
    assert t["enabled"] and not t["connected"] and t["vin"] is None
    assert t["pairing_link"] == "https://tesla.com/_ak/me.github.io"
    assert t["public_key_url"] == "https://me.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem"
    assert t["redirect_uri"] == "https://hub.test/oauth/tesla/callback" and t["has_secret"]
    assert g == {"enabled": True, "connected": False, "account": None, "calendar_id": "primary",
                 "redirect_uri": "https://hub.test/oauth/google/callback"}


def test_integration_endpoints_need_the_admin(client: TestClient) -> None:
    assert client.get(f"{API}/integrations").status_code == 401
    assert client.post(f"{API}/integrations/tesla/connect").status_code == 401
    assert client.post(f"{API}/integrations/tesla/register").status_code == 401


def test_disabled_integration_says_what_to_set(make_client: Callable[..., TestClient],
                                               make_config: Callable[..., Config]) -> None:
    c = make_client(make_config())
    assert c.get(f"{API}/integrations", headers=ADMIN_H).json()["tesla"] == {"enabled": False}
    r = c.post(f"{API}/integrations/google/connect", headers=ADMIN_H)
    assert r.status_code == 409 and "GOOGLE_CLIENT_ID" in r.json()["detail"]


def test_google_connect_round_trip(client: TestClient, ntfy: NtfyMock) -> None:
    token = ntfy.router.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600}))
    _, state = connect(client, "google")
    r = client.get("/oauth/google/callback", params={"code": "4/abc", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/#/setup?connected=google"
    assert parse_qs(token.calls.last.request.content.decode())["redirect_uri"] == [
        "https://hub.test/oauth/google/callback"]
    assert client.get(f"{API}/integrations", headers=ADMIN_H).json()["google"]["connected"] is True
    # the state was single-use
    again = client.get("/oauth/google/callback", params={"code": "4/abc", "state": state})
    assert again.status_code == 400 and "expired or was already used" in again.text


def test_tesla_connect_picks_the_only_car_and_adds_car_skills(client: TestClient, ntfy: NtfyMock) -> None:
    ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600}))
    ntfy.router.get("https://tesla.test/api/1/vehicles").mock(return_value=httpx.Response(200, json={
        "response": [{"vin": TESLA_VIN, "display_name": "Blue"}]}))
    _, state = connect(client, "tesla")
    r = client.get("/oauth/tesla/callback", params={"code": "NA_abc", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/#/setup?connected=tesla"
    status = client.get(f"{API}/integrations", headers=ADMIN_H).json()["tesla"]
    assert status["connected"] and status["vin"] == TESLA_VIN and status["region"] == "na"
    skills = {s["id"] for s in client.get(f"{API}/skills", headers=ADMIN_H).json()}
    assert {"car_lock", "car_status", "car_garage", "car_temp"} <= skills


@pytest.mark.parametrize("params", [
    {"code": "x", "state": "made-up"},
    {"code": "x"},
    {"code": "x", "state": "a" * 200},
])
def test_callback_refuses_without_a_valid_state(client: TestClient, params: dict[str, str]) -> None:
    r = client.get("/oauth/google/callback", params=params)
    assert r.status_code == 400
    assert not services(client).runtime.gcal._access


def test_callback_state_is_bound_to_its_provider(client: TestClient) -> None:
    _, state = connect(client, "google")
    assert client.get("/oauth/tesla/callback", params={"code": "NA_x", "state": state}).status_code == 400


def test_callback_shows_provider_errors_escaped(client: TestClient) -> None:
    _, state = connect(client, "google")
    r = client.get("/oauth/google/callback", params={"state": state, "error": "access_denied",
                                                      "error_description": "<script>x</script>"})
    assert r.status_code == 400 and "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_callback_exchange_failure_is_shown(client: TestClient, ntfy: NtfyMock) -> None:
    ntfy.router.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(
        400, json={"error": "invalid_grant"}))
    _, state = connect(client, "google")
    r = client.get("/oauth/google/callback", params={"code": "bad", "state": state})
    assert r.status_code == 502 and "Not connected" in r.text


def test_unknown_provider_callback(client: TestClient) -> None:
    assert client.get("/oauth/github/callback", params={"code": "x", "state": "y"}).status_code == 404


def test_connect_needs_a_public_url(make_client: Callable[..., TestClient], make_config: Callable[..., Config]) -> None:
    cfg = make_config({"google": GOOGLE})
    c = make_client(cfg.model_copy(update={"server": cfg.server.model_copy(update={"public_url": None})}))
    r = c.post(f"{API}/integrations/google/connect", headers=ADMIN_H)
    assert r.status_code == 409 and "public_url" in r.json()["detail"]


def test_pick_vehicle_and_disconnect(client: TestClient, ntfy: NtfyMock) -> None:
    svc = services(client)
    client.portal.call(svc.store.set, "tesla.refresh_token", "r0")  # type: ignore[union-attr]
    ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r1", "expires_in": 3600}))
    ntfy.router.get("https://tesla.test/api/1/vehicles").mock(return_value=httpx.Response(200, json={
        "response": [{"vin": TESLA_VIN, "display_name": "Blue"}, {"vin": "5YJ3E1EA7KF000002", "display_name": "Red"}]}))
    cars = client.get(f"{API}/integrations/tesla/vehicles", headers=ADMIN_H).json()
    assert [c["name"] for c in cars] == ["Blue", "Red"]
    assert client.post(f"{API}/integrations/tesla/vehicle", json={"vin": "5YJ3E1EA7KF000002"},
                       headers=ADMIN_H).json() == {"vin": "5YJ3E1EA7KF000002"}
    assert client.post(f"{API}/integrations/tesla/vehicle", json={"vin": "not-a-vin"},
                       headers=ADMIN_H).status_code == 422
    assert client.post(f"{API}/integrations/tesla/disconnect", headers=ADMIN_H).json() == {"ok": True}
    status = client.get(f"{API}/integrations", headers=ADMIN_H).json()["tesla"]
    assert not status["connected"] and status["vin"] is None


def test_register_errors_are_reported(client: TestClient, ntfy: NtfyMock) -> None:
    ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    r = client.post(f"{API}/integrations/tesla/register", headers=ADMIN_H)
    assert r.status_code == 502 and "partner token" in r.json()["detail"]


async def test_oauth_state_expiry(make_runtime: Callable[..., Any]) -> None:
    rt = await make_runtime()
    now = time.time()
    s = await new_state(rt.store, "google", now=now)
    assert not await take_state(rt.store, s, "google", now=now + STATE_TTL_SECONDS + 1)
    assert not await take_state(rt.store, s, "google", now=now)          # consumed by the attempt above
    s2 = await new_state(rt.store, "google", now=now)
    assert await take_state(rt.store, s2, "google", now=now + 5)
