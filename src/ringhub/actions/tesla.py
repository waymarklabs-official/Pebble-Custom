"""Tesla Fleet API commands (precondition, frunk, trunk, lock, honk, charge port...).

Model Y requires signed vehicle commands, so point ``tesla.base_url`` at Tesla's
``tesla-http-proxy`` (github.com/teslamotors/vehicle-command) which signs and forwards them.
Refresh tokens rotate on every use; the newest one is persisted in the hub's state store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import Field

from ..config import TeslaConfig
from .base import ActionError, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime
    from ..store import Store

log = logging.getLogger(__name__)
_REFRESH_KEY = "tesla.refresh_token"


class TeslaClient:
    def __init__(self, cfg: TeslaConfig, store: Store) -> None:
        self._cfg = cfg
        self._store = store
        verify: Any = str(cfg.ca_cert) if cfg.ca_cert else True
        self._http = httpx.AsyncClient(verify=verify, timeout=20)
        self._access: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _token(self) -> str:
        async with self._lock:
            if self._access and time.time() < self._expires - 60:
                return self._access
            refresh = await self._store.get(_REFRESH_KEY) or self._cfg.refresh_token.get_secret_value()
            try:
                resp = await self._http.post(self._cfg.auth_url, data={
                    "grant_type": "refresh_token",
                    "client_id": self._cfg.client_id,
                    "refresh_token": refresh,
                })
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ActionError(f"tesla token refresh failed: {e!r}") from None
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))
            if data.get("refresh_token"):
                await self._store.set(_REFRESH_KEY, data["refresh_token"])
            return self._access

    async def _call(self, method: str, path: str, json: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        url = f"{self._cfg.base_url.rstrip('/')}/api/1/vehicles/{self._cfg.vin}{path}"
        try:
            resp = await self._http.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as e:
            raise ActionError(f"tesla {path}: {e!r}") from None
        if resp.status_code == 408:
            raise ActionError("tesla: vehicle is asleep/unreachable")
        if resp.status_code >= 400:
            raise ActionError(f"tesla {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json().get("response")

    async def wake(self) -> None:
        deadline = time.monotonic() + self._cfg.wake_timeout_seconds
        await self._call("POST", "/wake_up")
        while time.monotonic() < deadline:
            info = await self._call("GET", "")
            if isinstance(info, dict) and info.get("state") == "online":
                return
            await asyncio.sleep(3)
        raise ActionError("tesla: vehicle did not wake up in time")

    async def command(self, name: str, params: dict[str, Any], wake: bool) -> Any:
        if wake:
            await self.wake()
        result = await self._call("POST", f"/command/{name}", json=params or None)
        if isinstance(result, dict) and result.get("result") is False:
            raise ActionError(f"tesla {name} rejected: {result.get('reason') or 'unknown reason'}")
        return result

    async def aclose(self) -> None:
        await self._http.aclose()


class TeslaParams(Params):
    command: str = Field(pattern=r"^[a-z_]{3,64}$")
    """Fleet API command, e.g. auto_conditioning_start, actuate_trunk, door_lock, flash_lights."""
    params: dict[str, Any] = Field(default_factory=dict)
    wake: bool = True


@action("tesla", TeslaParams, primary="command")
async def tesla(p: TeslaParams, rt: Runtime, v: Vars) -> Vars:
    if rt.tesla is None:
        raise ActionError("tesla action used but no 'tesla' section is configured")
    result = await rt.tesla.command(p.command, p.params, p.wake)
    return {"tesla": {"command": p.command, "result": result}}
