"""Google Calendar API client: connect (OAuth), add events, read the agenda, undo an add.

Scopes: ``calendar.events`` (create and read events) plus ``openid email`` (to show which account
is connected). The refresh token lives in the hub's state store. Google keeps it valid until it is
revoked, provided the OAuth app is *published* ("In production"): apps left in "Testing" get
refresh tokens that expire after 7 days. docs/INTEGRATIONS.md walks through it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx

from .actions.base import ActionError
from .config import GoogleConfig

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

SCOPES = "openid email https://www.googleapis.com/auth/calendar.events"
REFRESH_KEY = "google.refresh_token"
ACCOUNT_KEY = "google.account"


def _email_from_id_token(id_token: str | None) -> str | None:
    """The account's email from the id_token Google just returned over TLS (display only)."""
    if not id_token or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return str(email) if email else None


class GoogleCalendar:
    def __init__(self, cfg: GoogleConfig, store: Store) -> None:
        self._cfg = cfg
        self._store = store
        self._http = httpx.AsyncClient(timeout=20)
        self._access: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def config(self) -> GoogleConfig:
        return self._cfg

    async def connected(self) -> bool:
        return bool(await self._store.get(REFRESH_KEY))

    async def account(self) -> str | None:
        acct = await self._store.get(ACCOUNT_KEY)
        return str(acct) if acct else None

    # ------------------------------------------------------------------ OAuth

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        query = {
            "client_id": self._cfg.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPES, "state": state, "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{self._cfg.authorize_url}?{urlencode(query)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> str | None:
        try:
            resp = await self._http.post(self._cfg.token_url, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self._cfg.client_id, "client_secret": self._cfg.client_secret.get_secret_value(),
            })
        except httpx.HTTPError as e:
            raise ActionError(f"google: couldn't reach Google to finish connecting ({e!r})") from None
        if resp.status_code >= 400:
            raise ActionError(f"google: connecting failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if not data.get("refresh_token"):
            raise ActionError("google: no refresh token returned — remove the app's access at "
                              "myaccount.google.com/permissions and connect again")
        email = _email_from_id_token(data.get("id_token"))
        async with self._lock:
            await self._store.set(REFRESH_KEY, data["refresh_token"])
            await self._store.set(ACCOUNT_KEY, email)
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))
        return email

    async def disconnect(self) -> None:
        async with self._lock:
            refresh = await self._store.get(REFRESH_KEY)
            await self._store.delete(REFRESH_KEY)
            await self._store.delete(ACCOUNT_KEY)
            self._access, self._expires = None, 0.0
        if refresh:   # best effort: also revoke on Google's side
            try:
                await self._http.post("https://oauth2.googleapis.com/revoke", data={"token": refresh})
            except httpx.HTTPError:
                pass

    async def _token(self) -> str:
        async with self._lock:
            if self._access and time.time() < self._expires - 60:
                return self._access
            refresh = await self._store.get(REFRESH_KEY)
            if not refresh:
                raise ActionError("Google Calendar isn't connected — connect it in the dashboard "
                                  "(Setup → Integrations)")
            try:
                resp = await self._http.post(self._cfg.token_url, data={
                    "grant_type": "refresh_token", "refresh_token": refresh, "client_id": self._cfg.client_id,
                    "client_secret": self._cfg.client_secret.get_secret_value(),
                })
            except httpx.HTTPError as e:
                raise ActionError(f"google token refresh failed: {e!r}") from None
            if resp.status_code in (400, 401):
                # invalid_grant: revoked, expired (an app still in "Testing"), or the password changed.
                raise ActionError("Google Calendar access expired or was revoked — reconnect it in the dashboard")
            try:
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ActionError(f"google token refresh failed: {e!r}") from None
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._access

    # ------------------------------------------------------------------ Calendar API

    async def _request(self, method: str, path: str, *, json_body: Any = None,
                       params: dict[str, str] | None = None) -> Any:
        url = f"{self._cfg.api_url}/calendars/{quote(self._cfg.calendar_id, safe='')}{path}"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            resp = await self._http.request(method, url, json=json_body, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ActionError(f"google calendar: {e!r}") from None
        if resp.status_code == 404:
            raise ActionError(f"google calendar: calendar {self._cfg.calendar_id!r} not found")
        if resp.status_code >= 400:
            raise ActionError(f"google calendar: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    async def add_event(self, summary: str, start: datetime | date, end: datetime | date,
                        description: str | None = None) -> dict[str, Any]:
        def when(d: datetime | date) -> dict[str, str]:
            if isinstance(d, datetime):
                return {"dateTime": d.isoformat(), "timeZone": str(d.tzinfo)} if d.tzinfo else \
                    {"dateTime": d.isoformat()}
            return {"date": d.isoformat()}

        body: dict[str, Any] = {"summary": summary, "start": when(start), "end": when(end)}
        if description:
            body["description"] = description
        event = await self._request("POST", "/events", json_body=body)
        if not isinstance(event, dict) or not event.get("id"):
            raise ActionError("google calendar: unexpected response when adding the event")
        return event

    async def delete_event(self, event_id: str) -> None:
        await self._request("DELETE", f"/events/{quote(event_id, safe='')}")

    async def events(self, start: datetime, end: datetime, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._request("GET", "/events", params={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": str(max(1, min(limit, 50))),
        })
        items = data.get("items") if isinstance(data, dict) else None
        return [e for e in (items or []) if isinstance(e, dict) and e.get("status") != "cancelled"]

    async def aclose(self) -> None:
        await self._http.aclose()
