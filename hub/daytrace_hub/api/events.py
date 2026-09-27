"""DT-11: POST /api/v1/events and the device cursor; DT-43: the nudge an ingest may answer with, and the nudge
choices (GET/PUT /api/v1/nudges); DT-42: meals read from plain text.

Resending is always safe: each event is stored under Event.dedup_key() with UNIQUE(device_id, dedup_key)
(docs/api.md, "Resending is always safe"). store_events() is also what the hub's own desktop tracker
(DT-16) and the seed generator (DT-15) call, without going through HTTP.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from fastapi import APIRouter, Body, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .. import nudges
from ..auth import AuthenticatedDevice, CurrentDevice, Editor, Reader, get_database
from ..db import Database, transaction, utc_offset_minutes, utc_text
from ..models import (
    MEAL_TYPES,
    BatchTooLargeError,
    Event,
    IngestResult,
    Kind,
    MalformedBatchError,
    ParsedMeal,
    RejectedEvent,
    parse_batch,
)
from ..redaction import REDACTED, redacted_forms, redactor_for
from . import API_PREFIX, ApiError

if TYPE_CHECKING:
    from ..llm import LLM

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
        dumped(event.data),
    )


REDACTABLE_FIELDS = ("title", "app", "app_id")
HIDE_SQL = "UPDATE events SET title = ?, app = ?, app_id = ?, data = ?, updated_at = ? WHERE device_id = ? AND dedup_key = ?"


def _site(data: str) -> Any:
    return json.loads(data).get("domain")


def _data_redacted(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Whether the two copies' data differ only by what redaction takes out: a redacted calendar event's extras, or
    a web event's site."""
    if stored["kind"] == "calendar_event":
        return REDACTED in (stored["title"], incoming["title"])
    if stored["kind"] == "web":
        left, right = json.loads(stored["data"]), json.loads(incoming["data"])
        rest_left = {key: value for key, value in left.items() if key != "domain"}
        rest_right = {key: value for key, value in right.items() if key != "domain"}
        return REDACTED in (left.get("domain"), right.get("domain")) and rest_left == rest_right
    return False


def dumped(data: dict[str, Any]) -> str:
    """Event data as it is stored: the one form every comparison of stored rows relies on."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def unread(row: dict[str, Any]) -> dict[str, Any]:
    """A stored or incoming row without what the hub read from a meal's text (DT-42, data.parsed): that reading is
    the hub's, so two copies of a meal are the same meal whatever it made of them."""
    if row["kind"] != "meal" or '"parsed"' not in row["data"]:
        return row
    data = json.loads(row["data"])
    data.pop("parsed", None)
    return {**row, "data": dumped(data)}


def same_event(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Whether two copies of an event say the same thing: every identity field equal, except what redaction
    (DT-44) hid on either side: a title, an app's name or id, a web event's site, a calendar event's extras, and
    what the hub read from a meal's text (DT-42)."""
    stored, incoming = unread(stored), unread(incoming)
    for name in IDENTITY_FIELDS:
        if stored[name] == incoming[name]:
            continue
        if name in REDACTABLE_FIELDS and REDACTED in (stored[name], incoming[name]):
            continue
        if name == "data" and _data_redacted(stored, incoming):
            continue
        return False
    return True


def hides_more(stored: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Whether the incoming copy of the same event hides something the stored one still shows."""
    if any(incoming[name] == REDACTED != stored[name] for name in REDACTABLE_FIELDS):
        return True
    if incoming["kind"] == "web":
        return _site(incoming["data"]) == REDACTED != _site(stored["data"])
    return incoming["kind"] == "calendar_event" and incoming["title"] == REDACTED and incoming["data"] != stored["data"]


def _hide(conn: sqlite3.Connection, incoming: dict[str, Any], now: str, device_id: str, key: str) -> None:
    """The stored copy of an event, redacted as the incoming copy is: the same event, now under a rule."""
    conn.execute(HIDE_SQL, (incoming["title"], incoming["app"], incoming["app_id"], incoming["data"], now, device_id, key))


def _content_copy(conn: sqlite3.Connection, device_id: str, original: Event, event: Event, key: str, now: str,
                  kept: Event | None = None) -> bool:
    """For an event with neither seq nor external_id (its key is a hash of what it says): whether it was stored
    already under the rules as they were, so it is one event, not two. Stored before a rule that hides it now: that
    copy is redacted and keyed again here. Stored while a rule (since switched off) hid it: the hidden copy stays.
    `kept` is the event as it is stored (a meal with the hub's reading of it), when that differs from `event`."""
    if conn.execute("SELECT 1 FROM events WHERE device_id = ? AND dedup_key = ?", (device_id, key)).fetchone():
        return False  # the usual path finds it
    if event is not original:
        earlier = original.dedup_key()
        if not conn.execute("SELECT 1 FROM events WHERE device_id = ? AND dedup_key = ?", (device_id, earlier)).fetchone():
            return False
        incoming = dict(zip(ROW_FIELDS, event_row(kept or event), strict=True))
        try:
            conn.execute("UPDATE events SET title = ?, app = ?, app_id = ?, data = ?, updated_at = ?, dedup_key = ?"
                         " WHERE device_id = ? AND dedup_key = ?",
                         (incoming["title"], incoming["app"], incoming["app_id"], incoming["data"], now, key, device_id, earlier))
        except sqlite3.IntegrityError:  # redacted, it matches an event already stored: one copy is kept
            conn.execute("DELETE FROM events WHERE device_id = ? AND dedup_key = ?", (device_id, earlier))
        return True
    return any(conn.execute("SELECT 1 FROM events WHERE device_id = ? AND dedup_key = ?", (device_id, form)).fetchone()
               for form in redacted_forms(original))


def store_events(conn: sqlite3.Connection, device_id: str, events: list[tuple[int, Event]],
                 readings: Mapping[int, dict[str, Any]] | None = None) -> StoreResult:
    """Store (index, event) pairs for one device. Must run inside `with transaction(conn):`.

    Each event is redacted first (DT-44), with the rules in force now: a sensitive title is never stored, and
    the key is worked out from the redacted event. `readings` (DT-42) is what the hub read from a meal's text, by
    index: stored with it as data.parsed, but never part of its key, so a resend is still the same meal.

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
    redactor = redactor_for(conn)
    for index, original in events:
        event = redactor.event(original)
        key = event.dedup_key()
        reading = (readings or {}).get(index)
        kept = event.model_copy(update={"data": {**event.data, "parsed": reading}}) if reading else event
        row = event_row(kept)
        if event.seq is None and event.external_id is None and _content_copy(conn, device_id, original, event, key, now, kept):
            result.duplicates += 1
            continue
        if conn.execute(INSERT_SQL, (device_id, key, *row, now)).rowcount:
            result.accepted += 1
            result.new_events.append(kept)
            continue
        stored = dict(zip(ROW_FIELDS, conn.execute(SELECT_SQL, (device_id, key)).fetchone(), strict=True))
        incoming = dict(zip(ROW_FIELDS, row, strict=True))
        if stored == incoming or unread(stored) == unread(incoming):  # the same meal, read (or not) another time
            result.duplicates += 1
        elif event.replaces_existing:
            older = stored["seq"] is not None and event.seq is not None and event.seq < stored["seq"]
            if older:
                result.duplicates += 1
                if same_event(stored, incoming) and hides_more(stored, incoming):
                    _hide(conn, incoming, now, device_id, key)  # a slow retry, but it hides what the stored copy shows
            else:
                conn.execute(UPDATE_SQL, (*row, now, device_id, key))
                result.replaced += 1
                result.replaced_events.append(event)
        elif event.seq is not None and not same_event(stored, incoming):
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
            if hides_more(stored, incoming):
                _hide(conn, incoming, now, device_id, key)  # the same event, now under a rule: its stored words go too
    return result


def last_seq(conn: sqlite3.Connection, device_id: str) -> int | None:
    value = conn.execute("SELECT MAX(seq) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
    return None if value is None else int(value)


# --- DT-42: meals from plain text ---------------------------------------------------------------------------------

MEAL_TIMEOUT = httpx.Timeout(connect=3.0, read=20.0, write=10.0, pool=5.0)  # the collector waits for the answer
MEAL_MAX_TOKENS = 1024
MEAL_MAX_ITEMS = 30
# A meal type the text doesn't name, from the hour it was eaten: from 04:00, 11:00 and 17:00, else a snack.
MEAL_HOURS = (("breakfast", 4, 11), ("lunch", 11, 15), ("dinner", 17, 22))
_MEAL_NAMES = {"breakfast": "breakfast", "brunch": "breakfast", "lunch": "lunch", "dinner": "dinner",
               "supper": "dinner", "snack": "snack", "snacks": "snack"}
_MEAL_NAMED = re.compile(r"\b(" + "|".join(_MEAL_NAMES) + r")\b", re.IGNORECASE)
_MEAL_SPLIT = re.compile(r"\s*(?:[,;\n+&]|\band\b|\bplus\b|\bthen\b)\s*", re.IGNORECASE)
_MEAL_LABEL = re.compile(r"^\s*(?:for\s+)?(?:a\s+)?(?:" + "|".join(_MEAL_NAMES) + r")\s*[:\-]\s*", re.IGNORECASE)
_MEAL_OPENING = re.compile(r"^\s*(?:i\s+)?(?:just\s+)?(?:had|ate|eaten)\s+", re.IGNORECASE)
_MEAL_CLOSING = re.compile(r"\s+(?:for|at)\s+(?:a\s+)?(?:" + "|".join(_MEAL_NAMES) + r")\s*[.!]*\s*$", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+")
MEAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
    "additionalProperties": False,
}
_MEAL_SYSTEM = (
    "You read a short note about a meal and list the foods and drinks in it, one per item. Keep each item's words and"
    " amount as the note says them ('two rotis' stays 'two rotis'). An amount is part of its food, even when the note"
    " gives it after the food: 'pizza, 2 slices' is one item, 'pizza, 2 slices'. Never add a food the note doesn't"
    ' name, and never estimate calories, weights or portions. Answer with JSON only: {"items": ["...", "..."]}.'
)


def needs_reading(event: Event) -> bool:
    """A meal sent as text that lacks its items or its meal type."""
    return event.kind == Kind.MEAL and isinstance(event.data.get("text"), str) and (
        "items" not in event.data or "meal_type" not in event.data)


def _words(text: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(text)}


def meal_items_from_text(text: str) -> list[str]:
    """The foods in a meal note, split on commas, 'and', '+' and the like: "two rotis and dal" is two items. A
    leading "Lunch:" or "I had" and a closing "for dinner" are not foods."""
    text = _MEAL_CLOSING.sub("", _MEAL_OPENING.sub("", _MEAL_LABEL.sub("", text.strip())))
    items = [" ".join(part.split()).strip(" .!") for part in _MEAL_SPLIT.split(text)]
    return [item[:100] for item in items if _WORD.search(item)][:MEAL_MAX_ITEMS]


def meal_items_from_model(text: str, llm: LLM) -> list[str] | None:
    """The foods in a meal note as the local model lists them, or None when it can't be used or answers with a food
    the note doesn't name (every item must share a word with the note)."""
    from ..llm import LLMError

    messages = [{"role": "system", "content": _MEAL_SYSTEM}, {"role": "user", "content": text}]
    try:
        reply = llm.json_reply(messages, MEAL_SCHEMA, "meal_items", max_tokens=MEAL_MAX_TOKENS, timeout=MEAL_TIMEOUT,
                               retry=False)
    except LLMError:
        return None
    items = reply.get("items") if isinstance(reply, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= MEAL_MAX_ITEMS:
        return None
    said = _words(text)
    cleaned = [" ".join(item.split()) for item in items if isinstance(item, str)]
    if len(cleaned) != len(items) or any(not 1 <= len(item) <= 100 or not _words(item) & said for item in cleaned):
        return None
    return cleaned


def meal_type_at(start: datetime) -> str:
    """The meal type for the hour a meal was eaten, in the time the device sent (the hub's own zone when it sent UTC,
    as a collector that doesn't know its zone does)."""
    local = start
    if start.utcoffset() == timedelta(0):
        from .timeline import resolve_tz

        local = start.astimezone(resolve_tz(None)[0])
    for meal_type, first, last in MEAL_HOURS:
        if first <= local.hour < last:
            return meal_type
    return "snack"


def read_meal(event: Event, llm: LLM | None) -> dict[str, Any]:
    """What the hub makes of a meal's text: the items and meal type it lacks, and where each came from. The local
    model lists the items when there is one; otherwise, or when its answer doesn't hold up, the text is split."""
    text = str(event.data["text"])
    reading: dict[str, Any] = {}
    if "items" not in event.data:
        items = meal_items_from_model(text, llm) if llm is not None else None
        by = "ai" if items else "text"
        items = items or meal_items_from_text(text) or [" ".join(text.split())[:100]]
        reading.update(items=items, items_by=by)
    if "meal_type" not in event.data:
        named = _MEAL_NAMED.search(text)
        if named:
            reading.update(meal_type=_MEAL_NAMES[named.group(1).casefold()], type_by="text")
        else:
            reading.update(meal_type=meal_type_at(event.start), type_by="time")
    return reading


def read_meals(database: Database, device_id: str, events: list[tuple[int, Event]], llm: LLM | None) -> dict[int, dict[str, Any]]:
    """The hub's reading of each meal sent as text, by index. A meal stored already, saying the same, keeps the
    reading it was stored with, so a resend is neither read again nor told something else."""
    wanted = [(index, event) for index, event in events if needs_reading(event)]
    if not wanted:
        return {}
    readings: dict[int, dict[str, Any]] = {}
    with database.connect() as conn:
        rules = redactor_for(conn)
        for index, event in wanted:
            shown = rules.event(event)
            row = conn.execute("SELECT data FROM events WHERE device_id = ? AND dedup_key = ?",
                               (device_id, shown.dedup_key())).fetchone()
            data = json.loads(row["data"]) if row is not None else None
            if isinstance(data, dict) and isinstance(data.get("parsed"), dict):
                earlier = data.pop("parsed")
                if data == shown.data:
                    readings[index] = earlier
    for index, event in wanted:
        if index not in readings:
            readings[index] = read_meal(event, llm)  # outside any transaction: the model may take a few seconds
    return readings


def parsed_meal(index: int, event: Event, reading: dict[str, Any]) -> ParsedMeal:
    items = event.data.get("items")
    meal_type = event.data.get("meal_type")
    return ParsedMeal(
        index=index,
        items=[str(item) for item in items] if isinstance(items, list) else list(reading["items"]),
        meal_type=meal_type if meal_type in MEAL_TYPES else reading["meal_type"],
        items_by="event" if isinstance(items, list) else reading["items_by"],
        type_by="event" if meal_type in MEAL_TYPES else reading["type_by"],
    )


# DT-43: called once the events are committed and the connection is closed, so the rules never hold the write lock.
pick_nudge = nudges.pick_nudge


def ingest(database: Database, device: AuthenticatedDevice, payload: Any, now: datetime | None = None,
           again: bool = False, llm: LLM | None = None) -> IngestResult:
    """Validate and store one request body for one device. Raises MalformedBatchError / BatchTooLargeError. `now` and
    `again` are for the demo (DT-48): the moment the nudge rules judge, and a nudge that needn't wait its turn. `llm`
    reads meals sent as text (DT-42); without it their text is split."""
    events, rejected = parse_batch(payload, expected_device_id=device.device_id)
    rejected_indexes = {r.index for r in rejected}
    good_indexes = [i for i in range(len(events) + len(rejected)) if i not in rejected_indexes]
    pairs = list(zip(good_indexes, events, strict=True))
    readings = read_meals(database, device.device_id, pairs, llm)
    with database.connect() as conn:
        try:
            with transaction(conn):
                stored = store_events(conn, device.device_id, pairs, readings)
        except sqlite3.IntegrityError:
            # The device was deleted after it was let in (delete-all, DT-46): pair again, as a revoked one does.
            if conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device.device_id,)).fetchone() is None:
                raise ApiError(401, "unauthorized", "this device isn't paired any more; pair it again",
                               headers={"WWW-Authenticate": "Bearer"}) from None
            raise
        highest = last_seq(conn, device.device_id)
    nudge = pick_nudge(database, device.device_id, stored.changed_events, now=now, again=again)
    refused = {r.index for r in stored.rejected}
    by_index = dict(pairs)
    return IngestResult(
        accepted=stored.accepted,
        replaced=stored.replaced,
        duplicates=stored.duplicates,
        rejected=sorted([*rejected, *stored.rejected], key=lambda r: r.index),
        last_seq=highest,
        nudge=nudge,
        meals=[parsed_meal(index, by_index[index], reading) for index, reading in sorted(readings.items())
               if index not in refused],
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
        result = await run_in_threadpool(ingest, database, device, payload, llm=getattr(request.app.state, "llm", None))
    except MalformedBatchError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    except BatchTooLargeError as exc:
        raise ApiError(413, "batch_too_large", str(exc)) from None
    sorter = getattr(request.app.state, "ai_categorizer", None)
    if sorter is not None and (result.accepted or result.replaced):
        sorter.wake()  # DT-42: new apps may need a category
    return result


@router.get("/devices/{device_id}/cursor", response_model=Cursor, summary="Highest seq the hub has for this device")
def get_cursor(
    device_id: str, device: CurrentDevice, database: Annotated[Database, Depends(get_database)]
) -> Cursor:
    if device_id != device.device_id:
        raise ApiError(403, "forbidden", "a device can only read its own cursor")
    with database.connect() as conn:
        return Cursor(device_id=device_id, last_seq=last_seq(conn, device_id))


# --- DT-43: the nudge choices ---------------------------------------------------------------------------------


class NudgeRuleState(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool


class NudgeLogEntry(BaseModel):
    rule: str
    device_id: str | None
    title: str
    body: str
    created_at: AwareDatetime


class NudgeSettings(BaseModel):
    """Each rule, on or off; whether this computer shows desktop notifications for its own activity; and the latest
    nudges, newest first."""

    rules: list[NudgeRuleState]
    desktop: bool
    cooldown_minutes: int
    recent: list[NudgeLogEntry]


class NudgeChoices(BaseModel):
    """The whole choice, both fields: a typo or a missing field is refused rather than turning every rule back on."""

    model_config = ConfigDict(extra="forbid")

    disabled: Annotated[list[str], Field(max_length=len(nudges.RULE_IDS), description="The rules switched off, by id; the others are on.")]
    desktop: Annotated[bool, Field(description="Desktop notifications for the hub computer's own activity.")]


def _nudge_settings(conn: sqlite3.Connection) -> NudgeSettings:
    choices = nudges.load_choices(conn)
    return NudgeSettings(
        rules=[NudgeRuleState(id=rule.id, name=rule.name, description=rule.description, enabled=rule.id not in choices.disabled)
               for rule in nudges.RULES],
        desktop=choices.desktop,
        cooldown_minutes=int(nudges.COOLDOWN.total_seconds() // 60),
        recent=[NudgeLogEntry(rule=row["rule"], device_id=row["device_id"], title=row["title"], body=row["body"],
                              created_at=datetime.fromisoformat(row["created_at"])) for row in nudges.recent(conn)],
    )


@router.get("/nudges", response_model=NudgeSettings, summary="The nudge rules, on or off, and the latest nudges")
def get_nudges(_: Reader, database: Annotated[Database, Depends(get_database)]) -> NudgeSettings:
    with database.connect() as conn:
        return _nudge_settings(conn)


@router.put("/nudges", response_model=NudgeSettings, summary="Switch nudge rules and desktop notifications on or off")
def put_nudges(body: Annotated[NudgeChoices, Body()], _: Editor, database: Annotated[Database, Depends(get_database)]) -> NudgeSettings:
    try:
        choices = nudges.check_choices(body.disabled, body.desktop)
    except ValueError as error:
        raise ApiError(400, "bad_request", str(error)) from None
    with database.connect() as conn:
        with transaction(conn):
            nudges.save_choices(conn, choices)
        return _nudge_settings(conn)
