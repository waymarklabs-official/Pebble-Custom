"""Command line entry point: ``ringhub serve | check | secret | send``."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx

from . import envfile
from .config import ConfigError, load_config
from .executor import StepError
from .models import Gesture, parse_gesture
from .webhook import SUPPORTED_VERSION, sign

DEFAULT_CONFIG = "config.yaml"


def _cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    srv = cfg.server
    print(f"OK  {args.config}")
    print(f"    dashboard: {'on' if srv.admin_token else 'OFF (set server.admin_token)'}")
    print(f"    phone context feed: {'on' if srv.context_token else 'OFF (set server.context_token)'}")
    print(f"    reminder buttons: {'on via ' + srv.public_url if srv.public_url else 'off (set server.public_url)'}")
    print(f"    feedback: {'ntfy ' + cfg.ntfy.topic if cfg.ntfy else 'none (failures are only logged!)'}")
    if cfg.tesla is None:
        car = "off (set TESLA_CLIENT_ID in .env)"
    else:
        car = (f"Tesla {cfg.tesla.vin[-6:]}" if cfg.tesla.vin else "Tesla (connect in Setup → Integrations)")
        if not cfg.tesla.client_secret or not cfg.tesla.client_secret.get_secret_value():
            car += " · TESLA_CLIENT_SECRET missing (needed to connect)"
        if not cfg.tesla.domain:
            car += " · TESLA_DOMAIN missing (needed for the car to trust this hub)"
    print(f"    car: {car}")
    if cfg.tesla is not None:
        from .oauth import redirect_uri_for, tesla_url_problems

        ru = redirect_uri_for("tesla", cfg.tesla, srv.public_url)
        print(f"    tesla sign-in returns to: {ru or 'nowhere (set TESLA_DOMAIN)'}")
        for problem in tesla_url_problems(ru, srv.public_url):
            print(f"      ! {problem}")
    gcal = "on (connect in Setup → Integrations)" if cfg.google else "off (set GOOGLE_CLIENT_ID in .env)"
    print(f"    google calendar: {gcal}")
    print(f"    voice fallback LLM: {'on' if cfg.llm else 'off'}")
    digest = cfg.memory.digest_time.strftime("%H:%M") if cfg.memory.digest_time else "off"
    print(f"    digest: {digest} · markdown mirror: {cfg.memory.markdown_dir or 'off'}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    cfg = load_config(args.config)
    app = create_app(cfg)
    uvicorn.run(app, host=args.host or cfg.server.host, port=args.port or cfg.server.port,
                log_level=args.log_level.lower(), proxy_headers=True, access_log=False)
    return 0


def _cmd_secret(_: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(32))
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    """Send a request signed exactly like the Index app, for end-to-end tests and for wiring
    other buttons (Stream Deck, Stream-Pi, a Flic, cron) into the same rules."""
    gesture = parse_gesture(args.gesture)
    delivery = str(uuid.uuid4())
    headers = {"X-Index-Webhook-Version": SUPPORTED_VERSION, "X-Index-Trigger": gesture.value,
               "X-Index-Delivery": delivery}
    now_ms = int(time.time() * 1000)
    if gesture.is_voice:
        boundary = uuid.uuid4().hex
        parts = [("transcription", args.transcript or ""), ("recordedAt", str(now_ms)), ("client", "ring")]
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        doc: dict[str, object] = {"event": "button", "gesture": gesture.value,
                                  "clicks": {Gesture.CLICK: 1, Gesture.DOUBLE_CLICK: 2}.get(gesture, 3),
                                  "pressedAt": now_ms, "client": "ring"}
        if args.payload is not None:
            try:
                doc["payload"] = json.loads(args.payload)
            except json.JSONDecodeError:
                doc["payload"] = args.payload
        body = json.dumps(doc).encode()
        headers["Content-Type"] = "application/json"
    if args.secret:
        ts = int(time.time())
        headers["X-Index-Timestamp"] = str(ts)
        headers["X-Index-Signature"] = sign(args.secret, SUPPORTED_VERSION, ts, delivery, gesture.value,
                                            False, body)
    resp = httpx.post(args.url, content=body, headers=headers, timeout=15)
    print(resp.status_code, resp.text)
    return 0 if resp.is_success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ringhub", description=__doc__)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the hub")
    p.add_argument("-c", "--config", type=Path, default=Path(DEFAULT_CONFIG))
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=_cmd_serve)

    p = sub.add_parser("check", help="validate a config file without starting")
    p.add_argument("-c", "--config", type=Path, default=Path(DEFAULT_CONFIG))
    p.set_defaults(fn=_cmd_check)

    p = sub.add_parser("secret", help="generate a webhook signing secret")
    p.set_defaults(fn=_cmd_secret)

    p = sub.add_parser("env", help="print a complete .env for the Pi / docker compose stack")
    envfile.add_arguments(p)
    p.set_defaults(fn=envfile.run)

    p = sub.add_parser("send", help="send a signed gesture like the ring app does")
    p.add_argument("gesture", help="click | double-click | triple-click | hold | click-hold")
    p.add_argument("--url", default="http://127.0.0.1:8787/ring")
    p.add_argument("--secret", help="signing secret (omit for unsigned)")
    p.add_argument("--transcript", help="transcript for hold / click-hold")
    p.add_argument("--payload", help="click payload (JSON or text)")
    p.set_defaults(fn=_cmd_send)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return int(args.fn(args))
    except (ConfigError, StepError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
