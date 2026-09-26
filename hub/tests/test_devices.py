"""Tests for DT-12: discovery, pairing and revoking devices. DT-22: the hub proving it paired a device."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import discovery
from daytrace_hub.api import ApiError
from daytrace_hub.api.devices import (
    WRONG_TRIES_PER_CLIENT,
    WRONG_TRIES_PER_CODE,
    PairClaim,
    PairingCodes,
    add_device,
    qr_payload,
)
from daytrace_hub.app import create_app
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, default_mdns_name, get_profile, load_settings, parse_lan_networks
from daytrace_hub.db import Database

LOCAL_URL = "http://localhost:8765"
PHONE = ("192.168.1.50", 40000)
SESSION = {"device_id": "android-1", "seq": 1, "kind": "app_session", "source": "usagestats",
           "start": "2026-09-25T14:03:10-04:00", "end": "2026-09-25T14:21:44-04:00", "app": "Instagram"}
ADAPTERS = [("Loopback", "127.0.0.1"), ("Wi-Fi", "192.168.1.23"), ("Tailscale", "100.101.102.103")]
RLO = chr(0x202E)  # right-to-left override: makes "a<RLO>gnp.exe" display as "aexe.png"
ZWJ = chr(0x200D)  # zero-width joiner, part of emoji such as a person at a laptop


@pytest.fixture(autouse=True)
def _fixed_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on this computer's real network adapters."""
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: "192.168.1.23")
    monkeypatch.setattr(discovery, "interface_ipv4s", lambda: list(ADAPTERS))


@pytest.fixture
def phone(client: TestClient) -> Iterator[TestClient]:
    """A phone on the same Wi-Fi, talking to the same running hub as `client`."""
    with TestClient(client.app, client=PHONE) as phone_client:
        yield phone_client


def other_phone(client: TestClient, address: str) -> TestClient:
    return TestClient(client.app, client=(address, 40000))


def start(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/pair/start")
    assert response.status_code == 200, response.text
    return response.json()


def claim_body(code: str, device_type: str = "android", name: str = "Galaxy phone") -> dict[str, str]:
    return {"code": code, "device_name": name, "device_type": device_type}


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


# --- starting a pairing ---------------------------------------------------------------------------------------


def test_pair_start_gives_a_code_and_the_hub_address(client: TestClient) -> None:
    response = client.post("/api/v1/pair/start")
    body = response.json()
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert body["url"] == "http://192.168.1.23:8765"  # the LAN IP, which every phone can reach
    assert body["urls"] == ["http://192.168.1.23:8765"]  # personal never offers Tailscale
    assert body["mdns_url"] is None  # mDNS is off in tests
    assert body["qr"] == "/api/v1/pair/qr.png"
    assert response.headers["cache-control"] == "no-store"
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)


def test_the_mdns_address_is_offered_when_mdns_is_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daytrace_hub import app as app_module

    # A fake zeroconf, so no test ever announces anything on the real network.
    monkeypatch.setattr(
        app_module, "Advertiser", lambda settings: discovery.Advertiser(settings, zeroconf_factory=FakeZeroconf)
    )
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path, advertise_mdns=True, mdns_name="daytrace-desk")
    with TestClient(create_app(settings), client=("127.0.0.1", 1), base_url="http://localhost:8767") as local:
        assert start(local)["mdns_url"] == "http://daytrace-desk.local:8767"


def test_shared_dev_also_offers_its_tailscale_address(tmp_path: Path) -> None:
    shared = Settings(profile=get_profile("shared-dev"), data_dir=tmp_path)
    with TestClient(create_app(shared), client=("127.0.0.1", 1), base_url="http://localhost:8766") as local:
        body = start(local)
    assert body["urls"] == ["http://192.168.1.23:8766", "http://100.101.102.103:8766"]
    assert body["url"] == "http://192.168.1.23:8766"


def test_without_a_lan_address_the_code_still_works_but_there_is_no_qr(
    client: TestClient, phone: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "primary_ipv4", lambda: None)
    monkeypatch.setattr(discovery, "interface_ipv4s", lambda: [("Loopback", "127.0.0.1")])
    body = start(client)
    assert (body["url"], body["urls"], body["qr"]) == (None, [], None)
    assert client.get("/api/v1/pair/qr.png").status_code == 404
    assert phone.post("/api/v1/pair/claim", json=claim_body(body["code"])).status_code == 201


def test_pairing_codes_can_only_be_started_on_the_hub_computer(phone: TestClient) -> None:
    response = phone.post("/api/v1/pair/start")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "http://evil.example"},  # a page on another site, fetch(..., {mode: "no-cors"})
        {"origin": "null"},  # a sandboxed frame or a file
        {"sec-fetch-site": "cross-site"},  # an <img> or form on another site (no Origin sent)
        {"host": "evil.local:8765"},  # DNS rebinding through a LAN-answered name
        {"host": "attacker:8765"},
        {"host": "evil.lan:8765"},
        {"host": "192.168.1.23:8765"},  # the LAN address is not "the hub computer talking to itself"
    ],
)
def test_other_sites_cannot_act_as_the_hub_computer(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/v1/pair/start", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://localhost:8765", "sec-fetch-site": "same-origin"},
        {"origin": "http://localhost:5173", "sec-fetch-site": "same-site"},  # the Vite dev server
        {"origin": "http://127.0.0.1:8765"},
        {"host": "127.0.0.1:8765"},
        {"host": "[::1]:8765"},
        {"sec-fetch-site": "none"},  # typed into the address bar
    ],
)
def test_the_local_dashboard_and_tools_are_trusted(client: TestClient, headers: dict[str, str]) -> None:
    assert client.post("/api/v1/pair/start", headers=headers).status_code == 200


def test_the_qr_code_is_a_png_with_the_url_and_code(client: TestClient) -> None:
    start(client)
    response = client.get("/api/v1/pair/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_qr_payload_is_small_json() -> None:
    active = PairingCodes().start("http://192.168.1.23:8765")
    assert json.loads(qr_payload(active)) == {"daytrace": 1, "url": "http://192.168.1.23:8765", "code": active.code}


def test_the_qr_code_cannot_be_used_to_guess_the_code(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    assert client.get(f"/api/v1/pair/qr.png?code={wrong(code)}").status_code == 200  # the parameter is ignored
    assert client.get("/api/v1/pair/qr.png", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert phone.get("/api/v1/pair/qr.png").status_code == 403
    phone.post("/api/v1/pair/claim", json=claim_body(code))
    assert client.get("/api/v1/pair/qr.png").status_code == 404  # used codes have no QR any more


# --- claiming a code ------------------------------------------------------------------------------------------


def test_a_phone_pairs_and_its_token_works(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    response = phone.post("/api/v1/pair/claim", json=claim_body(code))
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"  # the only copy of the token
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
    for device_type in ("android", "android", "ios", "macos", "viewer"):
        code = start(client)["code"]
        ids.append(phone.post("/api/v1/pair/claim", json=claim_body(code, device_type)).json()["device_id"])
    assert ids == ["android-1", "android-2", "iphone-1", "mac-1", "viewer-1"]  # iphone-1 as in the Shortcuts


def test_the_example_shortcut_payloads_work_for_the_first_iphone(client: TestClient, phone: TestClient) -> None:
    payloads = Path(__file__).resolve().parents[2] / "ios" / "shortcuts" / "payloads"
    token = phone.post("/api/v1/pair/claim", json=claim_body(start(client)["code"], "ios", "iPhone")).json()["token"]
    for name in ("app-event.json", "health-sync.json", "meal.json"):
        body = json.loads((payloads / name).read_text(encoding="utf-8"))
        result = phone.post("/api/v1/events", json=body, headers={"Authorization": f"Bearer {token}"}).json()
        assert result["rejected"] == [], name


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
    assert client.get("/api/v1/pair/qr.png").status_code == 404


def test_five_wrong_codes_lock_that_client_out(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    statuses = [
        phone.post("/api/v1/pair/claim", json=claim_body(wrong(code))).status_code
        for _ in range(WRONG_TRIES_PER_CLIENT)
    ]
    assert statuses == [400] * (WRONG_TRIES_PER_CLIENT - 1) + [429]
    locked = phone.post("/api/v1/pair/claim", json=claim_body(code))  # even the right code
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "too_many_attempts"
    new_code = start(client)["code"]  # a new code starts fresh
    assert phone.post("/api/v1/pair/claim", json=claim_body(new_code)).status_code == 201


def test_one_noisy_device_cannot_lock_out_the_real_phone(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    with other_phone(client, "192.168.1.66") as noisy:
        for _ in range(WRONG_TRIES_PER_CLIENT + 3):
            noisy.post("/api/v1/pair/claim", json=claim_body(wrong(code)))
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).status_code == 201


def test_a_code_locks_after_twenty_wrong_tries_from_anywhere(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    for n in range(WRONG_TRIES_PER_CODE // (WRONG_TRIES_PER_CLIENT - 1)):
        with other_phone(client, f"192.168.1.{100 + n}") as guesser:
            for _ in range(WRONG_TRIES_PER_CLIENT - 1):
                guesser.post("/api/v1/pair/claim", json=claim_body(wrong(code)))
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).status_code == 429


def test_wrong_codes_say_how_many_tries_are_left(client: TestClient, phone: TestClient) -> None:
    code = start(client)["code"]
    response = phone.post("/api/v1/pair/claim", json=claim_body(wrong(code)))
    assert f"{WRONG_TRIES_PER_CLIENT - 1} tries left" in response.json()["error"]["message"]
    assert phone.post("/api/v1/pair/claim", json=claim_body(code)).status_code == 201


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


def test_a_failed_device_write_leaves_the_code_usable(db: Database) -> None:
    pairing = PairingCodes()
    code = pairing.start("http://192.168.1.23:8765").code

    def failing() -> Any:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        pairing.claim(code, "192.168.1.50", failing)
    body = PairClaim(code=code, device_name="Galaxy phone", device_type="android")
    assert pairing.claim(code, "192.168.1.50", lambda: add_device(db, body, "personal")).device_id == "android-1"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "12345", "device_name": "x", "device_type": "android"},
        {"code": "1234567", "device_name": "x", "device_type": "android"},
        {"code": "12a456", "device_name": "x", "device_type": "android"},
        {"code": "123456", "device_name": "x", "device_type": "toaster"},
        {"code": "123456", "device_name": "   ", "device_type": "android"},
        {"code": "123456", "device_name": "bad\nname", "device_type": "android"},
        {"code": "123456", "device_name": "a" + RLO + "gnp.exe", "device_type": "android"},  # flips the text
        {"code": "123456", "device_name": "x\u0085y", "device_type": "android"},  # a C1 control
        {"code": "123456", "device_name": "x" * 65, "device_type": "android"},
        {"code": "123456", "device_name": "x", "device_type": "android", "extra": 1},
        {"code": "123456", "device_type": "android"},
    ],
)
def test_bad_claims_get_422(phone: TestClient, body: dict[str, Any]) -> None:
    response = phone.post("/api/v1/pair/claim", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("  My phone  ", "My phone"),
        (" " + "x" * 64 + " ", "x" * 64),  # 64 characters once trimmed
        ("\U0001f469" + ZWJ + "\U0001f4bb laptop", "\U0001f469" + ZWJ + "\U0001f4bb laptop"),  # emoji with a joiner
    ],
)
def test_device_names_are_trimmed_and_emoji_are_fine(
    client: TestClient, phone: TestClient, typed: str, stored: str
) -> None:
    code = start(client)["code"]
    response = phone.post("/api/v1/pair/claim", json=claim_body(code, name=typed))
    assert response.status_code == 201
    assert response.json()["name"] == stored


def test_two_phones_racing_for_one_code_cannot_both_win(db: Database) -> None:
    pairing = PairingCodes()
    code = pairing.start("http://192.168.1.23:8765").code
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def race(n: int) -> None:
        body = PairClaim(code=code, device_name=f"phone {n}", device_type="android")
        barrier.wait()
        try:
            pairing.claim(code, f"192.168.1.{n}", lambda: add_device(db, body, "personal"))
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


def test_a_rebinding_page_on_the_hub_computer_cannot_list_devices(client: TestClient, db: Database) -> None:
    paired(db)
    assert client.get("/api/v1/devices", headers={"host": "evil.lan:8765"}).status_code == 401


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


# --- proving the hub (DT-22) ----------------------------------------------------------------------------------

NONCE = "0123456789abcdef" * 2


def expected_proof(token: str, nonce: str) -> str:
    """What the phone computes on its side: HMAC-SHA256(key = sha256(token) as hex, message = nonce)."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.new(token_hash.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def test_the_hub_proves_it_paired_the_phone_without_the_token(client: TestClient, phone: TestClient) -> None:
    token = phone.post("/api/v1/pair/claim", json=claim_body(start(client)["code"])).json()["token"]
    response = phone.post("/api/v1/devices/android-1/proof", json={"nonce": NONCE})  # no Authorization header
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"device_id": "android-1", "proof": expected_proof(token, NONCE)}
    other = phone.post("/api/v1/devices/android-1/proof", json={"nonce": "f" * 64}).json()["proof"]
    assert other == expected_proof(token, "f" * 64) != response.json()["proof"]  # a new nonce, a new answer
    assert token not in response.text


def test_revoked_or_unknown_devices_get_no_proof(client: TestClient, phone: TestClient, db: Database) -> None:
    paired(db)
    assert client.delete("/api/v1/devices/android-1").status_code == 204
    for device_id in ("android-1", "android-99"):
        response = phone.post(f"/api/v1/devices/{device_id}/proof", json={"nonce": NONCE})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "body", [{"nonce": "abc"}, {"nonce": NONCE.upper()}, {"nonce": "g" * 32}, {}, {"nonce": NONCE, "x": 1}]
)
def test_the_nonce_must_be_fresh_looking_hex(phone: TestClient, db: Database, body: dict[str, Any]) -> None:
    paired(db)
    response = phone.post("/api/v1/devices/android-1/proof", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_revoking_an_unknown_device_is_404(client: TestClient) -> None:
    assert client.delete("/api/v1/devices/nobody").status_code == 404


# --- which addresses phones get -------------------------------------------------------------------------------


def test_phone_addresses_put_the_default_route_first_and_skip_unusable_ones(tmp_path: Path) -> None:
    personal = Settings(profile=get_profile("personal"), data_dir=tmp_path)
    adapters = [
        ("Loopback Pseudo-Interface 1", "127.0.0.1"),
        ("Ethernet", "169.254.3.4"),
        ("Ethernet 2", "192.168.0.10"),
        ("Wi-Fi", "10.0.0.8"),
        ("vEthernet (WSL)", "172.29.80.1"),
        ("VirtualBox Host-Only Network", "192.168.56.1"),
        ("docker0", "172.17.0.1"),
        ("Public", "8.8.8.8"),
        ("Tailscale", "100.101.102.103"),
    ]
    assert discovery.phone_addresses(personal, "10.0.0.8", adapters) == ["10.0.0.8", "192.168.0.10"]


def test_a_vpn_holding_the_default_route_is_not_put_in_the_qr_code(tmp_path: Path) -> None:
    personal = Settings(profile=get_profile("personal"), data_dir=tmp_path)
    adapters = [("Corporate VPN", "10.8.0.5"), ("Wi-Fi", "192.168.1.23")]
    assert discovery.phone_addresses(personal, "10.8.0.5", adapters) == ["192.168.1.23"]


def test_tailscale_is_offered_only_on_shared_dev(tmp_path: Path) -> None:
    shared = Settings(profile=get_profile("shared-dev"), data_dir=tmp_path)
    assert discovery.phone_addresses(shared, "192.168.1.23", ADAPTERS) == ["192.168.1.23", "100.101.102.103"]


def test_phone_addresses_follow_the_lan_allow_list(tmp_path: Path) -> None:
    narrowed = Settings(profile=get_profile("demo"), data_dir=tmp_path, lan_networks=parse_lan_networks("10.0.0.0/8"))
    assert discovery.phone_addresses(narrowed, "192.168.1.23", [("Wi-Fi", "192.168.1.23"), ("Eth", "10.0.0.8")]) == [
        "10.0.0.8"
    ]


# --- mDNS -----------------------------------------------------------------------------------------------------


def test_the_service_record(tmp_path: Path) -> None:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path, mdns_name="daytrace-desk")
    info = discovery.service_info(settings, ["192.168.1.23"])
    assert info.type == "_daytrace._tcp.local."
    assert info.name == "Daytrace hub (demo)._daytrace._tcp.local."
    assert info.port == 8767
    assert info.server == "daytrace-desk.local."
    assert info.parsed_addresses() == ["192.168.1.23"]
    assert info.properties == {b"profile": b"demo", b"version": b"0.1.0", b"api": b"/api/v1"}


class FakeZeroconf:
    instances: ClassVar[list[FakeZeroconf]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.registered: list[Any] = []
        self.updated: list[Any] = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info: Any, allow_name_change: bool = False) -> None:
        self.registered.append((info, allow_name_change))

    async def async_update_service(self, info: Any) -> None:
        self.updated.append(info)

    async def async_unregister_all_services(self) -> None:
        self.registered.clear()

    async def async_close(self) -> None:
        self.closed = True


class BrokenZeroconf(FakeZeroconf):
    async def async_register_service(self, info: Any, allow_name_change: bool = False) -> None:
        raise OSError("no multicast on this network")

    async def async_close(self) -> None:
        raise OSError("close failed too")


def advertiser(tmp_path: Path, addresses: list[list[str]], factory: Any = FakeZeroconf, profile: str = "shared-dev") -> Any:
    settings = Settings(profile=get_profile(profile), data_dir=tmp_path)
    FakeZeroconf.instances.clear()
    return discovery.Advertiser(settings, zeroconf_factory=factory, find_addresses=lambda: addresses[0])


def test_the_advertiser_announces_lan_addresses_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        found = [["192.168.1.23", "100.101.102.103"]]
        adv = advertiser(tmp_path, found)
        assert await adv.refresh() is True
        zeroconf = FakeZeroconf.instances[0]
        info, allow_name_change = zeroconf.registered[0]
        assert info.parsed_addresses() == ["192.168.1.23"]  # multicast never crosses Tailscale
        assert allow_name_change is True  # a second hub on the Wi-Fi gets "(2)" instead of failing
        await adv.stop()
        assert zeroconf.closed and not zeroconf.registered

    asyncio.run(scenario())


def test_the_advertiser_follows_address_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        found = [[]]
        adv = advertiser(tmp_path, found, profile="personal")
        assert await adv.refresh() is False  # the hub started before Wi-Fi connected
        found[0] = ["192.168.1.23"]
        assert await adv.refresh() is True
        found[0] = ["192.168.1.40"]  # a new DHCP address
        assert await adv.refresh() is True
        zeroconf = FakeZeroconf.instances[0]
        assert [i.parsed_addresses() for i in zeroconf.updated] == [["192.168.1.40"]]
        found[0] = []  # Wi-Fi gone
        assert await adv.refresh() is False
        assert zeroconf.closed

    asyncio.run(scenario())


def test_the_advertiser_never_breaks_startup_or_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        adv = advertiser(tmp_path, [["192.168.1.23"]], factory=BrokenZeroconf, profile="personal")
        with pytest.raises(OSError, match="no multicast"):
            await adv.refresh()  # the background loop logs this and tries again later
        adv.refresh_seconds = 0.01
        adv.start()
        await asyncio.sleep(0.05)
        await adv.stop()  # even though closing fails as well

    asyncio.run(scenario())


def test_the_running_hub_advertises_in_the_background_and_withdraws(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from daytrace_hub import app as app_module

    FakeZeroconf.instances.clear()
    monkeypatch.setattr(
        app_module, "Advertiser", lambda settings: discovery.Advertiser(settings, zeroconf_factory=FakeZeroconf)
    )
    settings = Settings(profile=get_profile("personal"), data_dir=tmp_path, advertise_mdns=True)
    with TestClient(create_app(settings), client=("127.0.0.1", 1), base_url=LOCAL_URL) as local:
        assert local.get("/api/v1/health").status_code == 200  # serving before mDNS finishes
        deadline = time.monotonic() + 5
        while not (FakeZeroconf.instances and FakeZeroconf.instances[0].registered):
            assert time.monotonic() < deadline, "the advertiser never registered"
            time.sleep(0.01)
    assert FakeZeroconf.instances[0].closed


# --- settings -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("computer", "name"),
    [
        ("DESKTOP-AB12CD", "daytrace-desktop-ab12cd"),
        ("Sais-MacBook-Pro.local", "daytrace-sais-macbook-pro"),
        ("my_pc", "daytrace-my-pc"),
        ("", "daytrace-hub"),
        ("---", "daytrace-hub"),
        ("x" * 80, "daytrace-" + "x" * 54),
    ],
)
def test_the_default_mdns_name_comes_from_the_pc_name(computer: str, name: str) -> None:
    assert default_mdns_name(computer) == name


def test_mdns_settings_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "DESKTOP-AB12CD")
    base = {"DAYTRACE_DATA_DIR": str(tmp_path)}
    assert load_settings("personal", env=base).advertise_mdns is True
    assert load_settings("personal", env=base).mdns_name == "daytrace-desktop-ab12cd"
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS": "off"}).advertise_mdns is False
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS": " "}).advertise_mdns is True
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS_NAME": "Sai-PC"}).mdns_name == "sai-pc"
    assert load_settings("personal", env={**base, "DAYTRACE_MDNS_NAME": ""}).mdns_name == "daytrace-desktop-ab12cd"
    with pytest.raises(ValueError, match="DAYTRACE_MDNS_NAME"):
        load_settings("personal", env={**base, "DAYTRACE_MDNS_NAME": "bad name.local"})
    with pytest.raises(ValueError, match="DAYTRACE_MDNS must be on or off"):
        load_settings("personal", env={**base, "DAYTRACE_MDNS": "disabled"})
