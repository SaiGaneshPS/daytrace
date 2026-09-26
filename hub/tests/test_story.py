"""Tests for DT-39: day story and number check (added by that ticket), and DT-37: the local model client."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub.app import create_app
from daytrace_hub.config import TAILSCALE_NETWORKS, Settings, get_profile
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
