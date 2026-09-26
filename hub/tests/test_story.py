"""Tests for DT-39: day story and number check (added by that ticket), and DT-37: the local model client."""
from __future__ import annotations

import httpx
import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub.llm import (
    DEFAULT_BASE_URL,
    LLM,
    LLMError,
    LLMRefused,
    LLMSettings,
    LocalOnlyTransport,
    address_allowed,
    checked_address,
    load_llm_settings,
)

# --- DT-37: the local model client ------------------------------------------------------------------------------


def test_settings_come_from_the_environment() -> None:
    assert load_llm_settings({}) == LLMSettings(DEFAULT_BASE_URL, None)
    ollama = load_llm_settings({"DAYTRACE_LLM_BASE_URL": " http://127.0.0.1:11434/v1/ ", "DAYTRACE_LLM_MODEL": "qwen3:14b"})
    assert ollama == LLMSettings("http://127.0.0.1:11434/v1", "qwen3:14b")
    for bad in ("127.0.0.1:1234/v1", "ftp://127.0.0.1/v1", "http://"):
        with pytest.raises(ValueError, match="DAYTRACE_LLM_BASE_URL"):
            load_llm_settings({"DAYTRACE_LLM_BASE_URL": bad})


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.0.0.2", "172.20.1.1", "192.168.1.5", "169.254.1.1", "fd00::1", "fe80::1",
     "100.100.1.1", "fd7a:115c:a1e0::5", "::ffff:192.168.1.5"],
)
def test_this_computer_the_lan_and_tailscale_are_allowed(address: str) -> None:
    assert address_allowed(address)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "100.128.0.1", "172.32.0.1", "0.0.0.0", "2001:4860::8888", "::ffff:8.8.8.8", "not-an-ip"]
)
def test_internet_addresses_are_refused(address: str) -> None:
    assert not address_allowed(address)


def test_a_name_is_checked_after_it_is_resolved() -> None:
    def resolver(answers: list[str]):  # type: ignore[no-untyped-def]
        return lambda host, port: answers

    assert checked_address("my-pc.local", 1234, resolver(["192.168.1.9"])) == "192.168.1.9"
    with pytest.raises(LLMRefused, match="93.184.215.14"):
        checked_address("models.example.com", 443, resolver(["93.184.215.14"]))
    with pytest.raises(LLMRefused):  # one public address among private ones is enough to refuse
        checked_address("sneaky.example", 1234, resolver(["192.168.1.9", "8.8.8.8"]))
    with pytest.raises(LLMError, match="can't find"):
        checked_address("nowhere.local", 1234, resolver([]))


def test_requests_go_to_the_address_that_was_checked() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    transport = LocalOnlyTransport(resolve=lambda host, port: ["192.168.1.9"], inner=httpx.MockTransport(record))
    with httpx.Client(transport=transport) as http:
        http.get("http://my-pc.local:1234/v1/models")
    assert seen[0].url.host == "192.168.1.9"  # a later DNS answer cannot move it elsewhere
    assert seen[0].headers["host"] == "my-pc.local:1234"


def test_an_internet_model_server_is_never_contacted() -> None:
    llm = LLM(LLMSettings("http://8.8.8.8:1234/v1"))  # the real transport: refused before any connection
    with pytest.raises(LLMRefused):
        llm.models()
    status = llm.status()
    assert status.reachable is False
    assert "Daytrace only uses a local model" in (status.error or "")
    llm.close()


def test_a_name_pointing_at_the_internet_is_refused_too() -> None:
    http = httpx.Client(transport=LocalOnlyTransport(resolve=lambda host, port: ["93.184.215.14"]))
    llm = LLM(LLMSettings("http://lmstudio.example.com:1234/v1"), http_client=http)
    with pytest.raises(LLMRefused, match="points to 93.184.215.14"):
        llm.models()
    llm.close()


def test_status_shows_the_model_and_that_it_can_call_tools(fake_llm: FakeModelServer) -> None:
    status = fake_llm.llm().status()
    assert (status.reachable, status.model, status.tool_calling, status.models, status.error) == (
        True, "qwen3-14b", True, ["qwen3-14b"], None
    )


def test_a_model_that_ignores_tools_is_reported(fake_llm: FakeModelServer) -> None:
    fake_llm.tool_calling = False
    assert fake_llm.llm().status().tool_calling is False


def test_the_tool_check_is_remembered(fake_llm: FakeModelServer) -> None:
    llm = fake_llm.llm()
    llm.status()
    llm.status()
    assert len(fake_llm.chats()) == 1  # the probe costs a reply, so it runs once


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


def test_the_configured_model_must_be_loaded(fake_llm: FakeModelServer) -> None:
    status = fake_llm.llm(model="llama-3.1-8b").status()
    assert status.reachable is True
    assert status.tool_calling is None
    assert status.error == "the model 'llama-3.1-8b' is not loaded in the model server (loaded: qwen3-14b)"


def test_a_server_with_no_model_loaded_says_so(fake_llm: FakeModelServer) -> None:
    fake_llm.models = []
    assert "no model loaded" in (fake_llm.llm().status().error or "")


def test_a_failed_request_is_tried_once_more(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [503]
    assert fake_llm.llm().models() == ["qwen3-14b"]
    assert len(fake_llm.requests) == 2


def test_a_server_that_keeps_failing_gives_a_plain_error(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [500, 500]
    with pytest.raises(LLMError, match="answered 500"):
        fake_llm.llm().models()


def test_chat_returns_the_reply_and_sends_the_model(fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("You spent 2 hours in YouTube.")
    reply = fake_llm.llm().chat([{"role": "user", "content": "How was my day?"}])
    assert reply.content == "You spent 2 hours in YouTube."
    assert fake_llm.chats()[0]["body"]["model"] == "qwen3-14b"


def test_the_ai_status_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/ai/status")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True and body["model"] == "qwen3-14b" and body["tool_calling"] is True
    assert body["base_url"] == "http://127.0.0.1:1234/v1"


def test_the_ai_status_needs_a_paired_device_from_other_computers(client: TestClient) -> None:
    phone = TestClient(client.app, client=("192.168.1.50", 40000))
    assert phone.get("/api/v1/ai/status").status_code == 401
