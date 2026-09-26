"""DT-11: POST /api/v1/events and the device cursor.

Resending is always safe: each event is stored under Event.dedup_key() with UNIQUE(device_id, dedup_key)
(docs/api.md, "Resending is always safe"). store_events() is also what the hub's own desktop tracker
(DT-16) and the seed generator (DT-15) call, without going through HTTP.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..auth import AuthenticatedDevice, CurrentDevice, get_database
from ..db import Database, transaction, utc_offset_minutes, utc_text
from ..models import (
    BatchTooLargeError,
    Event,
    IngestResult,
    MalformedBatchError,
    Nudge,
    RejectedEvent,
    parse_batch,
)
from . import API_PREFIX, ApiError

# 500 events always fit: the models cap data at 16 KB and text fields at a few hundred characters.
MAX_BODY_BYTES = 8 * 1024 * 1024
# Fields that say what happened. A seq reused with different values here is a collector bug, not a resend.
IDENTITY_FIELDS = ("kind", "source", "start_utc", "end_utc", "app", "app_id", "title", "data")
ROW_FIELDS = (
    "seq", "external_id", "kind", "source", "start_utc", "end_utc", "utc_offset_min",
    "app", "app_id", "title", "category", "data",
)
_INSERT_COLUMNS = ("device_id", "dedup_key", *ROW_FIELDS, "received_at")
INSERT_SQL = (
    f"INSERT INTO events ({', '.join(_INSERT_COLUMNS)}) VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})"
    " ON CONFLICT (device_id, dedup_key) DO NOTHING"
)
SELECT_SQL = f"SELECT {', '.join(ROW_FIELDS)} FROM events WHERE device_id = ? AND dedup_key = ?"
UPDATE_SQL = (
    f"UPDATE events SET {', '.join(f'{name} = ?' for name in ROW_FIELDS)}, updated_at = ?"
    " WHERE device_id = ? AND dedup_key = ?"
)

router = APIRouter(prefix=API_PREFIX, tags=["events"])


class Cursor(BaseModel):
    device_id: str
    last_seq: int | None


@dataclass
class StoreResult:
    accepted: int = 0
    replaced: int = 0
    duplicates: int = 0
    rejected: list[RejectedEvent] = field(default_factory=list)
    new_events: list[Event] = field(default_factory=list)
    replaced_events: list[Event] = field(default_factory=list)

    @property
    def changed_events(self) -> list[Event]:
        return [*self.new_events, *self.replaced_events]


def event_row(event: Event) -> tuple[Any, ...]:
    """The stored values of an event, in ROW_FIELDS order."""
    return (
        event.seq,
        event.external_id,
        event.kind.value,
        event.source.value,
        utc_text(event.start),
        utc_text(event.end) if event.end is not None else None,
        utc_offset_minutes(event.start),
        event.app,
        event.app_id,
        event.title,
        event.category.value if event.category is not None else None,
        json.dumps(event.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
    )


def store_events(conn: sqlite3.Connection, device_id: str, events: list[tuple[int, Event]]) -> StoreResult:
    """Store (index, event) pairs for one device. Must run inside `with transaction(conn):`.

    - New key: stored (accepted).
    - Same key, same values: ignored (duplicates), whatever the key type.
    - ext: key with new values: the stored copy is replaced (replaced), unless both copies carry a seq and
      the incoming one is older, which happens when a slow retry arrives after a newer copy (duplicates).
    - seq: key with a different event behind it: rejected with code seq_conflict, so a collector that
      restarted its numbering (for example after a reinstall) renumbers instead of losing events.
    - content: keys cover the event's values, so a match is always a duplicate.
    """
    if not conn.in_transaction:
        raise RuntimeError("store_events must run inside a transaction (with transaction(conn): ...)")
    now = utc_text(datetime.now(UTC))
    result = StoreResult()
    for index, event in events:
        key = event.dedup_key()
        row = event_row(event)
        if conn.execute(INSERT_SQL, (device_id, key, *row, now)).rowcount:
            result.accepted += 1
            result.new_events.append(event)
            continue
        stored = dict(zip(ROW_FIELDS, conn.execute(SELECT_SQL, (device_id, key)).fetchone(), strict=True))
        incoming = dict(zip(ROW_FIELDS, row, strict=True))
        if stored == incoming:
            result.duplicates += 1
        elif event.replaces_existing:
            older = stored["seq"] is not None and event.seq is not None and event.seq < stored["seq"]
            if older:
                result.duplicates += 1
            else:
                conn.execute(UPDATE_SQL, (*row, now, device_id, key))
                result.replaced += 1
                result.replaced_events.append(event)
        elif event.seq is not None and any(stored[name] != incoming[name] for name in IDENTITY_FIELDS):
            result.rejected.append(
                RejectedEvent(
                    index=index,
                    code="seq_conflict",
                    seq=event.seq,
                    reason=(
                        f"seq {event.seq} was already used for a different event on this device;"
                        " renumber unsynced events after last_seq and send them again"
                    ),
                )
            )
        else:
            result.duplicates += 1
    return result


def last_seq(conn: sqlite3.Connection, device_id: str) -> int | None:
    value = conn.execute("SELECT MAX(seq) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
    return None if value is None else int(value)


def pick_nudge(conn: sqlite3.Connection, device: AuthenticatedDevice, changed: list[Event]) -> Nudge | None:
    """DT-43 fills this in (focus blocks, late-night scrolling, daily social limit).

    Called after the events are committed, so rule checks never hold the database write lock.
    """
    return None


def ingest(database: Database, device: AuthenticatedDevice, payload: Any) -> IngestResult:
    """Validate and store one request body for one device. Raises MalformedBatchError / BatchTooLargeError."""
    events, rejected = parse_batch(payload, expected_device_id=device.device_id)
    rejected_indexes = {r.index for r in rejected}
    good_indexes = [i for i in range(len(events) + len(rejected)) if i not in rejected_indexes]
    with database.connect() as conn:
        with transaction(conn):
            stored = store_events(conn, device.device_id, list(zip(good_indexes, events, strict=True)))
        highest = last_seq(conn, device.device_id)
        nudge = pick_nudge(conn, device, stored.changed_events)
    return IngestResult(
        accepted=stored.accepted,
        replaced=stored.replaced,
        duplicates=stored.duplicates,
        rejected=sorted([*rejected, *stored.rejected], key=lambda r: r.index),
        last_seq=highest,
        nudge=nudge,
    )


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


async def read_json_body(request: Request) -> Any:
    """The request body as UTF-8 JSON, read in chunks so an oversized body is refused before it is buffered."""
    too_large = ApiError(
        413,
        "body_too_large",
        f"the request body must be at most {MAX_BODY_BYTES // (1024 * 1024)} MB; send fewer events per request",
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise too_large
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8-sig")  # UTF-8, with or without a byte order mark
    except UnicodeDecodeError:
        raise ApiError(400, "bad_request", "the body must be UTF-8 encoded JSON") from None
    if not text.strip():
        raise ApiError(400, "bad_request", "the request body is empty")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError is a ValueError
        raise ApiError(400, "bad_request", f"the body is not valid JSON: {exc}") from None


@router.post(
    "/events",
    response_model=IngestResult,
    summary="Send one event or a batch of up to 500",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "description": 'One event, or {"events": [...]} with 1 to 500 events (docs/event-schema.json).',
                    }
                }
            },
        }
    },
)
async def post_events(
    request: Request, device: CurrentDevice, database: Annotated[Database, Depends(get_database)]
) -> IngestResult:
    if device.is_viewer:
        raise ApiError(403, "forbidden", "viewer tokens can read the dashboard but cannot send events")
    payload = await read_json_body(request)
    try:
        return await run_in_threadpool(ingest, database, device, payload)
    except MalformedBatchError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    except BatchTooLargeError as exc:
        raise ApiError(413, "batch_too_large", str(exc)) from None


@router.get("/devices/{device_id}/cursor", response_model=Cursor, summary="Highest seq the hub has for this device")
def get_cursor(
    device_id: str, device: CurrentDevice, database: Annotated[Database, Depends(get_database)]
) -> Cursor:
    if device_id != device.device_id:
        raise ApiError(403, "forbidden", "a device can only read its own cursor")
    with database.connect() as conn:
        return Cursor(device_id=device_id, last_seq=last_seq(conn, device_id))
