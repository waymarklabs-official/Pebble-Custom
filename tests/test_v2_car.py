"""Smart car skill against a respx-mocked Tesla Fleet API (token, vehicle, vehicle_data, commands)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from ringhub.actions.base import ActionError
from ringhub.catalog import Mode, Skill
from ringhub.config import Config
from ringhub.engine import Engine
from ringhub.runtime import Runtime
from ringhub.vehicle import VehicleState, parse_vehicle_data

from .conftest import TESLA_AUTH, TESLA_CONFIG, TESLA_VEHICLE, NtfyMock, ring_event

Factory = Callable[..., Any]
CAR_LAT, CAR_LON = 41.8781, -87.6298


def vehicle_data(*, locked: bool = True, user_present: bool = False, shift: str | None = None,
                 lat: float = CAR_LAT, lon: float = CAR_LON) -> httpx.Response:
    return httpx.Response(200, json={"response": {
        "vehicle_state": {"locked": locked, "is_user_present": user_present},
        "drive_state": {"shift_state": shift, "latitude": lat, "longitude": lon},
        "climate_state": {"is_climate_on": False},
    }})


def state(name: str) -> httpx.Response:
    return httpx.Response(200, json={"response": {"state": name, "vin": "x"}})


class Fleet:
    def __init__(self, router: Any) -> None:
        self.auth = router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
            "access_token": "access-1", "expires_in": 3600, "refresh_token": "refresh-1"}))
        self.vehicle = router.get(TESLA_VEHICLE).mock(return_value=state("online"))
        self.data = router.get(f"{TESLA_VEHICLE}/vehicle_data").mock(return_value=vehicle_data())
        self.wake = router.post(f"{TESLA_VEHICLE}/wake_up").mock(return_value=state("online"))
        self.command = router.post(url__regex=rf"^{TESLA_VEHICLE}/command/\w+$").mock(
            return_value=httpx.Response(200, json={"response": {"result": True, "reason": ""}}))

    def commands(self) -> list[tuple[str, Any]]:
        out = []
        for call in self.command.calls:
            body = json.loads(call.request.content) if call.request.content else None
            out.append((call.request.url.path.rsplit("/", 1)[-1], body))
        return out


@pytest.fixture
def fleet(ntfy: NtfyMock) -> Fleet:
    return Fleet(ntfy.router)


@pytest.fixture
async def rt(make_runtime: Factory, make_config: Callable[..., Config], fleet: Fleet) -> Runtime:
    runtime: Runtime = await make_runtime(make_config(TESLA_CONFIG))
    return runtime


async def run_car(rt: Runtime, skill: str = "car") -> dict[str, Any]:
    result = await rt.run_skill(skill, await rt.base_vars())
    return dict(result["car"]) if "car" in result else result


# ----------------------------------------------------------------------------- the decision table


async def test_asleep_car_is_woken_and_preconditioned(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.vehicle.side_effect = [state("asleep"), state("online")]  # read, then the wake poll
    assert await run_car(rt) == {"did": "precondition", "state": "asleep"}
    assert not fleet.data.called  # never read vehicle_data from a sleeping car (would 408, not wake)
    assert fleet.wake.call_count == 1
    assert fleet.commands() == [("auto_conditioning_start", None)]
    assert ntfy.last()["message"] == "🚗 Climate on (car was asleep)"


async def test_offline_car_counts_as_asleep(rt: Runtime, fleet: Fleet) -> None:
    fleet.vehicle.side_effect = [state("offline"), state("online")]
    assert (await run_car(rt))["did"] == "precondition"
    assert fleet.wake.called


async def test_locked_online_car_preconditions_without_waking(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=True))
    assert await run_car(rt) == {"did": "precondition", "state": "locked"}
    assert not fleet.wake.called
    assert fleet.commands() == [("auto_conditioning_start", None)]
    assert ntfy.last()["message"] == "🚗 Climate on (car was locked)"


async def test_unlocked_parked_car_opens_the_trunk(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=False, shift="P"))
    assert await run_car(rt) == {"did": "rear_trunk", "state": "unlocked"}
    assert fleet.commands() == [("actuate_trunk", {"which_trunk": "rear"})]
    assert not fleet.wake.called
    assert ntfy.last()["message"] == "🚗 Trunk opening"


async def test_frunk_variant(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    car = await rt.catalog.skill("car")
    assert car is not None
    await rt.catalog.put(Skill.model_validate({**car.model_dump(), "params": {"unlocked": "frunk"}}))
    fleet.data.mock(return_value=vehicle_data(locked=False))
    assert (await run_car(rt))["did"] == "front_trunk"
    assert fleet.commands() == [("actuate_trunk", {"which_trunk": "front"})]
    assert ntfy.last()["message"] == "🚗 Frunk opening"


@pytest.mark.parametrize("data,summary", [
    (vehicle_data(locked=False, user_present=True), "unlocked, someone inside"),
    (vehicle_data(locked=False, shift="D"), "unlocked, in D"),
    (vehicle_data(locked=False, shift="R"), "unlocked, in R"),
])
async def test_never_opens_anything_with_someone_inside_or_in_gear(rt: Runtime, fleet: Fleet, ntfy: NtfyMock,
                                                                   data: httpx.Response, summary: str) -> None:
    fleet.data.mock(return_value=data)
    assert await run_car(rt) == {"did": "nothing", "state": summary}
    assert fleet.commands() == []
    assert ntfy.last()["message"] == "🚗 You're in the car — not opening anything."


async def test_refuses_when_the_phone_is_far_from_an_unlocked_car(rt: Runtime, fleet: Fleet,
                                                                  ntfy: NtfyMock) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=False))
    await rt.context.set("location", {"lat": CAR_LAT + 0.01, "lon": CAR_LON, "at": time.time()})  # ~1.1 km
    assert await run_car(rt) == {"did": "nothing", "state": "unlocked", "far": True}
    assert fleet.commands() == []
    push = ntfy.last()
    assert "not near it" in push["message"] and push["priority"] == 4  # important: always delivered


async def test_phone_next_to_the_car_is_fine(rt: Runtime, fleet: Fleet) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=False))
    await rt.context.set("location", {"lat": CAR_LAT + 0.0005, "lon": CAR_LON, "at": time.time()})  # ~55 m
    assert (await run_car(rt))["did"] == "rear_trunk"


async def test_stale_phone_location_is_not_trusted(rt: Runtime, fleet: Fleet) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=False))
    await rt.context.set("location", {"lat": 0.0, "lon": 0.0, "at": time.time() - 3600})
    assert (await run_car(rt))["did"] == "rear_trunk"


async def test_far_phone_does_not_block_climate(rt: Runtime, fleet: Fleet) -> None:
    await rt.context.set("location", {"lat": 0.0, "lon": 0.0, "at": time.time()})
    assert (await run_car(rt))["did"] == "precondition"


@pytest.mark.parametrize("params,data,message", [
    ({"unlocked": "nothing"}, vehicle_data(locked=False), "🚗 Car is unlocked."),
    ({"locked": "nothing"}, vehicle_data(locked=True), "🚗 Car is locked."),
])
async def test_nothing_options(rt: Runtime, fleet: Fleet, ntfy: NtfyMock, params: dict[str, str],
                               data: httpx.Response, message: str) -> None:
    car = await rt.catalog.skill("car")
    assert car is not None
    await rt.catalog.put(Skill.model_validate({**car.model_dump(), "params": params}))
    fleet.data.mock(return_value=data)
    assert (await run_car(rt))["did"] == "nothing"
    assert fleet.commands() == []
    assert ntfy.last()["message"] == message


# ----------------------------------------------------------------------------- failures


async def test_unreachable_api_is_an_error_and_does_nothing(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.vehicle.mock(return_value=httpx.Response(500, text="upstream down"))
    with pytest.raises(ActionError, match="couldn't reach the car"):
        await run_car(rt)
    assert fleet.commands() == [] and not fleet.wake.called


async def test_unreachable_car_pushes_an_error_through_the_engine(rt: Runtime, fleet: Fleet,
                                                                   ntfy: NtfyMock) -> None:
    fleet.vehicle.mock(side_effect=httpx.ConnectError("no route"))
    await rt.catalog.put(Mode.model_validate({"id": "garage", "name": "Garage", "bindings": {"click": "car"}}),
                         position=0)
    outcome = await Engine(rt).handle(ring_event("click"))
    assert outcome.startswith("error:") and "couldn't reach the car" in outcome
    push = ntfy.last()
    assert push["title"] == "⚠️ click failed" and "couldn't reach the car" in push["message"]
    assert (await rt.store.recent_events(1))[0]["status"] == "error"


async def test_token_refresh_failure_means_unreachable(rt: Runtime, fleet: Fleet) -> None:
    fleet.auth.mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))
    with pytest.raises(ActionError, match="couldn't reach the car"):
        await run_car(rt)
    assert not fleet.vehicle.called


async def test_rejected_command_is_an_error(rt: Runtime, fleet: Fleet) -> None:
    fleet.data.mock(return_value=vehicle_data(locked=False))
    fleet.command.mock(return_value=httpx.Response(200, json={"response": {"result": False,
                                                                           "reason": "vehicle_unavailable"}}))
    with pytest.raises(ActionError, match="actuate_trunk rejected: vehicle_unavailable"):
        await run_car(rt)


async def test_wake_timeout_is_an_error(make_runtime: Factory, make_config: Callable[..., Config],
                                        fleet: Fleet) -> None:
    cfg = make_config({"tesla": {**TESLA_CONFIG["tesla"], "wake_timeout_seconds": 0}})
    rt = await make_runtime(cfg)
    fleet.vehicle.mock(return_value=state("asleep"))
    with pytest.raises(ActionError, match="did not wake up in time"):
        await run_car(rt)
    assert fleet.wake.called and fleet.commands() == []


async def test_car_skill_without_tesla_config(make_runtime: Factory) -> None:
    rt = await make_runtime()
    with pytest.raises(ActionError, match="needs the 'tesla' section"):
        await rt.executor.run([{"car.smart": {}}], await rt.base_vars())


# ----------------------------------------------------------------------------- reads, cache, token


async def test_state_is_cached_within_ttl_and_invalidated_after_commands(rt: Runtime, fleet: Fleet) -> None:
    assert rt.vehicle is not None
    first = await rt.vehicle.state()
    second = await rt.vehicle.state()
    assert first is second and first is not None and first.locked is True
    assert fleet.vehicle.call_count == 1 and fleet.data.call_count == 1
    rt.vehicle._cached = replace(first, fetched_at=time.time() - 31)  # TTL is 30 s
    await rt.vehicle.state()
    assert fleet.data.call_count == 2
    await run_car(rt)  # uses the cached read, then invalidates after commanding
    assert fleet.data.call_count == 2
    await rt.vehicle.state()
    assert fleet.data.call_count == 3


async def test_vehicle_data_request_shape(rt: Runtime, fleet: Fleet) -> None:
    assert rt.vehicle is not None
    await rt.vehicle.state()
    req = fleet.data.calls.last.request
    assert req.headers["Authorization"] == "Bearer access-1"
    assert parse_qs(req.url.query.decode())["endpoints"] == ["vehicle_state;drive_state;climate_state;location_data"]


async def test_token_refresh_rotates_and_persists(rt: Runtime, fleet: Fleet, make_runtime: Factory,
                                                  make_config: Callable[..., Config]) -> None:
    assert rt.vehicle is not None
    await rt.vehicle.state()
    rt.vehicle.invalidate()
    await rt.vehicle.state()
    assert fleet.auth.call_count == 1  # access token reused until it nearly expires
    form = parse_qs(fleet.auth.calls.last.request.content.decode())
    assert form == {"grant_type": ["refresh_token"], "client_id": ["client-id"], "refresh_token": ["refresh-0"]}
    assert await rt.store.get("tesla.refresh_token") == "refresh-1"

    # A restart uses the rotated token, not the (now invalid) one from the config file.
    rt2 = await make_runtime(make_config(TESLA_CONFIG), seed=False)
    assert rt2.vehicle is not None
    await rt2.vehicle.state()
    assert parse_qs(fleet.auth.calls.last.request.content.decode())["refresh_token"] == ["refresh-1"]


async def test_short_lived_token_is_refreshed_each_time(rt: Runtime, fleet: Fleet) -> None:
    fleet.auth.mock(return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 30}))
    assert rt.tesla is not None
    await rt.tesla.online_state()
    await rt.tesla.online_state()
    assert fleet.auth.call_count == 2  # within the 60 s safety margin
    assert await rt.store.get("tesla.refresh_token") is None  # nothing rotated, nothing stored


async def test_asleep_state_keeps_last_known_position_when_stale_allowed(rt: Runtime, fleet: Fleet) -> None:
    assert rt.vehicle is not None
    await rt.vehicle.state()
    rt.vehicle.invalidate()
    fleet.vehicle.mock(return_value=state("asleep"))
    asleep = await rt.vehicle.state(allow_stale=True)
    assert asleep is not None
    assert (asleep.online, asleep.locked, asleep.lat, asleep.lon) == (False, True, CAR_LAT, CAR_LON)
    rt.vehicle.invalidate()
    plain = await rt.vehicle.state()
    assert plain is not None and plain.lat is None
    rt.vehicle.invalidate()
    fleet.vehicle.mock(return_value=httpx.Response(503))
    assert await rt.vehicle.state() is None
    stale = await rt.vehicle.state(allow_stale=True)
    assert stale is not None and stale.lat == CAR_LAT


async def test_http_408_means_asleep(rt: Runtime, fleet: Fleet) -> None:
    fleet.data.mock(return_value=httpx.Response(408))
    assert rt.tesla is not None
    with pytest.raises(ActionError, match="asleep"):
        await rt.tesla.vehicle_data()


def test_parse_vehicle_data_is_defensive() -> None:
    s = parse_vehicle_data({"vehicle_state": {"locked": "yes"}, "drive_state": {"latitude": "x"},
                            "location_data": {"latitude": 1.5, "longitude": 2.5}}, now=5.0)
    assert s == VehicleState(online=True, locked=None, lat=1.5, lon=2.5, fetched_at=5.0)
    assert s.summary == "lock state unknown"
    assert parse_vehicle_data({}, now=0).as_dict()["summary"] == "lock state unknown"
    assert VehicleState(online=False).summary == "asleep"


async def test_car_command_template_wakes_then_commands(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.vehicle.side_effect = [state("online")]
    await rt.run_skill("frunk", await rt.base_vars())
    assert fleet.wake.called
    assert fleet.commands() == [("actuate_trunk", {"which_trunk": "front"})]
    assert ntfy.last()["message"] == "🧳 Open the frunk"  # car_command confirms
