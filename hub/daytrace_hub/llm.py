"""DT-37: client for a local OpenAI-compatible model server (LM Studio, Ollama).

The hub only ever talks to a model on this computer, your LAN or your tailnet, never the internet. Every request
goes through `LocalOnlyTransport`: an IP address must be loopback, a private LAN range or Tailscale; a host name
is resolved and every address it resolves to must be allowed; and the request is sent to the address that was
checked, so the name cannot be pointed somewhere else in between (DNS rebinding). No proxy, no redirects.

`LLM.chat()` is what the day story (DT-39) and "Ask your day" (DT-40) use. `LLM.status()` feeds
GET /api/v1/ai/status: whether a model server answers, which model is used, and whether it can call tools.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import openai
from openai import OpenAI

from .config import DEFAULT_LAN_NETWORKS, LOOPBACK_NETWORKS, TAILSCALE_NETWORKS, parse_ip

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"  # LM Studio; Ollama serves http://127.0.0.1:11434/v1
# Connecting is quick on a LAN; a long answer from a big model is not.
TIMEOUT = httpx.Timeout(connect=3.0, read=120.0, write=10.0, pool=5.0)
STATUS_TIMEOUT = httpx.Timeout(5.0)
TOOL_CHECK_TIMEOUT = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=5.0)  # may include loading the model
TOOL_CHECK_TTL_SECONDS = 600.0
MAX_RETRIES = 1
ALLOWED_NETWORKS = (*LOOPBACK_NETWORKS, *DEFAULT_LAN_NETWORKS, *TAILSCALE_NETWORKS)

Resolver = Callable[[str, int], list[str]]


class LLMError(Exception):
    """The model server can't be used right now. The message says why, in plain words."""


class LLMRefused(LLMError):
    """The model server's address is not on this computer, your LAN or your tailnet, so nothing was sent."""


@dataclass(frozen=True)
class LLMSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None  # None: the first model the server lists


def load_llm_settings(env: Mapping[str, str] | None = None) -> LLMSettings:
    """DAYTRACE_LLM_BASE_URL and DAYTRACE_LLM_MODEL. The address itself is checked on every request."""
    env = os.environ if env is None else env
    base_url = env.get("DAYTRACE_LLM_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"DAYTRACE_LLM_BASE_URL must be an http(s) address like {DEFAULT_BASE_URL}, got {base_url!r}")
    return LLMSettings(base_url=base_url, model=env.get("DAYTRACE_LLM_MODEL", "").strip() or None)


def address_allowed(text: str) -> bool:
    """Loopback, a private LAN range or Tailscale (IPv4-mapped IPv6 counts as the IPv4 address)."""
    address = parse_ip(text)
    return address is not None and any(address in network for network in ALLOWED_NETWORKS)


def system_resolver(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def checked_address(host: str, port: int, resolve: Resolver = system_resolver) -> str:
    """The address to connect to for `host`. Raises LLMRefused unless every address it stands for is allowed."""
    if parse_ip(host) is not None:
        if not address_allowed(host):
            raise LLMRefused(f"{host} is not on this computer, your LAN or your tailnet; Daytrace only uses a local model")
        return host
    try:
        addresses = resolve(host, port)
    except OSError as exc:
        raise LLMError(f"can't find the model server {host!r} on the network") from exc
    if not addresses:
        raise LLMError(f"can't find the model server {host!r} on the network")
    for address in addresses:
        if not address_allowed(address):
            raise LLMRefused(
                f"{host} points to {address}, which is not on this computer, your LAN or your tailnet;"
                " Daytrace only uses a local model"
            )
    return addresses[0]


class LocalOnlyTransport(httpx.BaseTransport):
    """Checks every request's destination, then sends it to the checked address (see the module docstring)."""

    def __init__(self, resolve: Resolver = system_resolver, inner: httpx.BaseTransport | None = None) -> None:
        self._resolve = resolve
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        address = checked_address(url.host, port, self._resolve)
        if url.scheme == "http" and address != url.host:
            # Connect to the address that was checked; the Host header keeps the name. (HTTPS needs the name for
            # its certificate, and a certificate cannot be moved to another address anyway.)
            request.url = url.copy_with(host=address)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def local_http_client(resolve: Resolver = system_resolver) -> httpx.Client:
    """The only way the hub talks to a model: checked addresses, no proxy from the environment, no redirects."""
    return httpx.Client(transport=LocalOnlyTransport(resolve), timeout=TIMEOUT, trust_env=False, follow_redirects=False)


@dataclass
class ModelStatus:
    base_url: str
    model: str | None
    reachable: bool
    tool_calling: bool | None  # None: not checked (no usable model)
    models: list[str] = field(default_factory=list)
    error: str | None = None


# A tool the probe asks the model to call. Any model that handles tools calls it for this prompt.
_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Returns the current time on the user's computer.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}
_PROBE_MESSAGES = [{"role": "user", "content": "What time is it right now? Use the get_current_time tool."}]


class LLM:
    """One local model server. Safe to share between threads (FastAPI runs sync endpoints in a thread pool)."""

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._http = http_client or local_http_client()
        self._client = OpenAI(
            api_key="local",  # local servers ignore it, but the client insists on one
            base_url=settings.base_url,
            http_client=self._http,
            max_retries=MAX_RETRIES,
            timeout=TIMEOUT,
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._tool_check: tuple[str, bool, float] | None = None  # (model, can call tools, when checked)

    def close(self) -> None:
        self._http.close()

    def models(self, retry: bool = True) -> list[str]:
        """GET /models: the models the server can use right now (the health check). The status check asks only
        once, so the dashboard hears quickly that nothing is running (Windows takes about 2 s to refuse)."""
        try:
            options = {"timeout": STATUS_TIMEOUT} if retry else {"timeout": STATUS_TIMEOUT, "max_retries": 0}
            page = self._client.with_options(**options).models.list()  # type: ignore[arg-type]
        except openai.OpenAIError as exc:
            raise self._explain(exc) from exc
        return [model.id for model in page.data]

    def model(self, available: list[str] | None = None) -> str:
        """The model to use: DAYTRACE_LLM_MODEL when it is loaded, otherwise the first one the server lists."""
        available = self.models() if available is None else available
        wanted = self.settings.model
        if wanted is not None:
            if wanted not in available:
                listed = ", ".join(available) or "none"
                raise LLMError(f"the model {wanted!r} is not loaded in the model server (loaded: {listed})")
            return wanted
        if not available:
            raise LLMError("the model server is running but has no model loaded; load one in LM Studio")
        return available[0]

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model: str | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        """One chat completion; returns the reply message (content, tool_calls). Raises LLMError."""
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        client = self._client if timeout is None else self._client.with_options(timeout=timeout)
        try:
            response = client.chat.completions.create(
                model=model or self.model(), messages=messages, temperature=temperature, **extra  # type: ignore[arg-type]
            )
        except openai.OpenAIError as exc:
            raise self._explain(exc) from exc
        if not response.choices:
            raise LLMError("the model server sent an empty answer")
        return response.choices[0].message

    def supports_tools(self, model: str) -> bool:
        """Whether `model` calls a tool when asked to. Checked once, then remembered for a while (it costs a reply)."""
        with self._lock:
            cached = self._tool_check
            if cached is not None and cached[0] == model and self._clock() - cached[2] < TOOL_CHECK_TTL_SECONDS:
                return cached[1]
            reply = self.chat(_PROBE_MESSAGES, tools=[_PROBE_TOOL], max_tokens=64, model=model, timeout=TOOL_CHECK_TIMEOUT)
            calls = getattr(reply, "tool_calls", None) or []
            works = any(getattr(call.function, "name", None) == "get_current_time" for call in calls)
            self._tool_check = (model, works, self._clock())
            return works

    def status(self) -> ModelStatus:
        """What the dashboard shows: never raises, the error explains any problem."""
        status = ModelStatus(base_url=self.settings.base_url, model=self.settings.model, reachable=False, tool_calling=None)
        try:
            status.models = self.models(retry=False)
        except LLMError as exc:
            status.error = str(exc)
            return status
        status.reachable = True
        try:
            status.model = self.model(status.models)
            status.tool_calling = self.supports_tools(status.model)
        except LLMError as exc:
            status.error = str(exc)
        return status

    def _explain(self, exc: openai.OpenAIError) -> LLMError:
        """The OpenAI client's error, in plain words. A refused address stays a refusal."""
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, LLMError):
                return cause
            cause = cause.__cause__ or cause.__context__
        if isinstance(exc, openai.APITimeoutError):
            return LLMError("the model server did not answer in time")
        if isinstance(exc, openai.APIConnectionError):
            return LLMError(
                f"no model server answers at {self.settings.base_url}; start the server in LM Studio (or Ollama)"
            )
        if isinstance(exc, openai.APIStatusError):
            return LLMError(f"the model server answered {exc.status_code}: {exc.message}"[:300])
        return LLMError(f"the model server could not be used: {exc}"[:300])
