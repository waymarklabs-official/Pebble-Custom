"""Tesla integration: config, connecting, registration, friendly car commands and their safety rules."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ringhub.actions.base import ActionError
from ringhub.actions.car import describe_state, to_celsius
from ringhub.actions.tesla import REFRESH_KEY, REGION_KEY, SERVERS, VIN_KEY, TeslaClient, public_key_hex
from ringhub.catalog import Skill
from ringhub.config import Config
from ringhub.runtime import Runtime
from ringhub.seed import car_skills, ensure_skills
from ringhub.vehicle import VehicleState

from .conftest import TESLA_AUTH, TESLA_CONFIG, TESLA_VEHICLE, TESLA_VIN, NtfyMock
from .test_v2_car import CAR_LAT, CAR_LON, Fleet, state

Factory = Callable[..., Any]

# A real prime256v1 key pair (public half only; generated with deploy/pi/tesla-keys.sh).
PUBLIC_PEM = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjmDOJrwQpXjwsueT99UrnKFB0TA1
NQNymMxAoB9K2kFXlhR4UV2Z3FpIK0C1V8dCzoNs67Y4EPwnMpDaw88SDA==
-----END PUBLIC KEY-----"""


@pytest.fixture
def fleet(ntfy: NtfyMock) -> Fleet:
    return Fleet(ntfy.router)


@pytest.fixture
async def rt(make_runtime: Factory, make_config: Callable[..., Config], fleet: Fleet) -> Runtime:
    runtime: Runtime = await make_runtime(make_config(TESLA_CONFIG))
    await ensure_skills(runtime.catalog, car_skills())
    return runtime


def tesla_cfg(make_config: Callable[..., Config], **tesla: Any) -> Config:
    return make_config({"tesla": {**TESLA_CONFIG["tesla"], **tesla}})


# ----------------------------------------------------------------------------- config


def test_tesla_and_google_stay_off_until_a_client_id_is_set(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"tesla": {"client_id": "", "client_secret": "", "domain": ""},
                       "google": {"client_id": "  ", "client_secret": ""}})
    assert cfg.tesla is None and cfg.google is None


def test_blank_tesla_fields_from_env_mean_unset(make_config: Callable[..., Config]) -> None:
    cfg = make_config({"tesla": {"client_id": "abc", "client_secret": "", "domain": "", "vin": "",
                                 "base_url": "", "region": "", "refresh_token": ""}})
    assert cfg.tesla is not None
    t = cfg.tesla
    assert (t.client_secret, t.domain, t.vin, t.region, t.refresh_token) == (None, None, None, None, None)
    assert t.base_url == "https://tesla-proxy:4443"


def test_google_needs_its_secret_when_turned_on(make_config: Callable[..., Config]) -> None:
    from ringhub.config import ConfigError

    with pytest.raises(ConfigError, match="GOOGLE_CLIENT_SECRET"):
        make_config({"google": {"client_id": "x.apps.googleusercontent.com", "client_secret": ""}})


def test_example_config_loads_with_and_without_integrations(tmp_path: Path) -> None:
    from ringhub.config import load_config

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    base = {"RINGHUB_ADMIN_TOKEN": "a" * 24, "RINGHUB_CONTEXT_TOKEN": "c" * 24, "RINGHUB_SECRET": "s" * 32}
    off = load_config(example, base)
    assert off.tesla is None and off.google is None
    on = load_config(example, {**base, "TESLA_CLIENT_ID": "tid", "TESLA_CLIENT_SECRET": "ts",
                               "TESLA_DOMAIN": "me.github.io", "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gs"})
    assert on.tesla is not None and on.tesla.domain == "me.github.io"
    assert str(on.tesla.ca_cert).endswith("data/tesla/tls-cert.pem")
    assert on.google is not None and on.google.calendar_id == "primary"


# ----------------------------------------------------------------------------- keys


def test_public_key_hex_reads_the_point_tesla_reports() -> None:
    hex_point = public_key_hex(PUBLIC_PEM)
    assert len(hex_point) == 130 and hex_point.startswith("04")


@pytest.mark.parametrize("pem", ["not a key", "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----"])
def test_public_key_hex_rejects_anything_else(pem: str) -> None:
    with pytest.raises(ActionError):
        public_key_hex(pem)


def test_key_script_never_replaces_a_key(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "deploy" / "pi").mkdir(parents=True)
    shutil.copy(root / "deploy" / "pi" / "tesla-keys.sh", tmp_path / "deploy" / "pi")
    run = lambda: subprocess.run(["bash", str(tmp_path / "deploy/pi/tesla-keys.sh")],  # noqa: E731
                                 capture_output=True, text=True, timeout=60, check=True)
    run()
    d = tmp_path / "data" / "tesla"
    first = {f: (d / f).read_bytes() for f in ("private-key.pem", "tls-cert.pem", "tls-key.pem")}
    assert oct((d / "private-key.pem").stat().st_mode & 0o777) == "0o600"
    hex_point = public_key_hex((d / "public-key.pem").read_text())
    assert hex_point.startswith("04")
    out = run().stdout
    assert "keeping the existing private key" in out
    assert {f: (d / f).read_bytes() for f in first} == first



def test_key_script_also_makes_the_callback_page(tmp_path: Path) -> None:
    """callback.html is needed on the public site before Tesla can be set up, so it comes out of the
    same step as the public key, made from RINGHUB_PUBLIC_URL in .env."""
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "deploy" / "pi").mkdir(parents=True)
    shutil.copy(root / "deploy" / "pi" / "tesla-keys.sh", tmp_path / "deploy" / "pi")
    (tmp_path / "src").symlink_to(root / "src")
    run = lambda: subprocess.run(["bash", str(tmp_path / "deploy/pi/tesla-keys.sh")],  # noqa: E731
                                 capture_output=True, text=True, timeout=60, check=True).stdout
    page = tmp_path / "data" / "tesla" / "callback.html"
    assert "callback.html: not made (no .env here yet)" in run() and not page.exists()
    (tmp_path / ".env").write_text("RINGHUB_PUBLIC_URL=http://not-https\n")
    assert "must look like https://" in run() and not page.exists()
    (tmp_path / ".env").write_text("RINGHUB_PUBLIC_URL=https://tesla-stream.tail51b89d.ts.net:8445\n")
    out = run()
    assert "data/tesla/callback.html   ->  callback.html" in out
    assert 'location.replace("https://tesla-stream.tail51b89d.ts.net:8445/oauth/callback" + q)' in page.read_text()
    assert oct(page.stat().st_mode & 0o777) == "0o644"


# ----------------------------------------------------------------------------- connecting


async def test_authorize_url_asks_for_the_right_scopes(rt: Runtime) -> None:
    assert rt.tesla is not None
    url = rt.tesla.authorize_url("st4te", "https://hub.test/oauth/tesla/callback")
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("https://auth.tesla.com/oauth2/v3/authorize?")
    assert q["state"] == ["st4te"] and q["redirect_uri"] == ["https://hub.test/oauth/tesla/callback"]
    assert {"offline_access", "vehicle_cmds", "vehicle_device_data", "vehicle_location"} <= set(q["scope"][0].split())


async def test_exchange_code_learns_the_region_and_keeps_the_refresh_token(
        make_runtime: Factory, make_config: Callable[..., Config], ntfy: NtfyMock) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, client_secret="sekret", refresh_token=None))
    assert rt.tesla is not None and not await rt.tesla.connected()
    route = ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a1", "refresh_token": "r1", "expires_in": 3600}))
    ntfy.router.get(f"{SERVERS['eu']}/api/1/users/region").mock(return_value=httpx.Response(200, json={
        "response": {"region": "eu", "fleet_api_base_url": SERVERS["eu"]}}))
    await rt.tesla.exchange_code("EU_abc123", "https://hub.test/oauth/tesla/callback")
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["client_secret"] == ["sekret"]
    assert body["audience"] == [SERVERS["eu"]] and body["code"] == ["EU_abc123"]
    assert await rt.store.get(REFRESH_KEY) == "r1" and await rt.store.get(REGION_KEY) == "eu"
    assert await rt.tesla.connected() and await rt.tesla.region() == "eu"


async def test_region_comes_from_tesla_not_the_code_prefix(
        make_runtime: Factory, make_config: Callable[..., Config], ntfy: NtfyMock) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, client_secret="s", refresh_token=None, read_url=None))
    assert rt.tesla is not None
    ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={
        "access_token": "a1", "refresh_token": "r1", "expires_in": 3600}))
    na = ntfy.router.get(f"{SERVERS['na']}/api/1/users/region").mock(return_value=httpx.Response(421, json={
        "error": "user out of region"}))
    eu = ntfy.router.get(f"{SERVERS['eu']}/api/1/users/region").mock(return_value=httpx.Response(200, json={
        "response": {"region": "eu", "fleet_api_base_url": SERVERS["eu"]}}))
    await rt.tesla.exchange_code("abc-without-prefix", "https://x/cb")
    assert na.called and eu.called and await rt.tesla.region() == "eu"


async def test_wrong_region_is_followed_once(
        make_runtime: Factory, make_config: Callable[..., Config], ntfy: NtfyMock, fleet: Fleet) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, read_url=None))
    assert rt.tesla is not None
    ntfy.router.get(f"{SERVERS['na']}/api/1/vehicles/{TESLA_VIN}").mock(return_value=httpx.Response(421, json={
        "error": "user out of region"}))
    ntfy.router.get(f"{SERVERS['na']}/api/1/users/region").mock(return_value=httpx.Response(421))
    ntfy.router.get(f"{SERVERS['eu']}/api/1/users/region").mock(return_value=httpx.Response(200, json={
        "response": {"region": "eu"}}))
    eu_car = ntfy.router.get(f"{SERVERS['eu']}/api/1/vehicles/{TESLA_VIN}").mock(return_value=state("asleep"))
    assert await rt.tesla.online_state() == "asleep"
    assert eu_car.called and await rt.tesla.region() == "eu"


@pytest.mark.parametrize(("status", "body", "words"), [
    (402, {"error": "payment required"}, "payment method on your developer account"),
    (403, {"error": "unauthorized_missing_scopes"}, "Vehicle Information and Vehicle Location scopes"),
    (404, {"error": "not_found"}, "pick the car again"),
    (412, {"error": "Account must be registered in the current region"}, "press Register"),
    (429, {}, "try again in a minute"),
    (500, {"error": "internal"}, "HTTP 500"),
])
async def test_tesla_refusals_are_explained(rt: Runtime, fleet: Fleet, status: int, body: dict[str, Any],
                                            words: str) -> None:
    fleet.vehicle.mock(return_value=httpx.Response(status, json=body))
    assert rt.tesla is not None
    with pytest.raises(ActionError) as e:
        await rt.tesla.online_state()
    assert words in str(e.value)
    if body.get("error"):
        assert f"Tesla says: {body['error']}" in str(e.value)


async def test_blocked_network_is_explained(rt: Runtime, fleet: Fleet) -> None:
    fleet.vehicle.side_effect = httpx.ConnectError("Name or service not known")
    assert rt.tesla is not None
    with pytest.raises(ActionError, match=r"couldn.t reach tesla\.test from this machine"):
        await rt.tesla.online_state()


async def test_exchange_code_needs_the_client_secret(rt: Runtime) -> None:
    assert rt.tesla is not None
    with pytest.raises(ActionError, match="TESLA_CLIENT_SECRET"):
        await rt.tesla.exchange_code("NA_x", "https://hub.test/oauth/tesla/callback")


async def test_refresh_token_rotation_is_persisted_and_revocation_is_explained(rt: Runtime, fleet: Fleet) -> None:
    assert rt.tesla is not None
    await rt.tesla.online_state()
    assert await rt.store.get(REFRESH_KEY) == "refresh-1"            # single-use: the new one is kept
    fleet.auth.mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))
    rt.tesla._access = None
    with pytest.raises(ActionError, match="reconnect Tesla"):
        await rt.tesla.online_state()


async def test_select_vehicle_picks_the_only_car_or_asks(
        make_runtime: Factory, make_config: Callable[..., Config], fleet: Fleet, ntfy: NtfyMock) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, vin=None))
    assert rt.tesla is not None
    cars = ntfy.router.get("https://tesla.test/api/1/vehicles").mock(return_value=httpx.Response(200, json={
        "response": [{"vin": TESLA_VIN, "display_name": "Blue", "state": "asleep"}]}))
    assert await rt.tesla.select_vehicle() == TESLA_VIN
    assert await rt.store.get(VIN_KEY) == TESLA_VIN
    cars.mock(return_value=httpx.Response(200, json={"response": [{"vin": TESLA_VIN}, {"vin": "5YJ3E1EA7KF000002"}]}))
    with pytest.raises(ActionError, match="pick which car"):
        await rt.tesla.select_vehicle()
    with pytest.raises(ActionError, match="isn't on this account"):
        await rt.tesla.select_vehicle("5YJ3E1EA7KF000009")


async def test_commands_say_how_to_pick_a_car_when_none_is_chosen(
        make_runtime: Factory, make_config: Callable[..., Config], fleet: Fleet) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, vin=None))
    assert rt.tesla is not None
    with pytest.raises(ActionError, match="no car selected"):
        await rt.tesla.command("door_lock", {}, wake=False)


async def test_register_domain_uses_a_partner_token_and_checks_the_key(
        make_runtime: Factory, make_config: Callable[..., Config], ntfy: NtfyMock, tmp_path: Path) -> None:
    pub = tmp_path / "public-key.pem"
    pub.write_text(PUBLIC_PEM)
    rt: Runtime = await make_runtime(tesla_cfg(make_config, client_secret="sekret", domain="me.github.io",
                                               public_key=str(pub)))
    assert rt.tesla is not None
    token = ntfy.router.post(TESLA_AUTH).mock(return_value=httpx.Response(200, json={"access_token": "partner"}))
    base = SERVERS["na"]
    reg = ntfy.router.post(f"{base}/api/1/partner_accounts").mock(return_value=httpx.Response(200, json={}))
    key = ntfy.router.get(f"{base}/api/1/partner_accounts/public_key").mock(return_value=httpx.Response(
        200, json={"response": {"public_key": public_key_hex(PUBLIC_PEM)}}))
    result = await rt.tesla.register_domain()
    body = parse_qs(token.calls.last.request.content.decode())
    assert body["grant_type"] == ["client_credentials"] and body["audience"] == [base]
    assert json.loads(reg.calls.last.request.content) == {"domain": "me.github.io"}
    assert reg.calls.last.request.headers["Authorization"] == "Bearer partner"
    assert key.calls.last.request.url.params["domain"] == "me.github.io"
    assert result == {"domain": "me.github.io", "region": "na", "registered_key": public_key_hex(PUBLIC_PEM),
                      "matches_local_key": True}
    key.mock(return_value=httpx.Response(200, json={"response": {"public_key": "04" + "ab" * 64}}))
    with pytest.raises(ActionError, match="different public key"):
        await rt.tesla.register_domain()


async def test_pairing_link_names_the_domain(make_runtime: Factory, make_config: Callable[..., Config]) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, domain="me.github.io"))
    assert rt.tesla is not None and rt.tesla.pairing_link() == "https://tesla.com/_ak/me.github.io"


async def test_reads_go_to_fleet_api_and_commands_to_the_proxy(
        make_runtime: Factory, make_config: Callable[..., Config], ntfy: NtfyMock, fleet: Fleet) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, read_url=None, base_url="https://tesla.test"))
    assert rt.tesla is not None
    region_read = ntfy.router.get(f"{SERVERS['na']}/api/1/vehicles/{TESLA_VIN}").mock(return_value=state("asleep"))
    assert await rt.tesla.online_state() == "asleep"
    assert region_read.called and not fleet.vehicle.called
    await rt.tesla.command("flash_lights", {}, wake=False)
    assert fleet.command.called                                     # the proxy (base_url)


async def test_unpaired_key_error_tells_you_to_pair(rt: Runtime, fleet: Fleet) -> None:
    fleet.command.mock(return_value=httpx.Response(500, text="vehicle rejected request: unknown public key"))
    assert rt.tesla is not None
    with pytest.raises(ActionError, match="pairing link"):
        await rt.tesla.command("door_lock", {}, wake=False)


async def test_proxy_not_running_is_explained(rt: Runtime, fleet: Fleet) -> None:
    fleet.command.side_effect = httpx.ConnectError("refused")
    assert rt.tesla is not None
    with pytest.raises(ActionError, match="proxy isn't running"):
        await rt.tesla.command("door_lock", {}, wake=False)


# ----------------------------------------------------------------------------- friendly commands


async def say(rt: Runtime, skill: str, ntfy: NtfyMock, **slots: Any) -> dict[str, Any]:
    v = await rt.base_vars()
    v.update(slots)
    result = await rt.run_skill(skill, v)
    return dict(result.get("car") or {})


@pytest.mark.parametrize(("skill", "command", "params", "message"), [
    ("car_lock", "door_lock", {}, "🔒 Car locked"),
    ("car_climate", "auto_conditioning_start", {}, "🌡️ Car climate on"),
    ("car_climate_off", "auto_conditioning_stop", {}, "Car climate off"),
    ("car_defrost", "set_preconditioning_max", {"on": True, "manual_override": True}, "🧊 Defrosting the car"),
    ("car_honk", "honk_horn", {}, "📯 Honked"),
    ("car_flash", "flash_lights", {}, "💡 Lights flashed"),
    ("car_port_open", "charge_port_door_open", {}, "🔌 Charge port open"),
    ("car_charge_stop", "charge_stop", {}, "⚡ Charging stopped"),
    ("car_sentry_on", "set_sentry_mode", {"on": True}, "🛡️ Sentry on"),
    ("car_seat_heat", "remote_seat_heater_request", {"seat_position": 0, "level": 3}, "💺 Seat heater on (high)"),
    ("car_wheel_heat", "remote_steering_wheel_heater_request", {"on": True}, "🔥 Steering wheel heater on"),
])
async def test_simple_commands(rt: Runtime, fleet: Fleet, ntfy: NtfyMock, skill: str, command: str,
                               params: dict[str, Any], message: str) -> None:
    fleet.vehicle.side_effect = [state("asleep"), state("online")]
    assert (await say(rt, skill, ntfy))["command"] == command
    assert fleet.commands() == [(command, params)]
    assert fleet.wake.called                                        # an asleep car is woken first
    assert ntfy.last()["message"] == message


async def test_wake_wakes_a_sleeping_car_without_the_proxy(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.vehicle.side_effect = [state("asleep"), state("online")]
    match = (await rt.phrase_index()).match("wake up the car")
    assert match is not None and match.skill_id == "car_wake"
    assert await say(rt, "car_wake", ntfy) == {"did": "wake", "was": "asleep"}
    assert fleet.wake.call_count == 1 and fleet.commands() == []     # Fleet API only: no key, no proxy
    assert ntfy.last()["message"] == "🚗 The car is awake"


async def test_wake_leaves_an_awake_car_alone(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    assert await say(rt, "car_wake", ntfy) == {"did": "wake", "was": "online"}
    assert not fleet.wake.called                                    # Tesla bills every wake_up
    assert ntfy.last()["message"] == "🚗 The car is already awake"


async def test_wake_says_when_the_car_does_not_answer(make_runtime: Factory, make_config: Callable[..., Config],
                                                     fleet: Fleet, ntfy: NtfyMock) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, wake_timeout_seconds=0))
    await ensure_skills(rt.catalog, car_skills())
    fleet.vehicle.mock(return_value=state("asleep"))
    with pytest.raises(ActionError, match="did not wake up in time"):
        await say(rt, "car_wake", ntfy)


def test_the_car_template_offers_every_car_action() -> None:
    from typing import get_args

    from ringhub.actions.car import CarAction
    from ringhub.templates import CarActionP

    assert get_args(CarActionP.model_fields["action"].annotation) == get_args(CarAction)


@pytest.mark.parametrize(("value", "celsius", "said"), [(70, 21.1, "70°F"), (68, 20.0, "68°F"), (21, 21.0, "70°F")])
async def test_set_temperature_by_voice(rt: Runtime, fleet: Fleet, ntfy: NtfyMock, value: float, celsius: float,
                                        said: str) -> None:
    match = (await rt.phrase_index()).match(f"set the car to {value}")
    assert match is not None and match.skill_id == "car_temp"
    await say(rt, "car_temp", ntfy, **match.slots)
    assert fleet.commands() == [("set_temps", {"driver_temp": celsius, "passenger_temp": celsius})]
    assert ntfy.last()["message"] == f"🌡️ Car set to {said}"


@pytest.mark.parametrize("value", [50, 90, 14, 30])
def test_temperatures_outside_the_cars_range_are_refused(value: float) -> None:
    with pytest.raises(ActionError, match="59-82°F"):
        to_celsius(value)


async def test_charge_limit_by_voice_and_bounds(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    match = (await rt.phrase_index()).match("charge the car to eighty percent")
    assert match is not None and match.skill_id == "car_charge_limit" and match.slots == {"value": "80"}
    await say(rt, "car_charge_limit", ntfy, **match.slots)
    assert fleet.commands() == [("set_charge_limit", {"percent": 80})]
    with pytest.raises(ActionError, match="50-100"):
        await say(rt, "car_charge_limit", ntfy, value="30")


@pytest.mark.parametrize("skill", ["car_unlock", "frunk", "car_trunk", "car_vent", "car_garage"])
async def test_opening_commands_refuse_without_a_phone_location(rt: Runtime, fleet: Fleet, ntfy: NtfyMock,
                                                                skill: str) -> None:
    result = await say(rt, skill, ntfy)
    assert result == {"did": "nothing", "reason": "no phone location"}
    assert fleet.commands() == []
    assert ntfy.last()["message"].startswith("🚗 Not done: I can't tell you're at the car")


@pytest.mark.parametrize("skill", ["car_unlock", "frunk", "car_trunk", "car_vent", "car_garage"])
async def test_opening_commands_refuse_when_you_are_far_away(rt: Runtime, fleet: Fleet, ntfy: NtfyMock,
                                                             skill: str) -> None:
    await rt.context.set("location", {"lat": CAR_LAT + 0.05, "lon": CAR_LON, "at": time.time()})   # ~5.6 km
    result = await say(rt, skill, ntfy)
    assert result["did"] == "nothing" and result["reason"] == "far"
    assert fleet.commands() == []
    assert "km from the car" in ntfy.last()["message"]


@pytest.mark.parametrize(("skill", "command", "params"), [
    ("car_unlock", "door_unlock", {}),
    ("frunk", "actuate_trunk", {"which_trunk": "front"}),
    ("car_vent", "window_control", {"command": "vent", "lat": CAR_LAT, "lon": CAR_LON}),
    ("car_garage", "trigger_homelink", {"lat": CAR_LAT, "lon": CAR_LON}),
])
async def test_opening_commands_run_when_you_are_at_the_car(rt: Runtime, fleet: Fleet, ntfy: NtfyMock, skill: str,
                                                            command: str, params: dict[str, Any]) -> None:
    await rt.context.set("location", {"lat": CAR_LAT + 0.0003, "lon": CAR_LON, "at": time.time()})  # ~33 m
    assert (await say(rt, skill, ntfy))["did"] != "nothing"
    assert fleet.commands() == [(command, params)]


async def test_at_the_car_check_can_be_turned_off_per_skill(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    await rt.catalog.put(Skill.model_validate({"id": "remote_unlock", "name": "Unlock for guests", "icon": "🔓",
                                               "template": "car_action",
                                               "params": {"action": "unlock", "require_near": False}}))
    await say(rt, "remote_unlock", ntfy)
    assert fleet.commands() == [("door_unlock", {})]


async def test_close_windows_sends_the_cars_location(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    await say(rt, "car_close_windows", ntfy)
    assert fleet.commands() == [("window_control", {"command": "close", "lat": CAR_LAT, "lon": CAR_LON})]


async def test_car_status_never_wakes_the_car(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.vehicle.mock(return_value=state("asleep"))
    await say(rt, "car_status", ntfy)
    assert ntfy.last()["message"] == "🚗 asleep, locked"
    assert not fleet.wake.called and not fleet.data.called and fleet.commands() == []


async def test_car_status_when_awake(rt: Runtime, fleet: Fleet, ntfy: NtfyMock) -> None:
    fleet.data.mock(return_value=httpx.Response(200, json={"response": {
        "vehicle_state": {"locked": True, "sentry_mode": True},
        "drive_state": {"latitude": CAR_LAT, "longitude": CAR_LON},
        "climate_state": {"inside_temp": 16.0, "is_climate_on": True},
        "charge_state": {"battery_level": 78, "battery_range": 241.4, "charging_state": "Charging"},
    }}))
    match = (await rt.phrase_index()).match("how much battery does the car have")
    assert match is not None and match.skill_id == "car_status"
    await say(rt, "car_status", ntfy)
    assert ntfy.last()["message"] == "🚗 🔋 78% (241 mi) · locked · 61°F inside · climate on · charging · sentry on"


def test_describe_state_reports_an_unlocked_car_loudly() -> None:
    s = VehicleState(online=True, locked=False, battery=40, fetched_at=time.time())
    assert describe_state(s, "C") == "🚗 🔋 40% · UNLOCKED"


# ----------------------------------------------------------------------------- default skills


async def test_car_skills_are_added_once_and_never_overwrite_yours(
        make_runtime: Factory, make_config: Callable[..., Config]) -> None:
    rt: Runtime = await make_runtime(make_config())                  # seeded before the car existed
    have = {s.id for s in await rt.catalog.skills()}
    assert "car_lock" not in have
    await rt.catalog.put(Skill.model_validate({"id": "car_lock", "name": "My lock", "icon": "🔐",
                                               "template": "note", "params": {"text": "locked"}}))
    added = await ensure_skills(rt.catalog, car_skills())
    assert "car_lock" not in added and "car_status" in added and "car_garage" in added
    assert next(s for s in await rt.catalog.skills() if s.id == "car_lock").name == "My lock"
    assert await ensure_skills(rt.catalog, car_skills()) == []


@pytest.mark.parametrize(("said", "skill"), [
    ("lock the car", "car_lock"), ("unlock my car", "car_unlock"), ("open the frunk", "frunk"),
    ("wake the car", "car_wake"),
    ("pop the trunk", "car_trunk"), ("warm up the car", "car_climate"), ("defrost the car", "car_defrost"),
    ("open the garage", "car_garage"), ("find my car", "car_flash"), ("turn on sentry mode", "car_sentry_on"),
    ("vent the windows", "car_vent"), ("how's the car", "car_status"), ("start charging", "car_charge_start"),
    ("heat my seat", "car_seat_heat"), ("set the charge limit to 90", "car_charge_limit"),
])
async def test_voice_phrases_reach_the_car_skills(rt: Runtime, said: str, skill: str) -> None:
    match = (await rt.phrase_index()).match(said)
    assert match is not None and match.skill_id == skill


def test_every_car_skill_compiles() -> None:
    from ringhub.templates import compile_skill

    for doc in car_skills():
        steps = compile_skill(Skill.model_validate(doc))
        assert steps and all(isinstance(s, dict) for s in steps)


async def test_tesla_client_reports_unconnected(make_runtime: Factory, make_config: Callable[..., Config]) -> None:
    rt: Runtime = await make_runtime(tesla_cfg(make_config, refresh_token=None))
    assert isinstance(rt.tesla, TeslaClient) and not await rt.tesla.connected()
    with pytest.raises(ActionError, match="not connected"):
        await rt.tesla.online_state()


async def test_vehicle_url_in_fixture_is_the_mock() -> None:
    assert TESLA_VEHICLE.startswith("https://tesla.test/")
