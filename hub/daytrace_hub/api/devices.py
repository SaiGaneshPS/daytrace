"""DT-12: pairing and device management.

Pairing: on the hub computer, POST /pair/start shows a 6-digit code (and a QR code with the hub URL). The
phone sends that code to POST /pair/claim and gets its own token. The QR code comes in two kinds: for the Daytrace
app (JSON it reads) and for a phone's camera (the Devices page's web address, which pairs that browser). Codes live in memory, one at a time:
single use and 5 minutes. Wrong guesses are limited per client (5) and per code (20), so nobody can guess
the code, and one noisy device on the Wi-Fi cannot lock everyone else out.

A device that pairs again gets its first id back (DT-22), so one phone is one device however often it pairs (after
a revoke, or "Forget this hub"): it sends the token it had (previous_token), which only the device itself holds (the
hub keeps its hash). The phone sends it only to a hub that first proved it holds that hash (POST /devices/{id}/
proof), so a stranger's hub never sees it. It still needs a fresh code from the hub computer, and the hub computer
sees the device come back (pair/status: returning). A reinstall starts a new device: nothing the phone kept survives.
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
from typing import Annotated, Literal

import qrcode
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ..auth import DeviceType, Reader, get_database, hash_token, new_token, register_device, require_local
from ..config import Settings
from ..db import Database, transaction, utc_text
from ..discovery import detect_phone_addresses, hub_url, mdns_url
from ..models import DEVICE_ID_PATTERN, has_hidden_characters
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
class ClaimedBy:
    device_id: str
    name: str
    device_type: str
    returning: bool = False  # DT-22: paired before, kept its id


@dataclass
class ActiveCode:
    code: str
    url: str | None
    expires_at: datetime  # shown to people
    expires_monotonic: float  # used for the check, so clock changes cannot extend a code
    wrong_by_client: dict[str, int] = field(default_factory=dict)
    used: bool = False
    # Names this code for the dashboard (its status, its QR picture) without the code itself in any URL or log.
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    claimed_by: ClaimedBy | None = None

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

    def latest(self) -> tuple[ActiveCode, bool] | None:
        """The last code started, used or not, and whether it can still be claimed; None before the first."""
        with self._lock:
            return (self._active, self._open(self._active)) if self._active is not None else None

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
            active.claimed_by = ClaimedBy(claimed.device_id, claimed.name, claimed.device_type, claimed.returning)
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
    id: str = Field(description="Names this code in GET /pair/status and GET /pair/qr.png (instead of the code).")
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
    previous_device_id: Annotated[str | None, Field(
        pattern=DEVICE_ID_PATTERN, description="DT-22: the id this device had on this hub, with previous_token."
    )] = None
    previous_token: Annotated[str | None, Field(
        min_length=1, max_length=200, pattern=r"^[!-~]+$",
        description="DT-22: the token it had here, even a revoked one. Send it only to a hub that proved it holds "
        "its hash (POST /devices/{id}/proof). It stops working now either way.",
    )] = None

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
    returning: bool = Field(
        default=False,
        description="DT-22: the same device paired before, so it keeps its id (and history); the old token no longer works.",
    )


class DeviceInfo(BaseModel):
    device_id: str
    name: str
    device_type: str
    has_token: bool = Field(
        description="False for the hub computer's own tracker and the demo data, which write on the hub directly "
        "(nothing to revoke: they never send with a token)."
    )
    paired_at: str
    last_seen: str | None
    revoked_at: str | None
    last_seq: int | None
    event_count: int
    events_24h: int = Field(description="Events that started in the last 24 hours.")


class DeviceList(BaseModel):
    devices: list[DeviceInfo]


class ClaimedDevice(BaseModel):
    device_id: str
    name: str
    device_type: str
    returning: bool = Field(default=False, description="DT-22: a device paired before came back with its first id.")


class PairStatus(BaseModel):
    id: str = Field(description="The last code started; a different id than yours means a newer code replaced it.")
    active: bool = Field(description="It can still be claimed (not used, not run out, not locked by wrong tries).")
    used: bool
    claimed_by: ClaimedDevice | None = Field(description="The device that used it, once it has been used.")


# --- pairing --------------------------------------------------------------------------------------------------


QrKind = Literal["app", "browser"]


def qr_payload(active: ActiveCode, kind: QrKind = "app") -> str:
    """What the QR code says. "app": JSON for the Daytrace app's scanner. "browser": the Devices page with the code
    after the `#`, so a phone's camera opens it and the page pairs that browser (a browser never sends the part
    after `#` to any server)."""
    if kind == "browser":
        return f"{active.url}/devices#pair={active.code}"
    return json.dumps({"daytrace": 1, "url": active.url, "code": active.code}, separators=(",", ":"))


def next_device_id(conn: sqlite3.Connection, device_type: str) -> str:
    """iphone-1, iphone-2, ...: the next free number, never reusing one (revoked devices keep their data)."""
    prefix = f"{ID_PREFIX[device_type]}-"
    rows = conn.execute("SELECT device_id FROM devices WHERE substr(device_id, 1, ?) = ?", (len(prefix), prefix))
    numbers = [int(rest) for (device_id,) in rows if (rest := device_id[len(prefix):]).isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


def returning_device(conn: sqlite3.Connection, body: PairClaim) -> sqlite3.Row | None:
    """The device this claim comes from, when it paired here before: same id, same type, and the token it sent is the
    one whose hash is stored (even revoked). None for a new device, or a token that isn't that device's."""
    if not (body.previous_device_id and body.previous_token):
        return None
    row = conn.execute("SELECT device_id, device_type, token_hash, revoked_at FROM devices WHERE device_id = ?",
                       (body.previous_device_id,)).fetchone()
    if row is None or row["device_type"] != body.device_type or not row["token_hash"]:
        return None
    return row if hmac.compare_digest(row["token_hash"], hash_token(body.previous_token)) else None


def add_device(database: Database, body: PairClaim, profile: str) -> PairClaimed:
    """A new device, or (DT-22) a device pairing again: it keeps its id and history, gets a new token (the one it sent
    stops working), its new name, and is no longer revoked. The stretch it was revoked is kept as a gap, so the stats
    never count those days as days it should have sent data."""
    now = utc_text(datetime.now(UTC))
    with database.connect() as conn, transaction(conn):
        earlier = returning_device(conn, body)
        if earlier is not None:
            device_id = str(earlier["device_id"])
            token = new_token()
            if earlier["revoked_at"] is not None:
                conn.execute("INSERT INTO device_gaps (device_id, from_utc, until_utc) VALUES (?, ?, ?)",
                             (device_id, earlier["revoked_at"], now))
            conn.execute("UPDATE devices SET token_hash = ?, name = ?, revoked_at = NULL WHERE device_id = ?",
                         (hash_token(token), body.device_name, device_id))
        else:
            device_id = next_device_id(conn, body.device_type)
            token = register_device(conn, device_id=device_id, name=body.device_name, device_type=body.device_type)
    return PairClaimed(
        device_id=device_id, device_type=body.device_type, name=body.device_name, token=token, profile=profile,
        returning=earlier is not None,
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
        id=active.id,
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
def pair_qr(
    pairing: Annotated[PairingCodes, Depends(get_pairing)],
    kind: Annotated[QrKind, Query(alias="for", description="app: for the Daytrace app; browser: for a phone's camera")] = "app",
    code_id: Annotated[str | None, Query(alias="id", description="The code's id from POST /pair/start: 404 when it is not the active code")] = None,
) -> Response:
    active = pairing.current()
    if active is None or active.url is None or (code_id is not None and not hmac.compare_digest(code_id, active.id)):
        raise ApiError(404, "not_found", "no pairing code is active; start pairing again")
    buffer = io.BytesIO()
    qrcode.make(qr_payload(active, kind), box_size=8, border=2).save(buffer)
    return Response(content=buffer.getvalue(), media_type="image/png", headers=NO_STORE)


@router.get(
    "/pair/status",
    response_model=PairStatus,
    dependencies=[Depends(require_local)],
    summary="Whether the last pairing code was used, and by which device (hub computer only)",
)
def pair_status(response: Response, pairing: Annotated[PairingCodes, Depends(get_pairing)]) -> PairStatus:
    latest = pairing.latest()
    if latest is None:
        raise ApiError(404, "not_found", "no pairing code has been started")
    active, still_open = latest
    by = active.claimed_by
    response.headers.update(NO_STORE)
    return PairStatus(
        id=active.id, active=still_open, used=active.used,
        claimed_by=ClaimedDevice(device_id=by.device_id, name=by.name, device_type=by.device_type, returning=by.returning) if by else None,
    )


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
            "SELECT d.device_id, d.name, d.device_type, d.token_hash IS NOT NULL AS has_token, d.paired_at, d.last_seen,"
            " d.revoked_at,"
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
