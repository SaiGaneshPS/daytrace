"""FastAPI app factory.

Routers are added by their tickets (DT-11 onwards); DT-30 mounts the dashboard.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings, client_allowed, load_settings
from .db import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the hub for one profile. The database is created and migrated when the app starts."""
    settings = settings or load_settings()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        yield

    app = FastAPI(
        title=f"Daytrace hub ({settings.profile.name})",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database

    @app.middleware("http")
    async def only_allowed_networks(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.client.host if request.client else None
        if not client_allowed(host, settings.profile):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "forbidden_network",
                        "message": f"the {settings.profile.name} profile does not accept requests from this network",
                        "details": [],
                    }
                },
            )
        return await call_next(request)

    return app
