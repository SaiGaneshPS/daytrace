"""DT-12: pairing and device management.

Pairing: on the hub computer, POST /pair/start shows a 6-digit code (and a QR code with the hub URL). The
phone sends that code to POST /pair/claim and gets its own token. Codes live in memory, one at a time:
single use and 5 minutes. Wrong guesses are limited per client (5) and per code (20), so nobody can guess
the code, and one noisy device on the Wi-Fi cannot lock everyone else out.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

import qrcode
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ..auth import DeviceType, Reader, get_database, register_device, require_local
from ..config import Settings
from ..db import Database, transaction, utc_text
from ..discovery import detect_phone_addresses, hub_url, mdns_url
from ..models import has_hidden_characters
from . import API_PREFIX, ApiError

CODE_LIFETIME = timedelta(minutes=5)
WRONG_TRIES_PER_CLIENT = 5
WRONG_TRIES_PER_CODE = 20  # across all clients: at most 20 guesses out of a million per code
# Device IDs start with these, so the first iPhone is iphone-1 like the Shortcut examples.
ID_PREFIX = {"windows": "windows", "macos": "mac", "android": "android", "ios": "iphone", "browser": "browser",
             "viewer": "viewer"}
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(prefix=API_PREFIX, tags=["devices"])


# --- pairing codes --------------------------------------------------------------------------------------------


@dataclass
class ActiveCode:
    code: str
    url: str | None
    expires_at: datetime  # shown to people
    expires_monotonic: float  # used for the check, so clock changes cannot extend a code
    wrong_by_client: dict[str, int] = field(default_factory=dict)
    used: bool = False

    @property
    def wrong_tries(self) -> int:
        return sum(self.wrong_by_client.values())


class PairingCodes:
    """The one pairing code that is currently valid, if any. Starting a new code replaces the old one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._active: ActiveCode | None = None

    def start(self, url: str | None) -> ActiveCode:
        with self._lock:
            self._active = ActiveCode(
                code=f"{secrets.randbelow(1_000_000):06d}",
                url=url,
                expires_at=datetime.now(UTC) + CODE_LIFETIME,
                expires_monotonic=self.clock() + CODE_LIFETIME.total_seconds(),
            )
            return self._active

    def _open(self, active: ActiveCode | None) -> bool:
        return (
            active is not None
            and not active.used
            and active.wrong_tries < WRONG_TRIES_PER_CODE
            and self.clock() < active.expires_monotonic
        )

    def current(self) -> ActiveCode | None:
        """The active code while it can still be claimed (for its QR code)."""
        with self._lock:
            return self._active if self._open(self._active) else None

    def claim(self, code: str, client: str, register: Callable[[], PairClaimed]) -> PairClaimed:
        """Check the code for this client and, if right, run `register` (which stores the device).

        Serialized, so a code can never be used twice. The code is spent only after `register` succeeds.
        """
        with self._lock:
            active = self._active
            if active is not None and active.wrong_by_client.get(client, 0) >= WRONG_TRIES_PER_CLIENT:
                raise _too_many()
            if active is not None and active.wrong_tries >= WRONG_TRIES_PER_CODE:
                raise _too_many()
            if not self._open(active):
                raise ApiError(400, "invalid_code", "that code is not valid any more; start pairing again on the hub")
            assert active is not None
            if not hmac.compare_digest(active.code, code):
                active.wrong_by_client[client] = active.wrong_by_client.get(client, 0) + 1
                left = WRONG_TRIES_PER_CLIENT - active.wrong_by_client[client]
                if left <= 0 or active.wrong_tries >= WRONG_TRIES_PER_CODE:
                    raise _too_many()
                raise ApiError(400, "invalid_code", f"wrong code; {left} {'try' if left == 1 else 'tries'} left")
            claimed = register()
            active.used = True
            return claimed


def _too_many() -> ApiError:
    return ApiError(429, "too_many_attempts", "too many wrong codes; start pairing again on the hub")


def get_pairing(request: Request) -> PairingCodes:
    return request.app.state.pairing


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


# --- request and response shapes ------------------------------------------------------------------------------


def _code_digits(value: object) -> object:
    """People type codes as 493 817 or 493-817; keep only what matters."""
    return value.replace(" ", "").replace("-", "") if isinstance(value, str) else value


def _trimmed(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


class PairStarted(BaseModel):
    code: str
    expires_at: datetime
    url: str | None = Field(
        description="The hub address for the QR code (its LAN IP, which every phone can reach), or null when "
        "this computer has no LAN address right now."
    )
    urls: list[str] = Field(description="Every address phones can use, LAN first, then Tailscale on shared-dev.")
    mdns_url: str | None = Field(description="The .local address, when mDNS is on.")
    qr: str | None


class PairClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, BeforeValidator(_code_digits), Field(pattern=r"^[0-9]{6}$")]
    device_name: Annotated[str, BeforeValidator(_trimmed), Field(min_length=1, max_length=64)]
    device_type: DeviceType

    @field_validator("device_name")
    @classmethod
    def _readable_name(cls, name: str) -> str:
        if has_hidden_characters(name):  # U+202E and friends could disguise a name in the device list
            raise ValueError("device_name must not contain control or formatting characters")
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
    """iphone-1, iphone-2, ...: the next free number, never reusing one (revoked devices keep their data)."""
    prefix = f"{ID_PREFIX[device_type]}-"
    rows = conn.execute("SELECT device_id FROM devices WHERE substr(device_id, 1, ?) = ?", (len(prefix), prefix))
    numbers = [int(rest) for (device_id,) in rows if (rest := device_id[len(prefix):]).isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


def add_device(database: Database, body: PairClaim, profile: str) -> PairClaimed:
    with database.connect() as conn, transaction(conn):
        device_id = next_device_id(conn, body.device_type)
        token = register_device(conn, device_id=device_id, name=body.device_name, device_type=body.device_type)
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
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    pairing: Annotated[PairingCodes, Depends(get_pairing)],
) -> PairStarted:
    urls = [hub_url(settings, address) for address in detect_phone_addresses(settings)]
    active = pairing.start(urls[0] if urls else None)
    response.headers.update(NO_STORE)
    return PairStarted(
        code=active.code,
        expires_at=active.expires_at.astimezone(),
        url=active.url,
        urls=urls,
        mdns_url=mdns_url(settings) if settings.advertise_mdns else None,
        qr=f"{API_PREFIX}/pair/qr.png" if active.url else None,
    )


@router.get(
    "/pair/qr.png",
    dependencies=[Depends(require_local)],
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
    summary="QR code for the active pairing code (hub computer only)",
)
def pair_qr(pairing: Annotated[PairingCodes, Depends(get_pairing)]) -> Response:
    active = pairing.current()
    if active is None or active.url is None:
        raise ApiError(404, "not_found", "no pairing code is active; start pairing again")
    buffer = io.BytesIO()
    qrcode.make(qr_payload(active), box_size=8, border=2).save(buffer)
    return Response(content=buffer.getvalue(), media_type="image/png", headers=NO_STORE)


@router.post("/pair/claim", response_model=PairClaimed, status_code=201, summary="Trade a pairing code for a token")
def pair_claim(
    body: PairClaim,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    pairing: Annotated[PairingCodes, Depends(get_pairing)],
    database: Annotated[Database, Depends(get_database)],
) -> PairClaimed:
    client = request.client.host if request.client else "unknown"
    claimed = pairing.claim(body.code, client, lambda: add_device(database, body, settings.profile.name))
    response.headers.update(NO_STORE)  # the only copy of the token
    return claimed


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


# --- proving the hub (DT-22) ----------------------------------------------------------------------------------


class ProofRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: Annotated[
        str, Field(pattern=r"^[0-9a-f]{32,128}$", description="32 to 128 lowercase hex characters, new for every check")
    ]


class Proof(BaseModel):
    device_id: str
    revoked: bool
    proof: str


def hub_proof(token_hash: str, nonce: str, revoked: bool = False) -> str:
    """HMAC-SHA256 keyed with the device's stored token hash (UTF-8 text, lowercase hex). The message is the nonce,
    or "revoked:" + nonce for a revoked device, so a revocation can be believed only when this hub signed it."""
    message = f"revoked:{nonce}" if revoked else nonce
    return hmac.new(token_hash.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


@router.post(
    "/devices/{device_id}/proof",
    response_model=Proof,
    summary="Prove this is the hub the device paired with, before it sends its token (no auth)",
)
def prove_hub(
    device_id: str, body: ProofRequest, response: Response, database: Annotated[Database, Depends(get_database)]
) -> Proof:
    """Until HTTPS (DT-47), a phone on another Wi-Fi with the same addresses could reach a stranger's device at
    the hub's address and hand it the token. So the phone first sends a fresh nonce here and checks the answer:
    only the hub that paired it holds the token hash that keys it. The token itself never travels for this.

    A revoked device gets a signed "revoked" answer (the token hash is kept on revoke), so the phone asks to pair
    again only when its own hub says so; an unsigned 401 could come from anyone."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT token_hash, revoked_at FROM devices WHERE device_id = ? AND token_hash IS NOT NULL", (device_id,)
        ).fetchone()
    if row is None:
        raise ApiError(401, "unauthorized", "this hub never paired that device")
    revoked = row["revoked_at"] is not None
    response.headers.update(NO_STORE)
    return Proof(device_id=device_id, revoked=revoked, proof=hub_proof(row["token_hash"], body.nonce, revoked))
