"""DT-14: GET/PUT/DELETE /api/v1/categories.

GET lists the categories and every app or site seen in the last 30 days with its category and where that
came from, so the dashboard can show "is this right?" and let the user fix it. PUT saves the user's choice;
DELETE removes an override (the dashboard sends the row's `override` key). Only the dashboard (a viewer token,
or the dashboard on the hub computer) can change categories.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Editor, Reader, get_database
from ..categories import (
    CATEGORIES,
    Categorizer,
    CategorySource,
    canonical_key,
    delete_override,
    load_overrides,
    override_key,
    save_override,
)
from ..db import Database, transaction, utc_text
from ..models import Category, has_hidden_characters
from . import API_PREFIX, ApiError

SEEN_DAYS = 30
MAX_APPS = 500
MAX_KEY_LENGTH = 200
AppKey = Annotated[str, Path(description="An app id, app name or web domain (slashes allowed).")]

router = APIRouter(prefix=API_PREFIX, tags=["categories"])


class AppCategory(BaseModel):
    key: str = Field(description="PUT /categories/{key} sets this app's category.")
    app: str = Field(description="The name to show: the app name, or the app id or domain when there is none.")
    app_id: str | None
    kind: str
    category: str
    source: CategorySource
    override: str | None = Field(description="The override that decided (user or ai); DELETE it to undo.")
    events: int = Field(description=f"Events for this app in the last {SEEN_DAYS} days (for sorting).")


class OverrideOut(BaseModel):
    key: str
    category: str
    source: str
    updated_at: str


class CategoryList(BaseModel):
    categories: list[str]
    apps: list[AppCategory]
    overrides: list[OverrideOut]


class CategoryChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category


@dataclass
class _Seen:
    """Everything seen for one app key, merged across spellings (Code, Code.exe, www.youtube.com, ...)."""

    kind: str
    names: dict[str, int] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    events: int = 0
    hint: str | None = None
    hint_at: str = ""

    def add(self, name: str | None, app_id: str | None, events: int, hint: str | None, latest: str) -> None:
        self.events += events
        if name:
            self.names[name] = self.names.get(name, 0) + events
        if app_id:
            self.ids[app_id] = self.ids.get(app_id, 0) + events
        if hint is not None and latest > self.hint_at:  # the most recent hint, as the timeline would use
            self.hint, self.hint_at = hint, latest


def _most_used(counts: dict[str, int]) -> str | None:
    return max(counts, key=lambda text: (counts[text], text)) if counts else None


def _clean_key(key: str) -> str:
    cleaned = canonical_key(key)
    if not cleaned or len(cleaned) > MAX_KEY_LENGTH or has_hidden_characters(cleaned):
        raise ApiError(400, "bad_request", "the key must be an app id, app name or domain of 1 to 200 characters")
    return cleaned


@router.get("/categories", response_model=CategoryList, summary="Categories, and the apps seen recently")
def list_categories(_: Reader, database: Annotated[Database, Depends(get_database)]) -> CategoryList:
    since = utc_text(datetime.now(UTC) - timedelta(days=SEEN_DAYS))
    with database.connect() as conn:
        categorizer = Categorizer.from_db(conn)
        overrides = load_overrides(conn)
        rows = conn.execute(
            "SELECT CASE WHEN kind = 'web' THEN json_extract(data, '$.domain') ELSE app END AS name,"
            " CASE WHEN kind = 'web' THEN NULL ELSE app_id END AS app_id,"
            " CASE WHEN kind = 'web' THEN 'web' ELSE 'app' END AS kind,"
            " category, COUNT(*) AS events, MAX(start_utc) AS latest"
            " FROM events WHERE kind IN ('app_session', 'window', 'web', 'app_open') AND start_utc >= ?"
            " GROUP BY 1, 2, 3, 4",
            (since,),
        ).fetchall()
    seen: dict[tuple[str, str], _Seen] = defaultdict(lambda: _Seen(kind="app"))
    for row in rows:
        key = override_key(row["name"], row["app_id"], row["kind"])
        if not key:
            continue  # nothing to show or to categorize by
        entry = seen[(row["kind"], key)]
        entry.kind = row["kind"]
        entry.add(row["name"], row["app_id"], row["events"], row["category"], row["latest"])
    apps = []
    for (kind, key), entry in sorted(seen.items(), key=lambda item: (-item[1].events, item[0][1]))[:MAX_APPS]:
        name, app_id = _most_used(entry.names), _most_used(entry.ids)
        found = categorizer.lookup(name, app_id, kind, entry.hint)
        apps.append(
            AppCategory(
                key=key, app=name or app_id or key, app_id=app_id, kind=kind, category=found.category,
                source=found.source, override=found.override_key, events=entry.events,
            )
        )
    return CategoryList(
        categories=list(CATEGORIES),
        apps=apps,
        overrides=[
            OverrideOut(key=key, category=o.category, source=o.source, updated_at=o.updated_at)
            for key, o in sorted(overrides.items())
        ],
    )


@router.put("/categories/{key:path}", response_model=OverrideOut, summary="Set the category of an app or site")
def put_category(
    key: AppKey, body: CategoryChoice, _: Editor, database: Annotated[Database, Depends(get_database)]
) -> OverrideOut:
    cleaned = _clean_key(key)
    with database.connect() as conn, transaction(conn):
        saved = save_override(conn, cleaned, body.category.value, "user")
    return OverrideOut(key=cleaned, category=saved.category, source=saved.source, updated_at=saved.updated_at)


@router.delete(
    "/categories/{key:path}", status_code=204, response_class=Response, summary="Remove a category override"
)
def delete_category(key: AppKey, _: Editor, database: Annotated[Database, Depends(get_database)]) -> Response:
    cleaned = _clean_key(key)
    with database.connect() as conn, transaction(conn):
        if not delete_override(conn, cleaned):
            raise ApiError(404, "not_found", f"no category override for {cleaned!r}")
    return Response(status_code=204)
