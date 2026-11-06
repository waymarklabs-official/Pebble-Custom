"""HTTP surface: the ring webhook, signed reminder links, the context feed, the dashboard."""

from __future__ import annotations

import html
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from . import __version__
from .actions.base import ActionError
from .api import Services, build_router
from .config import Config
from .engine import Engine
from .limits import BodyLimitMiddleware
from .models import Gesture, RingEvent
from .oauth import redirect_uri, take_state
from .runtime import Runtime
from .seed import car_skills, ensure_skills, seed_if_empty
from .store import Store
from .webhook import WebhookError, authenticate, is_test_delivery, parse_button_body

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
API_BODY_LIMIT = 1024 * 1024
"""Dashboard/API/context bodies are small; the catalog import is the largest (~tens of KB)."""
LINK_SECRET_KEY = "secret.links"  # noqa: S105 - a state-table key name, not a secret


def _ms_to_dt(ms: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


async def _parse_event(request: Request, body: bytes, svc: Services, delivery_id: str,
                       gesture: Gesture) -> RingEvent:
    now = datetime.now(UTC)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        button = parse_button_body(body)
        if button.gesture is not gesture:
            raise WebhookError(400, "gesture in body does not match X-Index-Trigger")
        return RingEvent(delivery_id=delivery_id, gesture=gesture, received_at=now,
                         occurred_at=_ms_to_dt(button.pressed_at_ms, now), payload=button.payload)

    if not ctype.startswith("multipart/form-data"):
        raise WebhookError(415, f"unsupported content type {ctype!r}")
    form = await request.form(max_files=1, max_fields=16)
    try:
        transcript = form.get("transcription")
        audio = form.get("audio")
        audio_path = None
        if isinstance(audio, UploadFile) and svc.config.ring.keep_audio:
            audio_dir = svc.runtime.audio_dir
            audio_dir.mkdir(parents=True, exist_ok=True)
            # Never trust the uploaded filename; the delivery id is stable per recording.
            safe_id = "".join(c for c in delivery_id if c.isalnum() or c in "-_")[:64] or uuid.uuid4().hex
            audio_path = audio_dir / f"{safe_id}.m4a"
            audio_path.write_bytes(await audio.read())
        return RingEvent(
            delivery_id=delivery_id,
            gesture=gesture,
            received_at=now,
            occurred_at=_ms_to_dt(form.get("recordedAt"), now),
            transcript=transcript.strip() if isinstance(transcript, str) else None,
            audio_path=audio_path,
        )
    finally:
        await form.close()


async def _link_secret(store: Store) -> bytes:
    """Per-install random secret for signed links and dashboard sessions (generated once)."""
    value = await store.get(LINK_SECRET_KEY)
    if not isinstance(value, str) or len(value) < 64:
        value = secrets.token_hex(32)
        await store.set(LINK_SECRET_KEY, value)
    return bytes.fromhex(value)


CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_BACK = '<p><a href="/#/setup">Back to Setup</a></p>'
_LINK_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>ringhub</title><body style="font:16px system-ui;padding:2rem;max-width:30rem;margin:auto">
<p>{text}</p>{form}</body>"""


def create_app(config: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(config.server.data_dir / "ringhub.sqlite3")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            secret = await _link_secret(store)
            runtime = Runtime(config, store, client, link_secret=secret)
            if await seed_if_empty(runtime.catalog, config):
                log.info("first boot: wrote default places, skills and modes")
            if runtime.tesla is not None and await runtime.tesla.connected():
                # A car connected after first boot (or new default car skills): add what's missing.
                added = await ensure_skills(runtime.catalog, car_skills())
                if added:
                    log.info("added car skills: %s", ", ".join(added))
            engine = Engine(runtime)
            await engine.start()
            app.state.services = Services(config, store, runtime, engine, secret)
            log.info("ringhub %s ready (dashboard %s, ntfy %s, tesla %s, google calendar %s)", __version__,
                     "on" if config.server.admin_token else "off — set server.admin_token",
                     "on" if config.ntfy else "off", "on" if config.tesla else "off",
                     "on" if config.google else "off")
            try:
                yield
            finally:
                await engine.stop()
                await runtime.aclose()
                store.close()

    app = FastAPI(title="ringhub", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(WebhookError)
    async def _webhook_error(request: Request, exc: WebhookError) -> JSONResponse:
        host = request.client.host if request.client else "?"
        log.warning("rejected webhook from %s: %s", host, exc.detail)
        return JSONResponse({"error": exc.detail}, status_code=exc.status)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        # Deliberately says nothing about the owner: the active mode reveals home/away/asleep.
        return {"ok": True, "version": __version__}

    @app.post(config.server.webhook_path)
    async def ring_webhook(request: Request) -> dict[str, Any]:
        svc: Services = request.app.state.services
        ring = svc.config.ring
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > ring.max_body_bytes:
            raise WebhookError(413, "body too large")
        body = await request.body()
        if len(body) > ring.max_body_bytes:
            raise WebhookError(413, "body too large")

        trigger = request.headers.get("x-index-trigger", "")
        envelope = authenticate(request.headers, body, ring.secrets_for(trigger),
                                max_skew_seconds=ring.max_skew_seconds)

        if is_test_delivery(envelope):
            log.info("test event received (%s)", request.headers.get("content-type", "?"))
            await svc.store.log_event(delivery_id=envelope.delivery_id, gesture="test",
                                      detail="webhook test", status="ok")
            if ring.notify_on_test:
                await svc.runtime.feedback.say("Ring → hub → phone works.", title="ringhub test OK",
                                               tags=["white_check_mark"])
            return {"ok": True, "test": True}

        gesture = envelope.gesture
        if gesture is None:
            raise WebhookError(400, f"unknown trigger {envelope.trigger!r}")
        event = await _parse_event(request, body, svc, envelope.delivery_id, gesture)

        if not await svc.store.claim_delivery(envelope.delivery_id, ring.dedup_ttl_hours * 3600):
            return {"ok": True, "duplicate": True}
        await svc.engine.submit(event)
        return {"ok": True}

    # ------------------------------------------------------------------ signed reminder links

    @app.api_route("/r/{rid}/{action}", methods=["GET", "POST"], response_model=None)
    async def reminder_link(rid: str, action: str, request: Request, exp: str = "", sig: str = "") -> Response:
        svc: Services = request.app.state.services
        path = f"/r/{rid}/{action}"
        if action not in ("done", "snooze") or not svc.runtime.links.verify(path, exp, sig):
            return HTMLResponse(_LINK_PAGE.format(text="This link is invalid or has expired.", form=""), 403)
        if request.method == "GET":
            # Link previews and prefetchers use GET: never act on it, offer a button instead.
            form = (f'<form method=post action="{path}?exp={exp}&sig={sig}">'
                    f'<button style="font-size:1.2rem;padding:.6rem 1.2rem">{action.title()}</button></form>')
            return HTMLResponse(_LINK_PAGE.format(text="Confirm:", form=form))
        try:
            r = await (svc.runtime.memory.done(rid) if action == "done" else svc.runtime.memory.snooze(rid))
        except ActionError as e:
            return JSONResponse({"error": str(e)}, 400)
        if r is None:
            return JSONResponse({"ok": True, "note": "already handled"})
        return JSONResponse({"ok": True, "status": r.status})

    # ------------------------------------------------------------------ OAuth return trip

    @app.get("/oauth/{provider}/callback", response_model=None)
    async def oauth_callback(provider: str, request: Request, code: str = "", state: str = "",
                             error: str = "", error_description: str = "") -> Response:
        """Tesla/Google send the browser back here. The single-use ``state`` (issued by the
        admin-only connect endpoint) is the authorization; the session cookie isn't sent on this
        cross-site return (SameSite=Strict)."""
        svc: Services = request.app.state.services
        rt = svc.runtime
        client = rt.tesla if provider == "tesla" else rt.gcal if provider == "google" else None
        if client is None:
            return HTMLResponse(_LINK_PAGE.format(text="Unknown or disabled integration.", form=""), 404)
        if not await take_state(svc.store, state, provider):
            return HTMLResponse(_LINK_PAGE.format(
                text="This sign-in link expired or was already used. Start again from Setup → Integrations.",
                form=""), 400)
        if error or not code:
            why = html.escape(error_description or error or "no authorization code")
            return HTMLResponse(_LINK_PAGE.format(text=f"Not connected: {why}", form=_BACK), 400)
        ru = redirect_uri(rt.links.public_url, provider)
        assert ru is not None   # a state is only issued when public_url is set
        try:
            await client.exchange_code(code, ru)
            if provider == "tesla" and rt.tesla is not None:
                try:
                    await rt.tesla.select_vehicle()   # the only car on the account, if there's just one
                except ActionError:
                    pass                               # several (or none): pick in the dashboard
                await ensure_skills(rt.catalog, car_skills())
        except ActionError as e:
            return HTMLResponse(_LINK_PAGE.format(text=f"Not connected: {html.escape(str(e))}", form=_BACK), 502)
        return RedirectResponse(f"/#/setup?connected={provider}", status_code=303)

    app.include_router(build_router(config))
    app.add_middleware(BodyLimitMiddleware, default=API_BODY_LIMIT,
                       overrides={config.server.webhook_path: config.ring.max_body_bytes})

    # ------------------------------------------------------------------ dashboard

    if (WEB_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html", headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            })

    return app
