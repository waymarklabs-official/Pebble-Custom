"""OAuth "Connect" flows for integrations (Tesla, Google), shared plumbing.

The browser goes to the provider and comes back to ``<public_url>/oauth/<provider>/callback``.
That return trip is a cross-site navigation, so the dashboard's SameSite=Strict session cookie is
not sent with it. The callback is authorized by a single-use ``state`` instead: an unguessable
nonce created by an admin-only endpoint, stored server-side, valid for ten minutes.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

STATE_TTL_SECONDS = 600
_PREFIX = "oauth.state."


def redirect_uri(public_url: str | None, provider: str) -> str | None:
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}/oauth/{provider}/callback"


async def new_state(store: Store, provider: str, now: float | None = None) -> str:
    nonce = secrets.token_urlsafe(32)
    await store.set(_PREFIX + nonce, {"provider": provider, "exp": (now or time.time()) + STATE_TTL_SECONDS})
    return nonce


async def take_state(store: Store, nonce: str, provider: str, now: float | None = None) -> bool:
    """True once for a valid, unexpired state issued for ``provider``; it is consumed either way."""
    if not nonce or len(nonce) > 128:
        return False
    key = _PREFIX + nonce
    entry = await store.get(key)
    if entry is None:
        return False
    await store.delete(key)
    return (isinstance(entry, dict) and entry.get("provider") == provider
            and float(entry.get("exp", 0)) > (now or time.time()))
