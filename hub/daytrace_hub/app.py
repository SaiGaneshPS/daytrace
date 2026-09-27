"""FastAPI app factory.

Routers are added by their tickets (DT-11 onwards); DT-30 serves the dashboard at every other path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import __version__
from .api import API_PREFIX, error_response, install_error_handlers
from .api import ai as ai_api
from .api import categories as categories_api
from .api import devices as devices_api
from .api import events as events_api
from .api import insights as insights_api
from .api import privacy as privacy_api
from .api import streaks as streaks_api
from .api import timeline as timeline_api
from .auth import is_trusted_local
from .config import LEDGER, Settings, client_allowed, host_allowed, load_settings, network_of, origin_allowed
from .db import Database
from .discovery import Advertiser, listen_addresses
from .llm import LLM, load_llm_settings, model_networks
from .tracker.base import TrackerService, platform_probe

# 1008 = policy violation; closing before accept makes the server answer the handshake with 403.
WEBSOCKET_POLICY_VIOLATION = 1008
logger = logging.getLogger("daytrace_hub")


class NetworkGuard:
    """Refuses clients from networks the profile does not serve, Host names that could be DNS rebinding, and
    requests from other web sites' pages (DT-45), and writes each request, served or refused, in the network ledger.

    A plain ASGI middleware (not @app.middleware("http")) so WebSocket connections are checked too.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            refusal = self._check(scope)
            if refusal is not None:
                if scope["type"] == "http":
                    await refusal(scope, receive, send)
                else:
                    await WebSocketClose(code=WEBSOCKET_POLICY_VIOLATION)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope) -> JSONResponse | None:
        profile = self.settings.profile
        client = scope.get("client")
        address = client[0] if client else None
        where = network_of(address)
        headers = Headers(scope=scope)
        refusal = None
        if not client_allowed(address, profile, self.settings.lan_networks):
            refusal = error_response(
                403, "forbidden_network", f"the {profile.name} profile does not accept requests from this network"
            )
        elif not host_allowed(headers.get("host"), profile):
            refusal = error_response(
                403, "forbidden_host", "use the hub's IP address, its PC name or its .local name to reach it"
            )
        elif not origin_allowed(headers.get("origin"), headers.get("host"), where == "localhost"):
            refusal = error_response(
                403, "forbidden_origin", "another web page can't call the hub; open the dashboard from the hub itself"
            )
        if refusal is not None:
            LEDGER.refuse(where)
        else:
            LEDGER.served(where)
        return refusal


# The dashboard only talks to the hub it came from; inline styles are for charts, data: and blob: images for the
# Wrapped card export (DT-34).
DASHBOARD_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
DASHBOARD_HEADERS = {
    "Content-Security-Policy": DASHBOARD_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Set here, not guessed: Windows can map .js to text/plain in the registry, which browsers refuse for modules.
DASHBOARD_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".map": "application/json",
    ".webmanifest": "application/manifest+json", ".txt": "text/plain; charset=utf-8", ".xml": "application/xml",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm", ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".pdf": "application/pdf",
}
IMMUTABLE = "public, max-age=31536000, immutable"  # file names with a content hash: a new build has new names
NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daytrace hub</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem">
<h1>The Daytrace hub is running</h1>
<p>The dashboard has not been built yet. In the repository's <code>dashboard</code> folder, run
<code>npm ci</code> and then <code>npm run build</code>, and reload this page. If the hub was installed on its own,
set <code>DAYTRACE_DASHBOARD_DIR</code> to the built dashboard folder.</p>
<p>The API is at <a href="/api/v1/health">/api/v1/health</a>.</p>
</body></html>
"""


def default_dashboard_dir() -> Path:
    """dashboard/dist next to the hub in this repository (DAYTRACE_DASHBOARD_DIR, in Settings, overrides it)."""
    return Path(__file__).resolve().parents[2] / "dashboard" / "dist"


def route_path(scope: Scope) -> str:
    """The path without the app's root_path (behind a proxy started with --root-path), as Starlette's routes match."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    if not root or not path.startswith(root):
        return path
    rest = path[len(root):]
    return rest if rest.startswith("/") else ("" if not rest else path)


def not_modified(response_headers: Headers | Any, request_headers: Headers) -> bool:
    """Whether the browser's copy is current (If-None-Match, else If-Modified-Since), so a 304 does."""
    etag = response_headers.get("etag")
    if_none_match = request_headers.get("if-none-match")
    if if_none_match is not None:
        tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
        return "*" in tags or (etag is not None and etag.removeprefix("W/") in tags)
    since, modified = request_headers.get("if-modified-since"), response_headers.get("last-modified")
    if not since or not modified:
        return False
    try:
        return parsedate_to_datetime(since) >= parsedate_to_datetime(modified)
    except (TypeError, ValueError):
        return False


class Dashboard:
    """The built dashboard (`npm run build` in dashboard/) at every path no route takes (the router's default).

    - A file that exists is served. Build output named by its content (assets/, workbox-*.js) may be cached for a
      year; everything else (index.html, the service worker, the manifest) is checked again on every load, and an
      unchanged file answers 304.
    - A page load (a navigation) of any other path gets index.html, so reloading any page works, even one with a
      dot in it (/story/2026.09.25). Anything else asking for a missing file with an extension gets a 404, never
      index.html posing as a script. Methods other than GET and HEAD get a 405.
    - Unknown /api paths are left to the API's own 404.
    - A path is checked before the disk is touched: a backslash, a colon (a drive, a stream, a UNC share), a NUL or
      a .. segment is refused, so nothing outside the folder is read and no other machine is ever contacted. The
      disk is read in a worker thread, never on the event loop.
    - Every response carries the strict Content-Security-Policy above.
    - Before the first build, a short page says how to build it.
    """

    def __init__(self, folder: Path, api_not_found: ASGIApp) -> None:
        self.folder = folder
        self.api_not_found = api_not_found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=WEBSOCKET_POLICY_VIOLATION)(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path = route_path(scope)
        if path == "/api" or path.startswith("/api/"):
            await self.api_not_found(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            response: Response = error_response(405, "method_not_allowed", "Method Not Allowed",
                                                headers={**DASHBOARD_HEADERS, "Allow": "GET, HEAD"})
        else:
            response = await run_in_threadpool(self.page, path, Headers(scope=scope))
        await response(scope, receive, send)

    @staticmethod
    def parts(path: str) -> list[str] | None:
        """The path's segments, or None when it could leave the folder or name another machine."""
        if any(bad in path for bad in ("\\", ":", "\x00")):
            return None
        parts = [part for part in path.split("/") if part not in ("", ".")]
        return None if ".." in parts else parts

    def page(self, path: str, request_headers: Headers) -> Response:
        index = self.folder / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, headers=self.headers("no-cache"))
        parts = self.parts(path)
        if parts is None:
            return self.missing()
        if parts:
            target = self.folder.joinpath(*parts)
            if target.is_file():
                return self.file(target, request_headers)
            navigation = (request_headers.get("sec-fetch-mode") == "navigate"
                          or "text/html" in request_headers.get("accept", ""))
            if not navigation and "." in parts[-1]:
                return self.missing()
        return self.file(index, request_headers)

    def file(self, target: Path, request_headers: Headers) -> Response:
        name = target.relative_to(self.folder).as_posix()
        immutable = name.startswith("assets/") or re.fullmatch(r"workbox-[\w-]+\.js", name) is not None
        media_type = DASHBOARD_TYPES.get(target.suffix.lower(), "application/octet-stream")
        response = FileResponse(target, stat_result=target.stat(), media_type=media_type,
                                headers=self.headers(IMMUTABLE if immutable else "no-cache"))
        if not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response

    def missing(self) -> Response:
        return error_response(404, "not_found", "Not Found", headers=dict(DASHBOARD_HEADERS))

    @staticmethod
    def headers(cache: str) -> dict[str, str]:
        return {**DASHBOARD_HEADERS, "Cache-Control": cache}


class Health(BaseModel):
    status: str
    profile: str
    version: str
    local: bool = Field(
        description="True when the request comes from the hub computer's own dashboard, which can show pairing codes "
        "and revoke devices (the same check those endpoints make)."
    )


def desktop_tracker(settings: Settings, database: Database) -> TrackerService | None:
    """This computer's tracker for the profile, or None on a platform without one yet (macOS: DT-17)."""
    found = platform_probe()
    if found is None:
        return None
    probe, device_type = found
    return TrackerService(database, probe, device_type, socket.gethostname(), settings.tracker_lock_path)


def create_app(settings: Settings | None = None, llm: LLM | None = None, dashboard_dir: Path | None = None) -> FastAPI:
    """Build the hub for one profile. The database is created and migrated when the app starts.

    `llm` is the local model server (DT-37); by default the one DAYTRACE_LLM_BASE_URL names. Tests pass a fake.
    `dashboard_dir` is the built dashboard (DT-30); by default DAYTRACE_DASHBOARD_DIR, else `default_dashboard_dir()`.
    """
    settings = settings or load_settings()
    database = Database(settings.database_path)
    model_server = llm or LLM(load_llm_settings(), networks=model_networks(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        advertiser = Advertiser(settings) if settings.advertise_mdns else None
        if advertiser is not None:
            advertiser.start()  # in the background: the hub serves right away
        tracker = desktop_tracker(settings, database) if settings.track_desktop else None
        if tracker is not None:
            tracker.start()  # DT-16: this computer's own screen, in a background thread
        try:
            yield
        finally:
            if tracker is not None:
                await asyncio.to_thread(tracker.stop)
            if advertiser is not None:
                await advertiser.stop()
            model_server.close()

    app = FastAPI(
        title=f"Daytrace hub ({settings.profile.name})",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.pairing = devices_api.PairingCodes()
    app.state.llm = model_server
    app.add_middleware(NetworkGuard, settings=settings)
    install_error_handlers(app)

    @app.get(f"{API_PREFIX}/health", response_model=Health, tags=["hub"], summary="Is the hub up (no auth)")
    def health(request: Request) -> Health:
        return Health(status="ok", profile=settings.profile.name, version=__version__, local=is_trusted_local(request))

    app.include_router(events_api.router)
    app.include_router(devices_api.router)
    app.include_router(timeline_api.router)
    app.include_router(categories_api.router)
    app.include_router(ai_api.router)
    app.include_router(insights_api.router)
    app.include_router(streaks_api.router)
    app.include_router(privacy_api.router)
    # What no route takes is the dashboard's: as the router's default, not a route, so a route added later still
    # comes first and a wrong method on an API path is still the API's 405.
    folder = dashboard_dir or settings.dashboard_dir or default_dashboard_dir()
    app.router.default = Dashboard(folder, api_not_found=app.router.not_found)
    return app


# --- listening only where the profile is reached (DT-45) ----------------------------------------------------------


def bind_listener(address: str, port: int) -> socket.socket:
    """A TCP socket bound to one address. On Windows no other program may bind the same address and port while
    the hub holds it (SO_EXCLUSIVEADDRUSE); elsewhere a restart can bind again at once (SO_REUSEADDR)."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
    except OSError:
        sock.close()
        raise
    return sock


def _shown(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


class HubServer(uvicorn.Server):
    """uvicorn, listening only on the addresses `find_addresses` gives (loopback, the LAN, and Tailscale only for
    profiles that accept it), never on every interface. The addresses are checked again every `check_every`
    seconds: a new Wi-Fi or DHCP address gets a listener, one that went away is closed. The app keeps running (its
    lifespan, the tracker and the model client are not restarted).

    Loopback that can't be bound (the port is taken) stops the start, as uvicorn would; another address that
    can't be bound is skipped for now and tried again at the next check."""

    def __init__(self, config: Any, find_addresses: Callable[[], list[str]], check_every: float = 15.0) -> None:
        super().__init__(config)
        self.find_addresses = find_addresses
        self.check_every = check_every
        self.listening: dict[str, Any] = {}  # address -> its asyncio server

    def _bind_all(self, addresses: list[str]) -> list[tuple[str, socket.socket]]:
        bound = []
        for address in addresses:
            try:
                bound.append((address, bind_listener(address, self.config.port)))
            except OSError as error:
                if address == "127.0.0.1":
                    raise SystemExit(f"can't listen on {_shown(address, self.config.port)}: {error}") from error
                logger.warning("not listening on %s for now: %s", _shown(address, self.config.port), error)
        return bound

    def _protocol(self, _loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Protocol:
        # The same protocol uvicorn's startup() makes for each listener.
        return self.config.http_protocol_class(config=self.config, server_state=self.server_state,
                                               app_state=self.lifespan.state, _loop=_loop)

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        bound = self._bind_all(self.find_addresses())
        await super().startup(sockets=[sock for _, sock in bound])
        self.listening = {address: server for (address, _), server in zip(bound, self.servers, strict=True)}
        self._record()

    def _record(self) -> None:
        LEDGER.listen(_shown(address, self.config.port) for address in self.listening)
        logger.info("listening on %s", ", ".join(_shown(address, self.config.port) for address in self.listening))

    async def refresh(self) -> None:
        """Listen on the addresses there are now: new ones added, gone ones closed (their open connections finish)."""
        wanted = set(self.find_addresses())
        changed = False
        for address in [address for address in self.listening if address not in wanted]:
            server = self.listening.pop(address)
            server.close()
            self.servers.remove(server)
            changed = True
        loop = asyncio.get_running_loop()
        for address, sock in self._bind_all([address for address in wanted if address not in self.listening]):
            server = await loop.create_server(self._protocol, sock=sock, ssl=self.config.ssl, backlog=self.config.backlog)
            self.servers.append(server)
            self.listening[address] = server
            changed = True
        if changed:
            self._record()

    async def on_tick(self, counter: int) -> bool:
        if counter and counter % max(1, round(self.check_every * 10)) == 0:  # uvicorn ticks every 0.1 s
            try:
                await self.refresh()
            except (OSError, SystemExit) as error:  # keep serving where it already listens
                logger.warning("could not update where the hub listens: %s", error)
        return await super().on_tick(counter)


def serve(settings: Settings) -> None:
    """Run the hub for one profile until it is stopped (Ctrl+C), listening only where the profile is reached."""
    # proxy_headers=False: the network check must see the real peer address, never an X-Forwarded-For value.
    # A connection that never closes (a phone that dropped off the Wi-Fi) must not keep Ctrl+C waiting.
    config = uvicorn.Config(create_app(settings), port=settings.profile.port, log_level="info", proxy_headers=False,
                            timeout_graceful_shutdown=5)
    HubServer(config, lambda: listen_addresses(settings)).run()
