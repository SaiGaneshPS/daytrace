"""DT-37 / DT-39 / DT-40: AI status, story and ask endpoints.

DT-37: GET /ai/status (the local model server, docs/api.md "AI"). DT-39 adds the story and DT-40 "Ask your day".
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..auth import Reader
from ..llm import LLM
from . import API_PREFIX

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
