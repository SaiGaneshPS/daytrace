"""DT-37 / DT-39 / DT-40: AI status, story and ask endpoints.

DT-37: GET /ai/status (the local model server, docs/api.md "AI"). DT-39: GET /story, the day story with its
number check. DT-40 adds "Ask your day".
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..db import Database
from ..llm import LLM
from ..story import day_story
from . import API_PREFIX, ApiError
from .timeline import EARLIEST, LATEST, current_time, resolve_tz

router = APIRouter(prefix=API_PREFIX, tags=["ai"])


class AiStatus(BaseModel):
    base_url: str
    model: str | None
    reachable: bool
    tool_calling: bool | None
    models: list[str]
    error: str | None


def get_llm(request: Request) -> LLM:
    return request.app.state.llm  # type: ignore[no-any-return]


@router.get("/ai/status", response_model=AiStatus, summary="The local model server: reachable, model, tool calling")
def ai_status(_: Reader, llm: Annotated[LLM, Depends(get_llm)]) -> AiStatus:
    """Always 200: when the model can't be used, `reachable` or `tool_calling` says so and `error` explains."""
    return AiStatus(**asdict(llm.status()))


class FactOut(BaseModel):
    label: str
    value: float | int | str
    unit: str


class Story(BaseModel):
    date: date
    tz: str
    story: str
    facts_used: list[FactOut] = Field(description="Every number in the story matches one of these.")
    model: str | None = Field(description="The local model that wrote it; null for the template story.")
    cached: bool
    fallback: bool = Field(description="True when the template story was used (model away, or numbers wrong twice).")
    reason: str | None = Field(description="Why the template story was used.")


@router.get("/story", response_model=Story, summary="A short story of one day, every number checked")
def get_story(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    llm: Annotated[LLM, Depends(get_llm)],
    day: Annotated[date | None, Query(alias="date", description="YYYY-MM-DD; default: today in tz")] = None,
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> Story:
    zone, zone_name = resolve_tz(tz)
    now = current_time()
    day = day or now.astimezone(zone).date()
    if not EARLIEST <= day <= LATEST:
        raise ApiError(400, "bad_request", f"date must be between {EARLIEST} and {LATEST}")
    result = day_story(database, llm, day, zone, zone_name, now)
    return Story(
        date=day, tz=zone_name, story=result.story, facts_used=[FactOut(**f.as_dict()) for f in result.facts],
        model=result.model, cached=result.cached, fallback=result.fallback, reason=result.reason,
    )
