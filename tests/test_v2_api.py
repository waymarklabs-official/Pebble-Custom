"""REST API: auth matrix, CRUD rules, reminders/notes/context endpoints, OwnTracks, signed links."""

from __future__ import annotations

import base64
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from ringhub import __version__
from ringhub.api import SESSION_COOKIE, make_session, session_valid
from ringhub.app import CSP, WEB_DIR
from ringhub.config import Config

from .conftest import ADMIN, ADMIN_H, CONTEXT, CONTEXT_H, shared_app_client, signed_request, wait_until

API = "/api/v1"
Factory = Callable[..., TestClient]


@pytest.fixture
def client(make_client: Factory) -> TestClient:
    return make_client()


def services(c: TestClient) -> Any:
    return c.app.state.services  # type: ignore[attr-defined]


def jobs(cfg: Config) -> set[str]:
    db = sqlite3.connect(cfg.server.data_dir / "ringhub.sqlite3")
    try:
        return {r[0] for r in db.execute("SELECT id FROM jobs")}
    finally:
        db.close()


def login(c: TestClient) -> None:
    r = c.post(f"{API}/session", json={"token": ADMIN})
    assert r.status_code == 200


# ----------------------------------------------------------------------------- auth

ADMIN_ROUTES: list[tuple[str, str, Any]] = [
    ("GET", "/now", None), ("GET", "/places", None), ("GET", "/skills", None), ("GET", "/modes", None),
    ("GET", "/templates", None), ("GET", "/notes", None), ("GET", "/reminders", None), ("GET", "/context", None),
    ("GET", "/events", None), ("GET", "/setup", None), ("GET", "/catalog/export", None),
    ("PUT", "/modes/gym", {"name": "Gym"}), ("PUT", "/places/gym", {"name": "Gym"}),
    ("PUT", "/skills/tea", {"name": "Tea", "template": "note", "params": {"text": "tea"}}),
    ("DELETE", "/skills/recall", None), ("DELETE", "/places/car_park", None), ("DELETE", "/notes/1", None),
    ("POST", "/override", {"mode": None}), ("POST", "/simulate", {"gesture": "click"}),
    ("POST", "/reminders", {"text": "x"}), ("POST", "/modes/order", {"ids": []}),
    ("POST", "/skills/validate", {"name": "x", "template": "list"}),
]
CONTEXT_ROUTES: list[tuple[str, str, Any]] = [
    ("POST", "/context/arrive/home", None), ("POST", "/context/leave/home", None),
    ("POST", "/context/focus/work", None), ("POST", "/context/visit/pharmacy", None),
    ("POST", "/context", {"set": {"guests": True}}),
]


class TestAuthMatrix:
    """One app for the whole matrix: these checks are about who may call what, not about state."""

    @pytest.fixture(scope="class")
    @classmethod
    def shared(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
        with shared_app_client(tmp_path_factory.mktemp("auth")) as c:
            yield c

    @pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=[f"{m} {p}" for m, p, _ in ADMIN_ROUTES])
    def test_admin_routes_need_the_admin_token(self, shared: TestClient, method: str, path: str, body: Any) -> None:
        c = shared
        assert c.request(method, API + path, json=body).status_code == 401
        assert c.request(method, API + path, json=body, headers=CONTEXT_H).status_code == 401
        ok = c.request(method, API + path, json=body, headers=ADMIN_H)
        assert ok.status_code not in (401, 403, 503), ok.text

    @pytest.mark.parametrize("auth", ["Bearer wrong-token-0123456789", ADMIN, f"Basic {ADMIN}", f"Token {ADMIN}",
                                      f"Bearer {ADMIN}x", f"Bearer {ADMIN[:-1]}", "Bearer ", ""])
    def test_malformed_or_wrong_authorization(self, shared: TestClient, auth: str) -> None:
        assert shared.get(f"{API}/now", headers={"Authorization": auth}).status_code == 401
        assert shared.post(f"{API}/context/arrive/home", headers={"Authorization": auth}).status_code == 401

    @pytest.mark.parametrize("method,path,body", CONTEXT_ROUTES, ids=[f"{m} {p}" for m, p, _ in CONTEXT_ROUTES])
    def test_context_routes_take_context_or_admin_token(self, shared: TestClient, method: str, path: str,
                                                        body: Any) -> None:
        assert shared.request(method, API + path, json=body).status_code == 401
        assert shared.request(method, API + path, json=body, headers=CONTEXT_H).status_code == 200
        assert shared.request(method, API + path, json=body, headers=ADMIN_H).status_code == 200

    def test_context_token_cannot_read_or_reconfigure(self, shared: TestClient) -> None:
        for method, path, body in [("GET", "/now", None), ("GET", "/context", None), ("GET", "/setup", None),
                                   ("PUT", "/modes/work", {"name": "Hacked"}), ("POST", "/catalog/import", {}),
                                   ("GET", "/notes", None), ("GET", "/events", None),
                                   ("POST", "/reminders", {"text": "x"}), ("POST", "/override", {"mode": "work"})]:
            r = shared.request(method, API + path, json=body, headers=CONTEXT_H)
            assert r.status_code == 401, (path, r.status_code)
        assert shared.post(f"{API}/session", json={"token": CONTEXT}).status_code == 401
        assert shared.get(f"{API}/modes", headers=ADMIN_H).json()[0]["name"] != "Hacked"


class TestRejections:
    """Writes that must be refused. They change nothing, so one app serves the whole class."""

    @pytest.fixture(scope="class")
    @classmethod
    def client(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
        with shared_app_client(tmp_path_factory.mktemp("rejections")) as c:
            yield c

    @pytest.mark.parametrize("pid,body", [("Gym", {"name": "Gym"}), ("gym", {}), ("gym", {"name": "G", "radius_m": 50}),
                                          ("gym", {"name": "G", "lat": 95}), ("gym", {"name": "G", "bogus": 1})])
    def test_place_validation(self, client: TestClient, pid: str, body: dict[str, Any]) -> None:
        r = client.put(f"{API}/places/{pid}", json=body, headers=ADMIN_H)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    @pytest.mark.parametrize("pid", ["work", "home", "car"])
    def test_places_used_by_modes_cannot_be_deleted(self, client: TestClient, pid: str) -> None:
        r = client.delete(f"{API}/places/{pid}", headers=ADMIN_H)
        assert r.status_code == 409 and "used by modes" in r.json()["detail"]

    @pytest.mark.parametrize("body,fragment", [
        ({"name": "X", "template": "note", "params": {}}, "text"),
        ({"name": "X", "template": "notify", "params": {"message": "m", "priority": 9}}, "priority"),
        ({"name": "X", "template": "custom", "steps": [{"no.such.action": {}}]}, "unknown action"),
        ({"name": "X", "template": "custom", "steps": [{"exec": {"argv": ["rm", "-rf", "/"]}}]}, "exec is disabled"),
        ({"name": "X", "template": "custom", "steps": []}, "at least one step"),
        ({"name": "X", "template": "custom", "steps": [{"notify": {"nope": 1}}]}, "unknown parameter"),
    ])
    def test_invalid_skills_are_rejected_with_422(self, client: TestClient, body: dict[str, Any],
                                                  fragment: str) -> None:
        r = client.put(f"{API}/skills/bad", json=body, headers=ADMIN_H)
        assert r.status_code == 422 and fragment in str(r.json()["detail"])
        assert client.post(f"{API}/skills/validate", json=body, headers=ADMIN_H).status_code == 422
        assert "bad" not in {s["id"] for s in client.get(f"{API}/skills", headers=ADMIN_H).json()}

    def test_skill_model_errors_are_422(self, client: TestClient) -> None:
        assert client.put(f"{API}/skills/Bad", json={"name": "X"}, headers=ADMIN_H).status_code == 422
        r = client.put(f"{API}/skills/x", json={"name": "X", "phrases": ["{who"]}, headers=ADMIN_H)
        assert r.status_code == 422

    def test_only_builtins_use_the_builtin_template(self, client: TestClient) -> None:
        r = client.put(f"{API}/skills/sneaky", json={"name": "S", "template": "builtin"}, headers=ADMIN_H)
        assert r.status_code == 400

    def test_validate_returns_compiled_steps(self, client: TestClient) -> None:
        r = client.post(f"{API}/skills/validate", json={"name": "N", "template": "notify", "params": {"message": "hi"}},
                        headers=ADMIN_H)
        assert r.json() == {"ok": True, "steps": [{"notify": {"message": "hi", "priority": 3}}]}

    def test_templates_endpoint(self, client: TestClient) -> None:
        ids = {t["id"] for t in client.get(f"{API}/templates", headers=ADMIN_H).json()}
        assert "builtin" not in ids and {"moment", "note", "notify", "car_smart", "custom"} <= ids

    @pytest.mark.parametrize("mid,body,status", [
        ("gym", {"name": "Gym", "bindings": {"click": "ghost"}}, 422),
        ("gym", {"name": "Gym", "bindings": {"hold+click": "moment"}}, 422),
        ("gym", {"name": "Gym", "bindings": {"click": "Bad Skill"}}, 422),
        ("gym", {"name": "Gym", "when": {"time": {"after": "7pm", "before": "06:00"}}}, 422),
        ("everywhere", {"name": "Everywhere", "when": {"places": ["home"]}}, 422),
        ("everywhere", {"name": "Everywhere", "enabled": False}, 422),
        ("Gym", {"name": "Gym"}, 422),
    ])
    def test_invalid_modes(self, client: TestClient, mid: str, body: dict[str, Any], status: int) -> None:
        before = client.get(f"{API}/modes", headers=ADMIN_H).json()
        r = client.put(f"{API}/modes/{mid}", json=body, headers=ADMIN_H)
        assert r.status_code == status
        assert client.get(f"{API}/modes", headers=ADMIN_H).json() == before

    def test_unknown_skill_message(self, client: TestClient) -> None:
        r = client.put(f"{API}/modes/gym", json={"name": "Gym", "bindings": {"click": "ghost", "double": "zz"}},
                       headers=ADMIN_H)
        assert r.json()["detail"] == "unknown skills: ghost, zz"

    def test_everywhere_cannot_be_deleted(self, client: TestClient) -> None:
        assert client.delete(f"{API}/modes/everywhere", headers=ADMIN_H).status_code == 400

    @pytest.mark.parametrize("body,fragment", [
        ({"text": "x", "trigger": "time"}, "need 'at' or 'after_minutes'"),
        ({"text": "x", "trigger": "time", "at": "2001-01-01T09:00:00Z"}, "already passed"),
        ({"text": "x", "trigger": "arrive"}, "need 'place'"),
        ({"text": "x", "trigger": "leave", "place": ""}, "need 'place'"),
        ({"text": "", "trigger": "none"}, ""),
        ({"text": "x" * 301}, ""),
        ({"text": "x", "trigger": "sometime"}, ""),
        ({"text": "x", "trigger": "time", "after_minutes": 0}, ""),
    ])
    def test_reminder_validation(self, client: TestClient, body: dict[str, Any], fragment: str) -> None:
        r = client.post(f"{API}/reminders", json=body, headers=ADMIN_H)
        assert r.status_code == 422 and fragment in str(r.json()["detail"])
        assert client.get(f"{API}/reminders?status=all", headers=ADMIN_H).json() == []

    @pytest.mark.parametrize("location", [{"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": 1}, {"lon": 1}, {}])
    def test_location_is_validated(self, client: TestClient, location: dict[str, float]) -> None:
        r = client.post(f"{API}/context", json={"location": location}, headers=CONTEXT_H)
        assert r.status_code == 422
        assert "location" not in ctx_now(client)

    def test_location_must_be_numbers(self, client: TestClient) -> None:
        r = client.post(f"{API}/context", json={"location": {"lat": "x", "lon": 1}}, headers=CONTEXT_H)
        assert r.status_code == 422

    @pytest.mark.parametrize("key", ["place", "place_since", "last_left", "focus", "location"])
    def test_reserved_keys_cannot_be_set_directly(self, client: TestClient, key: str) -> None:
        r = client.post(f"{API}/context", json={"set": {key: "home"}}, headers=CONTEXT_H)
        assert r.status_code == 422 and "arrive/leave/focus/location" in r.json()["detail"]

    @pytest.mark.parametrize("key", ["bad key", "a-b", "x" * 33, "", "semi;colon"])
    def test_bad_context_keys(self, client: TestClient, key: str) -> None:
        assert client.post(f"{API}/context", json={"set": {key: 1}}, headers=CONTEXT_H).status_code == 422


class TestSession:
    """Dashboard login. One app; every test starts without cookies."""

    @pytest.fixture(scope="class")
    @classmethod
    def client(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
        with shared_app_client(tmp_path_factory.mktemp("session")) as c:
            yield c

    @pytest.fixture(autouse=True)
    def _no_cookies(self, client: TestClient) -> None:
        client.cookies.clear()

    def test_cookie_session_flags(self, client: TestClient) -> None:
        r = client.post(f"{API}/session", json={"token": ADMIN})
        cookie = r.headers["set-cookie"]
        flags = {part.strip().split("=")[0].lower() for part in cookie.split(";")}
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "httponly" in flags and "secure" not in flags  # plain http on the LAN
        assert "samesite=strict" in cookie.lower().replace(" ", "")
        assert "path=/" in cookie.lower()
        assert ADMIN not in cookie

    def test_wrong_login_is_rejected(self, client: TestClient) -> None:
        assert client.post(f"{API}/session", json={"token": "nope"}).status_code == 401
        assert client.post(f"{API}/session", json={"token": CONTEXT}).status_code == 401
        assert "set-cookie" not in client.post(f"{API}/session", json={"token": "nope"}).headers
        assert client.get(f"{API}/session").json() == {"authenticated": False, "enabled": True}

    def test_cookie_writes_need_the_csrf_header(self, client: TestClient) -> None:
        login(client)
        assert client.get(f"{API}/session").json()["authenticated"] is True
        assert client.get(f"{API}/now").status_code == 200
        body = {"name": "Gym"}
        assert client.put(f"{API}/places/gym", json=body).status_code == 403
        assert client.put(f"{API}/places/gym", json=body, headers={"X-Ringhub": "0"}).status_code == 403
        assert client.post(f"{API}/override", json={"mode": "work"}).status_code == 403
        assert client.delete(f"{API}/skills/recall").status_code == 403
        assert client.put(f"{API}/places/gym", json=body, headers={"X-Ringhub": "1"}).status_code == 200
        # bearer scripts don't need it
        client.cookies.clear()
        assert client.put(f"{API}/places/gym", json=body, headers=ADMIN_H).status_code == 200

    def test_tampered_and_expired_cookies_are_rejected(self, client: TestClient) -> None:
        secret = services(client).secret
        client.cookies.set(SESSION_COOKIE, make_session(secret, ADMIN, 0, now=time.time() - 31 * 86400))
        assert client.get(f"{API}/now").status_code == 401
        good = make_session(secret, ADMIN, 0)
        exp, epoch, sig = good.split(".")
        for bad in (f"{int(exp) + 1000}.{epoch}.{sig}", f"{exp}.{epoch}.{'0' * len(sig)}", f"{exp}.1.{sig}",
                    f"{exp}.{sig}",
                    make_session(b"another-install-secret-0123456789abcdef", ADMIN, 0),
                    make_session(secret, "a-different-admin-token", 0)):
            client.cookies.set(SESSION_COOKIE, bad)
            assert client.get(f"{API}/now").status_code == 401, bad
        client.cookies.set(SESSION_COOKIE, good)
        assert client.get(f"{API}/now").status_code == 200
        # Non-ASCII values (sendable by raw clients) are rejected, not a TypeError/500.
        assert not session_valid(secret, ADMIN, 0, f"{exp}.{epoch}.é")
        assert not session_valid(secret, ADMIN, 0, "٩٩٩٩٩٩٩٩٩٩.0." + sig)  # Unicode digits

    def test_revoke_signs_out_every_browser(self, client: TestClient) -> None:
        login(client)
        stolen = client.cookies.get(SESSION_COOKIE)
        assert client.post(f"{API}/session/revoke", headers={"X-Ringhub": "1"}).status_code == 200
        client.cookies.set(SESSION_COOKIE, stolen)
        assert client.get(f"{API}/now").status_code == 401
        login(client)  # a fresh sign-in works under the new epoch
        assert client.get(f"{API}/now").status_code == 200

    def test_logout_clears_the_cookie(self, client: TestClient) -> None:
        login(client)
        r = client.delete(f"{API}/session")
        assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()
        assert client.get(f"{API}/now").status_code == 401


def test_cookie_is_secure_over_https(make_client: Factory) -> None:
    c = make_client(base_url="https://testserver")
    cookie = c.post(f"{API}/session", json={"token": ADMIN}).headers["set-cookie"].lower()
    assert "secure" in {p.strip() for p in cookie.split(";")}


def test_dashboard_disabled_without_admin_token(make_client: Factory, make_config: Callable[..., Config],
                                                tmp_path: Any) -> None:
    cfg = make_config(server={"data_dir": str(tmp_path / "d"), "context_token": CONTEXT})
    c = make_client(cfg, seeded=False)
    assert c.get(f"{API}/now").status_code == 503
    assert c.get(f"{API}/now", headers=CONTEXT_H).status_code == 503
    assert c.post(f"{API}/session", json={"token": ADMIN}).status_code == 401
    assert c.get(f"{API}/session").json() == {"authenticated": False, "enabled": False}
    assert c.post(f"{API}/context/arrive/home", headers=CONTEXT_H).status_code == 200  # the phone still works


# ----------------------------------------------------------------------------- misc surface


def test_first_boot_seeds_the_catalog_and_healthz(make_client: Factory) -> None:
    c = make_client(seeded=False)  # an empty data dir, as on a fresh install
    assert c.get("/healthz").json() == {"ok": True, "version": __version__}  # no presence leak
    assert {p["id"] for p in c.get(f"{API}/places", headers=ADMIN_H).json()} == {"home", "work", "car"}
    assert c.get(f"{API}/modes", headers=ADMIN_H).json()[-1]["id"] == "everywhere"


@pytest.mark.skipif(not (WEB_DIR / "index.html").exists(), reason="dashboard not built")
def test_index_is_served_with_csp(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == CSP
    assert "script-src" not in CSP or "'unsafe-inline'" not in CSP.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in CSP and "default-src 'self'" in CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-cache"


def test_now_describes_mode_and_gestures(client: TestClient) -> None:
    client.post(f"{API}/context/arrive/work", headers=CONTEXT_H)
    now = client.get(f"{API}/now", headers=ADMIN_H).json()
    assert now["mode"]["id"] == "work" and now["reason"] == "at work" and now["manual"] is False
    g = {x["key"]: x for x in now["gestures"]}
    assert list(g) == ["click", "double-click", "triple-click", "single-click-hold", "double-click-hold"]
    assert g["click"]["skill"]["id"] == "moment" and g["click"]["inherited"] is False
    assert g["double-click"]["skill"]["id"] == "next"
    assert g["single-click-hold"]["skill"]["id"] == "capture" and g["single-click-hold"]["inherited"] is True
    assert now["context"]["place"] == "work"
    assert now["features"] == {"tesla": False, "google": False, "ntfy": True, "llm": False, "tv": False,
                               "links": True}
    assert now["vehicle"] is None and now["attention"] is None


class TestCatalogEdits:
    """Accepted catalog edits. Each test works on its own ids, so they share one app."""

    @pytest.fixture(scope="class")
    @classmethod
    def client(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
        with shared_app_client(tmp_path_factory.mktemp("catalog")) as c:
            yield c

    def test_setup_lists_urls_and_token(self, client: TestClient) -> None:
        setup = client.get(f"{API}/setup", headers=ADMIN_H).json()
        assert setup["webhook_url"] == "https://hub.test/ring" and setup["context_token"] == CONTEXT
        assert {"home", "work", "car"} <= {p["id"] for p in setup["places"]}

    def test_place_crud(self, client: TestClient) -> None:
        body = {"name": "Gym", "aliases": ["The Gym", "gym"], "lat": 41.9, "lon": -87.6}
        r = client.put(f"{API}/places/gym", json=body, headers=ADMIN_H)
        assert r.status_code == 200 and r.json()["aliases"] == ["gym", "the gym"] and r.json()["id"] == "gym"
        # the path id wins over any id in the body
        r = client.put(f"{API}/places/gym", json={"id": "evil", "name": "Gym 2"}, headers=ADMIN_H)
        assert r.json()["id"] == "gym"
        places = {p["id"]: p for p in client.get(f"{API}/places", headers=ADMIN_H).json()}
        assert places["gym"]["name"] == "Gym 2" and "evil" not in places
        assert client.delete(f"{API}/places/gym", headers=ADMIN_H).json() == {"deleted": True}
        assert client.delete(f"{API}/places/gym", headers=ADMIN_H).json() == {"deleted": False}

    def test_skill_crud_and_usage(self, client: TestClient) -> None:
        body = {"name": "Tea", "icon": "🍵", "template": "note", "params": {"text": "Had tea"}, "phrases": ["had tea"]}
        assert client.put(f"{API}/skills/tea", json=body, headers=ADMIN_H).status_code == 200
        skills = {s["id"]: s for s in client.get(f"{API}/skills", headers=ADMIN_H).json()}
        assert skills["tea"]["builtin"] is False and skills["tea"]["used_by"] == []
        assert skills["capture"]["builtin"] is True
        hold = {"mode": "everywhere", "name": "Everywhere", "gesture": "single-click-hold"}
        assert hold in skills["capture"]["used_by"]
        assert client.delete(f"{API}/skills/tea", headers=ADMIN_H).json() == {"deleted": True}

    def test_builtin_skills_only_change_cosmetically_and_cannot_be_deleted(self, client: TestClient) -> None:
        r = client.put(f"{API}/skills/capture", json={"name": "Talk", "icon": "🗣️", "template": "notify",
                                                      "params": {"message": "pwned"}, "steps": [{"noop": {}}]},
                       headers=ADMIN_H)
        assert r.status_code == 200
        got = r.json()
        assert (got["name"], got["icon"], got["template"], got["params"]) == ("Talk", "🗣️", "builtin", {})
        assert client.delete(f"{API}/skills/capture", headers=ADMIN_H).status_code == 400

    def test_bound_skills_cannot_be_deleted(self, client: TestClient) -> None:
        r = client.delete(f"{API}/skills/moment", headers=ADMIN_H)
        assert r.status_code == 409 and "Everywhere" in r.json()["detail"]
        assert client.delete(f"{API}/skills/recall", headers=ADMIN_H).json() == {"deleted": True}

    def test_run_skill_now(self, client: TestClient) -> None:
        assert client.post(f"{API}/skills/took_meds/run", headers=ADMIN_H).json() == {"ok": True}
        assert client.get(f"{API}/notes?kind=log", headers=ADMIN_H).json()[0]["text"] == "Took meds ✓"
        r = client.post(f"{API}/skills/ghost/run", headers=ADMIN_H)
        assert r.status_code == 400 and "no longer exists" in r.json()["detail"]

    def test_mode_crud(self, client: TestClient) -> None:
        body = {"name": "Yoga", "when": {"places": ["studio"]},
                "bindings": {"double+single": "took_meds", "click": "moment"}}
        r = client.put(f"{API}/modes/yoga", json=body, headers=ADMIN_H)
        assert r.status_code == 200 and r.json()["bindings"] == {"double-click+click": "took_meds", "click": "moment"}
        modes = [m["id"] for m in client.get(f"{API}/modes", headers=ADMIN_H).json()]
        assert modes[-1] == "everywhere" and "yoga" in modes
        assert client.delete(f"{API}/modes/yoga", headers=ADMIN_H).json() == {"deleted": True}
        assert client.delete(f"{API}/modes/yoga", headers=ADMIN_H).json() == {"deleted": False}


# ----------------------------------------------------------------------------- engine-facing endpoints


def test_override_endpoint(client: TestClient) -> None:
    assert client.post(f"{API}/override", json={"mode": "work", "minutes": 30}, headers=ADMIN_H).status_code == 200
    now = client.get(f"{API}/now", headers=ADMIN_H).json()
    assert (now["mode"]["id"], now["manual"], now["reason"]) == ("work", True, "set by hand")
    assert client.post(f"{API}/override", json={"mode": "ghost"}, headers=ADMIN_H).status_code == 400
    assert client.post(f"{API}/override", json={"mode": "work", "minutes": 0}, headers=ADMIN_H).status_code == 422
    assert client.post(f"{API}/override", json={"mode": None}, headers=ADMIN_H).status_code == 200
    assert client.get(f"{API}/now", headers=ADMIN_H).json()["manual"] is False


def test_simulate_goes_through_the_engine(client: TestClient) -> None:
    r = client.post(f"{API}/simulate", json={"gesture": "hold", "transcript": "simulated thought"}, headers=ADMIN_H)
    assert r.status_code == 200 and r.json()["delivery_id"].startswith("sim-")
    events = wait_until(lambda: client.get(f"{API}/events", headers=ADMIN_H).json())  # logged after the skill ran
    assert events[0]["delivery_id"] == r.json()["delivery_id"] and events[0]["gesture"] == "hold"
    assert events[0]["status"] == "ok" and events[0]["detail"] == "everywhere: capture"
    assert client.get(f"{API}/notes", headers=ADMIN_H).json()[0]["text"] == "simulated thought"
    assert client.post(f"{API}/simulate", json={"gesture": "quadruple"}, headers=ADMIN_H).status_code == 422


def test_signed_webhook_hold_lands_in_memory(client: TestClient) -> None:
    body, headers = signed_request("single-click-hold", transcript="from the ring itself", delivery="ring-1")
    assert client.post("/ring", content=body, headers=headers).json() == {"ok": True}
    notes = wait_until(lambda: client.get(f"{API}/notes", headers=ADMIN_H).json())
    assert notes[0]["text"] == "from the ring itself"
    body, headers = signed_request("single-click-hold", transcript="from the ring itself", delivery="ring-1")
    assert client.post("/ring", content=body, headers=headers).json() == {"ok": True, "duplicate": True}


def test_mode_order_keeps_everywhere_last(client: TestClient) -> None:
    ids = [m["id"] for m in client.get(f"{API}/modes", headers=ADMIN_H).json()]
    wanted = ["everywhere", *reversed(ids[:-1])]  # client puts the fallback first
    assert client.post(f"{API}/modes/order", json={"ids": wanted}, headers=ADMIN_H).status_code == 200
    got = [m["id"] for m in client.get(f"{API}/modes", headers=ADMIN_H).json()]
    assert got == [*reversed(ids[:-1]), "everywhere"]
    assert client.post(f"{API}/modes/order", json={"ids": ids[:2]}, headers=ADMIN_H).status_code == 400
    assert client.post(f"{API}/modes/order", json={"ids": [*ids, "ghost"]}, headers=ADMIN_H).status_code == 400
    assert [m["id"] for m in client.get(f"{API}/modes", headers=ADMIN_H).json()] == got


def test_mode_order_changes_resolution(client: TestClient) -> None:
    client.post(f"{API}/context/arrive/home", headers=CONTEXT_H)
    client.post(f"{API}/context/focus/sleep", headers=CONTEXT_H)
    assert client.get(f"{API}/now", headers=ADMIN_H).json()["mode"]["id"] == "sleep"
    ids = [m["id"] for m in client.get(f"{API}/modes", headers=ADMIN_H).json()]
    reordered = [i for i in ids if i not in ("home", "everywhere")]
    client.post(f"{API}/modes/order", json={"ids": ["home", *reordered]}, headers=ADMIN_H)
    assert client.get(f"{API}/now", headers=ADMIN_H).json()["mode"]["id"] == "home"


# ----------------------------------------------------------------------------- mode order, import / export


def test_export_import_round_trip(client: TestClient) -> None:
    exported = client.get(f"{API}/catalog/export", headers=ADMIN_H).json()
    assert exported["version"] == 1 and {"place", "skill", "mode"} <= set(exported)
    client.put(f"{API}/places/gym", json={"name": "Gym"}, headers=ADMIN_H)
    assert client.post(f"{API}/catalog/import", json=exported, headers=ADMIN_H).json() == {"ok": True}
    again = client.get(f"{API}/catalog/export", headers=ADMIN_H).json()
    assert again == exported  # gym is gone: an import replaces everything


def test_import_without_everywhere_is_rejected_atomically(client: TestClient) -> None:
    exported = client.get(f"{API}/catalog/export", headers=ADMIN_H).json()
    broken = {**exported, "mode": [m for m in exported["mode"] if m["id"] != "everywhere"]}
    r = client.post(f"{API}/catalog/import", json=broken, headers=ADMIN_H)
    assert r.status_code == 422 and "everywhere" in r.json()["detail"]
    bad = {**exported, "place": [*exported["place"], {"id": "X", "name": "x"}]}
    assert client.post(f"{API}/catalog/import", json=bad, headers=ADMIN_H).status_code == 422
    assert client.get(f"{API}/catalog/export", headers=ADMIN_H).json() == exported


def test_import_with_duplicate_ids_is_a_422(make_client: Factory) -> None:
    c = make_client(raise_server_exceptions=False)
    exported = c.get(f"{API}/catalog/export", headers=ADMIN_H).json()
    dup = {**exported, "place": [*exported["place"], exported["place"][0]]}
    r = c.post(f"{API}/catalog/import", json=dup, headers=ADMIN_H)
    assert r.status_code == 422


# ----------------------------------------------------------------------------- reminders & signed links


class TestRemindersAndLinks:
    """Reminder endpoints and signed push links. Every test makes and checks its own reminders."""

    @pytest.fixture(scope="class")
    @classmethod
    def client(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
        with shared_app_client(tmp_path_factory.mktemp("reminders")) as c:
            yield c

    def test_time_reminder_after_minutes_is_scheduled(self, client: TestClient) -> None:
        r = client.post(f"{API}/reminders", json={"text": "Stretch", "trigger": "time", "after_minutes": 30},
                        headers=ADMIN_H)
        assert r.status_code == 200
        rem = r.json()
        assert rem["status"] == "pending" and rem["due_at"] == pytest.approx(time.time() + 1800, abs=5)
        assert f"reminder:{rem['id']}" in jobs(services(client).config)
        assert rem["id"] in [x["id"] for x in client.get(f"{API}/reminders", headers=ADMIN_H).json()]

    def test_time_reminder_naive_at_is_local_time(self, client: TestClient) -> None:
        cfg = services(client).config
        local = (datetime.now(cfg.tz) + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        r = client.post(f"{API}/reminders", json={"text": "Dentist", "trigger": "time",
                                                  "at": local.replace(tzinfo=None).isoformat()}, headers=ADMIN_H)
        assert r.status_code == 200 and r.json()["due_at"] == pytest.approx(local.timestamp())

    def test_place_and_list_reminders(self, client: TestClient) -> None:
        a = client.post(f"{API}/reminders", json={"text": "Lift", "trigger": "arrive", "place": "The Gym"},
                        headers=ADMIN_H).json()
        assert (a["trigger"], a["place"]) == ("arrive", "the_gym")
        b = client.post(f"{API}/reminders", json={"text": "Milk"}, headers=ADMIN_H).json()
        assert (b["trigger"], b["place"], b["due_at"]) == ("none", None, None)

    def test_reminder_actions_and_409(self, client: TestClient) -> None:
        rid = client.post(f"{API}/reminders", json={"text": "Milk"}, headers=ADMIN_H).json()["id"]
        snoozed = client.post(f"{API}/reminders/{rid}/snooze", headers=ADMIN_H).json()
        assert (snoozed["trigger"], snoozed["snoozes"]) == ("time", 1)
        assert snoozed["due_at"] == pytest.approx(time.time() + 600, abs=5)
        assert client.post(f"{API}/reminders/{rid}/done", headers=ADMIN_H).json()["status"] == "done"
        assert f"reminder:{rid}" not in jobs(services(client).config)
        for action in ("done", "snooze", "cancel"):
            r = client.post(f"{API}/reminders/{rid}/{action}", headers=ADMIN_H)
            assert r.status_code == 409 and r.json()["detail"] == "reminder is not open"
        assert client.post(f"{API}/reminders/{rid}/explode", headers=ADMIN_H).status_code == 422
        assert client.post(f"{API}/reminders/nope/done", headers=ADMIN_H).status_code == 409
        assert rid not in [x["id"] for x in client.get(f"{API}/reminders", headers=ADMIN_H).json()]
        assert status(client, rid) == "done"
        assert client.get(f"{API}/reminders?status=weird", headers=ADMIN_H).status_code == 422
        rid2 = client.post(f"{API}/reminders", json={"text": "Eggs"}, headers=ADMIN_H).json()["id"]
        assert client.post(f"{API}/reminders/{rid2}/cancel", headers=ADMIN_H).json()["status"] == "cancelled"

    def test_link_get_only_shows_a_confirm_page(self, client: TestClient) -> None:
        rid = new_reminder(client)
        url = link(client, rid, "done")
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "Confirm" in r.text and "<form method=post" in r.text and f'action="{url.replace("&", "&")}"' in r.text
        assert status(client, rid) == "pending"  # link previews and prefetchers must not act

    def test_link_post_acts_once(self, client: TestClient) -> None:
        rid = new_reminder(client)
        url = link(client, rid, "done")
        assert client.post(url).json() == {"ok": True, "status": "done"}
        assert status(client, rid) == "done"
        assert client.post(url).json() == {"ok": True, "note": "already handled"}

    def test_snooze_link(self, client: TestClient) -> None:
        rid = new_reminder(client)
        assert client.post(link(client, rid, "snooze")).json() == {"ok": True, "status": "pending"}
        snoozed = next(x for x in client.get(f"{API}/reminders", headers=ADMIN_H).json() if x["id"] == rid)
        assert snoozed["trigger"] == "time" and snoozed["snoozes"] == 1

    def test_tampered_or_expired_links_are_403(self, client: TestClient) -> None:
        rid = new_reminder(client)
        good = link(client, rid, "done")
        path, query = good.split("?")
        params = dict(p.split("=") for p in query.split("&"))
        other = new_reminder(client)
        bad_urls = [
            f"{path}?exp={params['exp']}&sig={'0' * 32}",                       # forged signature
            f"{path}?exp={int(params['exp']) + 60}&sig={params['sig']}",        # extended expiry
            f"/r/{rid}/snooze?{query}",                                          # different action
            f"/r/{other}/done?{query}",                                          # different reminder
            f"{path}?exp=abc&sig={params['sig']}",
            path,                                                                # no signature at all
            link(client, rid, "done", ttl=-10),                                  # expired
            link(client, rid, "cancel"),                                         # validly signed, not allowed
        ]
        for url in bad_urls:
            for method in ("GET", "POST"):
                r = client.request(method, url)
                assert r.status_code == 403, (method, url)
                assert "invalid or has expired" in r.text
        assert status(client, rid) == "pending" and status(client, other) == "pending"
        assert client.post(good).status_code == 200


# ----------------------------------------------------------------------------- notes


def add_notes(c: TestClient, *texts: str) -> list[dict[str, Any]]:
    for t in texts:
        c.post(f"{API}/simulate", json={"gesture": "hold", "transcript": t}, headers=ADMIN_H)
    return wait_until(lambda: (n := c.get(f"{API}/notes", headers=ADMIN_H).json()) and len(n) >= len(texts) and n)


def test_notes_search_edit_delete(client: TestClient) -> None:
    add_notes(client, "the dentist moved to friday", "garage code is 4417", "buy running shoes")
    hits = client.get(f"{API}/notes", params={"q": "dentists"}, headers=ADMIN_H).json()
    assert [h["text"] for h in hits] == ["the dentist moved to friday"]
    nid = hits[0]["id"]
    assert client.patch(f"{API}/notes/{nid}", json={"text": "orthodontist on monday"}, headers=ADMIN_H).json() == {
        "ok": True}
    assert client.get(f"{API}/notes", params={"q": "dentist"}, headers=ADMIN_H).json() == []
    assert client.get(f"{API}/notes", params={"q": "orthodontist"}, headers=ADMIN_H).json()[0]["id"] == nid
    assert client.patch(f"{API}/notes/999999", json={"text": "x"}, headers=ADMIN_H).status_code == 404
    assert client.patch(f"{API}/notes/{nid}", json={"text": ""}, headers=ADMIN_H).status_code == 422
    assert client.patch(f"{API}/notes/abc", json={"text": "x"}, headers=ADMIN_H).status_code == 422
    assert client.delete(f"{API}/notes/{nid}", headers=ADMIN_H).json() == {"deleted": True}
    assert client.delete(f"{API}/notes/{nid}", headers=ADMIN_H).json() == {"deleted": False}
    assert client.get(f"{API}/notes", params={"q": "orthodontist"}, headers=ADMIN_H).json() == []


def test_notes_filters_and_limits(client: TestClient) -> None:
    add_notes(client, "one note", "two note", "three note")
    client.post(f"{API}/skills/took_meds/run", headers=ADMIN_H)
    assert [n["kind"] for n in client.get(f"{API}/notes?kind=log", headers=ADMIN_H).json()] == ["log"]
    assert len(client.get(f"{API}/notes?limit=2", headers=ADMIN_H).json()) == 2
    assert len(client.get(f"{API}/notes?limit=-5", headers=ADMIN_H).json()) == 1  # clamped to >= 1
    assert client.get(f"{API}/notes", params={"q": '"); DROP TABLE notes; --'}, headers=ADMIN_H).status_code == 200
    assert client.get(f"{API}/notes", params={"q": "NEAR(one two) OR *"}, headers=ADMIN_H).status_code == 200
    assert len(client.get(f"{API}/notes?limit=500", headers=ADMIN_H).json()) == 4


# ----------------------------------------------------------------------------- context feed


def ctx_now(c: TestClient) -> dict[str, Any]:
    return dict(c.get(f"{API}/context", headers=ADMIN_H).json()["now"])


def test_arrive_leave_focus_and_visit(client: TestClient) -> None:
    r = client.post(f"{API}/context/arrive/work", headers=CONTEXT_H)
    assert r.json() == {"ok": True, "mode": "work", "place": "work"}
    # a leave seconds later is geofence jitter
    assert client.post(f"{API}/context/leave/work", headers=CONTEXT_H).json()["place"] == "work"
    assert client.post(f"{API}/context/focus/Sleep", headers=CONTEXT_H).json()["mode"] == "sleep"
    assert ctx_now(client)["focus"] == "sleep"
    for off in ("off", "none", "OFF"):
        client.post(f"{API}/context/focus/sleep", headers=CONTEXT_H)
        assert client.post(f"{API}/context/focus/{off}", headers=CONTEXT_H).json()["mode"] == "work"
        assert ctx_now(client)["focus"] is None
    assert client.post(f"{API}/context/visit/pharmacy", headers=CONTEXT_H).json() == {"ok": True}
    now = ctx_now(client)
    assert now["place"] == "work" and now["last_visit"]["place"] == "pharmacy"
    history = client.get(f"{API}/context", headers=ADMIN_H).json()["history"]
    assert {h["source"] for h in history} == {"shortcut"}


def test_path_places_are_slugified(client: TestClient) -> None:
    assert client.post(f"{API}/context/arrive/Grandma's House", headers=CONTEXT_H).json()["place"] == "grandma_s_house"


def test_unusable_place_names_are_rejected(client: TestClient) -> None:
    assert client.post(f"{API}/context/arrive/🏠", headers=CONTEXT_H).status_code == 400


def test_post_context_body(client: TestClient) -> None:
    r = client.post(f"{API}/context", json={"arrive": "Home", "focus": "Work", "set": {"Guests": True},
                                            "location": {"lat": 41.9, "lon": -87.6, "accuracy": 12}}, headers=CONTEXT_H)
    assert r.status_code == 200 and r.json()["place"] == "home"
    now = ctx_now(client)
    assert (now["place"], now["focus"], now["guests"]) == ("home", "work", True)
    loc = now["location"]
    assert (loc["lat"], loc["lon"], loc["accuracy"]) == (41.9, -87.6, 12)
    assert loc["at"] == pytest.approx(time.time(), abs=5)
    client.post(f"{API}/context", json={"set": {"guests": None}}, headers=CONTEXT_H)
    assert "guests" not in ctx_now(client)


def test_reserved_keys_cannot_be_set_with_other_casing(client: TestClient) -> None:
    for key, value in (("Place", "home"), ("FOCUS", 5), ("LAST_LEFT", "x"), ("Pending_Leave", {})):
        r = client.post(f"{API}/context", json={"set": {key: value}}, headers=CONTEXT_H)
        assert r.status_code == 422, key
    assert ctx_now(client)["place"] is None
    # and the ring keeps working
    assert client.get(f"{API}/now", headers=ADMIN_H).status_code == 200


# ----------------------------------------------------------------------------- OwnTracks


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_owntracks_auth(client: TestClient) -> None:
    msg = {"_type": "location", "lat": 41.9, "lon": -87.6, "tst": int(time.time())}
    r = client.post(f"{API}/owntracks", json=msg)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Basic"
    assert client.post(f"{API}/owntracks", json=msg, headers=basic("me", "wrong")).status_code == 401
    assert client.post(f"{API}/owntracks", json=msg, headers=basic("me", ADMIN)).status_code == 401
    assert client.post(f"{API}/owntracks", json=msg, headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert client.post(f"{API}/owntracks", json=msg, headers=basic("anyone", CONTEXT)).status_code == 200
    assert client.post(f"{API}/owntracks", json=msg, headers=CONTEXT_H).status_code == 200
    assert client.post(f"{API}/owntracks", content=b"not json",
                       headers={**basic("me", CONTEXT), "Content-Type": "application/json"}).status_code == 400
    assert client.post(f"{API}/owntracks", json=[1, 2], headers=basic("me", CONTEXT)).json() == []


def test_owntracks_transitions_map_by_name_and_alias(client: TestClient) -> None:
    h = basic("me", CONTEXT)
    t = int(time.time())
    client.post(f"{API}/owntracks", json={"_type": "transition", "event": "enter", "desc": "The Office",
                                          "tst": t - 600, "lat": 41.88, "lon": -87.63, "acc": 20}, headers=h)
    now = ctx_now(client)
    assert now["place"] == "work" and now["place_since"] == t - 600
    assert now["location"] == {"lat": 41.88, "lon": -87.63, "accuracy": 20, "at": t - 600}
    client.post(f"{API}/owntracks", json={"_type": "transition", "event": "leave", "desc": "WORK", "tst": t},
                headers=h)
    now = ctx_now(client)
    assert now["place"] is None and now["last_left"] == {"place": "work", "at": t}
    client.post(f"{API}/owntracks", json={"_type": "transition", "event": "enter", "desc": "Narnia", "tst": t},
                headers=h)
    assert ctx_now(client)["place"] is None
    sources = {x["source"] for x in client.get(f"{API}/context", headers=ADMIN_H).json()["history"]}
    assert sources == {"owntracks"}


def test_owntracks_future_timestamps_are_clamped(client: TestClient) -> None:
    client.post(f"{API}/owntracks", json={"_type": "location", "lat": 1.0, "lon": 2.0, "tst": time.time() + 86400},
                headers=CONTEXT_H)
    assert ctx_now(client)["location"]["at"] <= time.time()


def test_owntracks_set_waypoints_once_until_places_change(client: TestClient) -> None:
    h = basic("me", CONTEXT)
    ping = {"_type": "location", "lat": 41.9, "lon": -87.6, "tst": int(time.time())}
    assert client.post(f"{API}/owntracks", json=ping, headers=h).json() == []  # no place has coordinates yet
    client.put(f"{API}/places/home", json={"name": "Home", "aliases": ["home"], "lat": 41.95, "lon": -87.65,
                                           "radius_m": 200}, headers=ADMIN_H)
    (cmd,) = client.post(f"{API}/owntracks", json=ping, headers=h).json()
    assert cmd["_type"] == "cmd" and cmd["action"] == "setWaypoints"
    (wp,) = cmd["waypoints"]["waypoints"]
    assert (wp["_type"], wp["desc"], wp["lat"], wp["lon"], wp["rad"]) == ("waypoint", "Home", 41.95, -87.65, 200)
    assert client.post(f"{API}/owntracks", json=ping, headers=h).json() == []
    assert client.post(f"{API}/owntracks", json=ping, headers=h).json() == []
    client.put(f"{API}/places/home", json={"name": "Home", "aliases": ["home"], "lat": 41.95, "lon": -87.65,
                                           "radius_m": 300}, headers=ADMIN_H)
    (cmd,) = client.post(f"{API}/owntracks", json=ping, headers=h).json()
    assert cmd["waypoints"]["waypoints"][0]["rad"] == 300
    # the waypoint desc round-trips: OwnTracks reports transitions with it
    client.post(f"{API}/owntracks", json={"_type": "transition", "event": "enter", "desc": wp["desc"]}, headers=h)
    assert ctx_now(client)["place"] == "home"


def test_owntracks_malformed_messages_are_not_500(make_client: Factory) -> None:
    c = make_client(raise_server_exceptions=False)
    assert c.post(f"{API}/owntracks", json={"_type": "location", "lat": 1.0}, headers=CONTEXT_H).status_code < 500
    assert c.post(f"{API}/owntracks", json={"_type": "location", "tst": "soon"}, headers=CONTEXT_H).status_code < 500


# ----------------------------------------------------------------------------- signed reminder links


def link(c: TestClient, rid: str, action: str, ttl: int = 3600) -> str:
    url = services(c).runtime.links.sign(f"/r/{rid}/{action}", ttl_seconds=ttl)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == "https://hub.test"
    return f"{parts.path}?{parts.query}"


def new_reminder(c: TestClient) -> str:
    return str(c.post(f"{API}/reminders", json={"text": "Feed the cat"}, headers=ADMIN_H).json()["id"])


def status(c: TestClient, rid: str) -> str:
    return next(x["status"] for x in c.get(f"{API}/reminders?status=all", headers=ADMIN_H).json() if x["id"] == rid)

