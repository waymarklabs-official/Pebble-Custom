"""ntfy publisher: the hub's voice on your phone, watch and desktop."""

from __future__ import annotations

import httpx

from .actions.base import ActionError
from .config import NtfyConfig


class Notifier:
    """Publishes to ntfy. Doubles as the hub's feedback channel (phone, Pebble watch, desktop)."""

    def __init__(self, cfg: NtfyConfig | None, client: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._cfg is not None

    async def publish(
        self,
        message: str,
        *,
        title: str | None = None,
        topic: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        click: str | None = None,
        actions: str | None = None,
    ) -> None:
        if self._cfg is None:
            raise ActionError("notify used but no 'ntfy' section is configured")
        headers: dict[str, str] = {}
        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = str(priority)
        if tags:
            headers["Tags"] = ",".join(tags)
        if click:
            headers["Click"] = click
        if actions:
            headers["Actions"] = actions
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token.get_secret_value()}"
        url = f"{self._cfg.url.rstrip('/')}/{topic or self._cfg.topic}"
        try:
            resp = await self._client.post(url, content=message.encode("utf-8"), headers=headers, timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ActionError(f"ntfy publish failed: {e!r}") from None
