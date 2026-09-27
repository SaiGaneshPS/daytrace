"""Tests for DT-45 (the network lockdown and the "no internet" proof) and DT-46 (export and delete everything).

The claims: the hub listens only on this computer and the addresses phones use (the tailnet only for shared-dev,
never a public address or carrier-grade NAT) and follows them as they change, even when a listener dies; the
socket guard refuses any internet connection from anything in the hub process; the model transport counts the
connections it opens and the attempts it refuses; every incoming request is counted, served or refused, including
other web sites' pages and malformed ones; and GET /privacy/network shows it all, with 0 internet connections.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import FakeModelServer
from fastapi.testclient import TestClient
from test_tracker_base import Script

from daytrace_hub import app as app_module
from daytrace_hub import redaction, streaks
from daytrace_hub.api import ApiError
from daytrace_hub.api import insights as insights_api
from daytrace_hub.api import privacy as privacy_api
from daytrace_hub.api.events import ingest
from daytrace_hub.api.privacy import data_tables
from daytrace_hub.api.timeline import resolve_tz
from daytrace_hub.app import HubServer, create_app, network_audit, serve
from daytrace_hub.auth import AuthenticatedDevice, hash_token, register_device
from daytrace_hub.config import LEDGER, Settings, get_profile, network_of, origin_allowed
from daytrace_hub.db import Database, load_migrations, transaction, utc_text
from daytrace_hub.discovery import listen_addresses, phone_addresses, served
from daytrace_hub.llm import LLM, LLMRefused, LLMSettings, LocalOnlyTransport, local_http_client
from daytrace_hub.seed import seed
from daytrace_hub.tracker.base import TrackerService

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
    # The server's own thread changes `listening` (and, on a loaded Windows machine, asyncio may close a listener
    # after a failed accept, which the hub rebinds at its next check), so every check waits and reads a copy.
    def listening() -> set[str]:
        return set(server.listening.copy())

    try:
        assert _until(lambda: server.started)
        assert _until(lambda: _reachable("127.0.0.1", port)) and listening() == {"127.0.0.1"}
        assert not _reachable(second, port)  # not on every address: only where it was told
        assert any(line == f"Daytrace listening on http://127.0.0.1:{port}" for line in console.lines)  # on the console
        wanted.append(second)  # a new address (a new Wi-Fi, a DHCP renewal)
        assert _until(lambda: _reachable(second, port)) and _until(lambda: listening() == {"127.0.0.1", second})
        assert _until(lambda: set(LEDGER.snapshot()["listening"]) == {f"127.0.0.1:{port}", f"{second}:{port}"})
        broken[0] = True  # a check that fails keeps the server serving
        time.sleep(0.8)
        assert _until(lambda: _reachable("127.0.0.1", port) and _reachable(second, port))
        assert _until(lambda: server.listening.copy().get(second) is not None)
        dead = server.listening.copy()[second]
        dead.get_loop().call_soon_threadsafe(dead.close)  # it stops serving under the hub (as asyncio does on Windows after a failed accept)
        assert _until(lambda: server.listening.copy().get(second) not in (None, dead))  # a new listener at the next check
        assert _until(lambda: _reachable(second, port))
        wanted.remove(second)  # it went away
        assert _until(lambda: second not in listening())
        assert _until(lambda: not _reachable(second, port)) and _until(lambda: _reachable("127.0.0.1", port))
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


# --- export and delete everything (DT-46) -------------------------------------------------------------------------


SEEDED_AT = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
PHRASE = privacy_api.DELETE_PHRASE
# Every table and column the schema has, each one a decision: exported as is, as JSON, or kept out (a secret, the
# database's bookkeeping). A migration that adds one fails this until it is decided here and in api/privacy.py.
SCHEMA = {
    "achievements": {"achievement_id", "earned_on", "tz", "dates", "unlocked_at"},
    "category_overrides": {"app_key", "category", "source", "updated_at"},
    "devices": {"device_id", "name", "device_type", "token_hash", "paired_at", "last_seen", "revoked_at"},
    "device_gaps": {"device_id", "from_utc", "until_utc"},
    "events": {"id", "device_id", "dedup_key", "seq", "external_id", "kind", "source", "start_utc", "end_utc", "utc_offset_min",
               "app", "app_id", "title", "category", "data", "received_at", "updated_at"},
    "goals": {"goal_id", "target", "updated_at"},
    "nudge_log": {"id", "rule", "device_id", "title", "body", "created_at"},
    "sessions": {"id", "device_id", "local_date", "start_utc", "end_utc", "seconds", "app", "app_id", "title", "category", "source_event_ids"},
    "settings": {"key", "value"},
    "story_cache": {"day", "tz", "facts_hash", "writer", "story", "facts", "model", "created_at"},
    "wrapped_cache": {"week", "tz", "facts_hash", "writer", "lines", "facts", "model", "created_at"},
}


@pytest.fixture
def seeded(tmp_path: Path, fake_llm: FakeModelServer) -> Iterator[tuple[TestClient, Settings]]:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    seed(settings, 3, UTC, SEEDED_AT)
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=LOCAL, base_url="http://localhost:8767") as client:
        yield client, settings


def counts(settings: Settings) -> dict[str, int]:
    with Database(settings.database_path).connect() as conn:
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table, _ in data_tables(conn)}


def add_titled_event(settings: Settings, title: str, key: str | None = None) -> None:
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, kind, source, start_utc, end_utc, utc_offset_min, app, title, data, received_at)"
            " VALUES ('seed-windows', ?, 'window', 'seed', '2026-09-25T15:00:00.000000Z', '2026-09-25T15:05:00.000000Z', 0, 'Notepad', ?, '{}',"
            " '2026-09-25T15:05:00.000000Z')", (key or f"content:{title}", title))


def on_disk(settings: Settings) -> bytes:
    found = b""
    for path in (settings.database_path, Path(f"{settings.database_path}-wal")):
        if path.exists():
            found += path.read_bytes()
    return found


def test_every_table_and_column_is_decided() -> None:
    database = Database(Path(tempfile.mkdtemp()) / "schema.db")
    database.initialize()
    with database.connect() as conn:
        found = {table: {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')} for table, _ in data_tables(conn)}
    assert found == SCHEMA
    for table, column in privacy_api.JSON_COLUMNS | privacy_api.SECRET_COLUMNS:
        assert column in SCHEMA[table], (table, column)


def test_the_export_is_every_table_as_json(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    with Database(settings.database_path).connect() as conn, transaction(conn):
        token = register_device(conn, device_id="android-5", name="Phone", device_type="android")
        conn.execute("INSERT INTO settings (key, value) VALUES ('redaction', ?)", (json.dumps({"disabled": ["health"], "custom": []}),))
        conn.execute("INSERT INTO story_cache (day, tz, facts_hash, writer, story, facts, model, created_at) VALUES"
                     " ('2026-09-24', 'UTC', 'h', 'w', 'A day.', '[{\"label\": \"odd\", \"value\": NaN}]', 'm', '2026-09-25T00:00:00Z')")
    response = client.get("/api/v1/privacy/export")
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    today = datetime.now(resolve_tz(None)[0]).date().isoformat()  # the hub's own day, not UTC's
    assert response.headers["content-disposition"] == f'attachment; filename="daytrace-demo-{today}.json"'
    assert response.headers["cache-control"] == "no-store"
    body = json.loads(response.text, parse_constant=lambda name: pytest.fail(f"{name} is not JSON"))  # strict JSON
    assert (body["daytrace_export"], body["profile"], body["schema_version"]) == (1, "demo", Database(settings.database_path).schema_version())
    assert {table: len(rows) for table, rows in body["tables"].items()} == counts(settings)  # every table, every row
    assert "schema_migrations" not in body["tables"] and "data_changes" not in body["tables"]
    assert all("token_hash" not in device for device in body["tables"]["devices"])
    assert token not in response.text and hash_token(token) not in response.text  # no secret, not even its hash
    assert isinstance(body["tables"]["events"][0]["data"], dict)
    assert body["tables"]["settings"] == [{"key": "redaction", "value": {"disabled": ["health"], "custom": []}}]
    assert body["tables"]["story_cache"][0]["facts"] == [{"label": "odd", "value": None}]  # NaN is not JSON: null
    assert list(settings.data_dir.glob(f"{privacy_api.SNAPSHOT_PREFIX}*")) == []  # its copy removed afterwards


def test_the_export_is_one_moment_and_holds_nothing_while_it_streams(seeded: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    _, settings = seeded
    monkeypatch.setattr(privacy_api, "EXPORT_CHUNK", 2000)
    database = Database(settings.database_path)
    before = counts(settings)["events"]
    snapshot = privacy_api.take_snapshot(database)
    pieces = privacy_api.export_chunks(snapshot, "demo")
    first = next(pieces)  # a download paused half way
    add_titled_event(settings, "arrived during the export")  # a device syncs meanwhile
    started = time.monotonic()
    result = privacy_api.delete_everything(database)  # and a delete-all isn't kept waiting
    assert result.wiped is True and time.monotonic() - started < 3
    document = json.loads(first + "".join(pieces))
    assert len(document["tables"]["events"]) == before  # as of the moment it started, in many pieces
    privacy_api.remove_file(snapshot)
    assert not snapshot.exists()


def test_a_copy_left_by_a_stopped_export_is_cleaned_up(seeded: tuple[TestClient, Settings]) -> None:
    _, settings = seeded
    stale = settings.data_dir / f"{privacy_api.SNAPSHOT_PREFIX}old.db"
    stale.write_bytes(b"left over")
    old = time.time() - privacy_api.STALE_SNAPSHOT_SECONDS - 60
    os.utime(stale, (old, old))
    privacy_api.remove_file(privacy_api.take_snapshot(Database(settings.database_path)))
    assert not stale.exists()


def test_tables_a_later_version_may_add(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    with Database(settings.database_path).connect() as conn:
        conn.execute("CREATE TABLE later_notes (k TEXT PRIMARY KEY, body TEXT) WITHOUT ROWID")
        conn.execute("INSERT INTO later_notes VALUES ('a', 'kept in a WITHOUT ROWID table')")
        try:
            conn.execute("CREATE VIRTUAL TABLE later_search USING fts5(body)")
            conn.execute("INSERT INTO later_search (body) VALUES ('found by full-text search')")
            fts = True
        except sqlite3.OperationalError:
            fts = False  # this SQLite has no FTS5
    tables = client.get("/api/v1/privacy/export").json()["tables"]
    assert tables["later_notes"] == [{"k": "a", "body": "kept in a WITHOUT ROWID table"}]
    if fts:
        assert tables["later_search"] == [{"body": "found by full-text search"}]
        assert not any(name.startswith("later_search_") for name in tables)  # its internals are not data tables
    assert client.post("/api/v1/privacy/delete", json={"confirm": PHRASE}).status_code == 200
    with Database(settings.database_path).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM later_notes").fetchone()[0] == 0
        if fts:
            assert conn.execute("SELECT COUNT(*) FROM later_search WHERE later_search MATCH 'search'").fetchone()[0] == 0  # still works


def test_only_the_hub_computer_can_export_or_delete(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    with Database(settings.database_path).connect() as conn, transaction(conn):
        viewer = register_device(conn, device_id="viewer-5", name="Phone browser", device_type="viewer")
    before = counts(settings)
    with TestClient(client.app, client=LAN) as phone:
        as_viewer = {"Authorization": f"Bearer {viewer}"}
        refused = phone.get("/api/v1/privacy/export", headers=as_viewer)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "local_only"
        deleted = phone.post("/api/v1/privacy/delete", json={"confirm": PHRASE}, headers=as_viewer)
        assert deleted.status_code == 403 and deleted.json()["error"]["code"] == "local_only"
    assert counts(settings) == before


@pytest.mark.parametrize("phrase", ["", "delete all my data", "DELETE ALL MY DAYTRACE DATA", " delete all my daytrace data", "yes"])
def test_delete_needs_the_exact_phrase(seeded: tuple[TestClient, Settings], phrase: str) -> None:
    client, settings = seeded
    before = counts(settings)
    response = client.post("/api/v1/privacy/delete", json={"confirm": phrase})
    assert response.status_code == 400 and "nothing was deleted" in response.json()["error"]["message"]
    assert counts(settings) == before
    assert client.post("/api/v1/privacy/delete", json={}).status_code == 422


def test_delete_empties_every_table_and_the_hub_keeps_working(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    with Database(settings.database_path).connect() as conn, transaction(conn):
        phone_token = register_device(conn, device_id="android-5", name="Phone", device_type="android")
    pending = client.post("/api/v1/pair/start").json()["code"]  # shown before the delete
    before = counts(settings)
    assert client.get("/api/v1/insights/overview", params={"range": "2026-09-23..2026-09-24", "tz": "UTC"}).json()["metrics"][0]["value"]
    response = client.post("/api/v1/privacy/delete", json={"confirm": PHRASE})
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == before and body["wiped"] is True
    assert all(count == 0 for count in counts(settings).values())  # every table empty
    assert Database(settings.database_path).schema_version() == len(load_migrations())  # the schema kept
    with TestClient(client.app, client=LAN) as phone:
        assert phone.get("/api/v1/insights/day", headers={"Authorization": f"Bearer {phone_token}"}).status_code == 401
        claimed = phone.post("/api/v1/pair/claim", json={"code": pending, "device_name": "Tablet", "device_type": "android"})
        assert claimed.status_code in (400, 404, 410)  # a code shown before the delete pairs nothing after it
    overview = client.get("/api/v1/insights/overview", params={"range": "2026-09-23..2026-09-24", "tz": "UTC"}).json()
    assert overview["metrics"][0]["value"] is None and overview["cached"] is False  # no stale answer
    assert client.post("/api/v1/pair/start").status_code == 200  # pairing works again
    again = client.post("/api/v1/privacy/delete", json={"confirm": "delete"})  # asked again, every time
    assert again.status_code == 400


def test_delete_forgets_what_the_process_held(seeded: tuple[TestClient, Settings]) -> None:
    client, _ = seeded
    client.put("/api/v1/privacy/redaction", json={"custom": [{"name": "Client", "words": ["Acme"]}]})
    client.get("/api/v1/streaks", params={"tz": "UTC"})
    assert redaction._parse_choices.cache_info().currsize and streaks._cache
    client.post("/api/v1/privacy/delete", json={"confirm": PHRASE, "keep_redaction_rules": False})
    assert redaction._parse_choices.cache_info().currsize == 0 and not streaks._cache and not insights_api._cache


def test_your_redaction_rules_are_kept_unless_you_say(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    client.put("/api/v1/privacy/redaction", json={"custom": [{"name": "Client", "words": ["Acme"]}]})
    client.post("/api/v1/privacy/delete", json={"confirm": PHRASE})
    rules = client.get("/api/v1/privacy/redaction").json()["rules"]
    assert rules[-1]["words"] == ["Acme"]  # what is recorded next stays protected
    client.post("/api/v1/privacy/delete", json={"confirm": PHRASE, "keep_redaction_rules": False})
    assert client.get("/api/v1/privacy/redaction").json()["rules"][-1]["id"] == "private"  # asked to forget them too
    assert counts(settings)["settings"] == 0


def test_deleted_rows_are_gone_from_the_file_even_with_another_connection_open(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    for number in range(200):  # rows the tracker rewrote: their old titles freed long before the delete
        add_titled_event(settings, f"OldTitle-{number}-Zebra", key=f"ext:span-{number}")
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute("UPDATE events SET title = 'NewTitle' WHERE title LIKE 'OldTitle-%'")
    add_titled_event(settings, "ZebraSecretTitle-9431")
    assert b"ZebraSecretTitle-9431" in on_disk(settings)
    with Database(settings.database_path).connect() as idle:  # the tracker thread or a dashboard request, open meanwhile
        idle.execute("SELECT 1").fetchone()
        assert client.post("/api/v1/privacy/delete", json={"confirm": PHRASE}).json()["wiped"] is True
    raw = on_disk(settings)
    assert b"ZebraSecretTitle-9431" not in raw and b"OldTitle-" not in raw and b"Visual Studio Code" not in raw
    assert settings.database_path.stat().st_size < 200_000  # compacted


def test_every_connection_overwrites_what_it_deletes(seeded: tuple[TestClient, Settings]) -> None:
    _, settings = seeded
    with Database(settings.database_path).connect() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_the_desktop_tracker_brings_nothing_back(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    probe = Script()
    service = TrackerService(Database(settings.database_path), probe, "windows", "PC", settings.data_dir / "tracker.lock",
                             redact=lambda reading: reading)
    client.app.state.tracker = service
    service.start()
    try:
        time.sleep(2.5)  # it holds an open span of VS Code, from before the delete
        deleted_at = datetime.now(UTC)
        assert client.post("/api/v1/privacy/delete", json={"confirm": PHRASE}).status_code == 200
        time.sleep(4.5)  # it tracks again, afresh
        with Database(settings.database_path).connect() as conn:
            starts = [row[0] for row in conn.execute("SELECT start_utc FROM events WHERE source = 'tracker'")]
        assert starts and all(start >= utc_text(deleted_at - timedelta(seconds=1)) for start in starts)  # nothing from before
    finally:
        service.stop()


def test_a_device_deleted_mid_request_is_told_to_pair_again(seeded: tuple[TestClient, Settings]) -> None:
    _, settings = seeded
    ghost = AuthenticatedDevice(device_id="android-99", name="Gone", device_type="android")  # let in just before the delete
    event = {"device_id": "android-99", "seq": 1, "kind": "app_session", "source": "usagestats", "app": "Maps",
             "start": "2026-09-25T14:00:00+00:00", "end": "2026-09-25T14:05:00+00:00"}
    with pytest.raises(ApiError) as refused:
        ingest(Database(settings.database_path), ghost, {"events": [event]})
    assert refused.value.status_code == 401 and refused.value.code == "unauthorized"


def test_the_routes_describe_their_answers() -> None:
    paths = create_app(Settings(profile=get_profile("demo"), data_dir=Path(tempfile.mkdtemp()))).openapi()["paths"]
    export = paths["/api/v1/privacy/export"]["get"]["responses"]
    assert "application/json" in export["200"]["content"] and "403" in export
    assert {"400", "403"} <= set(paths["/api/v1/privacy/delete"]["post"]["responses"])


# --- where the data lives (DT-36) ------------------------------------------------------------------------------


def test_storage_says_where_the_data_lives_and_how_much_there_is(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    body = client.get("/api/v1/privacy/storage").json()
    path = settings.database_path
    wal = path.with_name(path.name + "-wal")
    size = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    with Database(path).connect() as conn:
        events, first, last = conn.execute("SELECT COUNT(*), MIN(start_utc), MAX(start_utc) FROM events").fetchone()
        devices = conn.execute("SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0]
    assert (body["profile"], body["folder"], body["file"]) == ("demo", str(path.parent), path.name)
    assert body["size_bytes"] == size and size > 0
    assert (body["events"], body["devices"]) == (events, devices) and events > 0
    assert datetime.fromisoformat(body["first_event"]) == datetime.fromisoformat(first)
    assert datetime.fromisoformat(body["last_event"]) == datetime.fromisoformat(last)


def test_storage_names_the_folder_only_on_the_hub_computer(seeded: tuple[TestClient, Settings]) -> None:
    client, settings = seeded
    with Database(settings.database_path).connect() as conn, transaction(conn):
        viewer = register_device(conn, device_id="viewer-6", name="Phone browser", device_type="viewer")
    here = client.get("/api/v1/privacy/storage").json()
    with TestClient(client.app, client=LAN) as phone:
        there = phone.get("/api/v1/privacy/storage", headers={"Authorization": f"Bearer {viewer}"}).json()
        assert phone.get("/api/v1/privacy/storage").status_code == 401  # unpaired
    assert there["folder"] is None  # the folder names this computer's user
    assert {**there, "folder": here["folder"], "size_bytes": 0} == {**here, "size_bytes": 0}


def test_storage_of_an_empty_profile(demo: TestClient) -> None:
    body = demo.get("/api/v1/privacy/storage").json()
    assert (body["events"], body["devices"], body["first_event"], body["last_event"]) == (0, 0, None, None)


def test_storage_survives_the_write_ahead_log_going_away(seeded: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    client, settings = seeded
    real = Path.stat

    def vanishing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith("-wal"):
            raise FileNotFoundError(self)  # closed and removed between a check and a look
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", vanishing)
    response = client.get("/api/v1/privacy/storage")
    assert response.status_code == 200 and response.json()["size_bytes"] == real(settings.database_path).st_size


def test_the_last_event_is_never_in_the_future(seeded: tuple[TestClient, Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    from daytrace_hub.api import timeline as timeline_api

    client, settings = seeded
    now = SEEDED_AT
    monkeypatch.setattr(timeline_api, "current_time", lambda: now)
    before = client.get("/api/v1/privacy/storage").json()
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO events (device_id, dedup_key, kind, source, start_utc, end_utc, utc_offset_min, data, received_at)"
                     " SELECT device_id, 'future-meeting', 'calendar_event', 'calendar', ?, ?, 0, '{}', ? FROM devices LIMIT 1",
                     (utc_text(now + timedelta(days=9)), utc_text(now + timedelta(days=9, hours=1)), utc_text(now)))
    after = client.get("/api/v1/privacy/storage").json()
    assert after["events"] == before["events"] + 1  # counted ...
    assert after["last_event"] == before["last_event"] and datetime.fromisoformat(after["last_event"]) <= now  # ... but not the latest

