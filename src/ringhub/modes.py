"""Which mode is active right now, and why. Pure functions over catalog + context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .catalog import FALLBACK_MODE, Mode, ModeWhen

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class Resolution:
    mode: Mode
    reason: str
    """Human explanation, shown in the dashboard and in the triple-click push."""
    manual: bool = False


def _matches(when: ModeWhen, ctx: dict[str, Any], now: datetime) -> str | None:
    """Return a reason string if every condition holds, else None."""
    reasons: list[str] = []
    place = ctx.get("place")
    if when.places:
        if place not in when.places:
            return None
        reasons.append(f"at {place}")
    if when.not_places and place in when.not_places:
        return None
    if when.focus:
        focus = (ctx.get("focus") or "").lower()
        if focus not in when.focus:
            return None
        reasons.append(f"{focus} focus")
    if when.time is not None:
        if not when.time.contains(now.time()):
            return None
        reasons.append(f"{when.time.after:%H:%M}-{when.time.before:%H:%M}")
    if when.days:
        if _DAYS[now.weekday()] not in when.days:
            return None
        reasons.append(",".join(when.days))
    for key, expected in when.context.items():
        if ctx.get(key) != expected:
            return None
        reasons.append(f"{key}={expected}")
    if when.recently is not None:
        r = when.recently
        stamp: float | None = None
        if r.event == "leave":
            last = ctx.get("last_left") or {}
            if last.get("place") == r.place:
                stamp = last.get("at")
        elif ctx.get("place") == r.place:
            stamp = ctx.get("place_since")
        if stamp is None or now.timestamp() - float(stamp) > r.within_minutes * 60:
            return None
        reasons.append(f"{'left' if r.event == 'leave' else 'arrived at'} {r.place} recently")
    return ", ".join(reasons) or "always"


def resolve(modes: list[Mode], ctx: dict[str, Any], now: datetime,
            override: dict[str, Any] | None = None) -> Resolution:
    """Manual override > first enabled matching mode > the fallback mode."""
    by_id = {m.id: m for m in modes}
    if override:
        mode = by_id.get(str(override.get("mode")))
        expires = override.get("expires_at")
        place_ok = override.get("place", ctx.get("place")) == ctx.get("place")
        if mode and mode.enabled and place_ok and (expires is None or float(expires) > now.timestamp()):
            return Resolution(mode, "set by hand", manual=True)
    for mode in modes:
        if mode.id == FALLBACK_MODE or not mode.enabled:
            continue
        reason = _matches(mode.when, ctx, now)
        if reason is not None:
            return Resolution(mode, reason)
    fallback = by_id.get(FALLBACK_MODE)
    if fallback is None:
        raise LookupError("catalog has no 'everywhere' mode")
    return Resolution(fallback, "no other mode applies")


def binding(modes: list[Mode], active: Mode, key: str) -> tuple[str | None, Mode | None]:
    """Skill bound to ``key`` in the active mode, falling through to the fallback mode."""
    if key in active.bindings:
        return active.bindings[key], active
    fallback = next((m for m in modes if m.id == FALLBACK_MODE), None)
    if fallback is not None and key in fallback.bindings:
        return fallback.bindings[key], fallback
    return None, None
