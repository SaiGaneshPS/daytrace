"""DT-11: API routers package, plus the error shape every endpoint uses.

Errors always look like {"error": {"code": ..., "message": ..., "details": [...]}} (docs/api.md, Errors).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

API_PREFIX = "/api/v1"
logger = logging.getLogger("daytrace_hub")

_CODES_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "batch_too_large",
    422: "invalid_request",
}


class ApiError(Exception):
    """Raise from any endpoint or dependency to answer with the documented error shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers


def error_response(
    status_code: int, code: str, message: str, details: list[Any] | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or []}},
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    async def http_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        code = _CODES_BY_STATUS.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None))

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        details = [
            f"{'.'.join(str(part) for part in problem.get('loc', ()))}: {problem.get('msg', 'invalid')}"
            for problem in exc.errors()
        ]
        return error_response(422, "invalid_request", "the request is not valid", details)

    async def database_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, sqlite3.OperationalError)
        if exc.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            # Another writer held the database past the busy timeout: safe to retry the same request.
            return error_response(503, "busy", "the hub is busy, try again in a moment", headers={"Retry-After": "1"})
        logger.exception("database error", exc_info=exc)
        return error_response(500, "internal_error", "the hub hit an unexpected error")

    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected error", exc_info=exc)
        return error_response(500, "internal_error", "the hub hit an unexpected error")

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(sqlite3.OperationalError, database_error)
    app.add_exception_handler(Exception, unexpected_error)
