"""Tests for DT-45: the network lockdown and the "no internet" proof. DT-46 (export, delete) adds its own.

The claims: the hub listens only on this computer and the addresses phones use (the tailnet only for shared-dev,
never a public address) and follows them as they change; every outgoing request goes through one transport that
lets only local destinations through and counts the rest as blocked; every incoming request is counted, served or
refused, including other web sites' pages; and GET /privacy/network shows it all, with 0 internet connections.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub.app import HubServer, create_app
from daytrace_hub.config import LEDGER, Settings, get_profile, network_of, origin_allowed
from daytrace_hub.discovery import listen_addresses, phone_addresses
from daytrace_hub.llm import LLM, LLMRefused, LLMSettings, LocalOnlyTransport

LOCAL = ("127.0.0.1", 50000)
LAN = ("192.168.1.40", 50000)
PUBLIC = ("8.8.8.8", 50000)


@pytest.fixture(autouse=True)
def fresh_ledger() -> Iterator[None]:
    LEDGER.reset()
    yield
    LEDGER.reset()


@pytest.fixture
def demo(tmp_path: Path, fake_llm: FakeModelServer) -> Iterator[TestClient]:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=LOCAL, base_url="http://localhost:8767") as client:
        yield client


def status(client: TestClient) -> dict:
    return client.get("/api/v1/privacy/network").json()


# --- the rules ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("address", "where"), [
    ("127.0.0.1", "localhost"), ("::1", "localhost"), ("192.168.1.20", "lan"), ("10.0.0.5", "lan"), ("fe80::1", "lan"),
    ("100.101.102.103", "tailscale"), ("fd7a:115c:a1e0::1", "tailscale"), ("8.8.8.8", "internet"),
    ("2001:4860:4860::8888", "internet"), ("not-an-address", "internet"), (None, "internet"),
])
def test_network_of(address: str | None, where: str) -> None:
    assert network_of(address) == where


@pytest.mark.parametrize(("origin", "host", "loopback", "allowed"), [
    (None, "localhost:8765", False, True),  # a phone app, a Shortcut, a plain page load
    ("http://localhost:8765", "localhost:8765", True, True),  # the dashboard on the hub computer
    ("http://192.168.2.10:8767", "192.168.2.10:8767", False, True),  # the dashboard on a phone
    ("http://daytrace-pc.local:8767", "daytrace-pc.local:8767", False, True),
    ("chrome-extension://abcdefghijklmnop", "192.168.2.10:8765", False, True),  # the browser extension
    ("http://localhost:5173", "localhost:8765", True, True),  # the dev server's proxy, on this computer
    ("http://localhost:5173", "192.168.2.10:8765", False, False),  # ... not for another computer
    ("https://evil.example", "localhost:8765", True, False),
    ("http://192.168.2.10:8767", "192.168.2.10:8765", False, False),  # another hub profile's pages
    ("null", "localhost:8765", True, False),  # a local file or a sandboxed frame
    ("javascript:alert(1)", "localhost:8765", True, False),
])
def test_which_pages_may_call_the_hub(origin: str | None, host: str, loopback: bool, allowed: bool) -> None:
    assert origin_allowed(origin, host, loopback) is allowed


def test_where_each_profile_listens() -> None:
    adapters = [("Wi-Fi", "192.168.1.20"), ("Tailscale", "100.101.102.103"), ("Ethernet 2", "203.0.113.9"),
                ("vEthernet (WSL)", "172.28.0.1"), ("Loopback", "127.0.0.1")]
    for name, expected in [("personal", ["192.168.1.20"]), ("demo", ["192.168.1.20"]), ("shared-dev", ["192.168.1.20", "100.101.102.103"])]:
        settings = Settings(profile=get_profile(name), data_dir=Path("."))
        phones = phone_addresses(settings, "192.168.1.20", adapters)
        assert listen_addresses(settings, phones) == ["127.0.0.1", "::1", *expected], name  # never public, a VM, or 0.0.0.0


# --- outgoing -----------------------------------------------------------------------------------------------------


def test_the_demo_makes_no_internet_connections(demo: TestClient, fake_llm: FakeModelServer) -> None:
    assert demo.get("/api/v1/ai/status").status_code == 200  # the local model, through the only way out
    body = status(demo)
    assert body["internet_connections"] == 0
    assert body["outgoing"]["localhost"] >= 1 and body["outgoing"]["internet"] == 0
    assert body["blocked"] == {"count": 0, "destinations": []}
    assert body["incoming"]["localhost"] >= 2  # these requests themselves


def test_an_internet_destination_is_blocked_and_counted() -> None:
    llm = LLM(LLMSettings(base_url="http://8.8.8.8:1234/v1"))
    assert llm.status().reachable is False
    named = httpx.Client(transport=LocalOnlyTransport(resolve=lambda host, port: ["93.184.216.34"]))
    with pytest.raises(LLMRefused):
        named.get("http://models.example.com:1234/v1/models")  # a name that points at the internet
    found = LEDGER.snapshot()
    destinations = {(entry["host"], entry["port"]): entry["count"] for entry in found["blocked"]["destinations"]}
    assert destinations[("8.8.8.8", 1234)] >= 1 and destinations[("models.example.com", 1234)] == 1
    assert found["blocked"]["count"] == sum(destinations.values())
    assert found["outgoing"].get("internet", 0) == 0  # refused, never made


# --- incoming -----------------------------------------------------------------------------------------------------


def test_incoming_requests_are_counted_served_or_refused(demo: TestClient) -> None:
    with TestClient(demo.app, client=LAN) as phone, TestClient(demo.app, client=PUBLIC) as stranger:
        assert phone.get("/api/v1/health").status_code == 200
        assert stranger.get("/api/v1/health").status_code == 403
    refused = demo.post("/api/v1/pair/start", headers={"origin": "https://evil.example"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden_origin"
    body = status(demo)
    assert body["incoming"]["lan"] == 1 and body["incoming"]["internet"] == 0
    assert body["refused"]["internet"] == 1 and body["refused"]["localhost"] == 1  # the stranger, the other site
    assert body["internet_connections"] == 0


def test_the_network_status_needs_a_paired_device(demo: TestClient) -> None:
    with TestClient(demo.app, client=LAN) as phone:
        assert phone.get("/api/v1/privacy/network").status_code == 401


# --- listening ----------------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _reachable(address: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{address}:{port}/api/v1/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _bindable(address: str) -> bool:
    try:
        with socket.socket() as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


@pytest.mark.real_network
def test_the_hub_listens_only_where_it_is_told_and_follows_changes(tmp_path: Path, fake_llm: FakeModelServer) -> None:
    second = "127.0.0.2"
    if not _bindable(second):
        pytest.skip("this computer has only 127.0.0.1 on loopback (macOS)")
    port = _free_port()
    wanted = ["127.0.0.1"]
    app = create_app(Settings(profile=get_profile("demo"), data_dir=tmp_path), llm=fake_llm.llm())
    config = uvicorn.Config(app, port=port, log_level="warning", timeout_graceful_shutdown=2)
    server = HubServer(config, lambda: list(wanted), check_every=0.3)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert list(server.listening) == ["127.0.0.1"] and _reachable("127.0.0.1", port)
        assert not _reachable(second, port)  # not on every address: only where it was told
        wanted.append(second)  # a new address (a new Wi-Fi, a DHCP renewal)
        for _ in range(60):
            if _reachable(second, port):
                break
            time.sleep(0.1)
        assert _reachable(second, port) and set(server.listening) == {"127.0.0.1", second}
        assert LEDGER.snapshot()["listening"] == [f"127.0.0.1:{port}", f"{second}:{port}"]
        wanted.remove(second)  # it went away
        for _ in range(60):
            if second not in server.listening:
                break
            time.sleep(0.1)
        time.sleep(0.2)
        assert not _reachable(second, port) and _reachable("127.0.0.1", port)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert not thread.is_alive()
