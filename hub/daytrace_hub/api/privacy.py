"""The Privacy page's API. DT-44: the redaction rules (see them, change them, try a title against them). DT-45:
the network status, the proof that the hub talks to nothing but this computer, your LAN and (for shared-dev) your
tailnet. DT-46: export everything, and delete everything (both only from the hub computer itself).

Reading the rules needs a paired device or the local dashboard; changing them needs the dashboard (a viewer token,
or the dashboard on the hub computer), as categories and goals do. A change applies to the next event stored.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..auth import Editor, Reader, get_database, is_trusted_local, require_local
from ..config import LEDGER
from ..db import Database, current_version, transaction, utc_text
from ..redaction import (
    MAX_CUSTOM_RULES,
    MAX_WORDS,
    REDACTED,
    WORD_LENGTH,
    Redactor,
    RulesError,
    builtin_rules,
    check_choices,
    redactor_for,
    save_choices,
    stored_matches,
)
from ..redaction import SETTINGS_KEY as REDACTION_KEY
from . import API_PREFIX, ApiError

router = APIRouter(prefix=API_PREFIX, tags=["privacy"])


class RedactionRule(BaseModel):
    id: str
    name: str
    description: str
    builtin: bool
    enabled: bool
    words: list[str] = Field(description="Your own rule's words or phrases (empty for a built-in rule).")


class RedactionRules(BaseModel):
    redacted: str = Field(description="What a matching title is stored as.")
    rules: list[RedactionRule]


# The sizes are checked as the request is read, so an oversized one is refused before any work (422).
class CustomRule(BaseModel):
    name: str = Field(max_length=200)
    words: list[Annotated[str, Field(max_length=4 * WORD_LENGTH[1])]] = Field(
        max_length=MAX_WORDS, description="Words or phrases, matched whole and ignoring case, in titles, app names and sites.")


class RedactionChoices(BaseModel):
    disabled: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=20,
                                                                  description="Built-in rules to switch off, by id.")
    custom: list[CustomRule] = Field(default_factory=list, max_length=MAX_CUSTOM_RULES,
                                     description=f"Your own rules, up to {MAX_CUSTOM_RULES}, each with up to {MAX_WORDS} words or phrases.")


class TitleCheck(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    app: str | None = Field(default=None, max_length=200)
    app_id: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=253, description="A site, as the browser extension sends it.")


class TitleCheckResult(BaseModel):
    redacted: bool
    stored_as: str | None = Field(description="The title (or, for a site alone, the site) as the hub would store it.")
    rule: str | None = Field(description="The id of the rule that matched.")
    rule_name: str | None


def _rules(redactor: Redactor) -> RedactionRules:
    disabled = set(redactor.choices.disabled)
    rules = [RedactionRule(id=rule.id, name=rule.name, description=rule.description, builtin=True, enabled=rule.id not in disabled, words=[])
             for rule in builtin_rules()]
    rules += [RedactionRule(id=rule.id, name=rule.name, description=rule.description, builtin=False, enabled=True, words=list(rule.words))
              for rule in redactor.rules if not rule.builtin]
    return RedactionRules(redacted=REDACTED, rules=rules)


@router.get("/privacy/redaction", response_model=RedactionRules, summary="The redaction rules in force")
def get_redaction(_: Reader, database: Annotated[Database, Depends(get_database)]) -> RedactionRules:
    with database.connect() as conn:
        return _rules(redactor_for(conn))


@router.put("/privacy/redaction", response_model=RedactionRules, summary="Switch built-in rules off or on, and set your own")
def put_redaction(body: RedactionChoices, _: Editor, database: Annotated[Database, Depends(get_database)]) -> RedactionRules:
    try:
        choices = check_choices(body.disabled, [(rule.name, rule.words) for rule in body.custom])
    except RulesError as error:
        raise ApiError(400, "bad_request", str(error)) from None
    with database.connect() as conn:
        with transaction(conn):
            save_choices(conn, choices)
        return _rules(redactor_for(conn))


@router.post("/privacy/redaction/check", response_model=TitleCheckResult, summary="Try a title against the rules in force")
def check_title(body: TitleCheck, _: Reader, database: Annotated[Database, Depends(get_database)]) -> TitleCheckResult:
    with database.connect() as conn:
        rule = redactor_for(conn).match(body.title, body.app, body.app_id, body.domain)
    shown = body.title if body.title is not None else body.domain
    return TitleCheckResult(redacted=rule is not None, stored_as=REDACTED if rule else shown,
                            rule=rule.id if rule else None, rule_name=rule.name if rule else None)


class StoredMatches(BaseModel):
    matches: int = Field(description="Stored events whose title, app or site the rules in force would hide.")


class ApplyRules(BaseModel):
    confirm: bool = Field(description="Must be true: hiding stored words can't be undone.")


class Applied(BaseModel):
    redacted: int


@router.get("/privacy/redaction/stored", response_model=StoredMatches, summary="How many stored events the rules would hide")
async def stored_redaction(_: Reader, database: Annotated[Database, Depends(get_database)]) -> StoredMatches:
    return StoredMatches(matches=await run_in_threadpool(stored_matches, database))


@router.post("/privacy/redaction/apply", response_model=Applied, summary="Hide the stored events the rules match (can't be undone)")
async def apply_redaction(body: ApplyRules, _: Editor, database: Annotated[Database, Depends(get_database)]) -> Applied:
    if not body.confirm:
        raise ApiError(400, "bad_request", "hiding stored words can't be undone: send confirm true to go ahead")
    return Applied(redacted=await run_in_threadpool(stored_matches, database, apply=True))


# --- the network status (DT-45) -----------------------------------------------------------------------------------


class NetworkCounts(BaseModel):
    localhost: int = 0
    lan: int = 0
    tailscale: int = 0
    internet: int = 0


class BlockedDestination(BaseModel):
    host: str
    port: int
    count: int
    last: datetime


class Blocked(BaseModel):
    count: int = Field(description="Outgoing requests refused, all of them (the first 20 destinations are listed).")
    destinations: list[BlockedDestination]


class NetworkStatus(BaseModel):
    since: datetime = Field(description="When the hub started counting (when it started).")
    internet_connections: int = Field(description="Requests to or from the internet the hub made or served. Always 0: "
                                                  "the only way out refuses them, and so does the way in.")
    outgoing: NetworkCounts = Field(description="Requests the hub made (to the local model), by where they went.")
    blocked: Blocked = Field(description="Requests the hub would have made to an address it doesn't allow, refused.")
    incoming: NetworkCounts = Field(description="Requests the hub served, by where they came from.")
    refused: NetworkCounts = Field(description="Requests the hub refused, by where they came from: a network the "
                                               "profile doesn't serve, a Host name that could be DNS rebinding, or "
                                               "another web site's page.")
    listening: list[str] = Field(description="The addresses the hub listens on right now (empty in tests).")
    guarded: bool = Field(description="Whether the socket guard is on: nothing in the hub process can connect to the "
                                      "internet, whatever code asks (a real hub always; not in tests).")


class Storage(BaseModel):
    """DT-36: where the profile's data lives and how much there is."""

    profile: str
    folder: str | None = Field(description="The folder that holds the database, on the hub computer only (it names that "
                                            "computer's folders); null anywhere else.")
    file: str = Field(description="The database file's name.")
    size_bytes: int = Field(description="The database file with its write-ahead log.")
    events: int
    first_event: datetime | None = Field(description="When the earliest stored event started (UTC).")
    last_event: datetime | None = Field(description="When the latest stored event started (UTC), up to now: calendar "
                                                    "events synced ahead don't count.")
    devices: int = Field(description="Devices paired now (not revoked).")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:  # the write-ahead log goes when the last connection closes, at any moment
        return 0


def storage_of(database: Database, profile: str, local: bool, now: datetime) -> Storage:
    path = database.path
    size = _size(path) + _size(path.with_name(path.name + "-wal"))
    with database.connect() as conn:
        events, first = conn.execute("SELECT COUNT(*), MIN(start_utc) FROM events").fetchone()
        last = conn.execute("SELECT MAX(start_utc) FROM events WHERE start_utc <= ?", (utc_text(now),)).fetchone()[0]
        devices = conn.execute("SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0]
    return Storage(
        profile=profile, folder=str(path.parent) if local else None, file=path.name, size_bytes=size, events=events,
        first_event=datetime.fromisoformat(first) if first else None, last_event=datetime.fromisoformat(last) if last else None,
        devices=devices,
    )


@router.get("/privacy/storage", response_model=Storage, summary="Where the data lives and how much there is")
async def storage(request: Request, _: Reader, database: Annotated[Database, Depends(get_database)]) -> Storage:
    from .timeline import current_time

    profile = request.app.state.settings.profile.name
    return await run_in_threadpool(storage_of, database, profile, is_trusted_local(request), current_time())


@router.get("/privacy/network", response_model=NetworkStatus, summary="Every connection since the hub started, by network")
def network_status(_: Reader) -> NetworkStatus:
    found = LEDGER.snapshot()
    outgoing, incoming = NetworkCounts(**found["outgoing"]), NetworkCounts(**found["incoming"])
    return NetworkStatus(
        since=found["since"], internet_connections=outgoing.internet + incoming.internet, outgoing=outgoing,
        blocked=Blocked(count=found["blocked"]["count"], destinations=[BlockedDestination(**entry) for entry in found["blocked"]["destinations"]]),
        incoming=incoming, refused=NetworkCounts(**found["refused"]), listening=found["listening"], guarded=found["guarded"],
    )


# --- export and delete everything (DT-46) --------------------------------------------------------------------------

# The database's own bookkeeping, not your data: which migrations ran, and the change counter.
INTERNAL_TABLES = frozenset({"schema_migrations", "data_changes"})
# Columns that hold JSON: exported as JSON, not as a string of it.
JSON_COLUMNS = frozenset({("events", "data"), ("sessions", "source_event_ids"), ("settings", "value"), ("goals", "target"),
                          ("achievements", "dates"), ("story_cache", "facts"), ("wrapped_cache", "lines"),
                          ("wrapped_cache", "facts")})
# Never exported: a device's token is a secret (only its hash is kept, but even that stays in the hub).
SECRET_COLUMNS = frozenset({("devices", "token_hash")})
DELETE_PHRASE = "delete all my daytrace data"
EXPORT_CHUNK = 64 * 1024  # characters gathered before a piece of the export is sent
SNAPSHOT_PREFIX = ".daytrace-export-"  # the copy an export is read from, next to the database; removed afterwards
STALE_SNAPSHOT_SECONDS = 3600.0  # a copy left behind (the hub stopped mid-export) is removed after this


def data_tables(conn: sqlite3.Connection) -> list[tuple[str, bool]]:
    """Every table that holds the profile's data, with whether it can be read in rowid order: ordinary tables (a
    later migration's too, WITHOUT ROWID ones included) and virtual tables (a full-text index holds its own rows),
    never a virtual table's shadow tables (its internals, read and emptied through it) or SQLite's own."""
    if sqlite3.sqlite_version_info >= (3, 37):
        rows = [(row["name"], row["type"], row["wr"]) for row in conn.execute("PRAGMA main.table_list")]
    else:  # pragma: no cover (older SQLite: no shadow tables to tell apart, no WITHOUT ROWID check)
        rows = [(row["name"], "table", 0) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    found = [(name, kind == "table" and not without_rowid) for name, kind, without_rowid in rows
             if kind in ("table", "virtual") and not name.startswith("sqlite_") and name not in INTERNAL_TABLES]
    return sorted(found)


def _finite(value: Any) -> Any:
    """JSON has no NaN or Infinity: a value that isn't a finite number is exported as null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _row(table: str, row: sqlite3.Row) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for key in row.keys():  # noqa: SIM118 (a Row iterates over its values, not its names)
        if (table, key) in SECRET_COLUMNS:
            continue
        value = row[key]
        if (table, key) in JSON_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # kept as it was stored
        item[key] = _finite(value)
    return item


def remove_file(path: Path) -> None:
    for leftover in (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm")):
        leftover.unlink(missing_ok=True)


def take_snapshot(database: Database) -> Path:
    """A copy of the database as of now (VACUUM INTO), next to it, for an export to read at its own pace: the live
    database is never held while a download runs, and a delete-all isn't kept waiting. Copies an earlier export left
    behind are removed first."""
    folder = database.path.parent
    for old in folder.glob(f"{SNAPSHOT_PREFIX}*.db"):
        if time.time() - old.stat().st_mtime > STALE_SNAPSHOT_SECONDS:
            remove_file(old)
    path = folder / f"{SNAPSHOT_PREFIX}{uuid.uuid4().hex}.db"
    with database.connect() as conn:
        conn.execute("VACUUM INTO ?", (str(path),))
    return path


def export_chunks(snapshot: Path, profile: str, now: datetime | None = None) -> Iterator[str]:
    """The profile's data as one JSON document, in pieces, from a snapshot: every data table's rows (device tokens
    left out), all as of the moment the snapshot was taken."""
    conn = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        header = {"daytrace_export": 1, "profile": profile, "exported_at": (now or datetime.now(UTC)).isoformat(),
                  "schema_version": current_version(conn), "note": "Device tokens are never exported."}
        buffer = json.dumps(header, ensure_ascii=False)[:-1] + ', "tables": {'
        tables = data_tables(conn)
        for number, (table, by_rowid) in enumerate(tables):
            buffer += json.dumps(table) + ": ["
            first = True
            cursor = conn.execute(f'SELECT * FROM "{table}"' + (" ORDER BY rowid" if by_rowid else ""))
            while rows := cursor.fetchmany(500):
                for row in rows:
                    buffer += ("" if first else ", ") + json.dumps(_row(table, row), ensure_ascii=False, allow_nan=False, default=str)
                    first = False
                    if len(buffer) >= EXPORT_CHUNK:
                        yield buffer
                        buffer = ""
            buffer += "]" + (", " if number < len(tables) - 1 else "")
        yield buffer + "}}"
    finally:
        conn.close()


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[Any] = Field(default_factory=list)


class ErrorOut(BaseModel):
    error: ErrorInfo


@router.get(
    "/privacy/export", summary="Everything the profile holds, as one JSON file (the hub computer only)",
    dependencies=[Depends(require_local)], response_class=StreamingResponse,
    responses={200: {"description": "The export, as a download.", "content": {"application/json": {"schema": {"type": "object"}}}},
               403: {"model": ErrorOut, "description": "Not from the hub computer (local_only)."}},
)
def export_all(request: Request, database: Annotated[Database, Depends(get_database)]) -> StreamingResponse:
    from .timeline import resolve_tz

    profile = request.app.state.settings.profile.name
    zone, _ = resolve_tz(None)
    filename = f"daytrace-{profile}-{datetime.now(zone).date().isoformat()}.json"  # the hub's own day
    snapshot = take_snapshot(database)
    return StreamingResponse(export_chunks(snapshot, profile), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                             background=BackgroundTask(remove_file, snapshot))


class DeleteAll(BaseModel):
    confirm: str = Field(description=f'Exactly "{DELETE_PHRASE}", on every call.')
    keep_redaction_rules: bool = Field(default=True, description="Keep your own redaction words and the built-in rules "
                                       "you switched off, so what is recorded next stays protected (default).")


class Deleted(BaseModel):
    deleted: dict[str, int] = Field(description="Rows deleted from each table.")
    wiped: bool = Field(description="True when the deleted rows are gone from the file too (overwritten, the file "
                                    "compacted and its log folded back). False when another connection kept that from "
                                    "finishing just now; the rows are deleted and overwritten either way.")


def delete_everything(database: Database, keep_redaction_rules: bool = True) -> Deleted:
    """Empty every data table, keeping the schema (the hub keeps working, and devices pair again). Every connection
    overwrites what it deletes (secure_delete); then the file is compacted and its write-ahead log folded back, so
    the deleted rows are gone from the file, not just unlinked."""
    with database.connect() as conn:
        with transaction(conn):
            conn.execute("PRAGMA defer_foreign_keys = ON")  # checked at the end: any order empties them
            counts = {}
            for table, _ in data_tables(conn):
                if table == "settings" and keep_redaction_rules:
                    counts[table] = conn.execute('DELETE FROM "settings" WHERE key != ?', (REDACTION_KEY,)).rowcount
                else:
                    counts[table] = conn.execute(f'DELETE FROM "{table}"').rowcount
        try:
            conn.execute("VACUUM")  # a compacted copy, written through the log
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]  # the log folded into the file
            wiped = busy == 0
        except sqlite3.OperationalError:
            wiped = False
    return Deleted(deleted=counts, wiped=wiped)


def forget_in_memory(app: Any) -> None:
    """What the hub process still held of the deleted data: cached answers and redaction rules, and the pairing
    code (a code shown before the delete must not pair a device after it)."""
    from .. import nudges, redaction, streaks
    from . import devices as devices_api
    from . import insights as insights_api

    for cached in (insights_api._cache, streaks._cache, streaks._readings):
        cached.clear()
    nudges.forget()
    redaction._parse_choices.cache_clear()
    redaction._redactor.cache_clear()
    app.state.pairing = devices_api.PairingCodes()


@router.post(
    "/privacy/delete", response_model=Deleted, summary="Delete everything the profile holds (the hub computer only)",
    dependencies=[Depends(require_local)],
    responses={400: {"model": ErrorOut, "description": "The phrase was not exactly right: nothing was deleted."},
               403: {"model": ErrorOut, "description": "Not from the hub computer (local_only)."}},
)
def delete_all(body: DeleteAll, request: Request, database: Annotated[Database, Depends(get_database)]) -> Deleted:
    if body.confirm != DELETE_PHRASE:
        raise ApiError(400, "bad_request", f'to delete everything, confirm with exactly "{DELETE_PHRASE}"; nothing was deleted')
    tracker = getattr(request.app.state, "tracker", None)
    if tracker is not None:
        tracker.stop()  # it writes what it holds (deleted next), and starts afresh after
    try:
        result = delete_everything(database, body.keep_redaction_rules)
        forget_in_memory(request.app)
    finally:
        if tracker is not None:
            tracker.restart()
    return result
