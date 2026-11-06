"""ntfy publisher: the hub's voice on your phone, watch and desktop.

Publishes with ntfy's JSON API (``POST <server>/`` with the topic in the body) rather than
``Title:``/``Actions:`` headers: headers must be ASCII, and titles here carry emoji (the Pebble
watch shows the title and first line, so the title is where the useful text goes).

iOS notes from the ntfy source: priority 1-2 are passive (silent), 3 normal, 4-5 Time Sensitive;
only ``view`` and ``http`` action buttons work, at most 3, and only on the newest notification.
"""

from __future__ import annotations

from typing import Any

import httpx

from .actions.base import ActionError
from .config import NtfyConfig


class Notifier:
    def __init__(self, cfg: NtfyConfig | None, client: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._cfg is not None

    def topic_for(self, kind: str) -> str | None:
        if self._cfg is None:
            return None
        if kind == "reminder" and self._cfg.reminders_topic:
            return self._cfg.reminders_topic
        return self._cfg.topic

    async def publish(
        self,
        message: str,
        *,
        title: str | None = None,
        topic: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        click: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        sequence_id: str | None = None,
    ) -> None:
        if self._cfg is None:
            raise ActionError("notify used but no 'ntfy' section is configured")
        body: dict[str, Any] = {"topic": topic or self._cfg.topic, "message": message}
        if title:
            body["title"] = title
        floor = self._cfg.min_priority
        if priority or floor > 1:
            body["priority"] = max(priority or 3, floor)
        if tags:
            body["tags"] = tags
        if click:
            body["click"] = click
        if actions:
            body["actions"] = actions[:3]
        if sequence_id:
            body["sequence_id"] = sequence_id
        headers = {}
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token.get_secret_value()}"
        try:
            resp = await self._client.post(self._cfg.url.rstrip("/") + "/", json=body, headers=headers, timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ActionError(f"ntfy publish failed: {e!r}") from None

    async def clear(self, sequence_id: str, topic: str | None = None) -> None:
        """Dismiss a delivered notification (Android/web; harmless no-op on iOS today)."""
        if self._cfg is None:
            return
        headers = {}
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token.get_secret_value()}"
        url = f"{self._cfg.url.rstrip('/')}/{topic or self._cfg.topic}/{sequence_id}/clear"
        try:
            await self._client.put(url, headers=headers, timeout=10)
        except httpx.HTTPError:
            pass  # best effort; old servers don't support it
