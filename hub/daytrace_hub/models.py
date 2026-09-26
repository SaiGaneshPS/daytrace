"""DT-9: Pydantic models for events, batches, ingest results and nudges.

These mirror docs/event-schema.json (the contract every collector follows) and add the rules JSON Schema
cannot express: an end time never before the start time, real calendar dates (no February 30), and times
that still exist once converted to UTC (the hub stores UTC).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

MAX_SAFE_INTEGER = 9_007_199_254_740_991  # largest integer JavaScript (and Shortcuts) can represent exactly
MAX_BATCH_EVENTS = 500

DEVICE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
EXTERNAL_ID_PATTERN = r"^[!-~]{1,200}$"  # printable ASCII, no spaces
# Same rules as docs/event-schema.json. ASCII digits only; seconds and fractions are optional.
TIMESTAMP_PATTERN = re.compile(
    r"[1-9][0-9]{3}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[Tt]([01][0-9]|2[0-3]):[0-5][0-9]"
    r"(:[0-5][0-9](\.[0-9]{1,9})?)?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])"
)
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_IPV4_PART = r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
DOMAIN_PATTERN = re.compile(
    rf"(?=.{{1,253}}$)(({_IPV4_PART}\.){{3}}{_IPV4_PART}|\[[0-9A-Fa-f:.]{{2,45}}\]|({_LABEL}\.)*{_LABEL})"
)
APP_ID_WEB_PATTERN = re.compile(r"[^\s/]{1,200}")


def _timestamp_text(value: Any) -> Any:
    """Accept ISO 8601 strings with an offset (what collectors send) or datetimes (what the hub builds itself).

    Pydantic alone would also accept Unix numbers and other formats; the contract does not.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError("times must be ISO 8601 with a time zone offset, like 2026-09-25T14:03:10-04:00")
    return value


def _whole_number(value: Any) -> Any:
    """Integers, or floats with nothing after the point (42.0), like JSON Schema's "integer". Not bools or text."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        # ValueError on purpose: Pydantic turns it into a 422-style validation error; a TypeError would crash (500).
        raise ValueError("must be a whole number")  # noqa: TRY004
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("must be a whole number")
        return int(value)
    return value


def _storable_time(value: datetime) -> datetime:
    """The hub stores times in UTC; 9999-12-31T23:30-05:00 is valid text but has no UTC equivalent."""
    try:
        utc_year = value.astimezone(UTC).year
    except OverflowError:
        raise ValueError("time is outside the supported range once converted to UTC") from None
    if utc_year < 1000:
        raise ValueError("time is outside the supported range once converted to UTC")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp_text), AfterValidator(_storable_time)]
WholeNumber = Annotated[int, BeforeValidator(_whole_number)]


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
    seq: Annotated[WholeNumber, Field(ge=0, le=MAX_SAFE_INTEGER)] | None = None
    external_id: Annotated[str, Field(pattern=EXTERNAL_ID_PATTERN)] | None = None
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
            _check_steps_data(self.data)
        elif self.kind is Kind.SLEEP:
            _check_sleep_data(self.data)
        elif self.kind is Kind.MEAL:
            _check_meal_data(self.data)
        elif self.kind is Kind.WEB:
            self._check_web()
        return self

    def _check_web(self) -> None:
        if set(self.data) != {"domain"}:
            raise ValueError("web events carry only data.domain (no URLs, titles or other fields)")
        domain = self.data["domain"]
        if not isinstance(domain, str) or not DOMAIN_PATTERN.fullmatch(domain):
            raise ValueError("web events need data.domain as a bare host such as youtube.com")
        if self.title is not None:
            raise ValueError("web events must not carry a page title")
        if self.app_id is not None and not APP_ID_WEB_PATTERN.fullmatch(self.app_id):
            raise ValueError("web events may only use app_id for the browser's own identifier")
        self.data["domain"] = domain.lower()

    def dedup_key(self) -> str:
        """The key that makes resending safe. Stored as UNIQUE(device_id, dedup_key) by the hub.

        external_id wins (the event replaces an older copy with the same ID), then seq, then a hash of the
        content for stateless collectors such as iPhone Shortcuts.
        """
        if self.external_id is not None:
            return f"ext:{self.external_id}"
        if self.seq is not None:
            return f"seq:{self.seq}"
        content = {
            "kind": self.kind.value,
            "start": self.start.astimezone(UTC).isoformat(),
            "end": self.end.astimezone(UTC).isoformat() if self.end else None,
            "app": self.app,
            "app_id": self.app_id,
            "title": self.title,
            "data": self.data,
        }
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"content:{digest}"

    @property
    def replaces_existing(self) -> bool:
        """True when a newer copy should overwrite a stored event with the same key (external IDs only)."""
        return self.external_id is not None


def _is_blank(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


def _check_steps_data(data: dict[str, Any]) -> None:
    count = data.get("count")
    if isinstance(count, float) and count.is_integer():
        count = data["count"] = int(count)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("steps events need data.count as a whole number of 0 or more")


def _check_sleep_data(data: dict[str, Any]) -> None:
    if "stage" in data and (not isinstance(data["stage"], str) or data["stage"] not in SLEEP_STAGES):
        raise ValueError(f"sleep data.stage must be one of {sorted(SLEEP_STAGES)}")
    if "measured" in data and not isinstance(data["measured"], bool):
        raise ValueError("sleep data.measured must be true or false")


def _check_meal_data(data: dict[str, Any]) -> None:
    if "text" not in data and "items" not in data:
        raise ValueError("meal events need data.text or data.items")
    if "text" in data:
        text = data["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 500 or _is_blank(text):
            raise ValueError("meal data.text must be 1 to 500 characters and not just spaces")
    if "items" in data:
        items = data["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            raise ValueError("meal data.items must be a list of 1 to 30 items")
        if any(not isinstance(item, str) or not 1 <= len(item) <= 100 or _is_blank(item) for item in items):
            raise ValueError("each meal item must be 1 to 100 characters and not just spaces")
    if "meal_type" in data and (not isinstance(data["meal_type"], str) or data["meal_type"] not in MEAL_TYPES):
        raise ValueError(f"meal data.meal_type must be one of {sorted(MEAL_TYPES)}")


class EventBatch(BaseModel):
    """The request body of POST /api/v1/events, for documentation and strict checks: one event or {"events": [...]}.

    The ingest endpoint itself uses parse_batch(), which reports bad events one by one instead of failing
    the whole batch.
    """

    model_config = ConfigDict(extra="forbid")

    events: Annotated[list[Event], Field(min_length=1, max_length=MAX_BATCH_EVENTS)]

    @model_validator(mode="before")
    @classmethod
    def _wrap_single_event(cls, value: Any) -> Any:
        if isinstance(value, dict) and "events" not in value:
            return {"events": [value]}
        return value


class RejectedEvent(BaseModel):
    """One event the hub refused, so the collector can drop it instead of resending it forever."""

    index: int
    seq: int | None = None
    external_id: str | None = None
    reason: str


class MalformedBatchError(ValueError):
    """The body is not one event object or {"events": [...]} (HTTP 400)."""


class BatchTooLargeError(ValueError):
    """More than MAX_BATCH_EVENTS events in one request (HTTP 413)."""


def parse_batch(payload: Any, expected_device_id: str | None = None) -> tuple[list[Event], list[RejectedEvent]]:
    """Validate each event on its own. Good events are returned; bad ones come back as RejectedEvent.

    The batch size is checked before any event is validated, so an oversized body costs nothing.
    When expected_device_id is given (the device the token belongs to), events for any other device are rejected.
    """
    if isinstance(payload, dict) and "events" in payload:
        if set(payload) != {"events"} or not isinstance(payload["events"], list):
            raise MalformedBatchError('send one event object, or {"events": [...]} with nothing else')
        items = payload["events"]
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise MalformedBatchError('send one event object, or {"events": [...]}')
    if not items:
        raise MalformedBatchError("the batch is empty")
    if len(items) > MAX_BATCH_EVENTS:
        raise BatchTooLargeError(f"at most {MAX_BATCH_EVENTS} events per request")

    events: list[Event] = []
    rejected: list[RejectedEvent] = []
    for index, raw in enumerate(items):
        seq = raw.get("seq") if isinstance(raw, dict) else None
        external_id = raw.get("external_id") if isinstance(raw, dict) else None
        reference = {
            "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            "external_id": external_id if isinstance(external_id, str) else None,
        }
        try:
            event = Event.model_validate(raw)
        except ValidationError as error:
            reason = "; ".join(_readable(problem) for problem in error.errors())
            rejected.append(RejectedEvent(index=index, reason=reason, **reference))
            continue
        if expected_device_id is not None and event.device_id != expected_device_id:
            rejected.append(RejectedEvent(index=index, reason="device_id does not match this token's device", **reference))
            continue
        events.append(event)
    return events, rejected


def _readable(problem: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in problem.get("loc", ()))
    message = str(problem.get("msg", "invalid"))
    message = message.removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


class Nudge(BaseModel):
    """A gentle, timely prompt returned to the device that sent the triggering event (DT-43)."""

    rule: str
    title: Annotated[str, Field(max_length=80)]
    body: Annotated[str, Field(max_length=240)]
    created_at: AwareDatetime


class IngestResult(BaseModel):
    """Response of POST /api/v1/events."""

    accepted: Annotated[int, Field(ge=0, description="New events stored by this request.")]
    replaced: Annotated[int, Field(ge=0, description="Stored events updated through their external_id.")] = 0
    duplicates: Annotated[int, Field(ge=0, description="Events already stored, ignored.")]
    rejected: list[RejectedEvent] = Field(default_factory=list)
    last_seq: Annotated[int | None, Field(description="Highest seq stored for this device, or null.")] = None
    nudge: Nudge | None = None
