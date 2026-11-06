"""OAuth "Connect" flows for integrations (Tesla, Google), shared plumbing.

**Where the provider sends the browser back (the redirect URI).**
- Google: ringhub's own neutral path ``<public_url>/oauth/callback`` works, even on a tailnet.
- Tesla rejects redirect URIs and app domains that contain "tesla", that aren't on a
  CA-certified domain, or that sit behind a reverse proxy (Tailscale Serve is one). So by default
  the Tesla redirect URI is a static page on the domain that already hosts your public key:
  ``https://<TESLA_DOMAIN>/callback.html``. That page (:func:`bridge_page`) forwards the browser,
  query string and all, to ringhub's ``/oauth/callback`` over your tailnet. If the phone isn't on
  the tailnet, the address it landed on can be pasted into the dashboard instead.

**Authorization.** The return trip is a cross-site navigation, so the dashboard's SameSite=Strict
session cookie isn't sent with it. The callback is authorized by a single-use ``state`` instead:
an unguessable nonce issued by an admin-only endpoint, bound to the provider, stored server-side,
and valid for ten minutes. The neutral callback path learns the provider from it.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from .store import Store

STATE_TTL_SECONDS = 600
CALLBACK_PATH = "/oauth/callback"
BRIDGE_FILE = "callback.html"
PROVIDERS = ("tesla", "google")
_PREFIX = "oauth.state."


def callback_url(public_url: str | None) -> str | None:
    """ringhub's own callback (where the bridge page forwards to, and Google's default)."""
    return f"{public_url.rstrip('/')}{CALLBACK_PATH}" if public_url else None


def redirect_uri_for(provider: str, cfg: Any, public_url: str | None) -> str | None:
    """The exact redirect URI to register with the provider (and to send it, character for character)."""
    explicit = getattr(cfg, "redirect_uri", None)
    if explicit:
        return str(explicit)
    if provider == "tesla" and getattr(cfg, "domain", None):
        return f"https://{cfg.domain}/{BRIDGE_FILE}"
    return callback_url(public_url)


def tesla_url_problems(url: str | None, public_url: str | None) -> list[str]:
    """Why Tesla's developer portal would refuse ``url`` (as its form states the rules)."""
    if not url:
        return []
    parts = urlsplit(url)
    problems = []
    if "tesla" in (parts.netloc + parts.path).lower():
        problems.append("Tesla rejects addresses that contain “tesla”.")
    if public_url and parts.netloc.lower() == urlsplit(public_url).netloc.lower():
        problems.append("This is ringhub's Tailscale address. Tesla rejects reverse-proxied domains "
                        "(Tailscale Serve is one): use your public-key domain instead.")
    if parts.scheme != "https":
        problems.append("Tesla needs an https address with a certificate from a public CA.")
    return problems


def bridge_page(target: str) -> str:
    """The static page to host at the Tesla redirect URI: forwards the browser to ringhub.

    The destination is fixed (never taken from the query string), so it can't be abused as an open
    redirect. The authorization code is useless without your client secret.
    """
    # JSON, with <, > and & escaped so nothing in the URL can end the script element.
    dest = json.dumps(target).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex">
<title>Connecting…</title>
<body style="font:16px system-ui;padding:2rem;max-width:32rem;margin:auto">
<p id="msg">Taking you back to ringhub…</p>
<p>Nothing happening? Your phone needs Tailscale on. Or copy this page's full address and paste it
in ringhub under Setup → Integrations → “Finish connecting”.</p>
<script>
(function () {{
  var q = location.search;
  if (!/[?&]state=/.test(q)) {{ document.getElementById("msg").textContent = "Nothing to do here."; return; }}
  location.replace({dest} + q);
}})();
</script>
"""


async def new_state(store: Store, provider: str, now: float | None = None) -> str:
    nonce = secrets.token_urlsafe(32)
    await store.set(_PREFIX + nonce, {"provider": provider, "exp": (now or time.time()) + STATE_TTL_SECONDS})
    return nonce


async def take_state(store: Store, nonce: str, now: float | None = None) -> str | None:
    """The provider a valid, unexpired state was issued for (once); it is consumed either way."""
    if not nonce or len(nonce) > 128:
        return None
    key = _PREFIX + nonce
    entry = await store.get(key)
    if entry is None:
        return None
    await store.delete(key)
    if not isinstance(entry, dict) or float(entry.get("exp", 0)) <= (now or time.time()):
        return None
    provider = entry.get("provider")
    return str(provider) if provider in PROVIDERS else None


def code_and_state(pasted: str) -> tuple[str, str, str, str]:
    """(code, state, error, error_description) from a pasted redirect address (or its query string)."""
    text = pasted.strip()
    query = urlsplit(text).query if "://" in text else text.lstrip("?")
    q = parse_qs(query)

    def one(k: str) -> str:
        return q.get(k, [""])[0]

    return one("code"), one("state"), one("error"), one("error_description")
