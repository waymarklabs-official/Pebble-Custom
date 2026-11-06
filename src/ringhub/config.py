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


class TeslaConfig(_Strict):
    """Tesla Fleet API, reached through Tesla's ``tesla-http-proxy`` for signed commands."""

    base_url: str
    """Usually your vehicle-command proxy, e.g. https://localhost:4443"""
    vin: str
    client_id: str
    refresh_token: SecretStr
    auth_url: str = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    ca_cert: Path | None = None
    """CA bundle for the proxy's self-signed TLS cert."""
    read_url: str | None = None
    """Where state reads go (defaults to base_url; the proxy forwards them unchanged)."""
    state_ttl_seconds: float = Field(30, ge=5, le=600)
    wake_timeout_seconds: float = 45.0


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
    tesla: TeslaConfig | None = None
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
