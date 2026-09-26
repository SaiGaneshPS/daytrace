"""DT-12: pairing and device management.

Pairing: on the hub computer, POST /pair/start shows a 6-digit code (and a QR code with the hub URL). The
phone sends that code to POST /pair/claim and gets its own token. Codes live in memory, one at a time:
single use, 5 minutes, and 5 wrong tries lock the code until a new one is started.
"""
from __future__ import annotations

import hmac
import io
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import qrcode
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ..auth import Reader, get_database, register_device, require_local
from ..config import Settings
from ..db import Database, transaction, utc_text
from ..discovery import detect_phone_addresses, hub_url, mdns_url
from . import API_PREFIX, ApiError

CODE_LIFETIME = timedelta(minutes=5)
MAX_WRONG_TRIES = 5
DeviceType = Literal["windows", "macos", "android", "ios", "browser", "viewer"]

router = APIRouter(prefix=API_PREFIX, tags=["devices"])


# --- pairing codes --------------------------------------------------------------------------------------------


@dataclass
class ActiveCode:
    code: str
    url: str
    expires_at: datetime  # shown to people
    expires_monotonic: float  # used for the check, so clock changes cannot extend a code
    wrong_tries: int = 0
    used: bool = False


class PairingCodes:
    """The one pairing code that is currently valid, if any. Starting a new code replaces the old one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.active: ActiveCode | None = None

    def start(self, url: str) -> ActiveCode:
        with self.lock:
            self.active = ActiveCode(
                code=f"{secrets.randbelow(1_000_000):06d}",
                url=url,
                expires_at=datetime.now(UTC) + CODE_LIFETIME,
                expires_monotonic=self.clock() + CODE_LIFETIME.total_seconds(),
            )
            return self.active

    def usable(self, code: str) -> ActiveCode | None:
        """The active code if it matches and can still be claimed (for showing its QR code)."""
        with self.lock:
            active = self.active
            if active is None or not self._open(active) or not hmac.compare_digest(active.code, code):
                return None
            return active

    def _open(self, active: ActiveCode) -> bool:
        return not active.used and active.wrong_tries < MAX_WRONG_TRIES and self.clock() < active.expires_monotonic


def get_pairing(request: Request) -> PairingCodes:
    return request.app.state.pairing


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


# --- request and response shapes ------------------------------------------------------------------------------


def _code_digits(value: object) -> object:
    """People type codes as 493 817 or 493-817; keep only what matters."""
    return value.replace(" ", "").replace("-", "") if isinstance(value, str) else value


class PairStarted(BaseModel):
    code: str
    expires_at: datetime
    url: str = Field(description="The hub address to put in the QR code: its LAN IP, which every phone can reach.")
    urls: list[str] = Field(description="Every address phones can use, LAN first, then Tailscale on shared-dev.")
    mdns_url: str
    qr: str


class PairClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, BeforeValidator(_code_digits), Field(pattern=r"^[0-9]{6}$")]
    device_name: Annotated[str, Field(min_length=1, max_length=64)]
    device_type: DeviceType

    @field_validator("device_name")
    @classmethod
    def _readable_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("device_name must not be blank")
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError("device_name must not contain control characters")
        return name


class PairClaimed(BaseModel):
    device_id: str
    device_type: DeviceType
    name: str
    token: str = Field(description="Shown once. Send it as Authorization: Bearer <token>.")
    profile: str


class DeviceInfo(BaseModel):
    device_id: str
    name: str
    device_type: str
    paired_at: str
    last_seen: str | None
    revoked_at: str | None
    last_seq: int | None
    event_count: int
    events_24h: int = Field(description="Events that started in the last 24 hours.")


class DeviceList(BaseModel):
    devices: list[DeviceInfo]


# --- pairing --------------------------------------------------------------------------------------------------


def qr_payload(active: ActiveCode) -> str:
    return json.dumps({"daytrace": 1, "url": active.url, "code": active.code}, separators=(",", ":"))


def next_device_id(conn: sqlite3.Connection, device_type: str) -> str:
    """android-1, android-2, ...: the next free number, never reusing one (revoked devices keep their data)."""
    prefix = f"{device_type}-"
    rows = conn.execute("SELECT device_id FROM devices WHERE substr(device_id, 1, ?) = ?", (len(prefix), prefix))
    numbers = [int(rest) for (device_id,) in rows if (rest := device_id[len(prefix):]).isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


def claim(pairing: PairingCodes, database: Database, body: PairClaim, profile: str) -> PairClaimed:
    """Check the code and add the device. Serialized, so a code can never be used twice."""
    with pairing.lock:
        active = pairing.active
        if active is not None and active.wrong_tries >= MAX_WRONG_TRIES:
            raise ApiError(429, "too_many_attempts", "too many wrong codes; start pairing again on the hub")
        if active is None or active.used or pairing.clock() >= active.expires_monotonic:
            raise ApiError(400, "invalid_code", "that code is not valid any more; start pairing again on the hub")
        if not hmac.compare_digest(active.code, body.code):
            active.wrong_tries += 1
            if active.wrong_tries >= MAX_WRONG_TRIES:
                raise ApiError(429, "too_many_attempts", "too many wrong codes; start pairing again on the hub")
            left = MAX_WRONG_TRIES - active.wrong_tries
            raise ApiError(400, "invalid_code", f"wrong code; {left} {'try' if left == 1 else 'tries'} left")
        with database.connect() as conn, transaction(conn):
            device_id = next_device_id(conn, body.device_type)
            token = register_device(conn, device_id=device_id, name=body.device_name, device_type=body.device_type)
        active.used = True  # only after the device was stored, so a failed write leaves the code usable
    return PairClaimed(
        device_id=device_id, device_type=body.device_type, name=body.device_name, token=token, profile=profile
    )


@router.post(
    "/pair/start",
    response_model=PairStarted,
    dependencies=[Depends(require_local)],
    summary="Show a new pairing code (hub computer only)",
)
def pair_start(
    settings: Annotated[Settings, Depends(get_settings)], pairing: Annotated[PairingCodes, Depends(get_pairing)]
) -> PairStarted:
    urls = [hub_url(settings, address) for address in detect_phone_addresses(settings)]
    active = pairing.start(urls[0] if urls else mdns_url(settings))
    return PairStarted(
        code=active.code,
        expires_at=active.expires_at.astimezone(),
        url=active.url,
        urls=urls,
        mdns_url=mdns_url(settings),
        qr=f"{API_PREFIX}/pair/qr.png?code={active.code}",
    )


@router.get(
    "/pair/qr.png",
    dependencies=[Depends(require_local)],
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
    summary="QR code for the active pairing code (hub computer only)",
)
def pair_qr(
    pairing: Annotated[PairingCodes, Depends(get_pairing)], code: Annotated[str, Query(pattern=r"^[0-9]{6}$")]
) -> Response:
    active = pairing.usable(code)
    if active is None:
        raise ApiError(404, "not_found", "no active pairing code matches; start pairing again")
    buffer = io.BytesIO()
    qrcode.make(qr_payload(active), box_size=8, border=2).save(buffer)
    return Response(content=buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/pair/claim", response_model=PairClaimed, status_code=201, summary="Trade a pairing code for a token")
def pair_claim(
    body: PairClaim,
    settings: Annotated[Settings, Depends(get_settings)],
    pairing: Annotated[PairingCodes, Depends(get_pairing)],
    database: Annotated[Database, Depends(get_database)],
) -> PairClaimed:
    return claim(pairing, database, body, settings.profile.name)


# --- devices --------------------------------------------------------------------------------------------------


@router.get("/devices", response_model=DeviceList, summary="Paired devices, including revoked ones")
def list_devices(_: Reader, database: Annotated[Database, Depends(get_database)]) -> DeviceList:
    since = utc_text(datetime.now(UTC) - timedelta(hours=24))
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT d.device_id, d.name, d.device_type, d.paired_at, d.last_seen, d.revoked_at,"
            " (SELECT MAX(seq) FROM events e WHERE e.device_id = d.device_id) AS last_seq,"
            " (SELECT COUNT(*) FROM events e WHERE e.device_id = d.device_id) AS event_count,"
            " (SELECT COUNT(*) FROM events e WHERE e.device_id = d.device_id AND e.start_utc >= ?) AS events_24h"
            " FROM devices d ORDER BY d.paired_at, d.device_id",
            (since,),
        ).fetchall()
    return DeviceList(devices=[DeviceInfo(**dict(row)) for row in rows])


@router.delete(
    "/devices/{device_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_local)],
    summary="Revoke a device's token (hub computer only); its data stays",
)
def revoke_device(device_id: str, database: Annotated[Database, Depends(get_database)]) -> Response:
    with database.connect() as conn, transaction(conn):
        found = conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not found:
            raise ApiError(404, "not_found", f"no device {device_id!r}")
        conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
            (utc_text(datetime.now(UTC)), device_id),
        )
    return Response(status_code=204)
