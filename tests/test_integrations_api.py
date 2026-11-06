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
from ringhub.oauth import (
    STATE_TTL_SECONDS,
    bridge_page,
    code_and_state,
    new_state,
    redirect_uri_for,
    take_state,
    tesla_url_problems,
)

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


REDIRECT = {"tesla": "https://me.github.io/callback.html", "google": "https://hub.test/oauth/callback"}


def connect(c: TestClient, provider: str) -> tuple[str, str]:
    r = c.post(f"{API}/integrations/{provider}/connect", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    q = parse_qs(urlsplit(url).query)
    assert q["redirect_uri"] == [REDIRECT[provider]]
    return url, q["state"][0]


def test_status_before_connecting(client: TestClient) -> None:
    data = client.get(f"{API}/integrations", headers=ADMIN_H).json()
    t, g = data["tesla"], data["google"]
    assert t["enabled"] and not t["connected"] and t["vin"] is None
    assert t["pairing_link"] == "https://tesla.com/_ak/me.github.io"
    assert t["public_key_url"] == "https://me.github.io/.well-known/appspecific/com.tesla.3p.public-key.pem"
    assert t["redirect_uri"] == "https://me.github.io/callback.html" and t["has_secret"]
    assert t["redirect_problems"] == [] and t["domain_problems"] == []
    assert 'location.replace("https://hub.test/oauth/callback" + q)' in t["bridge_html"]
    assert g == {"enabled": True, "connected": False, "account": None, "calendar_id": "primary",
                 "redirect_uri": "https://hub.test/oauth/callback"}


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
    r = client.get("/oauth/callback", params={"code": "4/abc", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "/#/setup?connected=google"
    assert parse_qs(token.calls.last.request.content.decode())["redirect_uri"] == ["https://hub.test/oauth/callback"]
    assert client.get(f"{API}/integrations", headers=ADMIN_H).json()["google"]["connected"] is True
    # the state was single-use
    again = client.get("/oauth/callback", params={"code": "4/abc", "state": state})
    assert again.status_code == 400 and "expired or was already used" in again.text


def test_tesla_connect_picks_the_only_car_and_adds_car_skills(client: TestClient, ntfy: NtfyMock) -> None:
    ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600}))
    ntfy.router.get("https://tesla.test/api/1/vehicles").mock(return_value=httpx.Response(200, json={
        "response": [{"vin": TESLA_VIN, "display_name": "Blue"}]}))
    _, state = connect(client, "tesla")
    # Tesla sends the browser to the page on your domain, which forwards the query here unchanged.
    r = client.get("/oauth/callback", params={"code": "NA_abc", "state": state})
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
    r = client.get("/oauth/callback", params=params)
    assert r.status_code == 400
    assert not services(client).runtime.gcal._access


async def test_state_names_its_provider(make_runtime: Callable[..., Any]) -> None:
    rt = await make_runtime()
    assert await take_state(rt.store, await new_state(rt.store, "google")) == "google"
    assert await take_state(rt.store, await new_state(rt.store, "tesla")) == "tesla"
    assert await take_state(rt.store, await new_state(rt.store, "github")) is None   # not a provider


def test_callback_shows_provider_errors_escaped(client: TestClient) -> None:
    _, state = connect(client, "google")
    r = client.get("/oauth/callback", params={"state": state, "error": "access_denied",
                                                      "error_description": "<script>x</script>"})
    assert r.status_code == 400 and "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_callback_exchange_failure_is_shown(client: TestClient, ntfy: NtfyMock) -> None:
    ntfy.router.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(
        400, json={"error": "invalid_grant"}))
    _, state = connect(client, "google")
    r = client.get("/oauth/callback", params={"code": "bad", "state": state})
    assert r.status_code == 502 and "Not connected" in r.text


def test_old_provider_named_paths_are_gone(client: TestClient) -> None:
    # Tesla rejects redirect URIs containing "tesla": the callback path is neutral.
    assert client.get("/oauth/tesla/callback", params={"code": "x", "state": "y"}).status_code == 404


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
    assert await take_state(rt.store, s, now=now + STATE_TTL_SECONDS + 1) is None
    assert await take_state(rt.store, s, now=now) is None             # consumed by the attempt above
    s2 = await new_state(rt.store, "google", now=now)
    assert await take_state(rt.store, s2, now=now + 5) == "google"


# ----------------------------------------------------------------------------- Tesla's URL rules, forwarding, paste


def test_tesla_redirect_defaults_to_the_public_key_domain(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"tesla": TESLA})
    assert cfg.tesla is not None
    assert redirect_uri_for("tesla", cfg.tesla, "https://hub.test") == "https://me.github.io/callback.html"
    no_domain = cfg.tesla.model_copy(update={"domain": None})
    assert redirect_uri_for("tesla", no_domain, "https://hub.test") == "https://hub.test/oauth/callback"
    explicit = cfg.tesla.model_copy(update={"redirect_uri": "https://example.net/cb"})
    assert redirect_uri_for("tesla", explicit, "https://hub.test") == "https://example.net/cb"


@pytest.mark.parametrize(("url", "expect"), [
    ("https://me.github.io/callback.html", []),
    ("https://tesla-stream.tail51b89d.ts.net:8445/oauth/callback", ["contain “tesla”", "Tailscale address"]),
    ("https://me.github.io/tesla/callback.html", ["contain “tesla”"]),
    ("https://hub.test/oauth/callback", ["Tailscale address"]),
])
def test_tesla_url_problems(url: str, expect: list[str]) -> None:
    problems = tesla_url_problems(url, "https://hub.test" if "hub.test" in url else
                                  "https://tesla-stream.tail51b89d.ts.net:8445")
    assert len(problems) == len(expect) and all(e in p for e, p in zip(expect, problems, strict=True))


def test_status_flags_a_redirect_tesla_would_reject(make_client: Callable[..., TestClient],
                                                    make_config: Callable[..., Config]) -> None:
    cfg = make_config({"tesla": {**TESLA, "domain": "tesla-key.example.com",
                                 "redirect_uri": "https://hub.test/oauth/callback"}})
    t = make_client(cfg).get(f"{API}/integrations", headers=ADMIN_H).json()["tesla"]
    assert any("Tailscale address" in p for p in t["redirect_problems"])
    assert t["domain_problems"] == ["Tesla rejects domains that contain “tesla”."]
    assert t["bridge_html"] is None                 # the redirect is ringhub itself: no page to host


def test_bridge_page_forwards_only_to_ringhub() -> None:
    page = bridge_page('https://hub.test/oauth/callback"</script><script>alert(1)//')
    assert page.count("<script>") == 1 and "\\u003c/script\\u003e" in page   # the target cannot close the script
    assert "location.replace(" in page and "location.search" in page and "no-referrer" in page
    assert "searchParams" not in page and "location.hash" not in page  # destination never read from the URL


@pytest.mark.parametrize(("pasted", "expect"), [
    ("https://me.github.io/callback.html?code=NA_abc&state=s1", ("NA_abc", "s1", "", "")),
    ("?code=c&state=s", ("c", "s", "", "")),
    ("code=c&state=s", ("c", "s", "", "")),
    ("https://me.github.io/callback.html?error=access_denied&error_description=no&state=s",
     ("", "s", "access_denied", "no")),
    ("  https://x/?state=s&code=c  ", ("c", "s", "", "")),
])
def test_code_and_state(pasted: str, expect: tuple[str, str, str, str]) -> None:
    assert code_and_state(pasted) == expect


def test_finish_by_pasting_the_address(client: TestClient, ntfy: NtfyMock) -> None:
    token = ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600}))
    ntfy.router.get("https://tesla.test/api/1/vehicles").mock(return_value=httpx.Response(200, json={
        "response": [{"vin": TESLA_VIN}]}))
    _, state = connect(client, "tesla")
    pasted = f"https://me.github.io/callback.html?locale=en-US&code=NA_xyz&state={state}&issuer=x"
    r = client.post(f"{API}/integrations/finish", json={"url": pasted}, headers=ADMIN_H)
    assert r.json() == {"provider": "tesla", "connected": True}
    sent = parse_qs(token.calls.last.request.content.decode())
    assert sent["code"] == ["NA_xyz"] and sent["redirect_uri"] == ["https://me.github.io/callback.html"]
    again = client.post(f"{API}/integrations/finish", json={"url": pasted}, headers=ADMIN_H)
    assert again.status_code == 400 and "expired or was already used" in again.json()["detail"]


@pytest.mark.parametrize(("pasted", "status", "message"), [
    ("https://me.github.io/callback.html", 400, "no code and state"),
    ("https://me.github.io/callback.html?error=access_denied&state=x", 400, "access_denied"),
    ("https://me.github.io/callback.html?code=c&state=forged", 400, "expired or was already used"),
])
def test_finish_refuses_bad_pastes(client: TestClient, pasted: str, status: int, message: str) -> None:
    r = client.post(f"{API}/integrations/finish", json={"url": pasted}, headers=ADMIN_H)
    assert r.status_code == status and message in r.json()["detail"]


def test_finish_needs_the_admin(client: TestClient) -> None:
    assert client.post(f"{API}/integrations/finish", json={"url": "x"}).status_code == 401


def test_check_command_warns_about_tesla_redirects(make_config: Callable[..., Config], capsys: Any) -> None:
    import yaml

    from ringhub.__main__ import main

    cfg = make_config({"tesla": {**TESLA, "domain": None, "redirect_uri": None}},
                      server={"data_dir": "/tmp/x", "admin_token": "a" * 24, "context_token": "c" * 24,
                              "public_url": "https://tesla-stream.tail51b89d.ts.net:8445"})
    path = cfg.server.data_dir.parent / "check.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "server": {"public_url": "https://tesla-stream.tail51b89d.ts.net:8445", "admin_token": "a" * 24},
        "ring": {"secrets": {"default": "s" * 32}}, "tesla": {"client_id": "tid"}}))
    assert main(["check", "-c", str(path)]) == 0
    out = capsys.readouterr().out
    assert "tesla sign-in returns to: https://tesla-stream.tail51b89d.ts.net:8445/oauth/callback" in out
    assert "contain “tesla”" in out and "Tailscale address" in out
