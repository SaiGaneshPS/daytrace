"""Shared fixtures: a temporary data folder and database (DT-10), and a fake local model server (DT-37)."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from daytrace_hub.app import create_app
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database
from daytrace_hub.llm import LLM, LLMSettings, LocalOnlyTransport

LOCAL_CLIENT = ("127.0.0.1", 50000)
LOCAL_URL = "http://localhost:8765"  # the hub computer's own dashboard address (trusted as local)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own data folder, so no test can ever touch real hub data."""
    data_dir = tmp_path / "daytrace-data"
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DAYTRACE_MDNS", "off")  # never announce anything on the real network from a test
    monkeypatch.delenv("DAYTRACE_LLM_BASE_URL", raising=False)  # tests never use the model server on this PC
    monkeypatch.delenv("DAYTRACE_LLM_MODEL", raising=False)
    monkeypatch.setenv("DAYTRACE_TRACKER", "off")  # never record this computer's screen from a test
    return data_dir


@pytest.fixture(autouse=True)
def _no_real_desktop_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub started by a test with tracking on fails loudly instead of recording this computer's screen."""
    from daytrace_hub import app

    def refuse() -> None:
        raise AssertionError("tests must not start the real desktop tracker; use a fake probe")

    monkeypatch.setattr(app, "platform_probe", refuse)


@pytest.fixture(autouse=True)
def _no_real_model_server(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that reaches for a real model server (LM Studio on this PC, say) fails loudly instead. The guard's own
    checks still run first; only the final connect is refused. Tests marked real_network may open local sockets."""
    if request.node.get_closest_marker("real_network"):
        return

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("tests must not connect to a real model server; use the fake_llm fixture")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", refuse)


class FakeModelServer:
    """Stands in for LM Studio: answers GET /models and POST /chat/completions like an OpenAI-compatible server.

    Queue replies with `reply_text` / `reply_tool_call`. Without a queued reply, a request that offers tools gets a
    call to its first tool when `tool_calling` is on, and plain text otherwise. Every request is kept in `requests`.
    """

    def __init__(self) -> None:
        self.models = ["qwen3-14b"]
        self.tool_calling = True
        self.down = False  # True: nothing listens (connection refused)
        self.status_codes: list[int] = []  # answered in turn before anything else, e.g. [500] for one failure
        self.replies: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.hold_chats: threading.Event | None = None  # set: chat replies wait until it is set
        self.chat_started = threading.Event()
        self.reject_tools = False  # True: 400 "does not support tools", as Ollama answers for such models
        self.models_body: Any = None  # not None: sent as-is for GET /models (a server that is not OpenAI-like)
        self.error_message = "fake failure"

    def reply_text(self, text: str) -> None:
        self.replies.append({"role": "assistant", "content": text})

    def reply_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        call = {
            "id": f"call_{len(self.replies)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        self.replies.append({"role": "assistant", "content": None, "tool_calls": [call]})

    def chats(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith("/chat/completions")]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content) if request.content else None
        headers = {name.lower(): value for name, value in request.headers.items()}
        self.requests.append({"method": request.method, "path": request.url.path, "body": body, "headers": headers})
        if request.url.path.endswith("/chat/completions"):
            self.chat_started.set()
            if self.hold_chats is not None:
                self.hold_chats.wait(timeout=10)
        if self.status_codes:
            code = self.status_codes.pop(0)
            return httpx.Response(code, json={"error": {"message": f"{self.error_message} {code}"}})
        if request.url.path.endswith("/models") and self.models_body is not None:
            return httpx.Response(200, json=self.models_body)
        if request.url.path.endswith("/chat/completions") and self.reject_tools and (body or {}).get("tools"):
            return httpx.Response(400, json={"error": {"message": "this model does not support tools"}})
        if request.url.path.endswith("/models"):
            data = [{"id": model, "object": "model", "created": 0, "owned_by": "fake"} for model in self.models]
            return httpx.Response(200, json={"object": "list", "data": data})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=self._completion(body or {}))
        return httpx.Response(404, json={"error": {"message": "not found"}})

    def _completion(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.replies:
            message = self.replies.pop(0)
        elif body.get("tools") and self.tool_calling:
            name = body["tools"][0]["function"]["name"]
            call = {"id": "call_probe", "type": "function", "function": {"name": name, "arguments": "{}"}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
        else:
            message = {"role": "assistant", "content": "I can't use tools, but here is an answer."}
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", ""),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def llm(self, model: str | None = None) -> LLM:
        """An LLM talking to this fake through the real guard (headers, address checks); only the socket is fake."""
        http = httpx.Client(transport=LocalOnlyTransport(inner=httpx.MockTransport(self.handle)))
        return LLM(LLMSettings(base_url="http://127.0.0.1:1234/v1", model=model), http_client=http)


@pytest.fixture
def fake_llm() -> FakeModelServer:
    return FakeModelServer()


@pytest.fixture(autouse=True)
def _no_real_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to pass a fake zeroconf fails loudly instead of using the network."""
    from daytrace_hub import discovery

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("tests must not start a real zeroconf; pass zeroconf_factory=FakeZeroconf")

    monkeypatch.setattr(discovery, "AsyncZeroconf", refuse)
    original_init = discovery.Advertiser.__init__

    def guarded_init(self: object, settings: object, zeroconf_factory: object = refuse, **kwargs: object) -> None:
        original_init(self, settings, zeroconf_factory=zeroconf_factory, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(discovery.Advertiser, "__init__", guarded_init)


@pytest.fixture
def settings(_isolated_data_dir: Path) -> Settings:
    return Settings(profile=get_profile("personal"), data_dir=_isolated_data_dir)


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def client(settings: Settings, fake_llm: FakeModelServer) -> Iterator[TestClient]:
    """A test client that looks like the dashboard on the hub computer itself, with the fake model server."""
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=LOCAL_CLIENT, base_url=LOCAL_URL) as test_client:
        yield test_client
