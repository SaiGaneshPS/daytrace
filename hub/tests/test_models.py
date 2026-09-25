"""Tests for DT-9: the event schema, the Pydantic models and the example payloads agree."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from daytrace_hub.models import (
    POINT_KINDS,
    SPAN_KINDS,
    Category,
    Event,
    EventBatch,
    IngestResult,
    Kind,
    Source,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "docs" / "event-schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
PAYLOADS = sorted((REPO / "ios" / "shortcuts" / "payloads").glob("*.json"))


def events_in(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("events", [payload])


def schema_accepts(event: dict[str, Any]) -> bool:
    return VALIDATOR.is_valid(event)


def model_accepts(event: dict[str, Any]) -> bool:
    try:
        Event.model_validate(event)
    except ValidationError:
        return False
    return True


SESSION = {
    "device_id": "android-1",
    "seq": 42,
    "kind": "app_session",
    "source": "usagestats",
    "start": "2026-09-25T14:03:10-04:00",
    "end": "2026-09-25T14:21:44-04:00",
    "app": "Instagram",
    "app_id": "com.instagram.android",
    "category": "social",
}

GOOD_EVENTS = {
    "app_session": SESSION,
    "window": {**SESSION, "kind": "window", "source": "tracker", "app": "Code", "title": "stats.py", "category": None},
    "web": {**SESSION, "kind": "web", "source": "browser", "app": "Edge", "data": {"domain": "www.youtube.com"}},
    "afk": {**SESSION, "kind": "afk", "source": "tracker", "app": None, "app_id": None, "category": None},
    "sleep": {**SESSION, "kind": "sleep", "source": "health_connect", "data": {"stage": "deep", "measured": True}},
    "steps": {**SESSION, "kind": "steps", "source": "health_connect", "data": {"count": 0}},
    "calendar_event": {**SESSION, "kind": "calendar_event", "source": "calendar", "title": "Study"},
    "app_open": {"device_id": "iphone-1", "seq": 1, "kind": "app_open", "source": "shortcuts",
                 "start": "2026-09-25T18:03:10Z", "app": "TikTok"},
    "app_close": {"device_id": "iphone-1", "seq": 2, "kind": "app_close", "source": "shortcuts",
                  "start": "2026-09-25T18:09:10.250Z", "app": "TikTok", "end": None},
    "screen_on": {"device_id": "android-1", "seq": 3, "kind": "screen_on", "source": "usagestats",
                  "start": "2026-09-25T07:05:00+05:30"},
    "screen_off": {"device_id": "android-1", "seq": 4, "kind": "screen_off", "source": "usagestats",
                   "start": "2026-09-25T23:55:00-04:00"},
    "meal": {"device_id": "iphone-1", "seq": 5, "kind": "meal", "source": "shortcuts",
             "start": "2026-09-25T20:15:00-04:00", "data": {"items": ["roti", "dal"], "meal_type": "dinner"}},
}


def with_changes(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    event = copy.deepcopy(base)
    for key, value in changes.items():
        if value is ...:
            event.pop(key, None)
        else:
            event[key] = value
    return event


# Rejected by BOTH the JSON Schema and the models.
BAD_EVENTS = {
    "unknown kind": with_changes(SESSION, kind="nap"),
    "unknown source": with_changes(SESSION, source="spyware"),
    "unknown category": with_changes(SESSION, category="shopping"),
    "missing device_id": with_changes(SESSION, device_id=...),
    "device_id with spaces": with_changes(SESSION, device_id="my phone"),
    "negative seq": with_changes(SESSION, seq=-1),
    "seq above the safe integer range": with_changes(SESSION, seq=9_007_199_254_740_992),
    "time without offset": with_changes(SESSION, start="2026-09-25T14:03:10"),
    "time as a Unix number": with_changes(SESSION, start=1790359390),
    "time with a space instead of T": with_changes(SESSION, start="2026-09-25 14:03:10Z"),
    "span without end": with_changes(SESSION, end=...),
    "span with null end": with_changes(SESSION, end=None),
    "point event with an end": with_changes(GOOD_EVENTS["app_open"], end="2026-09-25T18:05:00Z"),
    "extra field": with_changes(SESSION, url="https://example.com"),
    "steps without count": with_changes(GOOD_EVENTS["steps"], data={}),
    "steps with negative count": with_changes(GOOD_EVENTS["steps"], data={"count": -3}),
    "steps with fractional count": with_changes(GOOD_EVENTS["steps"], data={"count": 2.5}),
    "sleep with unknown stage": with_changes(GOOD_EVENTS["sleep"], data={"stage": "dreaming"}),
    "sleep with text measured": with_changes(GOOD_EVENTS["sleep"], data={"measured": "yes"}),
    "meal without text or items": with_changes(GOOD_EVENTS["meal"], data={"meal_type": "lunch"}),
    "meal with empty text": with_changes(GOOD_EVENTS["meal"], data={"text": ""}),
    "meal with unknown type": with_changes(GOOD_EVENTS["meal"], data={"text": "toast", "meal_type": "brunch"}),
    "web with a page title": with_changes(GOOD_EVENTS["web"], title="Funny cats - YouTube"),
    "web with a full URL": with_changes(GOOD_EVENTS["web"], data={"domain": "https://youtube.com/watch?v=1"}),
    "web without domain": with_changes(GOOD_EVENTS["web"], data={}),
    "title too long": with_changes(SESSION, title="x" * 501),
}


def test_schema_itself_is_valid() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_schema_and_models_list_the_same_values() -> None:
    props = SCHEMA["properties"]
    assert set(props["kind"]["enum"]) == {k.value for k in Kind}
    assert set(props["source"]["enum"]) == {s.value for s in Source}
    assert set(props["category"]["enum"]) - {None} == {c.value for c in Category}
    span_rule, point_rule = SCHEMA["allOf"][0], SCHEMA["allOf"][1]
    assert set(span_rule["if"]["properties"]["kind"]["enum"]) == {k.value for k in SPAN_KINDS}
    assert set(point_rule["if"]["properties"]["kind"]["enum"]) == {k.value for k in POINT_KINDS}
    assert SPAN_KINDS | POINT_KINDS == set(Kind)


@pytest.mark.parametrize("path", PAYLOADS, ids=lambda p: p.name)
def test_example_payloads_are_valid(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = events_in(payload)
    for event in events:
        errors = [e.message for e in VALIDATOR.iter_errors(event)]
        assert not errors, errors
    assert len(EventBatch.model_validate(payload).events) == len(events)


@pytest.mark.parametrize("path", PAYLOADS, ids=lambda p: p.name)
def test_shortcut_examples_use_start_time_in_ms_as_seq(path: Path) -> None:
    # iPhone Shortcuts have no local store, so seq is the event time in Unix milliseconds (docs/api.md).
    for event in EventBatch.model_validate(json.loads(path.read_text(encoding="utf-8"))).events:
        assert event.seq == int(event.start.timestamp() * 1000), event


def test_every_kind_has_a_good_example() -> None:
    assert set(GOOD_EVENTS) == {k.value for k in Kind}


@pytest.mark.parametrize("kind", sorted(GOOD_EVENTS))
def test_good_events_are_accepted_by_schema_and_models(kind: str) -> None:
    event = GOOD_EVENTS[kind]
    assert schema_accepts(event), [e.message for e in VALIDATOR.iter_errors(event)]
    assert model_accepts(event)


@pytest.mark.parametrize("case", sorted(BAD_EVENTS))
def test_bad_events_are_rejected_by_schema_and_models(case: str) -> None:
    event = BAD_EVENTS[case]
    assert not schema_accepts(event), f"schema accepted: {case}"
    assert not model_accepts(event), f"models accepted: {case}"


def test_end_before_start_is_rejected_by_the_models() -> None:
    # JSON Schema cannot compare two fields, so this rule lives in the models only.
    event = with_changes(SESSION, start="2026-09-25T15:00:00-04:00", end="2026-09-25T14:00:00-04:00")
    assert schema_accepts(event)
    assert not model_accepts(event)


def test_end_equal_to_start_is_allowed() -> None:
    assert model_accepts(with_changes(SESSION, end=SESSION["start"]))


def test_offsets_are_compared_as_real_times() -> None:
    # 14:30 at -04:00 is 18:30 UTC, which is after 18:00 UTC.
    event = with_changes(SESSION, start="2026-09-25T18:00:00Z", end="2026-09-25T14:30:00-04:00")
    assert model_accepts(event)


def test_a_single_event_is_wrapped_into_a_batch() -> None:
    batch = EventBatch.model_validate(GOOD_EVENTS["app_open"])
    assert [e.kind for e in batch.events] == [Kind.APP_OPEN]


def test_an_empty_batch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": []})


def test_a_batch_with_one_bad_event_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": [SESSION, BAD_EVENTS["unknown kind"]]})


def test_the_hub_can_build_events_from_datetimes() -> None:
    parsed = Event.model_validate(SESSION)
    rebuilt = Event(**parsed.model_dump())
    assert rebuilt == parsed


def test_ingest_result_shape() -> None:
    result = IngestResult(accepted=3, duplicates=1, last_seq=44)
    assert result.model_dump() == {"accepted": 3, "duplicates": 1, "last_seq": 44, "nudge": None}
