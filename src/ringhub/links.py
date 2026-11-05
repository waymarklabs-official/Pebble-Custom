"""Signed, expiring links for push-notification buttons ("Done", "Snooze").

ntfy stores message headers (including action URLs) on its server, so action buttons must not
carry the admin token. Each link instead authorizes exactly one action on one reminder until
it expires: ``/r/{id}/{action}?exp=...&sig=...``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import urlencode


class LinkSigner:
    def __init__(self, secret: bytes, public_url: str | None) -> None:
        if len(secret) < 32:
            raise ValueError("link secret must be at least 32 bytes")
        self._secret = secret
        self.public_url = public_url.rstrip("/") if public_url else None

    def _sig(self, path: str, exp: int) -> str:
        return hmac.new(self._secret, f"{path}\n{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def sign(self, path: str, ttl_seconds: int = 7 * 86400, now: float | None = None) -> str | None:
        """Absolute URL for ``path`` or None when no public URL is configured."""
        if self.public_url is None:
            return None
        exp = int((now if now is not None else time.time()) + ttl_seconds)
        return f"{self.public_url}{path}?{urlencode({'exp': exp, 'sig': self._sig(path, exp)})}"

    def verify(self, path: str, exp: str, sig: str, now: float | None = None) -> bool:
        if not (exp.isascii() and exp.isdigit()) or not re.fullmatch(r"[0-9a-f]{32}", sig):
            return False
        exp_i = int(exp)
        if exp_i < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(self._sig(path, exp_i).encode(), sig.encode())
