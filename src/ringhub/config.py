"""Configuration schema and loader.

The config file is YAML. Any string may reference environment variables as ``${NAME}`` or
``${NAME:-fallback}`` so secrets never need to live in the file itself.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import time as dtime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    field_validator,
    model_validator,
)

from .models import TEST_TRIGGER, Gesture, parse_gesture

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised for any invalid configuration; message is meant for humans."""


def expand_env(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    env: Mapping[str, str] = os.environ if environ is None else environ

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable ${{{name}}} is referenced in config but not set")

    if isinstance(value, str):
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def _gesture(v: Any) -> Gesture:
    return v if isinstance(v, Gesture) else parse_gesture(str(v))


def _blank_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and not v.strip() else v


def _as_list(v: Any) -> Any:
    return v if v is None or isinstance(v, list) else [v]


GestureField = Annotated[Gesture, BeforeValidator(_gesture)]
GestureList = Annotated[list[GestureField], BeforeValidator(_as_list)]
StrList = Annotated[list[str], BeforeValidator(_as_list)]
OptionalSecret = Annotated[SecretStr | None, BeforeValidator(_blank_to_none)]
"""``${TOKEN:-}`` in YAML yields "" when unset; treat that as not configured."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- server / ring


class ServerConfig(_Strict):
    host: str = "0.0.0.0"  # noqa: S104 - a LAN service; put it behind Tailscale/Caddy for TLS
    port: int = 8787
    webhook_path: str = "/ring"
    data_dir: Path = Path("./data")
    admin_token: OptionalSecret = None
    """Dashboard login and admin API. Both are disabled when unset."""
    context_token: OptionalSecret = None
    """Scoped token your phone's Shortcuts use to report place/Focus. It can only write context."""
    public_url: str | None = None
    """How your phone reaches the hub (e.g. https://hub.tailnet.ts.net). Enables Done/Snooze buttons."""

    @field_validator("admin_token", "context_token")
    @classmethod
    def _long_enough(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 16:
            raise ValueError("tokens must be at least 16 characters (use `ringhub secret`)")
        return v

    @field_validator("public_url", mode="before")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        v = _blank_to_none(v)
        if v is not None and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("public_url must look like https://host[:port]")
        return v.rstrip("/") if v else v


class RingConfig(_Strict):
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    """Signing secret per gesture name, or ``default`` for all gestures."""
    allow_unsigned: bool = False
    """Accept requests without a signature. Only for local experiments."""
    max_skew_seconds: int = Field(300, ge=30, le=3600)
    combo_window_ms: int = Field(650, ge=100, le=5000)
    """How long to wait for the next gesture when the current one starts a configured combo."""
    max_body_bytes: int = Field(25 * 1024 * 1024, ge=1024)
    keep_audio: bool = True
    notify_on_test: bool = True
    """Answer the app's 'Send test event' with an ntfy push, proving the full loop works."""
    dedup_ttl_hours: int = Field(168, ge=1)

    @field_validator("secrets")
    @classmethod
    def _known_keys(cls, v: dict[str, SecretStr]) -> dict[str, SecretStr]:
        out: dict[str, SecretStr] = {}
        for key, secret in v.items():
            name = "default" if key == "default" else parse_gesture(key).value
            if len(secret.get_secret_value()) < 16:
                raise ValueError(f"signing secret for {key!r} is too short (use >= 32 random bytes)")
            out[name] = secret
        return out

    @model_validator(mode="after")
    def _require_signing(self) -> RingConfig:
        if not self.secrets and not self.allow_unsigned:
            raise ValueError(
                "ring.secrets is empty: configure a signing secret (recommended) "
                "or set ring.allow_unsigned: true"
            )
        return self

    def secrets_for(self, trigger: str) -> list[str]:
        """Candidate signing secrets for a request with this ``X-Index-Trigger``."""
        if trigger in self.secrets:
            return [self.secrets[trigger].get_secret_value()]
        if trigger == TEST_TRIGGER or "default" not in self.secrets:
            # Test events don't name their gesture; unknown triggers get every secret so
            # they are still authenticated (and then rejected) rather than waved through.
            return [s.get_secret_value() for s in self.secrets.values()]
        return [self.secrets["default"].get_secret_value()]


class NtfyConfig(_Strict):
    url: str = "https://ntfy.sh"
    topic: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    reminders_topic: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    """Optional separate topic for reminders, so they don't stack with confirmations."""
    token: OptionalSecret = None
    min_priority: int = Field(1, ge=1, le=5)
    """Floor for every push. 4 makes everything Time Sensitive on iPhone (alerts through Focus);
    priorities 1-2 are delivered silently."""


# --------------------------------------------------------------------------- devices


class AndroidTvConfig(_Strict):
    """Android TV, Google TV, Chromecast with Google TV, Shield and Fire TV (all speak ADB)."""

    type: Literal["androidtv", "googletv", "firetv"]
    host: str
    port: int = 5555
    room: str | None = None
    adb_path: str = "adb"
    timeout_seconds: float = 8.0


class RokuConfig(_Strict):
    type: Literal["roku"]
    host: str
    port: int = 8060
    room: str | None = None
    timeout_seconds: float = 4.0


DeviceConfig = Annotated[AndroidTvConfig | RokuConfig, Field(discriminator="type")]


class MediaSourceConfig(_Strict):
    """An HTTP endpoint that returns the newest playable item, e.g. your Pi downloader."""

    latest_url: str
    url_field: str = "url"
    """Dotted path to the media URL in the JSON response (``items.0.url`` works too)."""
    title_field: str | None = "title"
    headers: dict[str, str] = Field(default_factory=dict)


TeslaRegion = Literal["na", "eu", "cn"]


class TeslaConfig(_Strict):
    """Tesla Fleet API. Commands go through Tesla's ``tesla-http-proxy``, which signs them (a Model Y
    only accepts signed commands); state reads go straight to Fleet API. Connect the account in the
    dashboard (Setup → Integrations): the hub keeps the rotating refresh token itself.
    See docs/INTEGRATIONS.md."""

    client_id: str
    """From your app at developer.tesla.com. An empty value turns the car integration off."""
    client_secret: SecretStr | None = None
    """Needed to connect the account and to register your domain (one-time steps)."""
    domain: str | None = Field(None, pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
    """The domain that hosts your public key (e.g. you.github.io); the car shows it on its Locks screen."""
    base_url: str | None = "https://tesla-proxy:4443"
    """The vehicle-command proxy (the compose service ``tesla-proxy``)."""
    ca_cert: Path | None = None
    """The proxy's self-signed TLS certificate (deploy/pi/tesla-keys.sh makes it)."""
    public_key: Path | None = None
    """Your public key, to check that the domain registration matches it."""
    read_url: str | None = None
    """Where state reads go; defaults to Fleet API for your region."""
    region: TeslaRegion | None = None
    """Learned when you connect; set it only to override."""
    vin: str | None = Field(None, pattern=r"^[A-HJ-NPR-Z0-9]{17}$")
    """Picked in the dashboard when you connect; set it only to override."""
    refresh_token: SecretStr | None = None
    """Bootstrap only (the dashboard's Connect does this for you)."""
    auth_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    authorize_url: str = "https://auth.tesla.com/oauth2/v3/authorize"
    scopes: str = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"
    state_ttl_seconds: float = Field(30, ge=5, le=600)
    wake_timeout_seconds: float = 45.0
    temperature_unit: Literal["F", "C"] = "F"
    """How the hub speaks temperatures (the car always takes Celsius)."""

    @field_validator("client_secret", "domain", "base_url", "ca_cert", "public_key", "read_url", "region", "vin",
                     "refresh_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("base_url", mode="after")
    @classmethod
    def _default_proxy(cls, v: str | None) -> str:
        return v or "https://tesla-proxy:4443"


class GoogleConfig(_Strict):
    """Google Calendar: add events by voice, hear your agenda. Connect in the dashboard
    (Setup → Integrations). See docs/INTEGRATIONS.md."""

    client_id: str
    """OAuth client (type "Web application") from console.cloud.google.com. Empty turns it off."""
    client_secret: SecretStr
    calendar_id: str = "primary"
    """Which calendar new events go to ("primary" is your main one)."""
    default_minutes: int = Field(60, ge=5, le=1440)
    """Length of an event when you don't say one ("for 30 minutes")."""
    authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
    api_url: str = "https://www.googleapis.com/calendar/v3"

    @field_validator("client_secret", mode="after")
    @classmethod
    def _secret_set(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("set GOOGLE_CLIENT_SECRET (Google Calendar is on because GOOGLE_CLIENT_ID is set)")
        return v

    @field_validator("calendar_id", mode="before")
    @classmethod
    def _primary_by_default(cls, v: Any) -> Any:
        return "primary" if isinstance(v, str) and not v.strip() else v


def _off_when_unset(v: Any) -> Any:
    """``tesla:``/``google:`` sections fed from .env stay off until a client id is set."""
    if isinstance(v, dict) and not str(v.get("client_id") or "").strip():
        return None
    return v


class LlmConfig(_Strict):
    """Optional fallback that maps free-form voice to one of your intents via Claude."""

    api_key: SecretStr
    model: str = "claude-opus-5-5"
    timeout_seconds: float = 15.0


# --------------------------------------------------------------------------- rules


def _parse_hhmm(v: Any) -> dtime:
    if isinstance(v, dtime):
        return v
    if isinstance(v, int):  # YAML 1.1 parses 22:00 as sexagesimal 1320
        return dtime(v // 60 % 24, v % 60)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::00)?", str(v).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"expected HH:MM, got {v!r}")
    return dtime(int(m.group(1)), int(m.group(2)))


Hhmm = Annotated[dtime, BeforeValidator(_parse_hhmm), PlainSerializer(lambda t: t.strftime("%H:%M"), return_type=str)]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class TimeWindow(_Strict):
    after: Hhmm
    before: Hhmm

    def contains(self, t: dtime) -> bool:
        if self.after <= self.before:
            return self.after <= t < self.before
        return t >= self.after or t < self.before  # wraps midnight


Step = dict[str, Any]


def normalize_steps(v: Any) -> Any:
    if not isinstance(v, list):
        raise ValueError("'do' must be a list of steps")
    for i, step in enumerate(v):
        if isinstance(step, str):
            continue  # bare action name with no params, e.g. "- mode.cycle"
        if not isinstance(step, dict) or len(step) != 1:
            raise ValueError(f"step {i} must be a single-key mapping like '- notify: {{...}}'")
    return [s if isinstance(s, dict) else {s: {}} for s in v]


Steps = Annotated[list[Step], BeforeValidator(normalize_steps)]


# --------------------------------------------------------------------------- root


class MemoryConfig(_Strict):
    ack_window_seconds: int = Field(120, ge=0, le=900)
    """After a reminder fires: click = done, double = snooze for this long. 0 disables."""
    digest_time: Hhmm | None = None
    """Evening summary push (captures + open reminders), e.g. "21:30"."""
    markdown_dir: Path | None = None
    """Mirror every note into daily Markdown files here (an Obsidian/Logseq vault)."""


class Config(_Strict):
    timezone: str = "UTC"
    server: ServerConfig = Field(default_factory=ServerConfig)
    ring: RingConfig
    ntfy: NtfyConfig | None = None
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    default_target: str | None = None
    """Device or room used for target 'here' when nothing else decides it."""
    media_sources: dict[str, MediaSourceConfig] = Field(default_factory=dict)
    tesla: Annotated[TeslaConfig | None, BeforeValidator(_off_when_unset)] = None
    google: Annotated[GoogleConfig | None, BeforeValidator(_off_when_unset)] = None
    llm: LlmConfig | None = None
    allow_exec: bool = False
    notify_on_error: bool = True

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {v!r}") from None
        return v

    @model_validator(mode="after")
    def _cross_refs(self) -> Config:
        rooms = {d.room for d in self.devices.values() if d.room}
        if self.default_target and self.default_target not in self.devices and \
                self.default_target not in rooms:
            raise ValueError(f"default_target {self.default_target!r} is not a device or room")
        overlap = set(self.devices) & rooms
        if overlap:
            raise ValueError(f"names used as both device and room: {sorted(overlap)}")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def rooms(self) -> set[str]:
        return {d.room for d in self.devices.values() if d.room}


def load_config(path: str | Path, environ: Mapping[str, str] | None = None) -> Config:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level")
    try:
        return Config.model_validate(expand_env(raw, environ))
    except ValueError as e:  # pydantic.ValidationError subclasses ValueError
        raise ConfigError(f"invalid config {p}:\n{e}") from None
