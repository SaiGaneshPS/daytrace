"""DT-11: Bearer-token device authentication.

Every paired device (phone, Shortcuts, Mac bridge, browser extension, dashboard viewer) has its own random
token. The hub stores only a SHA-256 hash of it: tokens are 256-bit random values, so a fast hash is enough
and a copied database does not reveal any token. DT-12 hands tokens out through pairing.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, Request

from .api import ApiError
from .db import Database, utc_text
from .models import DEVICE_ID_PATTERN

TOKEN_PREFIX = "dt_"
DEVICE_TYPES = ("windows", "macos", "android", "ios", "browser", "viewer")  # matches devices.device_type
LAST_SEEN_EVERY = timedelta(seconds=60)  # how often a busy device's last_seen is written
_BEARER = re.compile(r"^\s*Bearer\s+(?P<token>[!-~]{1,200})\s*$", re.IGNORECASE)
_DEVICE_ID = re.compile(DEVICE_ID_PATTERN)


@dataclass(frozen=True)
class AuthenticatedDevice:
    device_id: str
    name: str
    device_type: str

    @property
    def is_viewer(self) -> bool:
        return self.device_type == "viewer"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_device(conn: sqlite3.Connection, *, device_id: str, name: str, device_type: str) -> str:
    """Add a device and return its token. The token is shown once and never stored in plain text."""
    if not _DEVICE_ID.fullmatch(device_id):
        raise ValueError("device_id must be 1 to 64 letters, digits, dots, dashes or underscores")
    if device_type not in DEVICE_TYPES:
        raise ValueError(f"device_type must be one of {', '.join(DEVICE_TYPES)}")
    token = new_token()
    conn.execute(
        "INSERT INTO devices (device_id, name, device_type, token_hash, paired_at) VALUES (?, ?, ?, ?, ?)",
        (device_id, name, device_type, hash_token(token), utc_text(datetime.now(UTC))),
    )
    return token


def device_for_token(conn: sqlite3.Connection, token: str) -> AuthenticatedDevice | None:
    """The active device this token belongs to, or None (unknown or revoked)."""
    presented = hash_token(token)
    row = conn.execute(
        "SELECT device_id, name, device_type, token_hash, last_seen FROM devices"
        " WHERE token_hash = ? AND revoked_at IS NULL",
        (presented,),
    ).fetchone()
    # The lookup is by hash, so timing reveals nothing about the token; compare_digest is belt and braces.
    if row is None or not hmac.compare_digest(row["token_hash"], presented):
        return None
    now = datetime.now(UTC)
    if row["last_seen"] is None or row["last_seen"] < utc_text(now - LAST_SEEN_EVERY):
        conn.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (utc_text(now), row["device_id"]))
    return AuthenticatedDevice(row["device_id"], row["name"], row["device_type"])


def get_database(request: Request) -> Database:
    return request.app.state.db


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


def require_device(
    database: Annotated[Database, Depends(get_database)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedDevice:
    """FastAPI dependency: the device behind `Authorization: Bearer <token>`, or 401."""
    if not authorization:
        raise _unauthorized("send the device token as Authorization: Bearer <token>")
    match = _BEARER.match(authorization)
    if not match:
        raise _unauthorized("the Authorization header must be: Bearer <token>")
    with database.connect() as conn:
        device = device_for_token(conn, match["token"])
    if device is None:
        raise _unauthorized("unknown or revoked token; pair this device again")
    return device


CurrentDevice = Annotated[AuthenticatedDevice, Depends(require_device)]
