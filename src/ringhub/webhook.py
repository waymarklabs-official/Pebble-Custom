"""Index webhook protocol v1: authentication and payload normalization.

Framework-agnostic and pure so it can be unit tested against the exact byte format the
app produces (see ``docs/INDEX_WEBHOOK_PROTOCOL.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import TEST_TRIGGER, Gesture

SUPPORTED_VERSION = "1"

H_VERSION = "x-index-webhook-version"
H_TRIGGER = "x-index-trigger"
H_TEST = "x-index-test"
H_DELIVERY = "x-index-delivery"
H_SIGNATURE = "x-index-signature"
H_TIMESTAMP = "x-index-timestamp"


class WebhookError(Exception):
    """A request that must be rejected. ``status`` is the HTTP status to answer with.

    4xx (other than 408/429) tells the app not to retry, which is what we want for
    forged, malformed or stale requests.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Envelope:
    """Authenticated header metadata of one delivery."""

    delivery_id: str
    trigger: str
    is_test: bool

    @property
    def gesture(self) -> Gesture | None:
        try:
            return Gesture(self.trigger)
        except ValueError:
            return None


def _header(headers: Mapping[str, str], name: str) -> str:
    # Starlette headers are case-insensitive already; plain dicts in tests may not be.
    value = headers.get(name)
    if value is None:
        for key, val in headers.items():
            if key.lower() == name:
                return val
        return ""
    return value


def signature_input(version: str, timestamp: int, delivery_id: str, trigger: str, is_test: bool,
                    body: bytes) -> bytes:
    prefix = f"v{version}\n{timestamp}\n{delivery_id}\n{trigger}\n{1 if is_test else 0}\n"
    return prefix.encode("utf-8") + body


def sign(secret: str, version: str, timestamp: int, delivery_id: str, trigger: str, is_test: bool,
         body: bytes) -> str:
    """Produce the ``X-Index-Signature`` value exactly as the app does."""
    msg = signature_input(version, timestamp, delivery_id, trigger, is_test, body)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def authenticate(
    headers: Mapping[str, str],
    body: bytes,
    secrets: Sequence[str],
    *,
    max_skew_seconds: int,
    now: float | None = None,
) -> Envelope:
    """Validate protocol version and the HMAC signature against any of ``secrets``.

    Several candidates exist because test events carry ``X-Index-Trigger: test-event`` and
    do not say which gesture's secret signed them. An empty ``secrets`` means signing is
    disabled, which config validation only allows with an explicit ``allow_unsigned``.
    Raises :class:`WebhookError` on any failure.
    """
    version = _header(headers, H_VERSION)
    if version != SUPPORTED_VERSION:
        raise WebhookError(400, f"unsupported webhook version {version!r}")

    trigger = _header(headers, H_TRIGGER)
    if not trigger:
        raise WebhookError(400, "missing X-Index-Trigger")
    delivery_id = _header(headers, H_DELIVERY)
    if not delivery_id or len(delivery_id) > 128:
        raise WebhookError(400, "missing or invalid X-Index-Delivery")
    is_test = _header(headers, H_TEST).lower() == "true"

    if secrets:
        ts_text = _header(headers, H_TIMESTAMP)
        try:
            ts = int(ts_text)
        except ValueError:
            raise WebhookError(401, "missing or malformed X-Index-Timestamp (is signing enabled in the app?)"
                               ) from None
        current = time.time() if now is None else now
        if abs(current - ts) > max_skew_seconds:
            raise WebhookError(401, "timestamp outside allowed window (check phone/hub clocks)")
        provided = _header(headers, H_SIGNATURE).lower()
        if not provided:
            raise WebhookError(401, "missing X-Index-Signature")
        if not re.fullmatch(r"[0-9a-f]{64}", provided):
            raise WebhookError(401, "invalid signature")
        # Evaluate every candidate so timing does not reveal which secret matched.
        ok = False
        for secret in secrets:
            expected = sign(secret, version, ts, delivery_id, trigger, is_test, body)
            ok |= hmac.compare_digest(expected.encode(), provided.encode())
        if not ok:
            raise WebhookError(401, "invalid signature")

    return Envelope(delivery_id=delivery_id, trigger=trigger, is_test=is_test)


@dataclass(frozen=True, slots=True)
class ButtonBody:
    gesture: Gesture
    pressed_at_ms: int
    payload: Any
    is_test: bool


def parse_button_body(body: bytes) -> ButtonBody:
    """Parse the JSON body the app sends for click / double-click / triple-click."""
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookError(400, "button event body is not JSON") from None
    if not isinstance(data, dict) or data.get("event") != "button":
        raise WebhookError(400, "not a button event")
    try:
        gesture = Gesture(str(data["gesture"]))
        pressed_at = int(data["pressedAt"])
    except (KeyError, ValueError, TypeError):
        raise WebhookError(400, "button event missing gesture/pressedAt") from None
    return ButtonBody(
        gesture=gesture,
        pressed_at_ms=pressed_at,
        payload=data.get("payload"),
        is_test=bool(data.get("test", False)),
    )


def is_test_delivery(envelope: Envelope) -> bool:
    return envelope.is_test or envelope.trigger == TEST_TRIGGER
