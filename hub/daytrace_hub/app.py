"""FastAPI app factory.

Routers are added by their tickets (DT-11 onwards); DT-30 mounts the dashboard.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import __version__
from .api import API_PREFIX, error_response, install_error_handlers
from .api import devices as devices_api
from .api import events as events_api
from .config import Settings, client_allowed, host_allowed, load_settings
from .db import Database
from .discovery import Advertiser

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
                403, "forbidden_host", "use the hub's IP address, its PC name or daytrace-hub.local to reach it"
            )
        return None


class Health(BaseModel):
    status: str
    profile: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the hub for one profile. The database is created and migrated when the app starts."""
    settings = settings or load_settings()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        advertiser = Advertiser(settings) if settings.advertise_mdns else None
        if advertiser is not None:
            await advertiser.start()
        try:
            yield
        finally:
            if advertiser is not None:
                await advertiser.stop()

    app = FastAPI(
        title=f"Daytrace hub ({settings.profile.name})",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.pairing = devices_api.PairingCodes()
    app.add_middleware(NetworkGuard, settings=settings)
    install_error_handlers(app)

    @app.get(f"{API_PREFIX}/health", response_model=Health, tags=["hub"], summary="Is the hub up (no auth)")
    def health() -> Health:
        return Health(status="ok", profile=settings.profile.name, version=__version__)

    app.include_router(events_api.router)
    app.include_router(devices_api.router)
    return app
