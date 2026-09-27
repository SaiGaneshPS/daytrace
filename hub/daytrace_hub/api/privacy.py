"""The Privacy page's API. DT-44: the redaction rules (see them, change them, try a title against them). DT-45:
the network status, the proof that the hub talks to nothing but this computer, your LAN and (for shared-dev) your
tailnet. DT-46: export everything, and delete everything (both only from the hub computer itself).

Reading the rules needs a paired device or the local dashboard; changing them needs the dashboard (a viewer token,
or the dashboard on the hub computer), as categories and goals do. A change applies to the next event stored.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..auth import Editor, Reader, get_database, require_local
from ..config import LEDGER
from ..db import Database, current_version, transaction
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
INTERNAL_TABLES = frozenset({"schema_migrations", "data_changes", "sqlite_sequence"})
# Columns that hold JSON: exported as JSON, not as a string of it.
JSON_COLUMNS = frozenset({("events", "data"), ("sessions", "source_event_ids"), ("settings", "value"), ("goals", "target"),
                          ("achievements", "dates"), ("story_cache", "facts"), ("wrapped_cache", "lines"),
                          ("wrapped_cache", "facts")})
# Never exported: a device's token is a secret (only its hash is kept, but even that stays in the hub).
SECRET_COLUMNS = frozenset({("devices", "token_hash")})
DELETE_PHRASE = "delete all my daytrace data"
EXPORT_CHUNK = 64 * 1024  # characters gathered before a piece of the export is sent


def data_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table that holds the profile's data (so a table a later migration adds is exported and deleted too),
    in name order."""
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [name for name in names if name not in INTERNAL_TABLES]


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
        item[key] = value
    return item


def export_chunks(database: Database, profile: str, now: datetime | None = None) -> Iterator[str]:
    """The profile's data as one JSON document, in pieces: every data table's rows (device tokens left out), read
    in one snapshot, so the export is consistent even while devices keep syncing."""
    with database.connect() as conn:
        conn.execute("BEGIN")  # a read transaction: every table as of one moment
        try:
            header = {"daytrace_export": 1, "profile": profile, "exported_at": (now or datetime.now(UTC)).isoformat(),
                      "schema_version": current_version(conn), "note": "Device tokens are never exported."}
            buffer = json.dumps(header, ensure_ascii=False)[:-1] + ', "tables": {'
            tables = data_tables(conn)
            for number, table in enumerate(tables):
                buffer += json.dumps(table) + ": ["
                first = True
                cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                while rows := cursor.fetchmany(500):
                    for row in rows:
                        buffer += ("" if first else ", ") + json.dumps(_row(table, row), ensure_ascii=False, default=str)
                        first = False
                        if len(buffer) >= EXPORT_CHUNK:
                            yield buffer
                            buffer = ""
                buffer += "]" + (", " if number < len(tables) - 1 else "")
            yield buffer + "}}"
        finally:
            if conn.in_transaction:
                conn.execute("COMMIT")


@router.get("/privacy/export", summary="Everything the profile holds, as one JSON file (the hub computer only)",
            dependencies=[Depends(require_local)], response_class=StreamingResponse)
def export_all(request: Request, database: Annotated[Database, Depends(get_database)]) -> StreamingResponse:
    profile = request.app.state.settings.profile.name
    filename = f"daytrace-{profile}-{datetime.now(UTC).date().isoformat()}.json"
    return StreamingResponse(export_chunks(database, profile), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


class DeleteAll(BaseModel):
    confirm: str = Field(description=f'Exactly "{DELETE_PHRASE}", on every call.')


class Deleted(BaseModel):
    deleted: dict[str, int] = Field(description="Rows deleted from each table.")
    wiped: bool = Field(description="True when the deleted rows are gone from the file too (overwritten, the file "
                                    "compacted). False when another connection kept that from finishing just now; the "
                                    "rows are deleted either way, and SQLite overwrites them as the file is used.")


def delete_everything(database: Database) -> Deleted:
    """Empty every data table, keeping the schema (the hub keeps working, and devices pair again). With
    secure_delete on, the deleted rows are overwritten with zeros; then the write-ahead log is folded back and the
    file compacted, so nothing deleted stays on the disk."""
    with database.connect() as conn:
        conn.execute("PRAGMA secure_delete = ON")
        with transaction(conn):
            tables = sorted(data_tables(conn), key=lambda name: name == "devices")  # devices last: events point at them
            counts = {table: conn.execute(f'DELETE FROM "{table}"').rowcount for table in tables}
        try:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            conn.execute("VACUUM")
            wiped = busy == 0
        except sqlite3.OperationalError:
            wiped = False
    return Deleted(deleted=counts, wiped=wiped)


@router.post("/privacy/delete", response_model=Deleted, summary="Delete everything the profile holds (the hub computer only)",
             dependencies=[Depends(require_local)])
def delete_all(body: DeleteAll, database: Annotated[Database, Depends(get_database)]) -> Deleted:
    if body.confirm != DELETE_PHRASE:
        raise ApiError(400, "bad_request", f'to delete everything, confirm with exactly "{DELETE_PHRASE}"; nothing was deleted')
    return delete_everything(database)
