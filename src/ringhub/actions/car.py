"""Friendly car commands for skills and voice ("lock the car", "set the car to 70", "open the garage").

Each one maps to a Fleet API command sent through the signing proxy. Commands that open the car
to the world (unlock, trunk, frunk, vent the windows, garage door) run only when your phone's
recent location puts you within ``NEAR_CAR_METRES`` of the car. A misheard command must not unlock
a car parked across town; use the Tesla app for that on purpose.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ..geo import distance_m, fresh_fix
from ..vehicle import VehicleState, parse_vehicle_data
from .base import ActionError, NoParams, Params, Vars, action
from .memory import NEAR_CAR_METRES, _say

if TYPE_CHECKING:
    from ..runtime import Runtime

CarAction = Literal[
    "lock", "unlock", "climate_on", "climate_off", "defrost_on", "defrost_off", "set_temp",
    "frunk", "trunk", "honk", "flash", "charge_port_open", "charge_port_close", "charge_start", "charge_stop",
    "charge_limit", "sentry_on", "sentry_off", "vent_windows", "close_windows",
    "seat_heat_on", "seat_heat_off", "wheel_heat_on", "wheel_heat_off", "garage",
]
NEEDS_NEAR: frozenset[str] = frozenset({"unlock", "frunk", "trunk", "vent_windows", "garage"})
NEEDS_LOCATION: frozenset[str] = frozenset({"vent_windows", "close_windows", "garage"})
TEMP_C = (15.0, 28.0)


def to_celsius(value: float) -> float:
    """The car takes Celsius. Its range (15-28 °C, 59-82 °F) doesn't overlap, so the number says which."""
    c = (value - 32) * 5 / 9 if value > 40 else value
    if not TEMP_C[0] <= round(c, 1) <= TEMP_C[1]:
        raise ActionError("the car takes 59-82°F (15-28°C)")
    return round(c, 1)


def show_temp(c: float, unit: str) -> str:
    return f"{round(c * 9 / 5 + 32)}°F" if unit == "F" else f"{c:g}°C"


class CarCommandParams(Params):
    action: CarAction
    value: float | None = Field(None, title="Value",
                                description="Temperature (°F or °C), charge limit (%) or seat heat level (1-3). "
                                            "A {value} in the skill's phrase fills it in.")
    require_near: bool | None = Field(None, title="Only when I'm at the car",
                                      description="Default: on for unlock, trunk, frunk, vent and garage.")


def _value(p: CarCommandParams, v: Vars) -> float | None:
    if p.value is not None:
        return p.value
    raw = v.get("value")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        raise ActionError(f"didn't understand the number {raw!r}") from None


async def _awake_state(rt: Runtime) -> VehicleState:
    """Wake the car if needed and read where it is (commands that need location must wake it anyway)."""
    assert rt.tesla is not None
    await rt.tesla.ensure_awake()
    return parse_vehicle_data(await rt.tesla.vehicle_data(), time.time())


def _command(p: CarCommandParams, value: float | None, state: VehicleState | None,
             unit: str) -> tuple[str, dict[str, Any], str]:
    """(Fleet API command, params, what to say)."""
    a = p.action
    loc = {"lat": state.lat, "lon": state.lon} if state and state.lat is not None and state.lon is not None else {}
    simple: dict[str, tuple[str, dict[str, Any], str]] = {
        "lock": ("door_lock", {}, "🔒 Car locked"),
        "unlock": ("door_unlock", {}, "🔓 Car unlocked"),
        "climate_on": ("auto_conditioning_start", {}, "🌡️ Car climate on"),
        "climate_off": ("auto_conditioning_stop", {}, "Car climate off"),
        "defrost_on": ("set_preconditioning_max", {"on": True, "manual_override": True}, "🧊 Defrosting the car"),
        "defrost_off": ("set_preconditioning_max", {"on": False, "manual_override": True}, "Defrost off"),
        "frunk": ("actuate_trunk", {"which_trunk": "front"}, "🧳 Frunk opening"),
        "trunk": ("actuate_trunk", {"which_trunk": "rear"}, "🧳 Trunk moving"),
        "honk": ("honk_horn", {}, "📯 Honked"),
        "flash": ("flash_lights", {}, "💡 Lights flashed"),
        "charge_port_open": ("charge_port_door_open", {}, "🔌 Charge port open"),
        "charge_port_close": ("charge_port_door_close", {}, "🔌 Charge port closed"),
        "charge_start": ("charge_start", {}, "⚡ Charging started"),
        "charge_stop": ("charge_stop", {}, "⚡ Charging stopped"),
        "sentry_on": ("set_sentry_mode", {"on": True}, "🛡️ Sentry on"),
        "sentry_off": ("set_sentry_mode", {"on": False}, "Sentry off"),
        "vent_windows": ("window_control", {"command": "vent", **loc}, "🪟 Windows venting"),
        "close_windows": ("window_control", {"command": "close", **loc}, "🪟 Windows closing"),
        "wheel_heat_on": ("remote_steering_wheel_heater_request", {"on": True}, "🔥 Steering wheel heater on"),
        "wheel_heat_off": ("remote_steering_wheel_heater_request", {"on": False}, "Steering wheel heater off"),
        "seat_heat_off": ("remote_seat_heater_request", {"heater": 0, "level": 0}, "Seat heater off"),
        "garage": ("trigger_homelink", loc, "🏠 Garage door"),
    }
    if a in simple:
        return simple[a]
    if a == "set_temp":
        if value is None:
            raise ActionError("say a temperature, e.g. “set the car to 70”")
        c = to_celsius(value)
        return "set_temps", {"driver_temp": c, "passenger_temp": c}, f"🌡️ Car set to {show_temp(c, unit)}"
    if a == "charge_limit":
        if value is None or not 50 <= value <= 100:
            raise ActionError("charge limit must be 50-100%")
        return "set_charge_limit", {"percent": int(value)}, f"🔋 Charge limit {int(value)}%"
    if a == "seat_heat_on":
        level = int(value) if value is not None else 3
        if not 1 <= level <= 3:
            raise ActionError("seat heat level is 1-3")
        return ("remote_seat_heater_request", {"heater": 0, "level": level},
                f"💺 Seat heater on ({['', 'low', 'medium', 'high'][level]})")
    raise ActionError(f"unknown car action {a!r}")


@action("car.command", CarCommandParams, primary="action")
async def car_command(p: CarCommandParams, rt: Runtime, v: Vars) -> Vars:
    if rt.tesla is None or rt.vehicle is None:
        raise ActionError("the car integration is off (set TESLA_CLIENT_ID in .env)")
    value = _value(p, v)
    near = p.require_near if p.require_near is not None else p.action in NEEDS_NEAR
    state: VehicleState | None = None
    if near or p.action in NEEDS_LOCATION:
        state = await _awake_state(rt)
    if near:
        phone = fresh_fix(await rt.context.get("location"))
        if phone is None:
            await _say(rt, v, "🚗 Not done: I can't tell you're at the car (no recent location from your phone).",
                       "important")
            return {"car": {"did": "nothing", "reason": "no phone location"}}
        if state is None or state.lat is None or state.lon is None:
            await _say(rt, v, "🚗 Not done: the car didn't report where it is.", "important")
            return {"car": {"did": "nothing", "reason": "no car location"}}
        metres = distance_m(phone[0], phone[1], state.lat, state.lon)
        if metres > NEAR_CAR_METRES:
            await _say(rt, v, f"🚗 Not done: you're about {round(metres / 100) / 10:g} km from the car. "
                              "Use the Tesla app if you mean it.", "important")
            return {"car": {"did": "nothing", "reason": "far", "metres": round(metres)}}
    if p.action == "garage" and not (state and state.lat is not None):
        raise ActionError("the car didn't report its location, which HomeLink needs")
    command, params, said = _command(p, value, state, rt.tesla.config.temperature_unit)
    await rt.tesla.command(command, params, wake=state is None)
    rt.vehicle.invalidate()
    await _say(rt, v, said, "normal")
    return {"car": {"did": p.action, "command": command}}


@action("car.status", NoParams)
async def car_status(p: NoParams, rt: Runtime, v: Vars) -> Vars:
    """Battery, range, locks and climate, without waking a sleeping car."""
    if rt.vehicle is None or rt.tesla is None:
        raise ActionError("the car integration is off (set TESLA_CLIENT_ID in .env)")
    state = await rt.vehicle.state(allow_stale=True)
    if state is None:
        raise ActionError("couldn't reach the car")
    await _say(rt, v, describe_state(state, rt.tesla.config.temperature_unit), "normal")
    return {"car": state.as_dict()}


def describe_state(s: VehicleState, unit: str) -> str:
    parts: list[str] = []
    if s.battery is not None:
        parts.append(f"🔋 {s.battery}%" + (f" ({round(s.range_mi)} mi)" if s.range_mi is not None else ""))
    if not s.online:
        parts.append("asleep, locked")
        return "🚗 " + " · ".join(parts) + (" (battery as last seen)" if s.battery is not None else "")
    parts.append("locked" if s.locked else "UNLOCKED" if s.locked is False else "lock state unknown")
    if s.inside_c is not None:
        parts.append(f"{show_temp(s.inside_c, unit)} inside")
    if s.climate_on:
        parts.append("climate on")
    if s.charging == "Charging":
        parts.append("charging")
    if s.sentry:
        parts.append("sentry on")
    if s.user_present:
        parts.append("someone inside")
    return "🚗 " + " · ".join(parts)
