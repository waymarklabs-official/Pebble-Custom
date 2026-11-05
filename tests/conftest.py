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


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    def factory(extra: dict[str, Any] | None = None, **overrides: Any) -> Config:
        doc: dict[str, Any] = {
            "timezone": "America/Chicago",
            "server": {"data_dir": str(tmp_path / "data"), "admin_token": ADMIN},
            "ring": {"secrets": {"default": SECRET}, "combo_window_ms": 150},
            "ntfy": {"url": "https://ntfy.test", "topic": "ring"},
            "notes_dir": str(tmp_path / "notes"),
            "modes": ["home", "tv"],
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
