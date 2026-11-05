from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from ringhub.config import Config, load_config
from ringhub.webhook import sign

SECRET = "test-secret-0123456789abcdefghijklmnop"
ADMIN = "admin-token-for-tests"
CONTEXT = "context-token-for-tests"


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    def factory(extra: dict[str, Any] | None = None, **overrides: Any) -> Config:
        doc: dict[str, Any] = {
            "timezone": "America/Chicago",
            "server": {"data_dir": str(tmp_path / "data"), "admin_token": ADMIN, "context_token": CONTEXT,
                       "public_url": "https://hub.test"},
            "ring": {"secrets": {"default": SECRET}, "combo_window_ms": 150},
            "ntfy": {"url": "https://ntfy.test", "topic": "ring"},
            "memory": {"markdown_dir": str(tmp_path / "vault")},
        }
        doc.update(extra or {})
        doc.update(overrides)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(doc))
        return load_config(path)

    return factory


def multipart_body(boundary: str, transcription: str | None, recorded_at: int,
                   audio: bytes | None = None, test: bool = False) -> bytes:
    """Byte-for-byte replica of buildWebhookMultipartBody() in the Index app."""
    crlf = "\r\n"
    out = b""
    if audio is not None:
        out += (f"--{boundary}{crlf}Content-Disposition: form-data; name=\"audio\"; filename=\"rec-1.m4a\"{crlf}"
                f"Content-Type: audio/mp4{crlf}{crlf}").encode() + audio + crlf.encode()
    if transcription is not None:
        out += (f"--{boundary}{crlf}Content-Disposition: form-data; name=\"transcription\"{crlf}{crlf}"
                f"{transcription}{crlf}").encode()
    meta = ""
    if test:
        meta += f"--{boundary}{crlf}Content-Disposition: form-data; name=\"test\"{crlf}{crlf}true{crlf}"
    meta += f"--{boundary}{crlf}Content-Disposition: form-data; name=\"recordedAt\"{crlf}{crlf}{recorded_at}{crlf}"
    meta += f"--{boundary}{crlf}Content-Disposition: form-data; name=\"client\"{crlf}{crlf}ring{crlf}"
    meta += f"--{boundary}--{crlf}"
    return out + meta.encode()


def signed_request(trigger: str, *, transcript: str | None = None, payload: Any = None,
                   secret: str | None = SECRET, delivery: str | None = None, test: bool = False,
                   audio: bytes | None = None, ts: int | None = None) -> tuple[bytes, dict[str, str]]:
    delivery = delivery or str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    headers = {"X-Index-Webhook-Version": "1", "X-Index-Trigger": trigger, "X-Index-Delivery": delivery}
    if trigger in ("single-click-hold", "double-click-hold") or (test and payload is None and transcript):
        boundary = str(uuid.uuid4())
        body = multipart_body(boundary, transcript, now_ms, audio=audio, test=test)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        gesture = trigger if trigger != "test-event" else "click"
        doc: dict[str, Any] = {"event": "button", "gesture": gesture, "clicks": 1, "pressedAt": now_ms,
                               "client": "ring"}
        if test:
            doc["test"] = True
        if payload is not None:
            doc["payload"] = payload
        body = json.dumps(doc, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if test:
        headers["X-Index-Test"] = "true"
    if secret:
        stamp = ts if ts is not None else int(time.time())
        headers["X-Index-Timestamp"] = str(stamp)
        headers["X-Index-Signature"] = sign(secret, "1", stamp, delivery, trigger, test, body)
    return body, headers


# ----------------------------------------------------------------------------- v2 fixtures
# Shared by tests/test_v2_*.py. Everything network-facing goes through respx; the ntfy fixture
# is a dependency of every runtime/app factory below, so a test can never reach the internet.

ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
CONTEXT_H = {"Authorization": f"Bearer {CONTEXT}"}
LINK_SECRET = b"link-secret-for-tests-0123456789abcdef"
NTFY_URL = "https://ntfy.test/"

TESLA_BASE = "https://tesla.test"
TESLA_AUTH = "https://auth.tesla.test/oauth2/v3/token"
TESLA_VIN = "5YJTEST000000001"
TESLA_VEHICLE = f"{TESLA_BASE}/api/1/vehicles/{TESLA_VIN}"
TESLA_CONFIG: dict[str, Any] = {"tesla": {
    "base_url": TESLA_BASE, "vin": TESLA_VIN, "client_id": "client-id", "refresh_token": "refresh-0",
    "auth_url": TESLA_AUTH, "state_ttl_seconds": 30, "wake_timeout_seconds": 5,
}}


class NtfyMock:
    """respx routes for the ntfy JSON API plus helpers over the captured publish bodies."""

    def __init__(self, router: Any) -> None:
        import httpx

        self.router = router
        self.publish = router.post(NTFY_URL).mock(return_value=httpx.Response(200, json={"id": "m"}))
        self.clear = router.put(url__regex=r"^https://ntfy\.test/[A-Za-z0-9_-]+/[^/]+/clear$").mock(
            return_value=httpx.Response(200, json={}))

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(call.request.content) for call in self.publish.calls]

    def texts(self) -> list[str]:
        return [f"{b.get('title', '')}\n{b['message']}" for b in self.bodies]

    def find(self, needle: str) -> dict[str, Any]:
        for body in self.bodies:
            if needle in body.get("title", "") or needle in body["message"]:
                return body
        raise AssertionError(f"no push containing {needle!r}; got {self.texts()!r}")

    def last(self) -> dict[str, Any]:
        assert self.publish.calls, "nothing was pushed"
        return self.bodies[-1]

    def reset(self) -> None:
        self.publish.calls.clear()
        self.clear.calls.clear()


_SEEDED: dict[bool, bytes] = {}


def preseed(cfg: Config) -> None:
    """First boot, done once per test session: copy a database already holding the default
    catalog into ``cfg``'s data dir (unless one exists). The seed only depends on whether Tesla
    is configured, and building it per test would dominate the suite's run time. First boot
    itself is still exercised by the tests that opt out (``seeded=False``)."""
    path = cfg.server.data_dir / "ringhub.sqlite3"
    if path.exists():
        return
    key = cfg.tesla is not None
    if key not in _SEEDED:
        import asyncio
        import tempfile
        from concurrent.futures import ThreadPoolExecutor

        from ringhub.catalog import Catalog
        from ringhub.seed import seed_if_empty
        from ringhub.store import Store

        def build() -> bytes:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "seed.sqlite3"
                store = Store(db)
                try:
                    assert asyncio.run(seed_if_empty(Catalog(store), cfg))
                finally:
                    store.close()
                return db.read_bytes()

        with ThreadPoolExecutor(1) as pool:  # works whether or not an event loop is running here
            _SEEDED[key] = pool.submit(build).result()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_SEEDED[key])


@pytest.fixture
def ntfy() -> Any:
    import respx

    with respx.mock(assert_all_called=False) as router:
        yield NtfyMock(router)


@pytest.fixture
async def make_runtime(make_config: Callable[..., Config], ntfy: NtfyMock) -> Any:
    """Build a real Runtime (store, catalog, context, memory, scheduler) on a temp database."""
    import httpx

    from ringhub.runtime import Runtime
    from ringhub.seed import seed_if_empty
    from ringhub.store import Store

    built: list[tuple[Any, Any, Any]] = []

    async def factory(cfg: Config | None = None, *, seed: bool = True) -> Runtime:
        cfg = cfg or make_config()
        if seed:
            preseed(cfg)
        store = Store(cfg.server.data_dir / "ringhub.sqlite3")
        client = httpx.AsyncClient(timeout=5)
        rt = Runtime(cfg, store, client, link_secret=LINK_SECRET)
        built.append((rt, client, store))
        if seed:
            await seed_if_empty(rt.catalog, cfg)
        return rt

    yield factory
    for rt, client, store in reversed(built):
        await rt.aclose()
        await client.aclose()
        store.close()


@pytest.fixture
def make_client(make_config: Callable[..., Config], ntfy: NtfyMock) -> Any:
    """TestClient(create_app(cfg)) with the lifespan running; closed at teardown."""
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from ringhub.app import create_app

    stack = ExitStack()

    def factory(cfg: Config | None = None, *, seeded: bool = True, **kwargs: Any) -> TestClient:
        cfg = cfg or make_config()
        if seeded:
            preseed(cfg)
        return stack.enter_context(TestClient(create_app(cfg), **kwargs))

    yield factory
    stack.close()


def ring_event(gesture: Any, *, transcript: str | None = None, at: float | None = None,
               audio: Path | None = None, delivery: str | None = None) -> Any:
    """A combo-resolved RingEvent as the engine sees it."""
    from datetime import UTC, datetime

    from ringhub.models import Gesture, RingEvent

    g = gesture if isinstance(gesture, Gesture) else Gesture(gesture)
    now = datetime.now(UTC)
    occurred = datetime.fromtimestamp(at, UTC) if at is not None else now
    return RingEvent(delivery_id=delivery or f"t-{uuid.uuid4().hex[:10]}", gesture=g, received_at=now,
                     occurred_at=occurred, transcript=transcript, audio_path=audio, sequence=(g,))


async def eventually(check: Callable[[], Any], within: float = 3.0) -> Any:
    """Poll an (a)sync predicate until it returns something truthy (no fixed sleeps)."""
    import asyncio
    import inspect

    deadline = time.monotonic() + within
    while True:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def wait_until(check: Callable[[], Any], within: float = 3.0) -> Any:
    """Synchronous twin of :func:`eventually` for TestClient-based tests."""
    deadline = time.monotonic() + within
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def shared_app_client(root: Path) -> Any:
    """Context manager: a respx-mocked app (same config as ``make_config()``) for a class-scoped
    fixture. Only for tests that tolerate shared state (auth checks, rejected writes)."""
    from contextlib import contextmanager

    import respx
    from fastapi.testclient import TestClient

    from ringhub.app import create_app

    @contextmanager
    def cm() -> Any:
        doc = {
            "timezone": "America/Chicago",
            "server": {"data_dir": str(root / "data"), "admin_token": ADMIN, "context_token": CONTEXT,
                       "public_url": "https://hub.test"},
            "ring": {"secrets": {"default": SECRET}, "combo_window_ms": 150},
            "ntfy": {"url": "https://ntfy.test", "topic": "ring"},
        }
        path = root / "config.yaml"
        path.write_text(yaml.safe_dump(doc))
        cfg = load_config(path)
        preseed(cfg)
        with respx.mock(assert_all_called=False) as router:
            NtfyMock(router)
            with TestClient(create_app(cfg)) as client:
                yield client

    return cm()
