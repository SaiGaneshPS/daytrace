"""Tests for DT-45: the network lockdown and the "no internet" proof. DT-46 (export, delete) adds its own.

The claims: the hub listens only on this computer and the addresses phones use (the tailnet only for shared-dev,
never a public address or carrier-grade NAT) and follows them as they change, even when a listener dies; the
socket guard refuses any internet connection from anything in the hub process; the model transport counts the
connections it opens and the attempts it refuses; every incoming request is counted, served or refused, including
other web sites' pages and malformed ones; and GET /privacy/network shows it all, with 0 internet connections.
"""
from __future__ import annotations

import http.server
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub import app as app_module
from daytrace_hub.app import HubServer, create_app, network_audit, serve
from daytrace_hub.config import LEDGER, Settings, get_profile, network_of, origin_allowed
from daytrace_hub.discovery import listen_addresses, phone_addresses, served
from daytrace_hub.llm import LLM, LLMRefused, LLMSettings, LocalOnlyTransport, local_http_client

LOCAL = ("127.0.0.1", 50000)
LAN = ("192.168.1.40", 50000)
PUBLIC = ("8.8.8.8", 50000)
HUB_DIR = Path(__file__).resolve().parents[1]


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


def status(client: TestClient) -> dict[str, Any]:
    return client.get("/api/v1/privacy/network").json()


# --- the rules ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("address", "where"), [
    ("127.0.0.1", "localhost"), ("::1", "localhost"), ("192.168.1.20", "lan"), ("10.0.0.5", "lan"), ("fe80::1", "lan"),
    ("100.101.102.103", "tailscale"), ("fd7a:115c:a1e0::1", "tailscale"), ("8.8.8.8", "internet"),
    ("2001:4860:4860::8888", "internet"), ("not-an-address", "internet"), (None, "internet"),
])
def test_network_of(address: str | None, where: str) -> None:
    assert network_of(address) == where


@pytest.mark.parametrize(("origin", "host", "allowed"), [
    (None, "localhost:8765", True),  # a phone app, a Shortcut, a plain page load
    ("http://localhost:8765", "localhost:8765", True),  # the dashboard on the hub computer (and the dev proxy)
    ("http://192.168.2.10:8767", "192.168.2.10:8767", True),  # the dashboard on a phone
    ("http://daytrace-pc.local:8767", "daytrace-pc.local:8767", True),
    ("chrome-extension://abcdefghijklmnop", "192.168.2.10:8765", True),  # the browser extension
    ("http://localhost:5173", "localhost:8765", False),  # another page on this computer (the proxy sends the hub's)
    ("http://localhost:8767", "localhost:8765", False),  # another profile's dashboard
    ("http://127.0.0.1:8765", "localhost:8765", True),  # the same hub, another loopback name
    ("http://[::1]:8765", "127.0.0.1:8765", True),
    ("http://127.0.0.1:8765", "192.168.2.10:8765", False),  # a loopback page calling the hub by its LAN name
    ("https://evil.example", "localhost:8765", False),
    ("null", "localhost:8765", False),  # a local file or a sandboxed frame
    ("javascript:alert(1)", "localhost:8765", False),
    ("http://[", "localhost:8765", False),  # doesn't parse
    ("http://localhost:port", "localhost:8765", False),
])
def test_which_pages_may_call_the_hub(origin: str | None, host: str, allowed: bool) -> None:
    assert origin_allowed(origin, host) is allowed


ADAPTERS = [("Wi-Fi", "192.168.1.20"), ("Tailscale", "100.101.102.103"), ("Ethernet 2", "203.0.113.9"),
            ("vEthernet (WSL)", "172.28.0.1"), ("Loopback", "127.0.0.1"), ("Cellular", "100.72.1.5")]


def test_where_each_profile_listens() -> None:
    for name, expected in [("personal", ["192.168.1.20"]), ("demo", ["192.168.1.20"]), ("shared-dev", ["192.168.1.20", "100.101.102.103"])]:
        settings = Settings(profile=get_profile(name), data_dir=Path("."))
        phones = phone_addresses(settings, "192.168.1.20", ADAPTERS)
        # Never public, a VM's, carrier-grade NAT on an ordinary adapter (Cellular), or 0.0.0.0.
        assert listen_addresses(settings, phones) == ["127.0.0.1", "::1", *expected], name


def test_the_tailnet_counts_only_on_its_own_adapter() -> None:
    settings = Settings(profile=get_profile("shared-dev"), data_dir=Path("."))
    found = phone_addresses(settings, None, [("Wi-Fi", "100.72.1.5"), ("tailscale0", "100.101.102.103"), ("utun3", "100.101.102.104")])
    assert found == ["100.101.102.103", "100.101.102.104"]  # the Wi-Fi's 100.72.x is an ISP's shared range


def test_mdns_tells_phones_only_addresses_being_served() -> None:
    assert served(["192.168.1.20", "192.168.1.21"]) == ["192.168.1.20", "192.168.1.21"]  # not listening yet: as found
    LEDGER.listen(["127.0.0.1:8767", "[::1]:8767", "192.168.1.20:8767"])
    assert served(["192.168.1.20", "192.168.1.21"]) == ["192.168.1.20"]  # .21 appeared but isn't bound yet


# --- the socket guard ---------------------------------------------------------------------------------------------


class FakeSocket:
    family = socket.AF_INET


@pytest.mark.parametrize(("event", "address", "refused"), [
    ("socket.connect", ("8.8.8.8", 53), True),
    ("socket.connect", ("2001:4860:4860::8888", 443, 0, 0), True),
    ("socket.connect", ("example.com", 80), True),  # a name: the hub's own code connects to checked addresses
    ("socket.sendto", ("1.1.1.1", 53), True),
    ("socket.connect", ("127.0.0.1", 1234), False),
    ("socket.connect", ("192.168.1.20", 1234), False),
    ("socket.connect", ("100.101.102.103", 8766), False),
    ("socket.connect", ("192.0.2.1", 9), False),  # primary_ipv4()'s route question: nothing is sent
    ("socket.sendto", ("224.0.0.251", 5353), False),  # mDNS
    ("socket.sendto", ("ff02::fb", 5353, 0, 0), False),
    ("socket.connect", "/tmp/a-unix-socket", False),
    ("socket.bind", ("8.8.8.8", 53), False),  # not a way out
])
def test_the_socket_guard(event: str, address: Any, refused: bool) -> None:
    if refused:
        with pytest.raises(PermissionError, match="never connects to the internet"):
            network_audit(event, (FakeSocket(), address))
        assert LEDGER.snapshot()["blocked"]["count"] == 1
    else:
        network_audit(event, (FakeSocket(), address))
        assert LEDGER.snapshot()["blocked"]["count"] == 0


def test_the_socket_guard_stops_any_code_in_the_hub_process() -> None:
    """In a process of its own (an audit hook can't be taken off): nothing in it can reach the internet."""
    code = (
        "import socket, urllib.request\n"
        "from daytrace_hub.app import install_network_audit\n"
        "install_network_audit()\n"
        "for attempt in (lambda: socket.create_connection(('8.8.8.8', 53), timeout=1),\n"
        "                lambda: urllib.request.urlopen('http://1.1.1.1/', timeout=1)):\n"
        "    try:\n"
        "        attempt()\n"
        "        print('CONNECTED')\n"
        "    except PermissionError:\n"
        "        print('REFUSED')\n"
        "    except Exception as error:\n"
        "        print('OTHER', type(error).__name__, getattr(error, 'reason', ''))\n"
        "s = socket.socket()\n"
        "s.settimeout(1)\n"
        "try:\n"
        "    s.connect(('127.0.0.1', 9))\n"
        "except PermissionError:\n"
        "    print('LOCAL REFUSED')\n"
        "except OSError:\n"
        "    print('LOCAL ALLOWED')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=HUB_DIR, check=False)
    lines = result.stdout.split()
    assert lines[0] == "REFUSED" and "never connects to the internet" in result.stdout + str(lines), result
    assert "CONNECTED" not in lines and "LOCAL ALLOWED" in result.stdout, result


# --- outgoing -----------------------------------------------------------------------------------------------------


def test_the_demo_makes_no_internet_connections(demo: TestClient) -> None:
    assert demo.get("/api/v1/ai/status").status_code == 200
    body = status(demo)
    assert body["internet_connections"] == 0 and body["outgoing"]["internet"] == 0
    assert body["blocked"] == {"count": 0, "destinations": []}
    assert body["incoming"]["localhost"] >= 2  # these requests themselves
    assert body["guarded"] is False  # the socket guard is turned on by serve() (a real hub), not in tests


class ModelStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"object": "list", "data": [{"id": "stub", "object": "model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


@pytest.mark.real_network
def test_the_model_transport_counts_the_connections_it_opens() -> None:
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        client = local_http_client()
        assert client.get(f"http://127.0.0.1:{stub.server_port}/v1/models").status_code == 200
        with pytest.raises(httpx.ConnectError):
            client.get("http://127.0.0.1:9/v1/models")  # nothing there: not a connection made
    finally:
        stub.shutdown()
    found = LEDGER.snapshot()
    assert found["outgoing"] == {"localhost": 1} and found["blocked"]["count"] == 0


def test_an_internet_destination_is_blocked_and_counted_once() -> None:
    llm = LLM(LLMSettings(base_url="http://8.8.8.8:1234/v1"))
    assert llm.status().reachable is False
    named = httpx.Client(transport=LocalOnlyTransport(resolve=lambda host, port: ["93.184.216.34"]))
    with pytest.raises(LLMRefused):
        named.get("http://models.example.com:1234/v1/models")  # a name that points at the internet
    found = LEDGER.snapshot()
    destinations = {(entry["host"], entry["port"]): entry["count"] for entry in found["blocked"]["destinations"]}
    assert destinations[("models.example.com", 1234)] == 1  # once: where it was refused
    assert destinations[("8.8.8.8", 1234)] >= 1 and found["blocked"]["count"] == sum(destinations.values())
    assert found["outgoing"].get("internet", 0) == 0


@pytest.mark.real_network
def test_names_are_looked_up_once_per_connection() -> None:
    lookups: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        lookups.append(host)
        return ["127.0.0.1"]

    with pytest.raises(httpx.ConnectError):  # nothing listens on port 9, but it tried: one lookup, by the backend
        httpx.Client(transport=LocalOnlyTransport(resolve=resolve)).get("http://gaming-pc.local:9/v1/models")
    assert lookups == ["gaming-pc.local"]


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


def test_a_malformed_origin_is_refused_not_an_error(demo: TestClient) -> None:
    response = demo.get("/api/v1/health", headers={"origin": "http://["})
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden_origin"
    assert status(demo)["refused"]["localhost"] == 1


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


class Lines(logging.Handler):
    """What the hub writes to uvicorn's console log. uvicorn's log config keeps it from reaching caplog, and
    configuring it (uvicorn.Config()) clears the logger's handlers: `listen()` after the Config is made."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def listen(self) -> None:
        logging.getLogger("uvicorn.error").addHandler(self)


@pytest.fixture
def console() -> Iterator[Lines]:
    handler = Lines()
    yield handler
    logging.getLogger("uvicorn.error").removeHandler(handler)


def _until(check: Any, seconds: float = 6.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return check()


@pytest.mark.real_network
def test_the_hub_listens_only_where_it_is_told_and_follows_changes(tmp_path: Path, fake_llm: FakeModelServer, console: Lines) -> None:
    second = "127.0.0.2"
    if not _bindable(second):
        pytest.skip("this computer has only 127.0.0.1 on loopback (macOS)")
    port = _free_port()
    wanted = ["127.0.0.1"]
    broken = [False]

    def find() -> list[str]:
        if broken[0]:
            broken[0] = False
            raise RuntimeError("the adapters couldn't be read just now")
        return list(wanted)

    app = create_app(Settings(profile=get_profile("demo"), data_dir=tmp_path), llm=fake_llm.llm())
    server = HubServer(uvicorn.Config(app, port=port, log_level="info", timeout_graceful_shutdown=2), find, check_every=0.3)
    console.listen()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _until(lambda: server.started)
        assert list(server.listening) == ["127.0.0.1"] and _reachable("127.0.0.1", port)
        assert not _reachable(second, port)  # not on every address: only where it was told
        assert any(line == f"Daytrace listening on http://127.0.0.1:{port}" for line in console.lines)  # on the console
        wanted.append(second)  # a new address (a new Wi-Fi, a DHCP renewal)
        assert _until(lambda: _reachable(second, port)) and set(server.listening) == {"127.0.0.1", second}
        assert LEDGER.snapshot()["listening"] == [f"127.0.0.1:{port}", f"{second}:{port}"]
        broken[0] = True  # a check that fails keeps the server serving
        time.sleep(0.8)
        assert _reachable("127.0.0.1", port) and _reachable(second, port)
        for sock in server.listening[second].sockets:  # the listening socket dies under it (Windows, a failed accept)
            sock._sock.close()
        assert _until(lambda: _reachable(second, port))  # bound again at the next check
        wanted.remove(second)  # it went away
        assert _until(lambda: second not in server.listening)
        assert _until(lambda: not _reachable(second, port)) and _reachable("127.0.0.1", port)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert not thread.is_alive()


def test_an_address_that_cant_be_bound_is_reported_once(tmp_path: Path, console: Lines) -> None:
    server = HubServer(uvicorn.Config(create_app(Settings(profile=get_profile("demo"), data_dir=tmp_path)), port=_free_port()), list)
    console.listen()
    for _ in range(3):
        assert server._bind_all(["192.0.2.123"]) == []  # not an address of this computer
    assert sum("isn't listening on 192.0.2.123" in line for line in console.lines) == 1


def test_ctrl_c_ends_the_hub_quietly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(self: HubServer, *args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt  # what uvicorn does after a clean shutdown on Ctrl+C

    monkeypatch.setattr(HubServer, "run", interrupted)
    monkeypatch.setattr(app_module, "install_network_audit", lambda: None)  # not in the test process
    assert serve(Settings(profile=get_profile("demo"), data_dir=tmp_path)) is None
