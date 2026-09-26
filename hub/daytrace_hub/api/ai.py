"""DT-37 / DT-39 / DT-40: AI status, story and ask endpoints.

DT-37: GET /ai/status (the local model server, docs/api.md "AI"). DT-39: GET /story, the day story with its
number check. DT-40: POST /ask, a question answered through tool calls, with the same number check.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..ask import MAX_QUESTION_CHARS, ask
from ..auth import Reader, get_database
from ..db import Database
from ..llm import LLM, LLMError
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
    fallback: bool = Field(description="True when the template story was used (model away, or unusable twice).")
    reason: str | None = Field(description="Why the template story was used.")
    in_progress: bool = Field(description="True while the day is not over: the story is of the day so far.")


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
        in_progress=result.in_progress,
    )


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    tz: str | None = Field(default=None, description="IANA time zone, e.g. America/Toronto; default: the hub's")


class ChartPoint(BaseModel):
    label: str
    value: float


class Chart(BaseModel):
    kind: str = Field(description="bar")
    title: str
    unit: str
    points: list[ChartPoint]


class Answer(BaseModel):
    answer: str
    facts_used: list[FactOut] = Field(description="Every number in the answer matches one of these.")
    tools_called: list[str]
    chart: Chart | None = Field(description="A small series the dashboard can draw, when a tool returned one.")
    model: str | None = Field(description="The local model that wrote it; null when the facts are shown instead.")
    fallback: bool = Field(description="True when the model's answer failed the number check twice: the facts are shown.")
    declined: bool = Field(description="True when the question was not about your day.")
    reason: str | None = Field(description="Why the facts are shown instead of an answer.")


@router.post("/ask", response_model=Answer, summary="Ask about your day: answered from your data through tool calls")
def post_ask(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    llm: Annotated[LLM, Depends(get_llm)],
    body: AskIn,
) -> Answer:
    """503 `ai_unavailable` when the local model can't be reached (unlike /story, there is no answer without it)."""
    zone, zone_name = resolve_tz(body.tz)
    if not body.question.strip():
        raise ApiError(400, "bad_request", "question must not be empty")
    try:
        result = ask(database, llm, body.question, zone, zone_name, current_time())
    except LLMError as error:
        raise ApiError(503, "ai_unavailable", str(error)) from error
    return Answer(
        answer=result.answer, facts_used=[FactOut(**f.as_dict()) for f in result.facts], tools_called=result.tools_called,
        chart=Chart(**result.chart) if result.chart else None, model=result.model, fallback=result.fallback,
        declined=result.declined, reason=result.reason,
    )
