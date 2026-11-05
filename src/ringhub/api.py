"""REST API (``/api/v1``) for the dashboard, the phone's context feed, and scripting.

Auth:
- **Dashboard**: ``POST /api/v1/session`` with the admin token sets a signed, HttpOnly,
  SameSite=Strict cookie. Cookie-authenticated writes must also send ``X-Ringhub: 1`` (a custom
  header cannot be sent cross-site without CORS, which is never enabled), which blocks CSRF.
- **Scripts**: ``Authorization: Bearer <admin_token>``.
- **Phone context feed**: ``Authorization: Bearer <context_token>``; it can only write context.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from .catalog import BUILTIN_SKILLS, FALLBACK_MODE, CatalogError, Mode, Place, Skill, slugify
from .config import Config, GestureField
from .engine import Engine, label
from .executor import StepError, validate_skill_steps
from .models import RingEvent
from .modes import binding
from .runtime import Runtime
from .store import Store
from .templates import TemplateError, compile_skill, describe

SESSION_COOKIE = "ringhub_session"
SESSION_TTL = 30 * 86400
GESTURES = ["click", "double-click", "triple-click", "single-click-hold", "double-click-hold"]


@dataclass
class Services:
    config: Config
    store: Store
    runtime: Runtime
    engine: Engine
    secret: bytes


def _svc(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(_svc)]


# ----------------------------------------------------------------------------- auth


def _session_sig(secret: bytes, exp: int) -> str:
    return hmac.new(secret, f"session\n{exp}".encode(), hashlib.sha256).hexdigest()


def make_session(secret: bytes, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_TTL)
    return f"{exp}.{_session_sig(secret, exp)}"


def session_valid(secret: bytes, value: str, now: float | None = None) -> bool:
    exp_s, _, sig = value.partition(".")
    if not exp_s.isdigit() or int(exp_s) < (now or time.time()):
        return False
    return hmac.compare_digest(_session_sig(secret, int(exp_s)), sig)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _token_ok(supplied: str | None, expected: Any) -> bool:
    if supplied is None or expected is None:
        return False
    return hmac.compare_digest(supplied.encode(), expected.get_secret_value().encode())


def require_admin(request: Request, svc: Svc) -> None:
    if svc.config.server.admin_token is None:
        raise HTTPException(503, "dashboard disabled: set server.admin_token")
    if _token_ok(_bearer(request), svc.config.server.admin_token):
        return
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and session_valid(svc.secret, cookie):
        if request.method not in ("GET", "HEAD") and request.headers.get("x-ringhub") != "1":
            raise HTTPException(403, "missing X-Ringhub header")
        return
    raise HTTPException(401, "not signed in")


def require_context_writer(request: Request, svc: Svc) -> None:
    if _token_ok(_bearer(request), svc.config.server.context_token):
        return
    require_admin(request, svc)


Admin = [Depends(require_admin)]


def _owntracks_auth(request: Request, svc: Services) -> bool:
    expected = svc.config.server.context_token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            _, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return _token_ok(password, expected)
    return _token_ok(_bearer(request), expected)


async def handle_owntracks(svc: Services, msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Map OwnTracks messages onto context; reply with our places as waypoints when they change."""
    rt = svc.runtime
    places = await rt.catalog.places()
    by_name = {}
    for p in places:
        for spoken in p.spoken():
            by_name[spoken] = p.id
    kind = msg.get("_type")
    stamp = float(msg.get("tst") or time.time())
    stamp = min(stamp, time.time())
    if kind == "transition":
        place = by_name.get(str(msg.get("desc", "")).strip().lower())
        if place:
            if msg.get("event") == "enter":
                await rt.context.arrive(place, source="owntracks", at=stamp)
            elif msg.get("event") == "leave":
                await rt.context.leave(place, source="owntracks", at=stamp)
    if kind in ("location", "transition") and isinstance(msg.get("lat"), int | float):
        await rt.context.set("location", {"lat": msg["lat"], "lon": msg["lon"], "accuracy": msg.get("acc"),
                                          "at": stamp}, source="owntracks", at=stamp)
    # Push our geofences to the phone whenever the place list changed (max 20: iOS limit).
    fenced = [p for p in places if p.lat is not None and p.lon is not None][:20]
    digest = hashlib.sha256(json.dumps([p.model_dump(mode="json") for p in fenced], sort_keys=True)
                            .encode()).hexdigest()
    if fenced and await rt.store.get("owntracks.waypoints") != digest:
        await rt.store.set("owntracks.waypoints", digest)
        return [{"_type": "cmd", "action": "setWaypoints", "waypoints": {"_type": "waypoints", "waypoints": [
            {"_type": "waypoint", "desc": p.name, "lat": p.lat, "lon": p.lon, "rad": p.radius_m,
             "tst": int(time.time())} for p in fenced]}}]
    return []


# ----------------------------------------------------------------------------- bodies


class LoginBody(BaseModel):
    token: str = Field(max_length=500)


class ContextBody(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict)
    location: dict[str, float] | None = None
    arrive: str | None = None
    leave: str | None = None
    focus: str | None = None


class SimulateBody(BaseModel):
    gesture: GestureField
    transcript: str | None = None


class OverrideBody(BaseModel):
    mode: str | None = None
    minutes: int | None = Field(None, ge=1, le=24 * 60)


class OrderBody(BaseModel):
    ids: list[str]


class ReminderBody(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    trigger: Literal["time", "arrive", "leave", "none"] = "none"
    at: datetime | None = None
    after_minutes: int | None = Field(None, ge=1, le=60 * 24 * 366)
    place: str | None = None


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


def _place_slug(raw: str) -> str:
    slug = slugify(raw)
    if not slug:
        raise HTTPException(400, "bad place")
    return slug


def build_router(config: Config) -> APIRouter:
    r = APIRouter(prefix="/api/v1")

    # ------------------------------------------------------------------ session

    @r.post("/session")
    async def login(body: LoginBody, request: Request, response: Response, svc: Svc) -> dict[str, Any]:
        if not _token_ok(body.token.strip(), svc.config.server.admin_token):
            raise HTTPException(401, "wrong token")
        response.set_cookie(SESSION_COOKIE, make_session(svc.secret), max_age=SESSION_TTL, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", path="/")
        return {"ok": True}

    @r.delete("/session")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @r.get("/session")
    async def session(request: Request, svc: Svc) -> dict[str, Any]:
        cookie = request.cookies.get(SESSION_COOKIE)
        ok = bool(cookie and session_valid(svc.secret, cookie)) or _token_ok(_bearer(request),
                                                                              svc.config.server.admin_token)
        return {"authenticated": ok, "enabled": svc.config.server.admin_token is not None}

    # ------------------------------------------------------------------ now

    @r.get("/now", dependencies=Admin)
    async def now(svc: Svc) -> dict[str, Any]:
        rt = svc.runtime
        ctx = await rt.context.snapshot()
        res = await rt.resolution(ctx)
        modes = await rt.catalog.modes()
        gestures = []
        for key in GESTURES:
            sid, from_mode = binding(modes, res.mode, key)
            skill = await rt.catalog.skill(sid) if sid else None
            gestures.append({
                "key": key, "label": label(key),
                "skill": {"id": skill.id, "name": skill.name, "icon": skill.icon} if skill else None,
                "inherited": bool(from_mode and from_mode.id != res.mode.id),
            })
        att = await rt.memory.attention()
        upcoming = [x.as_dict() for x in (await rt.reminders.open())[:8]]
        recent = [n.as_dict() for n in await rt.notes.recent(8)]
        vehicle = None
        if rt.vehicle is not None and rt.vehicle._cached is not None:
            vehicle = rt.vehicle._cached.as_dict()
        return {
            "mode": res.mode.model_dump(mode="json"), "reason": res.reason, "manual": res.manual,
            "gestures": gestures, "context": ctx, "attention": att, "reminders": upcoming, "notes": recent,
            "vehicle": vehicle, "features": {"tesla": rt.tesla is not None, "ntfy": rt.notifier.enabled,
                                             "llm": rt.llm is not None, "tv": bool(svc.config.devices),
                                             "links": rt.links.public_url is not None},
        }

    @r.post("/override", dependencies=Admin)
    async def set_override(body: OverrideBody, svc: Svc) -> dict[str, Any]:
        try:
            await svc.runtime.set_override(body.mode, timedelta(minutes=body.minutes) if body.minutes else None)
        except Exception as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    @r.post("/simulate", dependencies=Admin)
    async def simulate(body: SimulateBody, svc: Svc) -> dict[str, Any]:
        """Fire a gesture as if the ring sent it (goes through combos and the attention layer)."""
        stamp = datetime.now(UTC)
        event = RingEvent(delivery_id=f"sim-{uuid.uuid4().hex[:12]}", gesture=body.gesture,
                          received_at=stamp, occurred_at=stamp, transcript=body.transcript)
        await svc.engine.submit(event)
        return {"ok": True, "delivery_id": event.delivery_id}

    # ------------------------------------------------------------------ catalog: places

    @r.get("/places", dependencies=Admin)
    async def places(svc: Svc) -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in await svc.runtime.catalog.places()]

    @r.put("/places/{pid}", dependencies=Admin)
    async def put_place(pid: str, body: dict[str, Any], svc: Svc) -> dict[str, Any]:
        try:
            place = Place.model_validate({**body, "id": pid})
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        await svc.runtime.catalog.put(place)
        return place.model_dump(mode="json")

    @r.delete("/places/{pid}", dependencies=Admin)
    async def delete_place(pid: str, svc: Svc) -> dict[str, Any]:
        users = [m.name for m in await svc.runtime.catalog.modes()
                 if pid in m.when.places or pid in m.when.not_places
                 or (m.when.recently and m.when.recently.place == pid)]
        if users:
            raise HTTPException(409, f"used by modes: {', '.join(users)}")
        return {"deleted": await svc.runtime.catalog.delete("place", pid)}

    # ------------------------------------------------------------------ catalog: skills

    @r.get("/templates", dependencies=Admin)
    async def templates() -> list[dict[str, Any]]:
        return describe()

    def _check_skill(skill: Skill) -> None:
        try:
            steps = compile_skill(skill)
            validate_skill_steps(steps, config, f"skill {skill.id!r}")
        except (TemplateError, StepError) as e:
            raise HTTPException(422, str(e)) from None

    @r.get("/skills", dependencies=Admin)
    async def skills(svc: Svc) -> list[dict[str, Any]]:
        modes = await svc.runtime.catalog.modes()
        out = []
        for s in await svc.runtime.catalog.skills():
            d = s.model_dump(mode="json")
            d["builtin"] = s.builtin
            d["used_by"] = [{"mode": m.id, "name": m.name, "gesture": k} for m in modes
                            for k, v in m.bindings.items() if v == s.id]
            out.append(d)
        return out

    @r.post("/skills/validate", dependencies=Admin)
    async def validate_skill(body: dict[str, Any]) -> dict[str, Any]:
        try:
            skill = Skill.model_validate({**body, "id": body.get("id") or "draft"})
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        _check_skill(skill)
        return {"ok": True, "steps": compile_skill(skill)}

    @r.put("/skills/{sid}", dependencies=Admin)
    async def put_skill(sid: str, body: dict[str, Any], svc: Svc) -> dict[str, Any]:
        existing = await svc.runtime.catalog.skill(sid)
        if sid in BUILTIN_SKILLS:
            if existing is None:
                raise HTTPException(400, "built-in skill missing; re-import defaults")
            # Built-ins: only cosmetic fields may change.
            body = {**existing.model_dump(), **{k: v for k, v in body.items() if k in ("name", "icon", "description")}}
        elif body.get("template") == "builtin":
            raise HTTPException(400, "only built-in skills can use the builtin template")
        try:
            skill = Skill.model_validate({**body, "id": sid})
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        _check_skill(skill)
        await svc.runtime.catalog.put(skill)
        return skill.model_dump(mode="json")

    @r.delete("/skills/{sid}", dependencies=Admin)
    async def delete_skill(sid: str, svc: Svc) -> dict[str, Any]:
        if sid in BUILTIN_SKILLS:
            raise HTTPException(400, "built-in skills can't be deleted")
        users = [m.name for m in await svc.runtime.catalog.modes() if sid in m.bindings.values()]
        if users:
            raise HTTPException(409, f"still assigned in modes: {', '.join(users)}")
        return {"deleted": await svc.runtime.catalog.delete("skill", sid)}

    @r.post("/skills/{sid}/run", dependencies=Admin)
    async def run_skill(sid: str, svc: Svc) -> dict[str, Any]:
        rt = svc.runtime
        variables = await rt.base_vars()
        stamp = datetime.now(UTC)
        variables["event"] = {"id": f"dash-{uuid.uuid4().hex[:8]}", "gesture": "dashboard", "sequence": [],
                              "transcript": "", "audio_path": None, "payload": None, "at": stamp}
        try:
            await rt.run_skill(sid, variables, via="dashboard")
        except Exception as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    # ------------------------------------------------------------------ catalog: modes

    async def _check_mode(mode: Mode, svc: Services) -> None:
        skill_ids = {s.id for s in await svc.runtime.catalog.skills()}
        missing = sorted(set(mode.bindings.values()) - skill_ids)
        if missing:
            raise HTTPException(422, f"unknown skills: {', '.join(missing)}")

    @r.get("/modes", dependencies=Admin)
    async def modes(svc: Svc) -> list[dict[str, Any]]:
        return [m.model_dump(mode="json") for m in await svc.runtime.catalog.modes()]

    @r.put("/modes/{mid}", dependencies=Admin)
    async def put_mode(mid: str, body: dict[str, Any], svc: Svc) -> dict[str, Any]:
        try:
            mode = Mode.model_validate({**body, "id": mid})
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        await _check_mode(mode, svc)
        await svc.runtime.catalog.put(mode)
        return mode.model_dump(mode="json")

    @r.delete("/modes/{mid}", dependencies=Admin)
    async def delete_mode(mid: str, svc: Svc) -> dict[str, Any]:
        if mid == FALLBACK_MODE:
            raise HTTPException(400, "the Everywhere mode can't be deleted")
        return {"deleted": await svc.runtime.catalog.delete("mode", mid)}

    @r.post("/modes/order", dependencies=Admin)
    async def order_modes(body: OrderBody, svc: Svc) -> dict[str, Any]:
        ids = [i for i in body.ids if i != FALLBACK_MODE] + [FALLBACK_MODE]
        try:
            await svc.runtime.catalog.reorder("mode", ids)
        except CatalogError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    @r.get("/catalog/export", dependencies=Admin)
    async def export(svc: Svc) -> dict[str, Any]:
        return {"version": 1, **(await svc.runtime.catalog.export())}

    @r.post("/catalog/import", dependencies=Admin)
    async def import_(body: dict[str, Any], svc: Svc) -> dict[str, Any]:
        try:
            await svc.runtime.catalog.replace_all(body)
        except (ValidationError, CatalogError) as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True}

    # ------------------------------------------------------------------ memory

    @r.get("/notes", dependencies=Admin)
    async def notes(svc: Svc, q: str = "", limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if q.strip():
            found = await svc.runtime.notes.search(q, limit)
        else:
            found = await svc.runtime.notes.recent(limit, kinds=[kind] if kind else None)
        return [n.as_dict() for n in found]

    @r.patch("/notes/{nid}", dependencies=Admin)
    async def edit_note(nid: int, body: NoteBody, svc: Svc) -> dict[str, Any]:
        if not await svc.runtime.notes.update_text(nid, body.text):
            raise HTTPException(404, "no such note")
        return {"ok": True}

    @r.delete("/notes/{nid}", dependencies=Admin)
    async def delete_note(nid: int, svc: Svc) -> dict[str, Any]:
        return {"deleted": await svc.runtime.notes.delete(nid)}

    @r.get("/reminders", dependencies=Admin)
    async def reminders(svc: Svc, status: Literal["open", "all"] = "open") -> list[dict[str, Any]]:
        items = await svc.runtime.reminders.open() if status == "open" else await svc.runtime.reminders.recent(200)
        return [x.as_dict() for x in items]

    @r.post("/reminders", dependencies=Admin)
    async def add_reminder(body: ReminderBody, svc: Svc) -> dict[str, Any]:
        rt = svc.runtime
        if body.trigger == "time":
            if body.after_minutes:
                due = time.time() + body.after_minutes * 60
            elif body.at:
                due = (body.at if body.at.tzinfo else body.at.replace(tzinfo=rt.config.tz)).timestamp()
            else:
                raise HTTPException(422, "time reminders need 'at' or 'after_minutes'")
            if due <= time.time():
                raise HTTPException(422, "that time has already passed")
            rem = await rt.reminders.add(body.text, "time", due_at=due)
            await rt.memory.schedule(rem)
        elif body.trigger in ("arrive", "leave"):
            if not body.place:
                raise HTTPException(422, "place reminders need 'place'")
            rem = await rt.reminders.add(body.text, body.trigger, place=_place_slug(body.place))
        else:
            rem = await rt.reminders.add(body.text, "none")
        return rem.as_dict()

    @r.post("/reminders/{rid}/{action}", dependencies=Admin)
    async def reminder_action(rid: str, action: Literal["done", "snooze", "cancel"], svc: Svc) -> dict[str, Any]:
        mem = svc.runtime.memory
        result = await (mem.done(rid) if action == "done" else mem.snooze(rid) if action == "snooze"
                        else mem.cancel(rid))
        if result is None:
            raise HTTPException(409, "reminder is not open")
        return result.as_dict()

    # ------------------------------------------------------------------ context (phone)

    @r.get("/context", dependencies=Admin)
    async def context(svc: Svc) -> dict[str, Any]:
        return {"now": await svc.runtime.context.snapshot(),
                "history": await svc.runtime.context.history(100)}

    async def _apply_context(svc: Services, body: ContextBody, source: str) -> dict[str, Any]:
        ctx = svc.runtime.context
        if body.leave:
            await ctx.leave(_place_slug(body.leave), source=source)
        if body.arrive:
            await ctx.arrive(_place_slug(body.arrive), source=source)
        if body.focus is not None:
            f = body.focus.strip().lower()
            await ctx.set("focus", None if f in ("", "off", "none") else f[:32], source=source)
        if body.location is not None:
            lat, lon = body.location.get("lat"), body.location.get("lon")
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise HTTPException(422, "location needs lat/lon")
            await ctx.set("location", {"lat": lat, "lon": lon, "accuracy": body.location.get("accuracy"),
                                       "at": time.time()}, source=source)
        for key, value in body.set.items():
            if key in ("place", "place_since", "last_left", "focus", "location"):
                raise HTTPException(422, f"use arrive/leave/focus/location for {key!r}")
            if not key.replace("_", "").isalnum() or len(key) > 32:
                raise HTTPException(422, f"bad context key {key!r}")
            await ctx.set(key.lower(), value, source=source)
        res = await svc.runtime.resolution()
        return {"ok": True, "mode": res.mode.id, "place": await ctx.get("place")}

    @r.post("/context", dependencies=[Depends(require_context_writer)])
    async def post_context(body: ContextBody, svc: Svc) -> dict[str, Any]:
        return await _apply_context(svc, body, "api")

    # Path-style endpoints: trivial to build in iOS Shortcuts (no JSON needed).
    @r.post("/context/arrive/{place}", dependencies=[Depends(require_context_writer)])
    async def arrive(place: str, svc: Svc) -> dict[str, Any]:
        return await _apply_context(svc, ContextBody(arrive=place), "shortcut")

    @r.post("/context/leave/{place}", dependencies=[Depends(require_context_writer)])
    async def leave(place: str, svc: Svc) -> dict[str, Any]:
        return await _apply_context(svc, ContextBody(leave=place), "shortcut")

    @r.post("/context/visit/{place}", dependencies=[Depends(require_context_writer)])
    async def visit(place: str, svc: Svc) -> dict[str, Any]:
        """Wallet tap / NFC tag: fire "next time I'm at X" reminders without moving you there."""
        await svc.runtime.context.visit(_place_slug(place), source="shortcut")
        return {"ok": True}

    @r.post("/owntracks")
    async def owntracks(request: Request, svc: Svc) -> list[dict[str, Any]]:
        """OwnTracks HTTP mode: silent, battery-friendly presence. Auth: HTTP Basic with the
        context token as password (any username), or a Bearer token."""
        if not _owntracks_auth(request, svc):
            raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})
        try:
            msg = await request.json()
        except ValueError:
            raise HTTPException(400, "expected JSON") from None
        return await handle_owntracks(svc, msg if isinstance(msg, dict) else {})

    @r.post("/context/focus/{name}", dependencies=[Depends(require_context_writer)])
    async def focus(name: str, svc: Svc) -> dict[str, Any]:
        return await _apply_context(svc, ContextBody(focus=name), "shortcut")

    # ------------------------------------------------------------------ misc

    @r.get("/events", dependencies=Admin)
    async def events(svc: Svc, limit: int = 100) -> list[dict[str, Any]]:
        return await svc.store.recent_events(max(1, min(limit, 500)))

    @r.get("/setup", dependencies=Admin)
    async def setup(svc: Svc) -> dict[str, Any]:
        cfg = svc.config.server
        return {
            "public_url": cfg.public_url,
            "webhook_url": f"{cfg.public_url}{cfg.webhook_path}" if cfg.public_url else None,
            "context_token": cfg.context_token.get_secret_value() if cfg.context_token else None,
            "places": [p.model_dump(mode="json") for p in await svc.runtime.catalog.places()],
            "timezone": svc.config.timezone,
        }

    return r
