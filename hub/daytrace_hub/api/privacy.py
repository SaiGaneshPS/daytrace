"""The Privacy page's API. DT-44: the redaction rules (see them, change them, try a title against them).
DT-45 / DT-46 add the network status, export and delete-all.

Reading the rules needs a paired device or the local dashboard; changing them needs the dashboard (a viewer token,
or the dashboard on the hub computer), as categories and goals do. A change applies to the next event stored.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import Editor, Reader, get_database
from ..db import Database, transaction
from ..redaction import (
    MAX_CUSTOM_RULES,
    MAX_WORDS,
    REDACTED,
    Redactor,
    RulesError,
    builtin_rules,
    check_choices,
    redactor_for,
    save_choices,
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


class CustomRule(BaseModel):
    name: str
    words: list[str] = Field(description="Words or phrases, matched whole and ignoring case, in titles and app names.")


class RedactionChoices(BaseModel):
    disabled: list[str] = Field(default_factory=list, description="Built-in rules to switch off, by id.")
    custom: list[CustomRule] = Field(default_factory=list, description=f"Your own rules, up to {MAX_CUSTOM_RULES}, "
                                                                          f"each with up to {MAX_WORDS} words or phrases.")


class TitleCheck(BaseModel):
    title: str = Field(max_length=500)
    app: str | None = Field(default=None, max_length=200)
    app_id: str | None = Field(default=None, max_length=200)


class TitleCheckResult(BaseModel):
    redacted: bool
    stored_as: str = Field(description="The title as the hub would store it.")
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
        rule = redactor_for(conn).match(body.title, body.app, body.app_id)
    return TitleCheckResult(redacted=rule is not None, stored_as=REDACTED if rule else body.title,
                            rule=rule.id if rule else None, rule_name=rule.name if rule else None)
