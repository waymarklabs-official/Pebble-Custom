"""Send an email from the hub, so a ring press can start an iPhone Shortcut hands-free.

iOS lets a personal automation run a Shortcut, without asking, when an email with a given sender
and subject arrives. Nothing else from outside the phone can start one unseen. So the subject is
the command, and the phone does the rest.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import TYPE_CHECKING

from pydantic import Field

from .base import ActionError, Params, Vars, action

if TYPE_CHECKING:
    from ..config import EmailConfig
    from ..runtime import Runtime

ADDRESS = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class EmailParams(Params):
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    to: str | None = Field(None, pattern=ADDRESS)
    """Default: ``email.to`` from the config."""
    body: str = Field("Sent by ringhub.", max_length=5000)


def build_message(cfg: EmailConfig, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.rsplit("@", 1)[-1])  # every press is a new email
    msg.set_content(body)
    return msg


def _send(cfg: EmailConfig, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    password = cfg.password.get_secret_value()
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds, context=context) as s:
            s.login(cfg.username, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_seconds) as s:
            s.starttls(context=context)   # raises if the server can't: never sends the password in the clear
            s.login(cfg.username, password)
            s.send_message(msg)


def explain(e: Exception, cfg: EmailConfig) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        hint = (" Gmail needs an App Password (myaccount.google.com/apppasswords, with 2-Step Verification on), "
                "not your Google password." if "gmail" in cfg.smtp_host or "google" in cfg.smtp_host else "")
        return f"email: {cfg.smtp_host} refused the login for {cfg.username}.{hint}"
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return f"email: {cfg.smtp_host}:{cfg.smtp_port} doesn't offer encryption (STARTTLS); nothing was sent"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"email: {cfg.smtp_host} refused the recipient"
    if isinstance(e, ssl.SSLError):
        return (f"email: TLS with {cfg.smtp_host}:{cfg.smtp_port} failed ({e.reason or e}). "
                "Port 465 is TLS from the start, 587 is STARTTLS.")
    if isinstance(e, TimeoutError | OSError):
        return f"email: couldn't reach {cfg.smtp_host}:{cfg.smtp_port} ({e})"
    return f"email: {e}"


@action("email.send", EmailParams, primary="subject")
async def send(p: EmailParams, rt: Runtime, v: Vars) -> Vars:
    cfg = rt.config.email
    if cfg is None:
        raise ActionError("email is off (set EMAIL_USERNAME, EMAIL_PASSWORD and EMAIL_TO in .env)")
    to = p.to or cfg.to
    msg = build_message(cfg, to, p.subject, p.body)
    try:
        await asyncio.to_thread(_send, cfg, msg)
    except (smtplib.SMTPException, OSError) as e:
        raise ActionError(explain(e, cfg)) from None
    return {"email": {"to": to, "subject": p.subject}}
