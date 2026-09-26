"""Tests for DT-12: discovery, pairing and revoking devices."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import discovery
from daytrace_hub.api import ApiError
from daytrace_hub.api.devices import MAX_WRONG_TRIES, PairClaim, PairingCodes, claim, qr_payload
from daytrace_hub.app import create_app
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, get_profile, load_settings, parse_lan_networks
from daytrace_hub.db import Database

PHONE = ("192.168.1.50", 40000)
SESSION = {"device_id": "android-1", "seq": 1, "kind": "app_session", "source": "usagestats",
           "start": "2026-09-25T14:03:10-04:00", "end": "2026-09-25T14:21:44-04:00", "app": "Instagram"}


@pytest.fixture(autouse=True)
def _fixed_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on this computer's real network adapters."""
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: "192.168.1.23")
    monkeypatch.setattr(discovery, "interface_ipv4s", lambda: ["127.0.0.1", "192.168.1.23", "100.101.102.103"])


@pytest.fixture
def phone(settings: Settings, client: TestClient) -> Iterator[TestClient]:
    """A phone on the same Wi-Fi, talking to the same running hub as `client`."""
    with TestClient(client.app, client=PHONE) as phone_client:
        yield phone_client


def start(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/pair/start")
    assert response.status_code == 200, response.text
    return response.json()


def claim_body(code: str, device_type: str = "android", name: str = "Galaxy phone") -> dict[str, str]:
    return {"code": code, "device_name": name, "device_type": device_type}


# --- starting a pairing ---------------------------------------------------------------------------------------


def test_pair_start_gives_a_code_and_the_hub_address(client: TestClient) -> None:
    body = start(client)
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert body["url"] == "http://192.168.1.23:8765"  # the LAN IP, which every phone can reach
    assert body["urls"] == ["http://192.168.1.23:8765"]  # personal never offers Tailscale
    assert body["mdns_url"] == "http://daytrace-hub.local:8765"
    assert body["qr"] == f"/api/v1/pair/qr.png?code={body['code']}"


def test_shared_dev_also_offers_its_tailscale_address(tmp_path: Path) -> None:
    shared = Settings(profile=get_profile("shared-dev"), data_dir=tmp_path)
    with TestClient(create_app(shared), client=("127.0.0.1", 1)) as local:
        body = start(local)
    assert body["urls"] == ["http://192.168.1.23:8766", "http://100.101.102.103:8766"]
    assert body["url"] == "http://192.168.1.23:8766"


def test_pairing_codes_can_only_be_started_on_the_hub_computer(phone: TestClient) -> None:
    response = phone.post("/api/v1/pair/start")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"


def test_the_qr_code_is_a_png_with_the_url_and_code(client: TestClient) -> None:
    body = start(client)
    response = client.get(body["qr"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_qr_payload_is_small_json() -> None:
    codes = PairingCodes()
    active = codes.start("http://192.168.1.23:8765")
    assert json.loads(qr_payload(active)) == {"daytrace": 1, "url": "http://192.168.1.23:8765", "code": active.code}


def test_the_qr_code_needs_the_active_code_and_the_hub_computer(client: TestClient, phone: TestClient) -> None:
    body = start(client)
    wrong = "000000" if body["code"] != "000000" else "111111"
    assert client.get(f"/api/v1/pair/qr.png?code={wrong}").status_code == 404
    assert phone.get(body["qr"]).status_code == 403


# --- claiming a code ------------------------------------------------------------------------------------------


def test_a_phone_pairs_and_its_token_works(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    response = phone.post("/api/v1/pair/claim", json=claim_body(code))
    assert response.status_code == 201
    body = response.json()
    assert body["device_id"] == "android-1"
    assert body["device_type"] == "android"
    assert body["name"] == "Galaxy phone"
    assert body["profile"] == "personal"
    assert body["token"].startswith("dt_")
    sent = phone.post("/api/v1/events", json=SESSION, headers={"Authorization": f"Bearer {body['token']}"})
    assert sent.status_code == 200
    assert sent.json()["accepted"] == 1


def test_a_code_works_only_once(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).status_code == 201
    again = phone.post("/api/v1/pair/claim", json=claim_body(code, name="Someone else"))
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "invalid_code"


def test_device_ids_count_up_per_type(client: TestClient, phone: TestClient) -> None:
    ids = []
    for device_type in ("android", "android", "ios", "viewer"):
        code = start(client)["code"]
        ids.append(phone.post("/api/v1/pair/claim", json=claim_body(code, device_type)).json()["device_id"])
    assert ids == ["android-1", "android-2", "ios-1", "viewer-1"]


def test_revoked_devices_keep_their_number(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    first = phone.post("/api/v1/pair/claim", json=claim_body(code)).json()["device_id"]
    assert client.delete(f"/api/v1/devices/{first}").status_code == 204
    code = start(client)["code"]
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).json()["device_id"] == "android-2"


@pytest.mark.parametrize("typed", ["{code}", " {code} ", "{a} {b}", "{a}-{b}"])
def test_codes_may_be_typed_with_spaces_or_a_dash(client: TestClient, phone: TestClient, typed: str) -> None:
    code = start(client)["code"]
    text = typed.format(code=code, a=code[:3], b=code[3:])
    assert phone.post("/api/v1/pair/claim", json=claim_body(text)).status_code == 201


def test_an_expired_code_is_refused(client: TestClient, phone: TestClient) -> None:
    pairing: PairingCodes = client.app.state.pairing
    now = [1000.0]
    pairing.clock = lambda: now[0]
    code = start(client)["code"]
    now[0] += 5 * 60 + 1
    response = phone.post("/api/v1/pair/claim", json=claim_body(code))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"
    assert client.get(f"/api/v1/pair/qr.png?code={code}").status_code == 404


def test_five_wrong_codes_lock_the_code(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    statuses = [phone.post("/api/v1/pair/claim", json=claim_body(wrong)).status_code for _ in range(MAX_WRONG_TRIES)]
    assert statuses == [400] * (MAX_WRONG_TRIES - 1) + [429]
    locked = phone.post("/api/v1/pair/claim", json=claim_body(code))  # even the right code
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "too_many_attempts"
    new_code = start(client)["code"]  # a new code starts fresh
    assert phone.post("/api/v1/pair/claim", json=claim_body(new_code)).status_code == 201


def test_wrong_codes_say_how_many_tries_are_left(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    response = phone.post("/api/v1/pair/claim", json=claim_body(wrong))
    assert f"{MAX_WRONG_TRIES - 1} tries left" in response.json()["error"]["message"]
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).status_code == 201  # 4 wrong still allows it


def test_starting_again_replaces_the_old_code(client: TestClient, phone: TestClient) -> None:
    old = start(client)["code"]
    new = start(client)["code"]
    if old != new:
        assert phone.post("/api/v1/pair/claim", json=claim_body(old)).status_code == 400
    assert phone.post("/api/v1/pair/claim", json=claim_body(new)).status_code == 201


def test_claiming_with_no_active_code_is_refused(phone: TestClient) -> None:
    response = phone.post("/api/v1/pair/claim", json=claim_body("123456"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "12345", "device_name": "x", "device_type": "android"},
        {"code": "1234567", "device_name": "x", "device_type": "android"},
        {"code": "12a456", "device_name": "x", "device_type": "android"},
        {"code": "123456", "device_name": "x", "device_type": "toaster"},
        {"code": "123456", "device_name": "   ", "device_type": "android"},
        {"code": "123456", "device_name": "bad\nname", "device_type": "android"},
        {"code": "123456", "device_name": "x" * 65, "device_type": "android"},
        {"code": "123456", "device_name": "x", "device_type": "android", "extra": 1},
        {"code": "123456", "device_type": "android"},
    ],
)
def test_bad_claims_get_422(phone: TestClient, body: dict[str, Any]) -> None:
    response = phone.post("/api/v1/pair/claim", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_device_names_are_trimmed(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    assert phone.post("/api/v1/pair/claim", json=claim_body(code, name="  My phone  ")).json()["name"] == "My phone"


def test_two_phones_racing_for_one_code_cannot_both_win(db: Database) -> None:
    pairing = PairingCodes()
    code = pairing.start("http://192.168.1.23:8765").code
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def race(n: int) -> None:
        barrier.wait()
        try:
            claim(pairing, db, PairClaim(code=code, device_name=f"phone {n}", device_type="android"), "personal")
            outcomes.append("won")
        except ApiError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=race, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("won") == 1
    assert outcomes.count("invalid_code") == 7


# --- listing and revoking devices -----------------------------------------------------------------------------


def paired(db: Database) -> dict[str, str]:
    with db.connect() as conn:
        return {
            "android-1": register_device(conn, device_id="android-1", name="Galaxy phone", device_type="android"),
            "viewer-1": register_device(conn, device_id="viewer-1", name="Phone browser", device_type="viewer"),
        }


def test_the_hub_computer_lists_devices_without_a_token(client: TestClient, db: Database) -> None:
    from datetime import UTC, datetime, timedelta

    tokens = paired(db)
    recent = datetime.now(UTC) - timedelta(hours=1)
    old = {**SESSION, "start": "2020-01-01T10:00:00Z", "end": "2020-01-01T10:05:00Z"}
    new = {**SESSION, "seq": 7, "start": recent.isoformat(), "end": (recent + timedelta(minutes=5)).isoformat()}
    client.post("/api/v1/events", json={"events": [old, new]}, headers={"Authorization": f"Bearer {tokens['android-1']}"})
    devices = {d["device_id"]: d for d in client.get("/api/v1/devices").json()["devices"]}
    assert set(devices) == {"android-1", "viewer-1"}
    android = devices["android-1"]
    assert android["name"] == "Galaxy phone"
    assert android["device_type"] == "android"
    assert android["last_seq"] == 7
    assert android["event_count"] == 2
    assert android["events_24h"] == 1  # only the event from an hour ago
    assert android["last_seen"] is not None
    assert android["revoked_at"] is None
    assert devices["viewer-1"]["last_seq"] is None


def test_other_computers_need_a_token_to_list_devices(phone: TestClient, db: Database) -> None:
    tokens = paired(db)
    assert phone.get("/api/v1/devices").status_code == 401
    viewer = phone.get("/api/v1/devices", headers={"Authorization": f"Bearer {tokens['viewer-1']}"})
    assert viewer.status_code == 200


def test_a_bad_token_is_refused_even_on_the_hub_computer(client: TestClient) -> None:
    assert client.get("/api/v1/devices", headers={"Authorization": "Bearer dt_nope"}).status_code == 401


def test_revoking_a_device_ends_its_token_and_keeps_its_data(client: TestClient, db: Database) -> None:
    tokens = paired(db)
    headers = {"Authorization": f"Bearer {tokens['android-1']}"}
    assert client.post("/api/v1/events", json=SESSION, headers=headers).status_code == 200
    assert client.delete("/api/v1/devices/android-1").status_code == 204
    assert client.post("/api/v1/events", json={**SESSION, "seq": 2}, headers=headers).status_code == 401
    android = next(d for d in client.get("/api/v1/devices").json()["devices"] if d["device_id"] == "android-1")
    assert android["revoked_at"] is not None
    assert android["event_count"] == 1
    assert client.delete("/api/v1/devices/android-1").status_code == 204  # revoking again is harmless


def test_revoking_is_for_the_hub_computer_only(phone: TestClient, db: Database) -> None:
    tokens = paired(db)
    response = phone.delete("/api/v1/devices/android-1", headers={"Authorization": f"Bearer {tokens['viewer-1']}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"


def test_revoking_an_unknown_device_is_404(client: TestClient) -> None:
    assert client.delete("/api/v1/devices/nobody").status_code == 404


# --- discovery ------------------------------------------------------------------------------------------------


def test_phone_addresses_put_the_default_route_first_and_skip_unusable_ones(tmp_path: Path) -> None:
    personal = Settings(profile=get_profile("personal"), data_dir=tmp_path)
    found = discovery.phone_addresses(
        personal,
        "10.0.0.8",
        ["127.0.0.1", "169.254.3.4", "192.168.56.1", "10.0.0.8", "8.8.8.8", "100.101.102.103", "not-an-ip"],
    )
    assert found == ["10.0.0.8", "192.168.56.1"]


def test_phone_addresses_follow_the_lan_allow_list(tmp_path: Path) -> None:
    narrowed = Settings(profile=get_profile("demo"), data_dir=tmp_path, lan_networks=parse_lan_networks("10.0.0.0/8"))
    assert discovery.phone_addresses(narrowed, "192.168.1.23", ["10.0.0.8"]) == ["10.0.0.8"]


def test_the_service_record(tmp_path: Path) -> None:
    info = discovery.service_info(Settings(profile=get_profile("demo"), data_dir=tmp_path), ["192.168.1.23"])
    assert info.type == "_daytrace._tcp.local."
    assert info.name == "Daytrace hub (demo)._daytrace._tcp.local."
    assert info.port == 8767
    assert info.server == "daytrace-hub.local."
    assert info.parsed_addresses() == ["192.168.1.23"]
    assert info.properties == {b"profile": b"demo", b"version": b"0.1.0", b"api": b"/api/v1"}


class FakeZeroconf:
    instances: ClassVar[list[FakeZeroconf]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.registered: list[Any] = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info: Any, allow_name_change: bool = False) -> None:
        self.registered.append((info, allow_name_change))

    async def async_unregister_all_services(self) -> None:
        self.registered.clear()

    async def async_close(self) -> None:
        self.closed = True


class BrokenZeroconf(FakeZeroconf):
    async def async_register_service(self, info: Any, allow_name_change: bool = False) -> None:
        raise OSError("no multicast on this network")


@pytest.mark.anyio
async def test_the_advertiser_announces_lan_addresses_only(tmp_path: Path) -> None:
    shared = Settings(profile=get_profile("shared-dev"), data_dir=tmp_path)
    FakeZeroconf.instances.clear()
    advertiser = discovery.Advertiser(shared, zeroconf_factory=FakeZeroconf)
    assert await advertiser.start(["192.168.1.23", "100.101.102.103"]) is True
    zeroconf = FakeZeroconf.instances[0]
    info, allow_name_change = zeroconf.registered[0]
    assert info.parsed_addresses() == ["192.168.1.23"]  # multicast never crosses Tailscale
    assert allow_name_change is True  # a second hub on the Wi-Fi gets "(2)" instead of failing
    await advertiser.stop()
    assert zeroconf.closed and not zeroconf.registered


@pytest.mark.anyio
async def test_the_advertiser_never_stops_the_hub_from_starting(tmp_path: Path) -> None:
    settings = Settings(profile=get_profile("personal"), data_dir=tmp_path)
    broken = discovery.Advertiser(settings, zeroconf_factory=BrokenZeroconf)
    assert await broken.start(["192.168.1.23"]) is False
    assert await discovery.Advertiser(settings, zeroconf_factory=FakeZeroconf).start([]) is False


def test_the_running_hub_advertises_and_withdraws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from daytrace_hub import app as app_module

    FakeZeroconf.instances.clear()
    monkeypatch.setattr(
        app_module, "Advertiser", lambda settings: discovery.Advertiser(settings, zeroconf_factory=FakeZeroconf)
    )
    settings = Settings(profile=get_profile("personal"), data_dir=tmp_path, advertise_mdns=True)
    with TestClient(create_app(settings), client=("127.0.0.1", 1)):
        assert FakeZeroconf.instances[0].registered
    assert FakeZeroconf.instances[0].closed


def test_mdns_settings_from_the_environment(tmp_path: Path) -> None:
    base = {"DAYTRACE_DATA_DIR": str(tmp_path)}
    assert load_settings("personal", env=base).advertise_mdns is True
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS": "off"}).advertise_mdns is False
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS_NAME": "Sai-PC"}).mdns_name == "sai-pc"
    with pytest.raises(ValueError, match="DAYTRACE_MDNS_NAME"):
        load_settings("personal", env={**base, "DAYTRACE_MDNS_NAME": "bad name.local"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
