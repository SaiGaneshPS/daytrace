"""DT-14: GET/PUT/DELETE /api/v1/categories.

GET lists the categories and every app or site seen in the last 30 days with its category and where that
came from, so the dashboard can show "is this right?" and let the user fix it. PUT saves the user's choice;
DELETE goes back to the built-in one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Reader, get_database
from ..categories import (
    CATEGORIES,
    Categorizer,
    CategorySource,
    delete_override,
    load_overrides,
    normalize_key,
    override_key,
    save_override,
)
from ..db import Database, transaction, utc_text
from ..models import Category
from . import API_PREFIX, ApiError

SEEN_DAYS = 30
MAX_APPS = 500
AppKey = Annotated[str, Path(min_length=1, max_length=200, description="An app name, app id or web domain.")]

router = APIRouter(prefix=API_PREFIX, tags=["categories"])


class AppCategory(BaseModel):
    key: str = Field(description="Send this to PUT /categories/{key} to change the category.")
    app: str
    app_id: str | None
    kind: str
    category: str
    source: CategorySource
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


def _clean_key(key: str) -> str:
    cleaned = normalize_key(key)
    if not cleaned or any(ord(char) < 32 for char in cleaned):
        raise ApiError(400, "bad_request", "the app key must be an app name, app id or domain")
    return cleaned


@router.get("/categories", response_model=CategoryList, summary="Categories, and the apps seen recently")
def list_categories(_: Reader, database: Annotated[Database, Depends(get_database)]) -> CategoryList:
    since = utc_text(datetime.now(UTC) - timedelta(days=SEEN_DAYS))
    with database.connect() as conn:
        categorizer = Categorizer.from_db(conn)
        rows = conn.execute(
            "SELECT CASE WHEN kind = 'web' THEN json_extract(data, '$.domain') ELSE app END AS name,"
            " CASE WHEN kind = 'web' THEN NULL ELSE app_id END AS app_id,"
            " CASE WHEN kind = 'web' THEN 'web' ELSE 'app' END AS kind,"
            " MAX(category) AS collector, COUNT(*) AS events"
            " FROM events WHERE kind IN ('app_session', 'window', 'web', 'app_open') AND start_utc >= ?"
            " GROUP BY 1, 2, 3 HAVING name IS NOT NULL ORDER BY events DESC, name LIMIT ?",
            (since, MAX_APPS),
        ).fetchall()
        overrides = load_overrides(conn)
    apps = []
    for row in rows:
        category, source = categorizer.lookup(row["name"], row["app_id"], row["kind"], row["collector"])
        key = override_key(row["name"], row["app_id"], row["kind"])
        assert key is not None
        apps.append(
            AppCategory(
                key=key, app=row["name"], app_id=row["app_id"], kind=row["kind"], category=category,
                source=source, events=row["events"],
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


@router.put("/categories/{key}", response_model=OverrideOut, summary="Set the category of an app or site")
def put_category(
    key: AppKey, body: CategoryChoice, _: Reader, database: Annotated[Database, Depends(get_database)]
) -> OverrideOut:
    cleaned = _clean_key(key)
    with database.connect() as conn, transaction(conn):
        saved = save_override(conn, cleaned, body.category.value, "user")
    return OverrideOut(key=cleaned, category=saved.category, source=saved.source, updated_at=saved.updated_at)


@router.delete(
    "/categories/{key}", status_code=204, response_class=Response, summary="Go back to the built-in category"
)
def delete_category(key: AppKey, _: Reader, database: Annotated[Database, Depends(get_database)]) -> Response:
    cleaned = _clean_key(key)
    with database.connect() as conn, transaction(conn):
        if not delete_override(conn, cleaned):
            raise ApiError(404, "not_found", f"no category override for {cleaned!r}")
    return Response(status_code=204)
