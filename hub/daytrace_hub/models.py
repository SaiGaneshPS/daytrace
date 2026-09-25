"""DT-9: Pydantic models for events, batches, ingest results and nudges.

These mirror docs/event-schema.json (the contract every collector follows) and add the cross-field rules
JSON Schema cannot express, such as an end time never being before the start time.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

MAX_SAFE_INTEGER = 9_007_199_254_740_991  # largest integer JavaScript (and Shortcuts) can represent exactly
MAX_BATCH_EVENTS = 500  # enforced by the ingest endpoint (413 when exceeded), see DT-11

DEVICE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
# Same rule as $defs.timestamp in docs/event-schema.json.
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


def _timestamp_text(value: Any) -> Any:
    """Accept ISO 8601 strings with an offset (what collectors send) or datetimes (what the hub builds itself).

    Pydantic alone would also accept Unix numbers and other formats; the contract does not.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.match(value):
        raise ValueError("times must be ISO 8601 strings with a time zone offset, like 2026-09-25T14:03:10-04:00")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp_text)]


class Kind(StrEnum):
    APP_SESSION = "app_session"
    WINDOW = "window"
    WEB = "web"
    AFK = "afk"
    SLEEP = "sleep"
    STEPS = "steps"
    CALENDAR_EVENT = "calendar_event"
    APP_OPEN = "app_open"
    APP_CLOSE = "app_close"
    SCREEN_ON = "screen_on"
    SCREEN_OFF = "screen_off"
    MEAL = "meal"


SPAN_KINDS = frozenset(
    {Kind.APP_SESSION, Kind.WINDOW, Kind.WEB, Kind.AFK, Kind.SLEEP, Kind.STEPS, Kind.CALENDAR_EVENT}
)
POINT_KINDS = frozenset({Kind.APP_OPEN, Kind.APP_CLOSE, Kind.SCREEN_ON, Kind.SCREEN_OFF, Kind.MEAL})


class Source(StrEnum):
    TRACKER = "tracker"
    USAGESTATS = "usagestats"
    SHORTCUTS = "shortcuts"
    HEALTH_CONNECT = "health_connect"
    HEALTHKIT = "healthkit"
    CALENDAR = "calendar"
    BROWSER = "browser"
    SCREENTIME_BRIDGE = "screentime_bridge"
    MANUAL = "manual"
    SEED = "seed"


class Category(StrEnum):
    SOCIAL = "social"
    VIDEO = "video"
    WORK = "work"
    STUDY = "study"
    COMMS = "comms"
    GAMES = "games"
    HEALTH = "health"
    OTHER = "other"


SLEEP_STAGES = frozenset({"in_bed", "asleep", "awake", "core", "deep", "rem"})
MEAL_TYPES = frozenset({"breakfast", "lunch", "dinner", "snack"})


class Event(BaseModel):
    """One thing that happened on one device."""

    model_config = ConfigDict(extra="forbid")

    device_id: Annotated[str, Field(pattern=DEVICE_ID_PATTERN)]
    seq: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)]
    kind: Kind
    source: Source
    start: Timestamp
    end: Timestamp | None = None
    app: Annotated[str, Field(max_length=200)] | None = None
    app_id: Annotated[str, Field(max_length=200)] | None = None
    title: Annotated[str, Field(max_length=500)] | None = None
    category: Category | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_kind_rules(self) -> Event:
        if self.kind in SPAN_KINDS:
            if self.end is None:
                raise ValueError(f"{self.kind.value} events need an end time")
            if self.end < self.start:
                raise ValueError("end must not be before start")
        elif self.end is not None:
            raise ValueError(f"{self.kind.value} events are a single moment and must not have an end time")

        if self.kind is Kind.STEPS:
            count = self.data.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("steps events need data.count as a whole number of 0 or more")
        elif self.kind is Kind.SLEEP:
            stage = self.data.get("stage")
            if stage is not None and stage not in SLEEP_STAGES:
                raise ValueError(f"sleep data.stage must be one of {sorted(SLEEP_STAGES)}")
            if "measured" in self.data and not isinstance(self.data["measured"], bool):
                raise ValueError("sleep data.measured must be true or false")
        elif self.kind is Kind.MEAL:
            _check_meal_data(self.data)
        elif self.kind is Kind.WEB:
            domain = self.data.get("domain")
            if not isinstance(domain, str) or not DOMAIN_PATTERN.match(domain):
                raise ValueError("web events need data.domain as a bare domain such as youtube.com")
            if self.title is not None:
                raise ValueError("web events must not carry a page title")
        return self


def _check_meal_data(data: dict[str, Any]) -> None:
    text = data.get("text")
    items = data.get("items")
    if text is None and items is None:
        raise ValueError("meal events need data.text or data.items")
    if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= 500):
        raise ValueError("meal data.text must be 1 to 500 characters")
    if items is not None:
        if not isinstance(items, list) or len(items) > 30:
            raise ValueError("meal data.items must be a list of at most 30 items")
        if any(not isinstance(item, str) or not 1 <= len(item) <= 100 for item in items):
            raise ValueError("each meal item must be 1 to 100 characters")
    meal_type = data.get("meal_type")
    if meal_type is not None and meal_type not in MEAL_TYPES:
        raise ValueError(f"meal data.meal_type must be one of {sorted(MEAL_TYPES)}")


class EventBatch(BaseModel):
    """What POST /api/v1/events accepts: either one event object or {"events": [...]}."""

    model_config = ConfigDict(extra="forbid")

    events: Annotated[list[Event], Field(min_length=1)]

    @model_validator(mode="before")
    @classmethod
    def _wrap_single_event(cls, value: Any) -> Any:
        if isinstance(value, dict) and "events" not in value:
            return {"events": [value]}
        return value


class Nudge(BaseModel):
    """A gentle, timely prompt returned to the device that sent the triggering event (DT-43)."""

    rule: str
    title: Annotated[str, Field(max_length=80)]
    body: Annotated[str, Field(max_length=240)]
    created_at: AwareDatetime


class IngestResult(BaseModel):
    """Response of POST /api/v1/events."""

    accepted: Annotated[int, Field(ge=0, description="New events stored by this request.")]
    duplicates: Annotated[int, Field(ge=0, description="Events ignored because (device_id, seq) was already stored.")]
    last_seq: Annotated[int | None, Field(description="Highest seq stored for this device, or null.")] = None
    nudge: Nudge | None = None
