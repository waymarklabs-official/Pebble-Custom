"""HTTP surface: the ring webhook, health check, and a token-protected admin API."""

from __future__ import annotations

import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from . import __version__
from .actions.base import ActionError
from .config import Config, GestureField
from .engine import Engine
from .executor import validate_config
from .models import Gesture, RingEvent
from .runtime import Runtime
from .store import Store
from .webhook import WebhookError, authenticate, is_test_delivery, parse_button_body

log = logging.getLogger(__name__)


@dataclass
class Services:
    config: Config
    store: Store
    runtime: Runtime
    engine: Engine


def _services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(_services)]


class SimulateBody(BaseModel):
    gesture: GestureField
    transcript: str | None = None
    payload: Any = None


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


def create_app(config: Config) -> FastAPI:
    validate_config(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(config.server.data_dir / "ringhub.sqlite3")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            runtime = Runtime(config, store, client)
            engine = Engine(runtime)
            await engine.start()
            app.state.services = Services(config, store, runtime, engine)
            log.info("ringhub %s ready: %d rules, %d intents, %d devices, modes=%s",
                     __version__, len(config.rules), len(config.intents), len(config.devices), config.modes)
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
        log.warning("rejected webhook from %s: %s", request.client.host if request.client else "?", exc.detail)
        return JSONResponse({"error": exc.detail}, status_code=exc.status)

    @app.get("/healthz")
    async def healthz(svc: Svc) -> dict[str, Any]:
        return {"ok": True, "version": __version__, "mode": await svc.runtime.mode()}

    @app.post(config.server.webhook_path)
    async def ring_webhook(request: Request, svc: Svc) -> dict[str, Any]:
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
            if ring.notify_on_test and svc.runtime.notifier.enabled:
                try:
                    await svc.runtime.notifier.publish("Ring -> hub -> ntfy works.", title="ringhub test OK",
                                                       tags=["white_check_mark"])
                except ActionError as e:
                    log.warning("test notification failed: %s", e)
            return {"ok": True, "test": True}

        gesture = envelope.gesture
        if gesture is None:
            raise WebhookError(400, f"unknown trigger {envelope.trigger!r}")
        event = await _parse_event(request, body, svc, envelope.delivery_id, gesture)

        if not await svc.store.claim_delivery(envelope.delivery_id, ring.dedup_ttl_hours * 3600):
            return {"ok": True, "duplicate": True}
        await svc.engine.submit(event)
        return {"ok": True}

    # ------------------------------------------------------------------ admin API

    def require_admin(request: Request, svc: Svc) -> None:
        token = svc.config.server.admin_token
        if token is None:
            raise HTTPException(404)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
            raise HTTPException(401, "bad admin token")

    admin = [Depends(require_admin)]

    @app.post("/api/simulate", dependencies=admin)
    async def simulate(body: SimulateBody, svc: Svc) -> dict[str, Any]:
        """Fire a gesture as if the ring sent it (goes through the combo engine too)."""
        now = datetime.now(UTC)
        event = RingEvent(delivery_id=f"sim-{uuid.uuid4().hex[:12]}", gesture=body.gesture,
                          received_at=now, occurred_at=now, transcript=body.transcript,
                          payload=body.payload)
        await svc.engine.submit(event)
        return {"ok": True, "delivery_id": event.delivery_id}

    @app.get("/api/events", dependencies=admin)
    async def events(svc: Svc, limit: int = 50) -> list[dict[str, Any]]:
        return await svc.store.recent_events(max(1, min(limit, 500)))

    @app.get("/api/state", dependencies=admin)
    async def state(svc: Svc) -> dict[str, Any]:
        return {"mode": await svc.runtime.mode(), "state": await svc.store.all_state()}

    @app.get("/api/jobs", dependencies=admin)
    async def jobs(svc: Svc) -> list[dict[str, Any]]:
        return [{"id": j.id, "run_at": j.run_at, "steps": j.steps} for j in await svc.runtime.scheduler.pending()]

    @app.delete("/api/jobs/{job_id}", dependencies=admin)
    async def cancel_job(job_id: str, svc: Svc) -> dict[str, Any]:
        return {"cancelled": await svc.runtime.scheduler.cancel(job_id)}

    return app
