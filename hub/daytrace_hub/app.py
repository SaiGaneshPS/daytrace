"""FastAPI app factory.

Routers are added by their tickets (DT-11 onwards); DT-30 serves the dashboard at every other path.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import Headers, URLPath
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import __version__
from .api import API_PREFIX, error_response, install_error_handlers
from .api import ai as ai_api
from .api import categories as categories_api
from .api import devices as devices_api
from .api import events as events_api
from .api import timeline as timeline_api
from .config import Settings, client_allowed, host_allowed, load_settings
from .db import Database
from .discovery import Advertiser
from .llm import LLM, load_llm_settings, model_networks
from .tracker.base import TrackerService, platform_probe

# 1008 = policy violation; closing before accept makes the server answer the handshake with 403.
WEBSOCKET_POLICY_VIOLATION = 1008


class NetworkGuard:
    """Refuses clients from networks the profile does not serve, and Host names that could be DNS rebinding.

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
        if not client_allowed(client[0] if client else None, profile, self.settings.lan_networks):
            return error_response(
                403, "forbidden_network", f"the {profile.name} profile does not accept requests from this network"
            )
        if not host_allowed(Headers(scope=scope).get("host"), profile):
            return error_response(
                403, "forbidden_host", "use the hub's IP address, its PC name or its .local name to reach it"
            )
        return None


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
    ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".webp": "image/webp", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}
IMMUTABLE = "public, max-age=31536000, immutable"  # file names with a content hash: a new build has new names
NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daytrace hub</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem">
<h1>The Daytrace hub is running</h1>
<p>The dashboard has not been built yet. In the repository's <code>dashboard</code> folder, run
<code>npm ci</code> and then <code>npm run build</code>, and reload this page.</p>
<p>The API is at <a href="/api/v1/health">/api/v1/health</a>.</p>
</body></html>
"""


def default_dashboard_dir() -> Path:
    """DAYTRACE_DASHBOARD_DIR, else dashboard/dist next to the hub in this repository."""
    configured = os.environ.get("DAYTRACE_DASHBOARD_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "dashboard" / "dist"


class Dashboard:
    """The built dashboard (`npm run build` in dashboard/) at every path the API doesn't use.

    - A file that exists is served. Build output named by its content (assets/, workbox-*.js) may be cached for a
      year; everything else (index.html, the service worker, the manifest) is checked again on every load.
    - Any other path without a file extension is a page of the app (/insights), so it gets index.html and
      reloading any page works. A missing file with an extension is a 404, never index.html posing as a script.
    - Methods other than GET and HEAD get a 405. /api is never the dashboard's (see DashboardRoute).
    - Paths never leave the folder, and every response carries the strict Content-Security-Policy above.
    - Before the first build, a short page says how to build it.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder.resolve()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] not in ("GET", "HEAD"):
            response: Response = error_response(405, "method_not_allowed", "Method Not Allowed", headers={"Allow": "GET, HEAD"})
        else:
            response = self.page(scope["path"])
        await response(scope, receive, send)

    def page(self, path: str) -> Response:
        index = self.folder / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, headers=self.headers("no-cache"))
        relative = path.lstrip("/")
        if relative:
            try:
                target: Path | None = (self.folder / relative).resolve()
            except (OSError, ValueError):  # a NUL byte, a name Windows can't have
                target = None
            if target is not None and target.is_relative_to(self.folder) and target.is_file():
                return self.file(target)
            if "." in relative.rsplit("/", 1)[-1]:
                return error_response(404, "not_found", "Not Found")
        return self.file(index)

    def file(self, target: Path) -> FileResponse:
        name = target.relative_to(self.folder).as_posix()
        immutable = name.startswith("assets/") or re.fullmatch(r"workbox-[\w-]+\.js", name) is not None
        media_type = DASHBOARD_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=media_type, headers=self.headers(IMMUTABLE if immutable else "no-cache"))

    @staticmethod
    def headers(cache: str) -> dict[str, str]:
        return {**DASHBOARD_HEADERS, "Cache-Control": cache}


class DashboardRoute(BaseRoute):
    """The last route: every HTTP path outside /api goes to the dashboard. /api paths are left to the API, so an
    unknown one is the API's JSON 404 and a wrong method on a real one its 405 (a catch-all mount would swallow
    both)."""

    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path = scope.get("path", "")
        if scope["type"] == "http" and path != "/api" and not path.startswith("/api/"):
            return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.dashboard(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        raise NoMatchFound(name, path_params)


class Health(BaseModel):
    status: str
    profile: str
    version: str


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
    `dashboard_dir` is the built dashboard (DT-30); by default `default_dashboard_dir()`.
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
    def health() -> Health:
        return Health(status="ok", profile=settings.profile.name, version=__version__)

    app.include_router(events_api.router)
    app.include_router(devices_api.router)
    app.include_router(timeline_api.router)
    app.include_router(categories_api.router)
    app.include_router(ai_api.router)
    app.router.routes.append(DashboardRoute(Dashboard(dashboard_dir or default_dashboard_dir())))  # last of all
    return app
