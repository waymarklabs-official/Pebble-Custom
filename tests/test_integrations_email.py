"""Email: a ring press sends a command subject that an iPhone automation turns into a Shortcut run."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from ringhub.actions import email as email_action
from ringhub.actions.base import ActionError
from ringhub.catalog import Skill
from ringhub.config import Config, ConfigError, load_config
from ringhub.runtime import Runtime
from ringhub.templates import compile_skill, describe

Factory = Callable[..., Any]
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
BASE_ENV = {"RINGHUB_ADMIN_TOKEN": "a" * 24, "RINGHUB_CONTEXT_TOKEN": "c" * 24, "RINGHUB_SECRET": "s" * 32}
EMAIL = {"username": "me@waymark.test", "password": "app-pass", "to": "me@icloud.test"}


class FakeSMTP:
    """Records what the hub does with the SMTP connection."""

    sent: list[EmailMessage]
    log: list[str]
    fail_login: bool = False
    no_starttls: bool = False

    def __init__(self, host: str, port: int, timeout: float = 0, context: ssl.SSLContext | None = None) -> None:
        self.log.append(f"connect {host}:{port}" + (" tls" if context is not None else ""))

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.append("quit")

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        if self.no_starttls:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        self.log.append("starttls")

    def login(self, user: str, password: str) -> None:
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.log.append(f"login {user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.log.append("send")
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    fake = type("Fake", (FakeSMTP,), {"sent": [], "log": []})
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake)
    monkeypatch.setattr(smtplib, "SMTP", fake)
    return fake


async def email_rt(make_runtime: Factory, make_config: Callable[..., Config], **email: Any) -> Runtime:
    runtime: Runtime = await make_runtime(make_config({"email": {**EMAIL, **email}}))
    await runtime.catalog.put(Skill.model_validate({
        "id": "lock_by_mail", "name": "Lock (hands-free)", "icon": "📧", "template": "shortcut_email",
        "params": {"subject": "ring lock car 7Q4"}}))
    return runtime


# ----------------------------------------------------------------------------- config


def test_email_stays_off_until_a_username_is_set() -> None:
    off = load_config(EXAMPLE, BASE_ENV)
    assert off.email is None
    on = load_config(EXAMPLE, {**BASE_ENV, "EMAIL_USERNAME": "me@gmail.com", "EMAIL_PASSWORD": "abcd efgh",
                               "EMAIL_TO": "me@icloud.com"})
    assert on.email is not None
    assert (on.email.sender, on.email.smtp_host, on.email.smtp_port) == ("me@gmail.com", "smtp.gmail.com", 465)
    assert on.email.password.get_secret_value() == "abcd efgh"


@pytest.mark.parametrize(("env", "error"), [
    ({"EMAIL_PASSWORD": ""}, "EMAIL_PASSWORD"),
    ({"EMAIL_TO": ""}, "to"),
    ({"EMAIL_TO": "not-an-address"}, "to"),
])
def test_email_needs_a_password_and_a_recipient(env: dict[str, str], error: str) -> None:
    full = {**BASE_ENV, "EMAIL_USERNAME": "me@gmail.com", "EMAIL_PASSWORD": "p", "EMAIL_TO": "me@icloud.com"}
    with pytest.raises(ConfigError, match=error):
        load_config(EXAMPLE, {**full, **env})


# ----------------------------------------------------------------------------- sending


async def test_a_press_emails_the_command_subject(make_runtime: Factory, make_config: Callable[..., Config],
                                                  smtp: type[FakeSMTP]) -> None:
    rt = await email_rt(make_runtime, make_config)
    result = await rt.run_skill("lock_by_mail", await rt.base_vars())
    assert result["email"] == {"to": "me@icloud.test", "subject": "ring lock car 7Q4"}
    assert smtp.log == ["connect smtp.gmail.com:465 tls", "login me@waymark.test", "send", "quit"]
    msg = smtp.sent[0]
    assert (msg["From"], msg["To"], msg["Subject"]) == ("me@waymark.test", "me@icloud.test", "ring lock car 7Q4")
    await rt.run_skill("lock_by_mail", await rt.base_vars())
    assert smtp.sent[0]["Message-ID"] != smtp.sent[1]["Message-ID"]   # each press is its own email


async def test_other_ports_use_starttls_before_logging_in(make_runtime: Factory,
                                                          make_config: Callable[..., Config],
                                                          smtp: type[FakeSMTP]) -> None:
    rt = await email_rt(make_runtime, make_config, smtp_port=587, from_addr="hub@waymark.test")
    await rt.run_skill("lock_by_mail", await rt.base_vars())
    assert smtp.log[:3] == ["connect smtp.gmail.com:587", "starttls", "login me@waymark.test"]
    assert smtp.sent[0]["From"] == "hub@waymark.test"


async def test_never_logs_in_without_encryption(make_runtime: Factory, make_config: Callable[..., Config],
                                                smtp: type[FakeSMTP]) -> None:
    smtp.no_starttls = True
    rt = await email_rt(make_runtime, make_config, smtp_port=587)
    with pytest.raises(ActionError, match="doesn't offer encryption"):
        await rt.run_skill("lock_by_mail", await rt.base_vars())
    assert not any(line.startswith("login") for line in smtp.log) and smtp.sent == []


async def test_a_refused_gmail_login_says_to_use_an_app_password(make_runtime: Factory,
                                                                 make_config: Callable[..., Config],
                                                                 smtp: type[FakeSMTP]) -> None:
    smtp.fail_login = True
    rt = await email_rt(make_runtime, make_config)
    with pytest.raises(ActionError, match="App Password"):
        await rt.run_skill("lock_by_mail", await rt.base_vars())


def test_unreachable_server_is_explained() -> None:
    from ringhub.config import EmailConfig

    c = EmailConfig.model_validate(EMAIL)
    assert "couldn't reach smtp.gmail.com:465" in email_action.explain(TimeoutError("timed out"), c)


async def test_email_off_is_explained(make_runtime: Factory, make_config: Callable[..., Config],
                                      smtp: type[FakeSMTP]) -> None:
    rt: Runtime = await make_runtime(make_config())
    await rt.catalog.put(Skill.model_validate({"id": "m", "name": "M", "template": "shortcut_email",
                                               "params": {"subject": "x"}}))
    with pytest.raises(ActionError, match="EMAIL_USERNAME"):
        await rt.run_skill("m", await rt.base_vars())


# ----------------------------------------------------------------------------- template


def test_template_compiles_and_rejects_header_injection() -> None:
    def steps(params: dict[str, Any]) -> list[dict[str, Any]]:
        return compile_skill(Skill.model_validate({"id": "s", "name": "S", "template": "shortcut_email",
                                                   "params": params}))

    assert steps({"subject": "ring lock car"}) == [{"email.send": {"subject": "ring lock car"}}]
    assert steps({"subject": "x", "to": "a@b.co"}) == [{"email.send": {"subject": "x", "to": "a@b.co"}}]
    from ringhub.templates import TemplateError

    for bad in ({"subject": ""}, {"subject": "x\r\nBcc: someone@else.com"}, {"subject": "x", "to": "nope"}):
        with pytest.raises(TemplateError):
            steps(bad)
    assert next(d for d in describe() if d["id"] == "shortcut_email")["needs"] == ["email"]
