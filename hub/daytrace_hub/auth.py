"""DT-11: Bearer-token device authentication (DT-12 adds local-only and reader access).

Every paired device (phone, Shortcuts, Mac bridge, browser extension, dashboard viewer) has its own random
token. The hub stores only a SHA-256 hash of it: tokens are 256-bit random values, so a fast hash is enough
and a copied database does not reveal any token. DT-12 hands tokens out through pairing.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, Request

from .api import ApiError
from .db import BUSY_TIMEOUT_MS, Database, utc_text
from .models import DEVICE_ID_PATTERN

TOKEN_PREFIX = "dt_"
DEVICE_TYPES = ("windows", "macos", "android", "ios", "browser", "viewer")  # matches devices.device_type
LAST_SEEN_EVERY = timedelta(seconds=60)  # how often a busy device's last_seen is written
LAST_SEEN_WAIT_MS = 100  # how long the last_seen write may wait for another writer
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
    """The active device this token belongs to, or None (unknown or revoked).

    The lookup is by the token's SHA-256, so response timing reveals nothing useful about the token.
    """
    row = conn.execute(
        "SELECT device_id, name, device_type, last_seen FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    _touch_last_seen(conn, row["device_id"], row["last_seen"])
    return AuthenticatedDevice(row["device_id"], row["name"], row["device_type"])


def _touch_last_seen(conn: sqlite3.Connection, device_id: str, last_seen: str | None) -> None:
    """Best effort: record when a device was last seen, at most once a minute.

    Never fails a request: if another writer holds the database, the update is skipped (it waits at most
    LAST_SEEN_WAIT_MS instead of the full busy timeout). A last_seen in the future (the clock was
    corrected backwards) is overwritten too.
    """
    now = datetime.now(UTC)
    now_text = utc_text(now)
    if last_seen is not None and utc_text(now - LAST_SEEN_EVERY) <= last_seen <= now_text:
        return
    conn.execute(f"PRAGMA busy_timeout = {LAST_SEEN_WAIT_MS}")
    try:
        conn.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now_text, device_id))
    except sqlite3.OperationalError:
        pass  # busy: try again on the next request
    finally:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


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


def is_local_request(request: Request) -> bool:
    """True when the request comes from the hub computer itself (loopback)."""
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def require_local(request: Request) -> None:
    """FastAPI dependency for local-only endpoints (pairing codes, revoking, delete-all)."""
    if not is_local_request(request):
        raise ApiError(403, "local_only", "this can only be done on the hub computer itself")


def require_reader(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedDevice | None:
    """FastAPI dependency for dashboard data: any paired device's token, or no token from the hub computer.

    The dashboard on the hub computer itself is trusted (DT-30); pages from other sites cannot read the
    answers (no CORS headers) and cannot pretend to be local (the Host check in app.NetworkGuard).
    Returns None for that trusted local dashboard.
    """
    if authorization is None and is_local_request(request):
        return None
    return require_device(database, authorization)


Reader = Annotated[AuthenticatedDevice | None, Depends(require_reader)]
