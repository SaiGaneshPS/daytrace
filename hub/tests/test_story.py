"""Tests for DT-37: the local model client, and DT-39: the day story and its number check."""
from __future__ import annotations

import ipaddress
import socket
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub import story as story_module
from daytrace_hub.api import ai as ai_api
from daytrace_hub.api.events import store_events
from daytrace_hub.app import create_app
from daytrace_hub.auth import register_device
from daytrace_hub.config import TAILSCALE_NETWORKS, Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.llm import (
    DEFAULT_BASE_URL,
    LLM,
    LOCAL_NETWORKS,
    LLMError,
    LLMRefused,
    LLMSettings,
    LocalOnlyBackend,
    address_allowed,
    checked_addresses,
    load_llm_settings,
    local_http_client,
    model_networks,
)
from daytrace_hub.models import Event
from daytrace_hub.stats import Stats
from daytrace_hub.story import (
    MAX_STORY_CHARS,
    Fact,
    StoryResult,
    cache_story,
    clean_reply,
    day_facts,
    day_story,
    numbers_in,
    sentences,
    template_story,
    unsupported_numbers,
)

# --- DT-37: the local model client ------------------------------------------------------------------------------


def test_settings_come_from_the_environment() -> None:
    assert load_llm_settings({}) == LLMSettings(DEFAULT_BASE_URL, None)
    ollama = load_llm_settings({"DAYTRACE_LLM_BASE_URL": " http://127.0.0.1:11434/v1/ ", "DAYTRACE_LLM_MODEL": "qwen3:14b"})
    assert ollama == LLMSettings("http://127.0.0.1:11434/v1", "qwen3:14b")


@pytest.mark.parametrize("bad", ["127.0.0.1:1234/v1", "localhost:1234/v1", "ftp://127.0.0.1/v1", "http://", "http://pc:port/v1"])
def test_a_bad_address_is_reported_instead_of_stopping_the_hub(bad: str) -> None:
    settings = load_llm_settings({"DAYTRACE_LLM_BASE_URL": bad})
    assert settings.problem is not None and "DAYTRACE_LLM_BASE_URL must be an address like" in settings.problem
    llm = LLM(settings)
    assert llm.status().error == settings.problem
    with pytest.raises(LLMError, match="DAYTRACE_LLM_BASE_URL"):
        llm.chat([{"role": "user", "content": "hi"}])
    llm.close()


def test_passwords_in_the_address_are_refused_and_never_shown() -> None:
    settings = load_llm_settings({"DAYTRACE_LLM_BASE_URL": "http://admin:hunter2@192.168.1.20:8080/v1"})
    assert settings.problem == "DAYTRACE_LLM_BASE_URL must not contain a user name or password"
    status = LLM(settings).status()
    assert status.base_url == "http://192.168.1.20:8080/v1"
    assert "hunter2" not in str(status)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.0.0.2", "172.20.1.1", "192.168.1.5", "169.254.1.1", "fd00::1", "fe80::1",
     "fe80::1%12", "::ffff:192.168.1.5", "[::1]"],
)
def test_this_computer_and_the_lan_are_allowed(address: str) -> None:
    assert address_allowed(address)


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "100.128.0.1", "172.32.0.1", "0.0.0.0", "2001:4860::8888", "::ffff:8.8.8.8", "not-an-ip",
     "169.254.169.254", "fd00:ec2::254", "100.100.1.1"],  # cloud metadata, and Tailscale unless the profile allows it
)
def test_the_internet_and_metadata_services_are_refused(address: str) -> None:
    assert not address_allowed(address)


def test_the_tailnet_only_for_profiles_without_real_data(tmp_path: Path) -> None:
    personal = model_networks(Settings(profile=get_profile("personal"), data_dir=tmp_path))
    shared = model_networks(Settings(profile=get_profile("shared-dev"), data_dir=tmp_path))
    assert not address_allowed("100.100.1.1", personal) and not address_allowed("fd7a:115c:a1e0::5", personal)
    assert address_allowed("100.100.1.1", shared) and address_allowed("fd7a:115c:a1e0::5", shared)
    assert all(network in shared for network in TAILSCALE_NETWORKS)


def test_a_narrowed_lan_is_respected(tmp_path: Path) -> None:
    home = (ipaddress.ip_network("192.168.1.0/24"),)
    networks = model_networks(Settings(profile=get_profile("personal"), data_dir=tmp_path, lan_networks=home))
    assert address_allowed("192.168.1.9", networks) and address_allowed("127.0.0.1", networks)
    assert not address_allowed("10.0.0.5", networks)


def test_a_name_is_checked_after_it_is_resolved() -> None:
    def resolver(answers: list[str]):  # type: ignore[no-untyped-def]
        return lambda host, port: answers

    assert checked_addresses("my-pc.local", 1234, LOCAL_NETWORKS, resolver(["::1", "192.168.1.9"])) == ["::1", "192.168.1.9"]
    with pytest.raises(LLMRefused, match="points to 93.184.215.14"):
        checked_addresses("models.example.com", 443, LOCAL_NETWORKS, resolver(["93.184.215.14"]))
    with pytest.raises(LLMRefused):  # one public address among private ones is enough to refuse
        checked_addresses("sneaky.example", 1234, LOCAL_NETWORKS, resolver(["192.168.1.9", "8.8.8.8"]))
    with pytest.raises(LLMError, match="can't find"):
        checked_addresses("nowhere.local", 1234, LOCAL_NETWORKS, resolver([]))


@pytest.mark.real_network
def test_each_checked_address_is_tried_in_turn() -> None:
    # "localhost" is ::1 first on Windows, while Ollama listens on 127.0.0.1 only: the next address must be tried.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        backend = LocalOnlyBackend(LOCAL_NETWORKS, resolve=lambda host, p: ["::1", "127.0.0.1"])
        stream = backend.connect_tcp("localhost", port, timeout=3)
        assert stream.get_extra_info("server_addr")[0] == "127.0.0.1"
        stream.close()


@pytest.mark.real_network
def test_the_guard_really_carries_requests_end_to_end() -> None:
    """A real local server, reached through the real guard and the OpenAI SDK: proves the guard is installed in
    httpx (it relies on httpx's connection pool, so an httpx upgrade that moved it would fail here)."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict[str, str]] = []

    class ModelsOnly(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({name.lower(): value for name, value in self.headers.items()})
            body = json.dumps({"object": "list", "data": [{"id": "qwen3-14b", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsOnly)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        via_name = LLM(LLMSettings(f"http://model-pc.local:{port}/v1"), http_client=local_http_client(
            resolve=lambda host, p: ["127.0.0.1"] if host == "model-pc.local" else []))
        assert via_name.models() == ["qwen3-14b"]  # the name was resolved by the guard, and the socket opened by it
        assert seen[0]["host"] == f"model-pc.local:{port}"
        assert "authorization" not in seen[0]
        via_name.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.real_network
def test_a_public_address_is_never_dialled() -> None:
    backend = LocalOnlyBackend(LOCAL_NETWORKS, resolve=lambda host, port: ["192.168.1.9", "93.184.215.14"])
    with pytest.raises(LLMRefused):
        backend.connect_tcp("sneaky.example", 1234, timeout=1)  # refused before any connection attempt


def test_an_internet_model_server_is_never_contacted() -> None:
    llm = LLM(LLMSettings("http://8.8.8.8:1234/v1"))  # the real guard: refused before any connection
    with pytest.raises(LLMRefused):
        llm.models()
    status = llm.status()
    assert status.reachable is False
    assert "Daytrace only uses a local model" in (status.error or "")
    llm.close()


def test_a_name_pointing_at_the_internet_is_refused_too() -> None:
    http = local_http_client(resolve=lambda host, port: ["93.184.215.14"])
    llm = LLM(LLMSettings("https://lmstudio.example.com/v1"), http_client=http)  # https is checked the same way
    with pytest.raises(LLMRefused, match="points to 93.184.215.14"):
        llm.models()
    llm.close()


def test_credentials_from_openai_variables_are_never_sent(monkeypatch: pytest.MonkeyPatch, fake_llm: FakeModelServer) -> None:
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer sk-real-secret\nX-Team: acme")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-real")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-real")
    llm = fake_llm.llm()
    llm.models()
    llm.chat([{"role": "user", "content": "hi"}])
    for request in fake_llm.requests:
        headers = request["headers"]
        assert set(headers) <= {"host", "accept", "accept-encoding", "content-type", "content-length", "connection",
                                "user-agent"}, headers
        assert "sk-real-secret" not in str(headers) and "org-real" not in str(headers)


def test_status_shows_the_model_and_that_it_can_call_tools(fake_llm: FakeModelServer) -> None:
    status = fake_llm.llm().status()
    assert (status.reachable, status.model, status.tool_calling, status.models, status.error) == (
        True, "qwen3-14b", True, ["qwen3-14b"], None
    )


def test_a_model_that_ignores_tools_is_reported(fake_llm: FakeModelServer) -> None:
    fake_llm.tool_calling = False
    assert fake_llm.llm().status().tool_calling is False


def test_a_server_that_rejects_tools_means_no_tool_calling_and_is_asked_once(fake_llm: FakeModelServer) -> None:
    fake_llm.reject_tools = True
    llm = fake_llm.llm()
    assert llm.status().tool_calling is False
    assert llm.status().tool_calling is False
    assert len(fake_llm.chats()) == 1


def test_the_tool_check_is_remembered(fake_llm: FakeModelServer) -> None:
    llm = fake_llm.llm()
    llm.status()
    llm.status()
    assert len(fake_llm.chats()) == 1  # the probe costs a reply, so it runs once


def test_the_tool_check_leaves_room_for_reasoning_models(fake_llm: FakeModelServer) -> None:
    fake_llm.llm().status()
    assert fake_llm.chats()[0]["body"]["max_tokens"] >= 512  # qwen3 and friends think before calling the tool


def test_the_status_answers_at_once_while_a_tool_check_runs(fake_llm: FakeModelServer) -> None:
    fake_llm.hold_chats = threading.Event()
    llm = fake_llm.llm()
    first: list[object] = []
    worker = threading.Thread(target=lambda: first.append(llm.status()))
    worker.start()
    assert fake_llm.chat_started.wait(5)
    started = time.monotonic()
    meanwhile = llm.status()  # must not wait for the probe (or queue up behind a lock)
    assert time.monotonic() - started < 1
    assert meanwhile.tool_calling is None and "couldn't tell yet" in (meanwhile.error or "")
    fake_llm.hold_chats.set()
    worker.join(5)
    assert first[0].tool_calling is True  # type: ignore[attr-defined]
    assert len(fake_llm.chats()) == 1


def test_status_when_no_model_server_runs(fake_llm: FakeModelServer) -> None:
    fake_llm.down = True
    status = fake_llm.llm().status()
    assert status.reachable is False
    assert status.tool_calling is None
    assert "start the server in LM Studio" in (status.error or "")


def test_the_status_check_asks_only_once(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [503]  # the dashboard should hear about a problem right away, not after a retry
    assert fake_llm.llm().status().reachable is False
    assert len(fake_llm.requests) == 1


@pytest.mark.parametrize("body", [{"models": []}, [], {"data": ["m1"]}, {"data": None}, "not json at all"])
def test_something_else_on_the_port_gives_a_plain_error(fake_llm: FakeModelServer, body: object) -> None:
    fake_llm.models_body = body
    status = fake_llm.llm().status()
    assert status.reachable is False
    assert "is not an OpenAI-compatible model server" in (status.error or "")


def test_the_configured_model_must_be_loaded(fake_llm: FakeModelServer) -> None:
    status = fake_llm.llm(model="llama-3.1-8b").status()
    assert status.reachable is True
    assert status.tool_calling is None
    assert status.error == "the model 'llama-3.1-8b' is not loaded in the model server (loaded: qwen3-14b)"


def test_embedding_models_are_not_picked_for_chat(fake_llm: FakeModelServer) -> None:
    fake_llm.models = ["text-embedding-nomic-embed-text-v1.5", "qwen3-14b"]
    assert fake_llm.llm().status().model == "qwen3-14b"
    fake_llm.models = ["text-embedding-nomic-embed-text-v1.5"]
    assert "no chat model loaded" in (fake_llm.llm().status().error or "")


def test_a_failed_request_is_tried_once_more(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [503]
    assert fake_llm.llm().models() == ["qwen3-14b"]
    assert len(fake_llm.requests) == 2


def test_a_server_that_keeps_failing_gives_a_short_plain_error(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [500, 500]
    fake_llm.error_message = "boom\x1b[31m" + "x" * 1000
    with pytest.raises(LLMError, match="answered 500") as caught:
        fake_llm.llm().models()
    assert "\x1b" not in str(caught.value) and len(str(caught.value)) < 300


def test_chat_returns_the_reply_and_asks_for_the_model_list_once(fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("You spent 2 hours in YouTube.")
    fake_llm.reply_text("And 40 minutes in Chrome.")
    llm = fake_llm.llm()
    assert llm.chat([{"role": "user", "content": "How was my day?"}]).content == "You spent 2 hours in YouTube."
    assert llm.chat([{"role": "user", "content": "And?"}]).content == "And 40 minutes in Chrome."
    assert fake_llm.chats()[0]["body"]["model"] == "qwen3-14b"
    assert len([r for r in fake_llm.requests if r["path"].endswith("/models")]) == 1


def test_the_ai_status_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True and body["model"] == "qwen3-14b" and body["tool_calling"] is True
    assert body["base_url"] == "http://127.0.0.1:1234/v1"


def test_the_ai_status_needs_a_paired_device_from_other_computers(client: TestClient) -> None:
    phone = TestClient(client.app, client=("192.168.1.50", 40000))
    assert phone.get("/api/v1/ai/status").status_code == 401


def test_a_typo_in_the_model_address_does_not_stop_the_hub(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAYTRACE_LLM_BASE_URL", "localhost:1234/v1")  # copied from LM Studio without http://
    with TestClient(create_app(settings), client=("127.0.0.1", 50000), base_url="http://localhost:8765") as hub:
        assert hub.get("/api/v1/health").status_code == 200
        status = hub.get("/api/v1/ai/status").json()
    assert status["reachable"] is False and "must be an address like" in status["error"]


# --- DT-39: the day story and its number check -------------------------------------------------------------------

DAY = date(2026, 9, 25)
ZONE = ZoneInfo("America/Toronto")
AFTER = datetime(2030, 1, 1, tzinfo=UTC)
AFTERNOON = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)  # 14:00 in Toronto: the day is not over yet
GOOD = (
    "Friday was a focused one. You spent 2 hours 35 minutes on screens, 125 minutes of it in Visual Studio Code. "
    "You started at 9:00 and worked for about two hours without a break, a focus score of 81. "
    "Instagram took 30 minutes around lunch. You only picked up your phone once."
)
FACTS = [
    Fact("screen time", 155, "minutes"), Fact("time in Visual Studio Code", 125, "minutes"),
    Fact("time in Instagram", 30, "minutes"), Fact("first screen use", "09:00", "time"),
    Fact("last screen use", "23:40", "time"), Fact("focused time (work or study blocks of 10+ minutes)", 125, "minutes"),
    Fact("focus score (0 to 100)", 81, "score"), Fact("phone pickups", 12, "times"),
    Fact("app switches per hour of screen time", 4.2, "per hour"),
    Fact("screen time after 11 pm the night before", 40, "minutes"),
]


def add(db: Database, device_id: str, device_type: str, events: list[dict[str, Any]]) -> None:
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
            register_device(conn, device_id=device_id, name=device_id, device_type=device_type)
        with transaction(conn):
            store_events(conn, device_id, [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)])


def window(start: str, end: str, app: str, app_id: str) -> dict[str, Any]:
    return {"kind": "window", "source": "tracker", "start": start, "end": end, "app": app, "app_id": app_id}


def phone_app(start: str, end: str, app: str, app_id: str, seq: int) -> dict[str, Any]:
    return {"kind": "app_session", "source": "usagestats", "seq": seq, "start": start, "end": end, "app": app, "app_id": app_id}


@pytest.fixture
def worked(db: Database) -> Database:
    """VS Code 09:00-11:05 on the desk (125 min) and Instagram 12:00-12:30 on the phone (Toronto time)."""
    add(db, "windows-1", "windows", [window("2026-09-25T09:00:00-04:00", "2026-09-25T11:05:00-04:00", "Visual Studio Code", "Code.exe")])
    add(db, "android-1", "android", [phone_app("2026-09-25T12:00:00-04:00", "2026-09-25T12:30:00-04:00", "Instagram",
                                               "com.instagram.android", 1)])
    return db


def story(db: Database, fake: FakeModelServer, now: datetime = AFTER, model: str | None = None) -> StoryResult:
    return day_story(db, fake.llm(model), DAY, ZONE, "America/Toronto", now)


def test_the_facts_come_from_the_stats_engine(worked: Database) -> None:
    with worked.connect() as conn:
        facts = {f.label: (f.value, f.unit) for f in day_facts(Stats(conn, ZONE, "America/Toronto", AFTER), DAY)}
    assert facts["screen time"] == (155, "minutes")
    assert facts["time in Visual Studio Code"] == (125, "minutes") and facts["time on social"] == (30, "minutes")
    assert facts["first screen use"] == ("09:00", "time") and facts["last screen use"] == ("12:30", "time")
    assert facts["focus score (0 to 100)"] == (81, "score")  # round(100 x 125 / (125 + 30))
    assert facts["phone pickups"] == (1, "times")
    assert not any(label.startswith("sleep") for label in facts)  # no phone use around the night: no sleep fact


def test_sessions_cut_at_midnight_are_not_the_first_or_last_screen_use(worked: Database) -> None:
    add(worked, "android-1", "android", [
        phone_app("2026-09-24T23:30:00-04:00", "2026-09-25T00:20:00-04:00", "Reddit", "com.reddit.frontpage", 2),
        phone_app("2026-09-25T23:50:00-04:00", "2026-09-26T00:10:00-04:00", "Reddit", "com.reddit.frontpage", 3),
    ])
    with worked.connect() as conn:
        facts = {f.label: f.value for f in day_facts(Stats(conn, ZONE, "America/Toronto", AFTER), DAY)}
    assert facts["first screen use"] == "09:00"  # not 00:00, where the night before's scrolling was cut
    assert "last screen use" not in facts  # still scrolling at midnight: the day's use didn't end that day


def test_amounts_are_read_whole() -> None:
    found = [(a.written, a.kind, a.value, a.precision) for a in numbers_in(
        "2 hours 35 minutes, 2h45m, two and a half hours, an hour and a half, half an hour, a three-hour block, "
        "a forty-minute one, 2.1 hours, one hundred and twenty five minutes")]
    assert found == [
        ("2 hours 35 minutes", "duration", 155, "exact"), ("2h45m", "duration", 165, "exact"),
        ("two and a half hours", "duration", 150, "half"), ("an hour and a half", "duration", 90, "half"),
        ("half an hour", "duration", 30, "half"), ("three-hour", "duration", 180, "hour"),
        ("forty-minute", "duration", 40, "exact"), ("2.1 hours", "duration", 126, "tenth"),
        ("one hundred and twenty five minutes", "duration", 125, "exact"),
    ]


def test_clock_times_know_am_and_pm() -> None:
    found = [(a.written, a.times, a.precision) for a in numbers_in(
        "11:40 pm, 11:40 am, 9 am, 8.15 am, half past eight, 23:40, nine o'clock, 11 at night, 30 amazing minutes")]
    assert found == [
        ("11:40 pm", (1420,), "exact"), ("11:40 am", (700,), "exact"), ("9 am", (540,), "hour"),
        ("8.15 am", (495,), "exact"), ("half past eight", (510, 1230), "exact"), ("23:40", (1420,), "exact"),
        ("nine o'clock", (540, 1260), "hour"), ("11 at night", (1380,), "hour"),
        ("30", (), "exact"),  # "amazing" is not "am"
    ]


def test_number_words_and_units() -> None:
    found = [(a.written, a.kind, a.value) for a in numbers_in(
        "twenty two, a dozen, zero, twice, one app, once, at one point, 155 seconds, 81 apps, 81%, 81 out of 100, "
        "4.2 switches an hour, 12 times, the 25th, 25 September 2026")]
    assert found == [
        ("twenty two", "bare", 22), ("a dozen", "bare", 12), ("zero", "bare", 0), ("twice", "count", 2),
        ("155 seconds", "other", 155), ("81 apps", "items", 81), ("81%", "percent", 81),
        ("81 out of 100", "score", 81), ("4.2 switches an hour", "rate", 4.2), ("12 times", "count", 12),
        ("25th", "ordinal", 25), ("25 September 2026", "date", 0),
    ]  # a lone "one" and "once" are not read: they are too often not numbers


@pytest.mark.parametrize(
    ("text", "wrong"),
    [
        ("You spent 2 hours 35 minutes on screens, 125 minutes of it in Visual Studio Code.", []),
        ("That is 155 minutes, or 2h35m, or about 2.6 hours.", []),
        ("You spent over two and a half hours on screens.", []),
        ("Code took about two hours; Instagram half an hour.", []),
        ("You started at 9 am, at 9:00, around nine o'clock, and stopped at 11:40 pm.", []),
        ("Your focus score was 81 out of 100, or 81%, and you picked up your phone 12 times.", []),
        ("You switched apps about 4 times an hour (4.2 per hour).", []),
        ("Your top two apps, in blocks of 10+ minutes, and 40 minutes after 11 pm the night before.", []),
        ("On Friday, 25 September 2026 (the 25th), you started after 9.", []),
        ("You picked up your phone 25 times and spent 9 hours on YouTube.", ["25 times", "9 hours"]),
        ("You spent 35 minutes on Instagram.", ["35 minutes"]),  # only the remainder of 2 hours 35 minutes
        ("You stopped at 12:09, or 8 pm, or 11:40 am.", ["12:09", "8 pm", "11:40 am"]),
        ("You spent 155 seconds, 125 days and 81 apps.", ["155 seconds", "125 days", "81 apps"]),
        ("It took four-hour blocks and fifty-minute breaks, 2h45m in all, with 100% focus.",
         ["four-hour", "fifty-minute", "2h45m", "100%"]),
        ("You had twenty two pickups, and scored 18 out of 100.", ["twenty two pickups", "18 out of 100"]),
        ("The last was on 24 September.", ["24 September"]),
        (GOOD, []),
    ],
)
def test_the_number_check(text: str, wrong: list[str]) -> None:
    assert unsupported_numbers(text, FACTS, DAY) == wrong


def test_abbreviations_do_not_end_sentences() -> None:
    assert sentences("You woke at 9 a.m. and opened Code. It was, e.g., calm. Done!") == 3


def test_thinking_is_removed_even_when_unfinished() -> None:
    assert clean_reply('<think>125 + 30</think> "Story."') == "Story."
    assert clean_reply("125 + 30</think> Story.") == "Story."  # the server left out the opening tag
    assert clean_reply("<think>Let me count: 125 + 30 = ") == ""  # cut off while thinking: no story at all


def test_a_good_story_is_kept_and_cached(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    first = story(worked, fake_llm)
    assert (first.story, first.model, first.cached, first.fallback) == (GOOD, "qwen3-14b", False, False)
    again = story(worked, fake_llm)
    assert again.cached is True and again.story == GOOD and again.facts == first.facts
    assert len(fake_llm.chats()) == 1  # the second request cost nothing


def test_a_planted_wrong_number_is_caught_and_retried(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD.replace("focus score of 81", "focus score of 95"))
    fake_llm.reply_text(GOOD)
    result = story(worked, fake_llm)
    assert result.story == GOOD and result.fallback is False
    retry = fake_llm.chats()[1]["body"]["messages"][-1]["content"]
    assert "95" in retry and "not in the facts" in retry


def test_wrong_numbers_twice_fall_back_to_the_template(worked: Database, fake_llm: FakeModelServer) -> None:
    wrong = GOOD.replace("125 minutes", "225 minutes")
    fake_llm.reply_text(wrong)
    fake_llm.reply_text(wrong)
    result = story(worked, fake_llm)
    assert result.fallback is True and result.model is None  # the template wrote it, not the model
    assert "225 minutes" in (result.reason or "") and "qwen3-14b" in (result.reason or "")
    assert result.story.startswith("On Friday, 25 September 2026 you spent 2 hours 35 minutes on screens.")
    assert unsupported_numbers(result.story, result.facts, DAY) == []  # the template never invents either
    assert story(worked, fake_llm).cached is False  # templates are not cached: the model gets another chance


def test_without_a_model_the_template_story_is_used(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.down = True
    result = story(worked, fake_llm)
    assert result.fallback is True and result.model is None
    assert "start the server in LM Studio" in (result.reason or "")


def test_a_story_with_the_wrong_length_is_retried(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("You spent 155 minutes on screens.")  # one sentence
    fake_llm.reply_text(f"<think>Let me count: 125 + 30 = 155.</think> {GOOD}")  # a reasoning model thinking aloud
    result = story(worked, fake_llm)
    assert result.story == GOOD and result.fallback is False
    assert "it had 1 sentence instead of 4 to 6" in fake_llm.chats()[1]["body"]["messages"][-1]["content"]


def test_a_story_that_is_only_too_long_is_told_so(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD.replace("Friday was a focused one.", "Friday was a focused one" + ", calm and steady" * 100 + "."))
    fake_llm.reply_text(GOOD)
    assert story(worked, fake_llm).story == GOOD
    retry = fake_llm.chats()[1]["body"]["messages"][-1]["content"]
    assert f"over the limit of {MAX_STORY_CHARS}" in retry and "sentences instead" not in retry


def test_a_reply_cut_off_while_thinking_is_retried(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("<think>Let me count: 125 + 30 = 155, and then", finish_reason="length")
    fake_llm.reply_text(GOOD)
    assert story(worked, fake_llm).story == GOOD
    first, retry = fake_llm.chats()
    assert first["body"]["max_tokens"] >= 1024  # room for a reasoning model to think before it writes
    assert "cut off" in retry["body"]["messages"][-1]["content"]


def test_new_data_means_a_new_story(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    story(worked, fake_llm)
    add(worked, "android-1", "android", [phone_app("2026-09-25T20:00:00-04:00", "2026-09-25T20:10:00-04:00", "WhatsApp",
                                                   "com.whatsapp", 2)])
    fake_llm.reply_text(GOOD.replace("2 hours 35 minutes", "2 hours 45 minutes"))
    result = story(worked, fake_llm)
    assert result.cached is False and "2 hours 45 minutes" in result.story


def test_a_new_model_setting_means_a_new_story(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    story(worked, fake_llm)
    fake_llm.reply_text(GOOD)
    assert story(worked, fake_llm, model="qwen3-14b").cached is False  # now chosen by name, not the loaded one
    assert story(worked, fake_llm, model="qwen3-14b").cached is True


def test_today_is_the_day_so_far(worked: Database, fake_llm: FakeModelServer) -> None:
    with worked.connect() as conn:
        facts = {f.label for f in day_facts(Stats(conn, ZONE, "America/Toronto", AFTERNOON), DAY)}
    assert "first screen use" in facts and "last screen use" not in facts  # the last use so far is just now
    fake_llm.down = True
    result = story(worked, fake_llm, now=AFTERNOON)
    assert result.in_progress is True
    assert result.story.startswith("So far on Friday, 25 September 2026, you have spent 2 hours 35 minutes on screens.")
    assert template_story([], DAY, in_progress=True).startswith("Nothing has been recorded yet on Friday")
    fake_llm.down = False
    fake_llm.reply_text(GOOD)
    story(worked, fake_llm, now=AFTERNOON)
    assert "The day is not over yet" in fake_llm.chats()[0]["body"]["messages"][1]["content"]


def test_todays_story_is_written_again_at_most_every_15_minutes(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    first = story(worked, fake_llm, now=AFTERNOON)
    add(worked, "android-1", "android", [phone_app("2026-09-25T13:00:00-04:00", "2026-09-25T13:10:00-04:00", "WhatsApp",
                                                   "com.whatsapp", 2)])
    soon = story(worked, fake_llm, now=AFTERNOON + timedelta(minutes=5))
    assert soon.cached is True and soon.story == GOOD and soon.facts == first.facts  # with the facts it was written from
    fake_llm.reply_text(GOOD.replace("2 hours 35 minutes", "2 hours 45 minutes"))
    later = story(worked, fake_llm, now=AFTERNOON + timedelta(minutes=20))
    assert later.cached is False and "2 hours 45 minutes" in later.story
    assert len(fake_llm.chats()) == 2


def test_two_requests_at_once_ask_the_model_once(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    fake_llm.hold_chats = threading.Event()
    llm = fake_llm.llm()
    results: list[StoryResult] = []

    def ask() -> None:
        results.append(day_story(worked, llm, DAY, ZONE, "America/Toronto", AFTER))

    first, second = threading.Thread(target=ask), threading.Thread(target=ask)
    first.start()
    assert fake_llm.chat_started.wait(5)
    second.start()
    time.sleep(0.2)  # the second request is now waiting for the first
    fake_llm.hold_chats.set()
    first.join(5)
    second.join(5)
    assert len(fake_llm.chats()) == 1
    assert sorted(r.cached for r in results) == [False, True] and {r.story for r in results} == {GOOD}


def test_a_busy_database_still_returns_the_story(worked: Database, fake_llm: FakeModelServer,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(conn: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(story_module, "transaction", busy)
    fake_llm.reply_text(GOOD)
    result = story(worked, fake_llm)
    assert result.story == GOOD and result.fallback is False
    fake_llm.reply_text(GOOD)
    assert story(worked, fake_llm).cached is False  # it just wasn't kept


def test_a_story_from_older_facts_never_replaces_a_newer_one(worked: Database) -> None:
    facts = [Fact("screen time", 155, "minutes")]
    cache_story(worked, DAY, "America/Toronto", "w", "h2", StoryResult("Newer.", facts, "m", False, False), AFTER)
    cache_story(worked, DAY, "America/Toronto", "w", "h1", StoryResult("Older.", facts, "m", False, False),
                AFTER - timedelta(minutes=1))
    with worked.connect() as conn:
        assert conn.execute("SELECT story FROM story_cache").fetchone()["story"] == "Newer."


def test_a_day_without_data_gets_a_gentle_note_and_no_model_call(db: Database, fake_llm: FakeModelServer) -> None:
    result = story(db, fake_llm)
    assert result.story.startswith("Nothing was recorded on Friday, 25 September 2026")
    assert result.facts == [] and fake_llm.chats() == []


def test_the_story_endpoint(worked: Database, settings: Settings, fake_llm: FakeModelServer,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_api, "current_time", lambda: AFTER)
    fake_llm.reply_text(GOOD)
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=("127.0.0.1", 50000), base_url="http://localhost:8765") as hub:
        body = hub.get("/api/v1/story", params={"date": "2026-09-25", "tz": "America/Toronto"}).json()
        assert hub.get("/api/v1/story", params={"date": "2026-09-25", "tz": "Mars/Base"}).status_code == 400
        phone = TestClient(hub.app, client=("192.168.1.50", 40000))
        assert phone.get("/api/v1/story", params={"date": "2026-09-25"}).status_code == 401
    assert (body["story"], body["model"], body["cached"], body["fallback"], body["in_progress"]) == (GOOD, "qwen3-14b", False, False, False)
    assert {"label": "screen time", "value": 155, "unit": "minutes"} in body["facts_used"]
