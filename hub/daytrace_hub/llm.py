"""DT-37: client for a local OpenAI-compatible model server (LM Studio, Ollama).

The hub only ever talks to a model on this computer or your LAN (and, for profiles without real data, your
tailnet), never the internet:

- `LocalOnlyBackend` does the connecting: it resolves the name itself, checks every address it resolves to, and
  opens the socket to a checked address (trying each in turn). Nothing resolves the name a second time, so it
  cannot be pointed elsewhere in between (DNS rebinding), for http and https alike (TLS still verifies the name).
- `LocalOnlyTransport` refuses a literal non-local address before anything happens, and sends only the headers a
  model server needs: the OpenAI SDK would otherwise pass on credentials it finds in OPENAI_* variables.
- No proxy from the environment, no redirects.

`LLM.chat()` is what the day story (DT-39) and "Ask your day" (DT-40) use. `LLM.status()` feeds
GET /api/v1/ai/status: whether a model server answers, which model is used, and whether it can call tools.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
import openai
from openai import OpenAI

from .config import DEFAULT_LAN_NETWORKS, LOOPBACK_NETWORKS, TAILSCALE_NETWORKS, IPNetwork, Settings, parse_ip

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"  # LM Studio; Ollama serves http://127.0.0.1:11434/v1
# Connecting is quick on a LAN; a long answer from a big model is not.
TIMEOUT = httpx.Timeout(connect=3.0, read=120.0, write=10.0, pool=5.0)
STATUS_TIMEOUT = httpx.Timeout(5.0)
TOOL_CHECK_TIMEOUT = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=5.0)  # may include loading the model
TOOL_CHECK_TTL_SECONDS = 600.0
TOOL_CHECK_RETRY_SECONDS = 60.0  # after a check that could not tell
MODEL_TTL_SECONDS = 60.0
MAX_RETRIES = 1
PROBE_MAX_TOKENS = 1024  # reasoning models think before they call the tool
# Cloud metadata services sit at link-local and unique-local addresses; a model server never does.
METADATA_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("169.254.169.254/32", "169.254.170.2/32", "fd00:ec2::254/128"))
# The only headers a model server gets. The SDK's telemetry and anything it picked up from OPENAI_CUSTOM_HEADERS,
# OPENAI_ORG_ID or OPENAI_PROJECT_ID (possibly a real OpenAI key) are dropped.
SENT_HEADERS = frozenset({"host", "accept", "accept-encoding", "content-type", "content-length", "connection", "user-agent"})

Resolver = Callable[[str, int], list[str]]


class LLMError(Exception):
    """The model server can't be used right now. The message says why, in plain words."""


class LLMRefused(LLMError):
    """The model server's address is not allowed (not this computer or your LAN), so nothing was sent."""


class LLMStatusError(LLMError):
    """The model server answered with an HTTP error."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LLMSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None  # None: the first chat model the server lists
    problem: str | None = None  # why base_url can't be used (a typo, say); shown in the status, the hub still runs


def redact_url(url: str) -> str:
    """The URL without any user name or password, for the screen and for messages."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def load_llm_settings(env: Mapping[str, str] | None = None) -> LLMSettings:
    """DAYTRACE_LLM_BASE_URL and DAYTRACE_LLM_MODEL. Never raises: a bad address is reported by the status (the
    rest of the hub must start anyway). The address itself is checked on every connection."""
    env = os.environ if env is None else env
    base_url = env.get("DAYTRACE_LLM_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
    model = env.get("DAYTRACE_LLM_MODEL", "").strip() or None
    parts = urlsplit(base_url)
    problem = None
    try:
        valid = parts.scheme in ("http", "https") and bool(parts.hostname) and (parts.port or 1) > 0
    except ValueError:  # a port that is not a number
        valid = False
    if not valid:
        problem = f"DAYTRACE_LLM_BASE_URL must be an address like {DEFAULT_BASE_URL}, not {redact_url(base_url)!r}"
    elif parts.username or parts.password:
        problem = "DAYTRACE_LLM_BASE_URL must not contain a user name or password"
    return LLMSettings(base_url=base_url, model=model, problem=problem)


LOCAL_NETWORKS = (*LOOPBACK_NETWORKS, *DEFAULT_LAN_NETWORKS)


def model_networks(settings: Settings | None = None) -> tuple[IPNetwork, ...]:
    """Where the model may be for a profile: this computer, the profile's LAN ranges (DAYTRACE_LAN_NETWORKS narrows
    them) and, only for profiles without real data, the tailnet. Your own data never goes over Tailscale, whose
    range is also carrier-grade NAT space shared with other customers of some ISPs."""
    if settings is None:
        return LOCAL_NETWORKS
    tailnet = TAILSCALE_NETWORKS if settings.profile.seedable else ()
    return (*LOOPBACK_NETWORKS, *settings.lan_networks, *tailnet)


def address_allowed(text: str, networks: Iterable[IPNetwork] = LOCAL_NETWORKS) -> bool:
    """Inside `networks` and not a cloud metadata address (IPv4-mapped IPv6 counts as the IPv4 address). Tailscale
    addresses count only when `networks` includes the tailnet: its IPv6 range sits inside the private fc00::/7, so
    it is checked first, the way client_allowed() does for incoming requests."""
    address = parse_ip(text.strip("[]"))
    if address is None or any(address in network for network in METADATA_NETWORKS):
        return False
    networks = tuple(networks)
    if any(address in network for network in TAILSCALE_NETWORKS):
        return all(network in networks for network in TAILSCALE_NETWORKS)
    return any(address in network for network in networks)


def system_resolver(host: str, port: int) -> list[str]:
    """Every address for `host`, in the system's order; IPv6 link-local ones keep their scope (fe80::1%12)."""
    found: list[str] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(sockaddr[0])
        if family == socket.AF_INET6 and len(sockaddr) >= 4 and sockaddr[3] and "%" not in address:
            address = f"{address}%{sockaddr[3]}"
        found.append(address)
    return list(dict.fromkeys(found))


def _refused(host: str, address: str | None = None) -> LLMRefused:
    where = f"{host} points to {address}, which" if address and address != host else host
    return LLMRefused(f"{where} is not on this computer or your LAN; Daytrace only uses a local model")


def checked_addresses(host: str, port: int, networks: Iterable[IPNetwork], resolve: Resolver = system_resolver) -> list[str]:
    """Every address `host` stands for. Raises LLMRefused unless all of them are allowed."""
    networks = tuple(networks)
    if parse_ip(host.strip("[]")) is not None:
        if not address_allowed(host, networks):
            raise _refused(host)
        return [host.strip("[]")]
    try:
        addresses = resolve(host, port)
    except OSError as exc:
        raise LLMError(f"can't find the model server {host!r} on the network") from exc
    if not addresses:
        raise LLMError(f"can't find the model server {host!r} on the network")
    for address in addresses:
        if not address_allowed(address, networks):
            raise _refused(host, address)
    return addresses


class LocalOnlyBackend(httpcore.SyncBackend):
    """Opens connections only to checked addresses (see the module docstring)."""

    def __init__(self, networks: Iterable[IPNetwork], resolve: Resolver = system_resolver) -> None:
        self._networks = tuple(networks)
        self._resolve = resolve

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        error: Exception | None = None
        for address in checked_addresses(host, port, self._networks, self._resolve):
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)  # type: ignore[arg-type]
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:  # e.g. ::1 first, server on IPv4 only
                error = exc
        raise error  # type: ignore[misc]  # checked_addresses never returns an empty list

    def connect_unix_socket(self, *_: Any, **__: Any) -> httpcore.NetworkStream:
        raise LLMRefused("Daytrace talks to the model server over TCP only")


class LocalOnlyTransport(httpx.BaseTransport):
    """The model guard as an httpx transport. `inner` is for tests (a fake server); by default it is httpx's own
    transport connecting through `LocalOnlyBackend`."""

    def __init__(
        self,
        networks: Iterable[IPNetwork] = LOCAL_NETWORKS,
        resolve: Resolver = system_resolver,
        inner: httpx.BaseTransport | None = None,
    ) -> None:
        self._networks = tuple(networks)
        if inner is None:
            inner = httpx.HTTPTransport()
            pool = getattr(inner, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise RuntimeError("this httpx version hides its connection pool, so the model guard can't be installed")
            pool._network_backend = LocalOnlyBackend(self._networks, resolve)
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if parse_ip(host) is not None and not address_allowed(host, self._networks):
            raise _refused(host)  # clear and immediate; names are checked when connecting
        for name in list(request.headers.keys()):
            if name.lower() not in SENT_HEADERS:
                del request.headers[name]
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def local_http_client(networks: Iterable[IPNetwork] = LOCAL_NETWORKS, resolve: Resolver = system_resolver) -> httpx.Client:
    """The only way the hub talks to a model: the guard, no proxy from the environment, no redirects."""
    return httpx.Client(
        transport=LocalOnlyTransport(networks, resolve), timeout=TIMEOUT, trust_env=False, follow_redirects=False
    )


@dataclass
class ModelStatus:
    base_url: str
    model: str | None
    reachable: bool
    tool_calling: bool | None  # None: unknown (no usable model, or the check could not tell yet)
    models: list[str] = field(default_factory=list)
    error: str | None = None


_PROBE_NAME = "get_current_time"
# A tool the probe asks the model to call. Any model that handles tools calls it for this prompt.
_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _PROBE_NAME,
        "description": "Returns the current time on the user's computer.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}
_PROBE_MESSAGES = [{"role": "user", "content": f"What time is it right now? Use the {_PROBE_NAME} tool."}]


def _plain(text: object, limit: int = 200) -> str:
    """Text from the model server, safe to show: printable characters only, and short."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:limit]


class LLM:
    """One local model server. Safe to share between threads (FastAPI runs sync endpoints in a thread pool)."""

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.Client | None = None,
        networks: Iterable[IPNetwork] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._http = http_client or local_http_client(LOCAL_NETWORKS if networks is None else networks)
        self._client: OpenAI | None = None
        if settings.problem is None:
            self._client = OpenAI(
                api_key="local",  # local servers ignore it (and the guard never sends it), but the SDK insists
                base_url=settings.base_url,
                http_client=self._http,
                max_retries=MAX_RETRIES,
                timeout=TIMEOUT,
            )
        self._clock = clock
        self._lock = threading.Lock()
        self._tool_checks: dict[str, tuple[bool | None, float]] = {}  # model -> (result, when)
        self._probing: set[str] = set()
        self._chosen: tuple[str, float] | None = None  # the model picked last, and when

    @property
    def shown_url(self) -> str:
        return redact_url(self.settings.base_url)

    def close(self) -> None:
        self._http.close()

    def _sdk(self) -> OpenAI:
        if self._client is None:
            raise LLMError(self.settings.problem or "the model server address is not usable")
        return self._client

    def models(self, retry: bool = True) -> list[str]:
        """GET /models: the models the server offers (the health check). The status check asks only once, so the
        dashboard hears quickly that nothing is running (Windows takes about 2 s to refuse a closed port)."""
        options: dict[str, Any] = {"timeout": STATUS_TIMEOUT} if retry else {"timeout": STATUS_TIMEOUT, "max_retries": 0}
        try:
            page = self._sdk().with_options(**options).models.list()
            return [str(model.id) for model in page.data]
        except openai.OpenAIError as exc:
            raise self._explain(exc) from exc
        except LLMError:
            raise
        except Exception as exc:  # a 200 that is not a model list: some other program on that port
            raise LLMError(f"what answers at {self.shown_url} is not an OpenAI-compatible model server") from exc

    def model(self, available: list[str] | None = None) -> str:
        """The model to use: DAYTRACE_LLM_MODEL when the server offers it, otherwise the first chat model it lists
        (embedding models, which servers list too, can't hold a conversation)."""
        available = self.models() if available is None else available
        wanted = self.settings.model
        if wanted is not None:
            if wanted not in available:
                listed = _plain(", ".join(available) or "none")
                raise LLMError(f"the model {wanted!r} is not loaded in the model server (loaded: {listed})")
            chosen = wanted
        else:
            chat_models = [name for name in available if "embed" not in name.lower()]
            if not chat_models:
                raise LLMError("the model server is running but has no chat model loaded; load one in LM Studio")
            chosen = chat_models[0]
        with self._lock:
            self._chosen = (chosen, self._clock())
        return chosen

    def current_model(self) -> str:
        """The model to use, asking the server at most once a minute (not on every turn of a conversation)."""
        with self._lock:
            chosen = self._chosen
        if chosen is not None and self._clock() - chosen[1] < MODEL_TTL_SECONDS:
            return chosen[0]
        return self.model()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        model: str | None = None,
        timeout: httpx.Timeout | None = None,
        retry: bool = True,
    ) -> Any:
        """One chat completion; returns the reply message (content, tool_calls). Raises LLMError."""
        extra: dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        if not retry:
            options["max_retries"] = 0
        try:
            client = self._sdk().with_options(**options) if options else self._sdk()
            response = client.chat.completions.create(
                model=model or self.current_model(), messages=messages, temperature=temperature, **extra  # type: ignore[arg-type]
            )
            return response.choices[0].message
        except openai.OpenAIError as exc:
            raise self._explain(exc) from exc
        except LLMError:
            raise
        except Exception as exc:  # a 200 that is not a chat completion
            raise LLMError(f"the model server at {self.shown_url} sent an answer Daytrace doesn't understand") from exc

    def supports_tools(self, model: str) -> bool | None:
        """Whether `model` calls a tool when asked to (None: couldn't tell). It costs a reply, so the answer is
        remembered for 10 minutes (a failed check for 1), and only one check runs at a time: meanwhile the status
        answers at once instead of waiting."""
        with self._lock:
            cached = self._tool_checks.get(model)
            if cached is not None:
                ttl = TOOL_CHECK_TTL_SECONDS if cached[0] is not None else TOOL_CHECK_RETRY_SECONDS
                if self._clock() - cached[1] < ttl:
                    return cached[0]
            if model in self._probing:
                return None
            self._probing.add(model)
        result: bool | None = None
        try:
            reply = self.chat(
                _PROBE_MESSAGES, tools=[_PROBE_TOOL], max_tokens=PROBE_MAX_TOKENS, model=model,
                timeout=TOOL_CHECK_TIMEOUT, retry=False,
            )
            calls = getattr(reply, "tool_calls", None) or []
            result = any(getattr(getattr(call, "function", None), "name", None) == _PROBE_NAME for call in calls)
        except LLMStatusError as exc:
            result = False if 400 <= exc.status_code < 500 else None  # "does not support tools" is an answer
        except LLMError:
            result = None
        finally:
            with self._lock:
                self._probing.discard(model)
                self._tool_checks[model] = (result, self._clock())
        return result

    def status(self) -> ModelStatus:
        """What the dashboard shows. Never raises: `error` explains any problem."""
        status = ModelStatus(base_url=self.shown_url, model=self.settings.model, reachable=False, tool_calling=None)
        try:
            status.models = self.models(retry=False)
            status.reachable = True
            status.model = self.model(status.models)
            status.tool_calling = self.supports_tools(status.model)
            if status.tool_calling is None:
                status.error = "couldn't tell yet whether the model can call tools; it is checked again shortly"
        except LLMError as exc:  # models(), model() and chat() turn every failure, odd replies included, into this
            status.error = str(exc)
        return status

    def _explain(self, exc: openai.OpenAIError) -> LLMError:
        """The OpenAI SDK's error, in plain words. A refused address stays a refusal."""
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, LLMError):
                return cause
            cause = cause.__cause__ or cause.__context__
        if isinstance(exc, openai.APITimeoutError):
            return LLMError("the model server did not answer in time")
        if isinstance(exc, openai.APIConnectionError):
            return LLMError(f"no model server answers at {self.shown_url}; start the server in LM Studio (or Ollama)")
        if isinstance(exc, openai.APIStatusError):
            return LLMStatusError(f"the model server answered {exc.status_code}: {_plain(exc.message)}", exc.status_code)
        return LLMError(f"the model server could not be used: {_plain(exc)}")
