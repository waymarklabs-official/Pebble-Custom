"""Tesla Fleet API: account connection, partner registration, state reads and signed commands.

- **Commands** go to Tesla's ``tesla-http-proxy`` (github.com/teslamotors/vehicle-command), which
  signs them with your private key; a Model Y only accepts signed commands.
- **State reads** (vehicle list, vehicle_data, online/asleep) don't need signing, so they go
  straight to Fleet API for your region. They work before the proxy is set up, and they never
  wake the car.
- **Tokens:** Tesla refresh tokens are single-use and rotate on every refresh. The newest one is
  kept in the hub's state store, never in config.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from pydantic import Field

from ..config import TeslaConfig
from .base import ActionError, Params, Vars, action

if TYPE_CHECKING:
    from ..runtime import Runtime
    from ..store import Store

log = logging.getLogger(__name__)

SERVERS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}
CN_TOKEN_URL = "https://auth.tesla.cn/oauth2/v3/token"  # noqa: S105 - a URL
PARTNER_SCOPES = "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds"
REFRESH_KEY = "tesla.refresh_token"
REGION_KEY = "tesla.region"
VIN_KEY = "tesla.vin"
# The fixed DER prefix of a P-256 SubjectPublicKeyInfo; the 65-byte uncompressed point follows it.
_P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def public_key_hex(pem: str) -> str:
    """The uncompressed EC point (04||X||Y, hex) of a P-256 public key PEM, as Tesla reports it."""
    body = "".join(line for line in pem.strip().splitlines() if line and not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError as e:
        raise ActionError("public key file is not valid PEM") from e
    if len(der) != 91 or not der.startswith(_P256_SPKI_PREFIX) or der[26] != 0x04:
        raise ActionError("public key must be a prime256v1 (P-256) EC public key")
    return der[26:].hex()


class TeslaClient:
    def __init__(self, cfg: TeslaConfig, store: Store) -> None:
        self._cfg = cfg
        self._store = store
        verify: Any = str(cfg.ca_cert) if cfg.ca_cert and Path(cfg.ca_cert).exists() else True
        # The signing proxy is a local service: never route it through an outbound proxy.
        self._proxy = httpx.AsyncClient(verify=verify, timeout=20, trust_env=False)
        self._http = httpx.AsyncClient(timeout=20)                    # Tesla's own endpoints
        self._access: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def config(self) -> TeslaConfig:
        return self._cfg

    # ------------------------------------------------------------------ account state

    async def _refresh_token(self) -> str | None:
        stored = await self._store.get(REFRESH_KEY)
        if stored:
            return str(stored)
        return self._cfg.refresh_token.get_secret_value() if self._cfg.refresh_token else None

    async def connected(self) -> bool:
        return bool(await self._refresh_token())

    async def region(self) -> str:
        return self._cfg.region or str(await self._store.get(REGION_KEY) or "na")

    async def server(self) -> str:
        return SERVERS[await self.region()]

    async def vin(self) -> str | None:
        return self._cfg.vin or (str(v) if (v := await self._store.get(VIN_KEY)) else None)

    async def _require_vin(self) -> str:
        vin = await self.vin()
        if not vin:
            raise ActionError("tesla: no car selected yet — connect Tesla in the dashboard (Setup → Integrations)")
        return vin

    async def _token_url(self) -> str:
        return CN_TOKEN_URL if await self.region() == "cn" else self._cfg.auth_url

    def _secret(self) -> str:
        secret = self._cfg.client_secret.get_secret_value() if self._cfg.client_secret else ""
        if not secret:
            raise ActionError("tesla: set TESLA_CLIENT_SECRET in .env (needed to connect and to register)")
        return secret

    # ------------------------------------------------------------------ OAuth (owner tokens)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        query = {"response_type": "code", "client_id": self._cfg.client_id, "redirect_uri": redirect_uri,
                 "scope": self._cfg.scopes, "state": state, "prompt_missing_scopes": "true"}
        return f"{self._cfg.authorize_url}?{urlencode(query)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Authorization code -> refresh token (stored). The code's prefix names the region."""
        prefix = code.split("_", 1)[0].lower()
        region = prefix if prefix in SERVERS else (self._cfg.region or "na")
        token_url = CN_TOKEN_URL if region == "cn" else self._cfg.auth_url
        try:
            resp = await self._http.post(token_url, data={
                "grant_type": "authorization_code", "client_id": self._cfg.client_id,
                "client_secret": self._secret(), "code": code, "audience": SERVERS[region],
                "redirect_uri": redirect_uri,
            })
        except httpx.HTTPError as e:
            raise ActionError(f"tesla: couldn't reach Tesla to finish connecting ({e!r})") from None
        if resp.status_code >= 400:
            raise ActionError(f"tesla: connecting failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if not data.get("refresh_token"):
            raise ActionError("tesla: no refresh token returned (the app must request offline_access)")
        async with self._lock:
            await self._store.set(REFRESH_KEY, data["refresh_token"])
            await self._store.set(REGION_KEY, region)
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))

    async def disconnect(self) -> None:
        async with self._lock:
            for key in (REFRESH_KEY, REGION_KEY, VIN_KEY):
                await self._store.delete(key)
            self._access, self._expires = None, 0.0

    async def _token(self) -> str:
        async with self._lock:
            if self._access and time.time() < self._expires - 60:
                return self._access
            refresh = await self._refresh_token()
            if not refresh:
                raise ActionError("tesla: not connected — connect Tesla in the dashboard (Setup → Integrations)")
            try:
                resp = await self._http.post(await self._token_url(), data={
                    "grant_type": "refresh_token", "client_id": self._cfg.client_id, "refresh_token": refresh,
                })
            except httpx.HTTPError as e:
                raise ActionError(f"tesla token refresh failed: {e!r}") from None
            if resp.status_code in (400, 401):
                raise ActionError("tesla: access was revoked or expired — reconnect Tesla in the dashboard")
            try:
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ActionError(f"tesla token refresh failed: {e!r}") from None
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in", 3600))
            if data.get("refresh_token"):   # single-use: persist the new one before anything else
                await self._store.set(REFRESH_KEY, data["refresh_token"])
            return self._access

    # ------------------------------------------------------------------ partner registration

    async def _partner_token(self) -> str:
        try:
            resp = await self._http.post(await self._token_url(), data={
                "grant_type": "client_credentials", "client_id": self._cfg.client_id,
                "client_secret": self._secret(), "audience": await self.server(), "scope": PARTNER_SCOPES,
            })
        except httpx.HTTPError as e:
            raise ActionError(f"tesla partner token: {e!r}") from None
        if resp.status_code >= 400:
            raise ActionError(f"tesla partner token: HTTP {resp.status_code} {resp.text[:200]}")
        return str(resp.json()["access_token"])

    async def register_domain(self) -> dict[str, Any]:
        """One-time: register your domain (and so your public key) with Fleet API in your region,
        then confirm Tesla holds the same key as the one this hub signs with."""
        domain = self._cfg.domain
        if not domain:
            raise ActionError("tesla: set TESLA_DOMAIN in .env (the site that hosts your public key)")
        token = await self._partner_token()
        headers = {"Authorization": f"Bearer {token}"}
        base = await self.server()
        try:
            resp = await self._http.post(f"{base}/api/1/partner_accounts", json={"domain": domain}, headers=headers)
            if resp.status_code >= 400:
                raise ActionError(f"tesla: registering {domain} failed: HTTP {resp.status_code} {resp.text[:300]}")
            check = await self._http.get(f"{base}/api/1/partner_accounts/public_key", params={"domain": domain},
                                         headers=headers)
        except httpx.HTTPError as e:
            raise ActionError(f"tesla: registering {domain}: {e!r}") from None
        registered = str(((check.json() if check.status_code < 400 else {}).get("response") or {})
                         .get("public_key") or "").lower()
        local = self.local_public_key()
        matches = None if local is None or not registered else registered == local
        if matches is False:
            raise ActionError(f"tesla: {domain} is registered, but with a different public key than this hub's "
                              "(data/tesla/public-key.pem). Host this hub's key at the domain and register again.")
        return {"domain": domain, "region": await self.region(), "registered_key": registered or None,
                "matches_local_key": matches}

    def local_public_key(self) -> str | None:
        path = self._cfg.public_key
        if path is None or not Path(path).exists():
            return None
        return public_key_hex(Path(path).read_text(encoding="utf-8"))

    def pairing_link(self) -> str | None:
        return f"https://tesla.com/_ak/{self._cfg.domain}" if self._cfg.domain else None

    # ------------------------------------------------------------------ reads (never wake the car)

    async def _read_base(self) -> str:
        return (self._cfg.read_url or await self.server()).rstrip("/")

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            resp = await self._http.get(f"{await self._read_base()}{path}", headers=headers, params=params)
        except httpx.HTTPError as e:
            raise ActionError(f"tesla {path}: {e!r}") from None
        if resp.status_code == 408:
            raise ActionError("tesla: vehicle is asleep/unreachable")
        if resp.status_code >= 400:
            raise ActionError(f"tesla {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json().get("response")

    async def vehicles(self) -> list[dict[str, Any]]:
        data = await self._get("/api/1/vehicles")
        return [{"vin": v.get("vin"), "name": v.get("display_name") or v.get("vin"), "state": v.get("state")}
                for v in (data or []) if isinstance(v, dict) and v.get("vin")]

    async def select_vehicle(self, vin: str | None = None) -> str:
        """Use ``vin``, or the only car on the account. Returns the VIN chosen."""
        cars = await self.vehicles()
        vins = [str(c["vin"]) for c in cars]
        if vin is None:
            if len(vins) != 1:
                raise ActionError("tesla: pick which car to use" if vins else "tesla: no cars on this account")
            vin = vins[0]
        if vin not in vins:
            raise ActionError("tesla: that car isn't on this account")
        await self._store.set(VIN_KEY, vin)
        return vin

    async def online_state(self) -> str:
        """``online`` / ``asleep`` / ``offline`` without waking the car."""
        info = await self._get(f"/api/1/vehicles/{await self._require_vin()}")
        return str(info.get("state", "unknown")) if isinstance(info, dict) else "unknown"

    async def vehicle_data(self, endpoints: str = "vehicle_state;drive_state;climate_state;location_data;"
                                                  "charge_state") -> dict[str, Any]:
        """A state snapshot. Raises (408) if the car is asleep rather than waking it."""
        data = await self._get(f"/api/1/vehicles/{await self._require_vin()}/vehicle_data",
                               params={"endpoints": endpoints})
        if not isinstance(data, dict):
            raise ActionError("tesla: unexpected vehicle_data response")
        return data

    # ------------------------------------------------------------------ commands (signed by the proxy)

    async def wake(self) -> None:
        vin = await self._require_vin()
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            await self._http.post(f"{await self._read_base()}/api/1/vehicles/{vin}/wake_up", headers=headers)
        except httpx.HTTPError as e:
            raise ActionError(f"tesla wake_up: {e!r}") from None
        deadline = time.monotonic() + self._cfg.wake_timeout_seconds
        while time.monotonic() < deadline:
            if await self.online_state() == "online":
                return
            await asyncio.sleep(3)
        raise ActionError("tesla: vehicle did not wake up in time")

    async def ensure_awake(self) -> None:
        """Wake the car only if it isn't online (each wake_up is billed by Tesla)."""
        if await self.online_state() != "online":
            await self.wake()

    async def command(self, name: str, params: dict[str, Any], wake: bool) -> Any:
        """``wake=True`` when the caller knows the car is asleep (or doesn't care to check)."""
        if wake:
            await self.wake()
        vin = await self._require_vin()
        headers = {"Authorization": f"Bearer {await self._token()}"}
        url = f"{(self._cfg.base_url or '').rstrip('/')}/api/1/vehicles/{vin}/command/{name}"
        try:
            resp = await self._proxy.post(url, json=params or {}, headers=headers)
        except httpx.ConnectError:
            raise ActionError("tesla: the command proxy isn't running (docker compose --profile tesla up -d)") from None
        except httpx.HTTPError as e:
            raise ActionError(f"tesla {name}: {e!r}") from None
        if resp.status_code == 408:
            raise ActionError("tesla: the car is asleep or out of reach")
        if resp.status_code >= 400:
            text = resp.text[:200]
            if "not paired" in text.lower() or "public key" in text.lower() or "unknown key" in text.lower():
                raise ActionError("tesla: the car doesn't know this hub's key yet — open the pairing link "
                                  "from Setup → Integrations on your phone")
            raise ActionError(f"tesla {name}: HTTP {resp.status_code} {text}")
        result = resp.json().get("response")
        if isinstance(result, dict) and result.get("result") is False:
            raise ActionError(f"tesla {name} rejected: {result.get('reason') or 'unknown reason'}")
        return result

    async def aclose(self) -> None:
        await self._proxy.aclose()
        await self._http.aclose()


class TeslaParams(Params):
    command: str = Field(pattern=r"^[a-z_]{3,64}$")
    """Fleet API command, e.g. auto_conditioning_start, actuate_trunk, door_lock, flash_lights."""
    params: dict[str, Any] = Field(default_factory=dict)
    wake: bool = True


@action("tesla", TeslaParams, primary="command")
async def tesla(p: TeslaParams, rt: Runtime, v: Vars) -> Vars:
    if rt.tesla is None:
        raise ActionError("tesla action used but the car integration is off (set TESLA_CLIENT_ID in .env)")
    result = await rt.tesla.command(p.command, p.params, p.wake)
    if rt.vehicle is not None:
        rt.vehicle.invalidate()
    return {"tesla": {"command": p.command, "result": result}}
