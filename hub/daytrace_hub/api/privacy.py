"""The Privacy page's API. DT-44: the redaction rules (see them, change them, try a title against them).
DT-45 / DT-46 add the network status, export and delete-all.

Reading the rules needs a paired device or the local dashboard; changing them needs the dashboard (a viewer token,
or the dashboard on the hub computer), as categories and goals do. A change applies to the next event stored.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..auth import Editor, Reader, get_database
from ..db import Database, transaction
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
