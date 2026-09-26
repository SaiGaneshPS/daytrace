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

MAX_BODY_BYTES = 2 * 1024 * 1024  # 500 events of about 4 KB each; anything bigger is refused unread
# Fields that say what happened. A seq reused with different values here is a collector bug, not a resend.
IDENTITY_FIELDS = ("kind", "source", "start_utc", "end_utc", "app", "app_id", "title", "data")

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


def event_row(event: Event) -> dict[str, Any]:
    """The stored columns of an event (besides device_id, dedup_key and the timestamps the hub adds)."""
    return {
        "seq": event.seq,
        "external_id": event.external_id,
        "kind": event.kind.value,
        "source": event.source.value,
        "start_utc": utc_text(event.start),
        "end_utc": utc_text(event.end) if event.end is not None else None,
        "utc_offset_min": utc_offset_minutes(event.start),
        "app": event.app,
        "app_id": event.app_id,
        "title": event.title,
        "category": event.category.value if event.category is not None else None,
        "data": json.dumps(event.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    }


def store_events(conn: sqlite3.Connection, device_id: str, events: list[tuple[int, Event]]) -> StoreResult:
    """Store (index, event) pairs for one device. Call inside `with transaction(conn):`.

    - New key: stored (accepted).
    - Same key, same values: ignored (duplicates), whatever the key type.
    - ext: key with new values: the stored copy is replaced (replaced).
    - seq: key with a different event behind it: rejected, so a collector that restarted its numbering
      (for example after a reinstall) finds out instead of losing events silently.
    - content: keys cover the event's values, so a match is always a duplicate.
    """
    now = utc_text(datetime.now(UTC))
    result = StoreResult()
    for index, event in events:
        key = event.dedup_key()
        row = event_row(event)
        columns = ("device_id", "dedup_key", *row, "received_at")
        inserted = conn.execute(
            f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
            " ON CONFLICT (device_id, dedup_key) DO NOTHING",
            (device_id, key, *row.values(), now),
        ).rowcount
        if inserted:
            result.accepted += 1
            result.new_events.append(event)
            continue
        stored = conn.execute(
            f"SELECT {', '.join(row)} FROM events WHERE device_id = ? AND dedup_key = ?", (device_id, key)
        ).fetchone()
        if all(stored[name] == value for name, value in row.items()):
            result.duplicates += 1
        elif event.replaces_existing:
            conn.execute(
                f"UPDATE events SET {', '.join(f'{name} = ?' for name in row)}, updated_at = ?"
                " WHERE device_id = ? AND dedup_key = ?",
                (*row.values(), now, device_id, key),
            )
            result.replaced += 1
        elif event.seq is not None and any(stored[name] != row[name] for name in IDENTITY_FIELDS):
            result.rejected.append(
                RejectedEvent(
                    index=index,
                    seq=event.seq,
                    reason=(
                        f"seq {event.seq} was already used for a different event on this device;"
                        " number new events after last_seq (GET /devices/{device_id}/cursor)"
                    ),
                )
            )
        else:
            result.duplicates += 1
    return result


def last_seq(conn: sqlite3.Connection, device_id: str) -> int | None:
    value = conn.execute("SELECT MAX(seq) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
    return None if value is None else int(value)


def pick_nudge(conn: sqlite3.Connection, device: AuthenticatedDevice, new_events: list[Event]) -> Nudge | None:
    """DT-43 fills this in (focus blocks, late-night scrolling, daily social limit)."""
    return None


def ingest(database: Database, device: AuthenticatedDevice, payload: Any) -> IngestResult:
    """Validate and store one request body for one device. Raises MalformedBatchError / BatchTooLargeError."""
    events, rejected = parse_batch(payload, expected_device_id=device.device_id)
    rejected_indexes = {r.index for r in rejected}
    good_indexes = [i for i in range(len(events) + len(rejected)) if i not in rejected_indexes]
    with database.connect() as conn:
        with transaction(conn):
            stored = store_events(conn, device.device_id, list(zip(good_indexes, events, strict=True)))
            nudge = pick_nudge(conn, device, stored.new_events)
        highest = last_seq(conn, device.device_id)
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
    """The request body as JSON, read in chunks so an oversized body is refused before it is buffered."""
    too_large = ApiError(413, "batch_too_large", f"the request body must be at most {MAX_BODY_BYTES // (1024 * 1024)} MB")
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
    body = b"".join(chunks)
    if not body.strip():
        raise ApiError(400, "bad_request", "the request body is empty")
    try:
        return json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
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
async def post_events(request: Request, device: CurrentDevice) -> IngestResult:
    if device.is_viewer:
        raise ApiError(403, "forbidden", "viewer tokens can read the dashboard but cannot send events")
    payload = await read_json_body(request)
    try:
        return await run_in_threadpool(ingest, request.app.state.db, device, payload)
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
