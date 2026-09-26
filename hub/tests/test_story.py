"""Tests for DT-37: the local model client, and DT-39: the day story and its number check."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

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
from daytrace_hub.story import Fact, StoryResult, day_facts, day_story, numbers_in, unsupported_numbers

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
GOOD = (
    "Friday was a focused one. You spent 2 hours 35 minutes on screens, 125 minutes of it in Visual Studio Code. "
    "You started at 9:00 and worked for about two hours without a break, a focus score of 81. "
    "Instagram took 30 minutes around lunch. You only picked up your phone once."
)


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


def story(db: Database, fake: FakeModelServer, day: date = DAY) -> StoryResult:
    return day_story(db, fake.llm(), day, ZONE, "America/Toronto", AFTER)


def test_the_facts_come_from_the_stats_engine(worked: Database) -> None:
    with worked.connect() as conn:
        facts = {f.label: (f.value, f.unit) for f in day_facts(Stats(conn, ZONE, "America/Toronto", AFTER), DAY)}
    assert facts["screen time"] == (155, "minutes")
    assert facts["time in Visual Studio Code"] == (125, "minutes") and facts["time on social"] == (30, "minutes")
    assert facts["first screen use"] == ("09:00", "time") and facts["last screen use"] == ("12:30", "time")
    assert facts["focus score (0 to 100)"] == (81, "score")  # round(100 x 125 / (125 + 30))
    assert facts["phone pickups"] == (1, "times")
    assert not any(label.startswith("sleep") for label in facts)  # no phone use around the night: no sleep fact


def test_numbers_are_read_with_what_they_count() -> None:
    found = [(value, kind) for _, value, kind in numbers_in(
        "2 hours 5 minutes, twenty-five times, at 11:40 pm, 34%, 1,440 in all, half an hour, one app")]
    assert found == [(2, "hours"), (5, "minutes"), (11, "clock"), (40, "clock"), (34, "percent"), (1440, "any"),
                     (30, "minutes"), (25, "count")]  # digits, then phrases and words; "one" is not a number


def test_a_unit_after_a_number_decides_what_it_may_match() -> None:
    facts = [Fact("screen time", 125, "minutes"), Fact("last screen use", "23:40", "time"), Fact("focus score (0 to 100)", 81, "score")]
    assert unsupported_numbers("You stopped at 11:40 and scored 81.", facts, DAY) == []
    assert unsupported_numbers("You spent 40 minutes and 81% of it...", facts, DAY) == ["40"]  # 40 is only a clock time
    assert unsupported_numbers("That was 81 minutes.", facts, DAY) == ["81"]  # 81 is a score, not a duration


@pytest.mark.parametrize(
    ("text", "wrong"),
    [
        ("You spent 125 minutes on screens.", []),
        ("You spent 2 hours 5 minutes on screens.", []),
        ("You spent about two hours on screens, 2.1 hours to be exact.", []),
        ("You spent 124 minutes on screens.", []),  # rounding
        ("Your focus score was 34%, and you picked up your phone 12 times.", []),
        ("You stopped at 11:40 pm on Friday, 25 September 2026.", []),
        ("You spent 3 hours on screens.", ["3"]),
        ("You picked up your phone 13 times.", ["13"]),
        ("You spent forty minutes on YouTube.", ["forty"]),
        ("You stopped at 10:40 pm.", ["10"]),
    ],
)
def test_the_number_check(text: str, wrong: list[str]) -> None:
    facts = [Fact("screen time", 125, "minutes"), Fact("focus score (0 to 100)", 34, "score"),
             Fact("phone pickups", 12, "times"), Fact("last screen use", "23:40", "time")]
    assert unsupported_numbers(text, facts, DAY) == wrong


def test_a_good_story_is_kept_and_cached(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    first = story(worked, fake_llm)
    assert (first.story, first.model, first.cached, first.fallback) == (GOOD, "qwen3-14b", False, False)
    again = story(worked, fake_llm)
    assert again.cached is True and again.story == GOOD
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
    assert result.fallback is True and "225" in (result.reason or "")
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


def test_new_data_means_a_new_story(worked: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(GOOD)
    story(worked, fake_llm)
    add(worked, "android-1", "android", [phone_app("2026-09-25T20:00:00-04:00", "2026-09-25T20:10:00-04:00", "WhatsApp",
                                                   "com.whatsapp", 2)])
    fake_llm.reply_text(GOOD.replace("2 hours 35 minutes", "2 hours 45 minutes"))
    result = story(worked, fake_llm)
    assert result.cached is False and "2 hours 45 minutes" in result.story


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
    assert (body["story"], body["model"], body["cached"], body["fallback"]) == (GOOD, "qwen3-14b", False, False)
    assert {"label": "screen time", "value": 155, "unit": "minutes"} in body["facts_used"]
