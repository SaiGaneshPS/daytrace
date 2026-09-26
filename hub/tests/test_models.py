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
    MAX_BATCH_EVENTS,
    POINT_KINDS,
    SPAN_KINDS,
    BatchTooLargeError,
    Category,
    Event,
    EventBatch,
    IngestResult,
    Kind,
    MalformedBatchError,
    Source,
    parse_batch,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "docs" / "event-schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)
PAYLOADS = sorted((REPO / "ios" / "shortcuts" / "payloads").glob("*.json"))


def events_in(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("events", [payload])


def schema_accepts(event: Any) -> bool:
    return VALIDATOR.is_valid(event)


def model_accepts(event: Any) -> bool:
    try:
        Event.model_validate(copy.deepcopy(event))
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
    "web": {**SESSION, "kind": "web", "source": "browser", "app": "Edge", "app_id": "msedge.exe",
            "data": {"domain": "www.YouTube.com"}},
    "afk": {**SESSION, "kind": "afk", "source": "tracker", "app": None, "app_id": None, "category": None},
    "sleep": {**SESSION, "kind": "sleep", "source": "health_connect", "external_id": "hc:9f2b",
              "data": {"stage": "deep", "measured": True}},
    "steps": {**SESSION, "kind": "steps", "source": "health_connect", "external_id": "steps:2026-09-25",
              "data": {"count": 0}},
    "calendar_event": {**SESSION, "kind": "calendar_event", "source": "calendar", "title": "Study"},
    "app_open": {"device_id": "iphone-1", "kind": "app_open", "source": "shortcuts",
                 "start": "2026-09-25T18:03:10Z", "app": "TikTok"},
    "app_close": {"device_id": "iphone-1", "kind": "app_close", "source": "shortcuts",
                  "start": "2026-09-25T18:09:10.250Z", "app": "TikTok", "end": None},
    "screen_on": {"device_id": "android-1", "seq": 3, "kind": "screen_on", "source": "usagestats",
                  "start": "2026-09-25T07:05:00+05:30"},
    "screen_off": {"device_id": "android-1", "seq": 4, "kind": "screen_off", "source": "usagestats",
                   "start": "2026-09-25T23:55:00-04:00"},
    "meal": {"device_id": "iphone-1", "kind": "meal", "source": "shortcuts",
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


# Accepted by BOTH the JSON Schema and the models (formats real collectors send).
ALSO_GOOD = {
    "minute precision (Kotlin OffsetDateTime)": with_changes(SESSION, start="2026-09-25T14:03-04:00"),
    "nanoseconds (Java Instant)": with_changes(SESSION, end="2026-09-25T18:21:44.123456789Z"),
    "lowercase t and z (RFC 3339)": with_changes(SESSION, start="2026-09-25t18:03:10z"),
    "whole-number float seq": with_changes(SESSION, seq=42.0),
    "whole-number float step count": with_changes(GOOD_EVENTS["steps"], data={"count": 8421.0}),
    "seq left out": with_changes(SESSION, seq=...),
    "web on localhost": with_changes(GOOD_EVENTS["web"], data={"domain": "localhost"}),
    "web on a LAN IP": with_changes(GOOD_EVENTS["web"], data={"domain": "192.168.1.20"}),
    "web on a punycode domain": with_changes(GOOD_EVENTS["web"], data={"domain": "xn--80ak6aa92e.xn--p1ai"}),
}

# Rejected by BOTH the JSON Schema and the models.
BAD_EVENTS = {
    "unknown kind": with_changes(SESSION, kind="nap"),
    "unknown source": with_changes(SESSION, source="spyware"),
    "unknown category": with_changes(SESSION, category="shopping"),
    "missing device_id": with_changes(SESSION, device_id=...),
    "device_id with spaces": with_changes(SESSION, device_id="my phone"),
    "negative seq": with_changes(SESSION, seq=-1),
    "fractional seq": with_changes(SESSION, seq=42.5),
    "seq as text": with_changes(SESSION, seq="42"),
    "seq above the safe integer range": with_changes(SESSION, seq=9_007_199_254_740_992),
    "external_id with spaces": with_changes(SESSION, external_id="steps 2026-09-25"),
    "time without offset": with_changes(SESSION, start="2026-09-25T14:03:10"),
    "time as a Unix number": with_changes(SESSION, start=1790359390),
    "time with a space instead of T": with_changes(SESSION, start="2026-09-25 14:03:10Z"),
    "month 13": with_changes(SESSION, start="2026-13-01T10:00:00Z"),
    "hour 25": with_changes(SESSION, start="2026-09-25T25:00:00Z"),
    "offset +99:99": with_changes(SESSION, start="2026-09-25T10:00:00+99:99"),
    "year 0000": with_changes(SESSION, start="0000-01-01T00:00:00Z"),
    "non-ASCII digits": with_changes(SESSION, start="٢٠٢٦-09-25T10:00:00Z"),
    "time with a trailing newline": with_changes(SESSION, start="2026-09-25T10:00:00Z\n"),
    "span without end": with_changes(SESSION, end=...),
    "span with null end": with_changes(SESSION, end=None),
    "point event with an end": with_changes(GOOD_EVENTS["app_open"], end="2026-09-25T18:05:00Z"),
    "extra field": with_changes(SESSION, url="https://example.com"),
    "steps without count": with_changes(GOOD_EVENTS["steps"], data={}),
    "steps with negative count": with_changes(GOOD_EVENTS["steps"], data={"count": -3}),
    "steps with fractional count": with_changes(GOOD_EVENTS["steps"], data={"count": 2.5}),
    "steps with true as count": with_changes(GOOD_EVENTS["steps"], data={"count": True}),
    "sleep with unknown stage": with_changes(GOOD_EVENTS["sleep"], data={"stage": "dreaming"}),
    "sleep with stage as a list": with_changes(GOOD_EVENTS["sleep"], data={"stage": ["deep"]}),
    "sleep with null stage": with_changes(GOOD_EVENTS["sleep"], data={"stage": None}),
    "sleep with text measured": with_changes(GOOD_EVENTS["sleep"], data={"measured": "yes"}),
    "meal without text or items": with_changes(GOOD_EVENTS["meal"], data={"meal_type": "lunch"}),
    "meal with empty text": with_changes(GOOD_EVENTS["meal"], data={"text": ""}),
    "meal with only spaces": with_changes(GOOD_EVENTS["meal"], data={"text": "   "}),
    "meal with empty items": with_changes(GOOD_EVENTS["meal"], data={"items": []}),
    "meal with a blank item": with_changes(GOOD_EVENTS["meal"], data={"items": ["roti", " "]}),
    "meal with null text": with_changes(GOOD_EVENTS["meal"], data={"text": None, "items": ["a"]}),
    "meal with unknown type": with_changes(GOOD_EVENTS["meal"], data={"text": "toast", "meal_type": "brunch"}),
    "meal with type as a dict": with_changes(GOOD_EVENTS["meal"], data={"text": "toast", "meal_type": {}}),
    "meal with null type": with_changes(GOOD_EVENTS["meal"], data={"text": "toast", "meal_type": None}),
    "web with a page title": with_changes(GOOD_EVENTS["web"], title="Funny cats - YouTube"),
    "web with a full URL": with_changes(GOOD_EVENTS["web"], data={"domain": "https://youtube.com/watch?v=1"}),
    "web without domain": with_changes(GOOD_EVENTS["web"], data={}),
    "web with extra data": with_changes(GOOD_EVENTS["web"], data={"domain": "youtube.com", "url": "https://y.com/a"}),
    "web with a URL in app_id": with_changes(GOOD_EVENTS["web"], app_id="https://youtube.com/watch?v=1"),
    "web domain with a trailing newline": with_changes(GOOD_EVENTS["web"], data={"domain": "youtube.com\n"}),
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
def test_shortcut_examples_follow_the_stateless_rules(path: Path) -> None:
    # iPhone Shortcuts keep no local store: no seq; health and calendar data carry an external_id.
    for event in EventBatch.model_validate(json.loads(path.read_text(encoding="utf-8"))).events:
        assert event.seq is None
        if event.kind in {Kind.SLEEP, Kind.STEPS, Kind.CALENDAR_EVENT}:
            assert event.external_id


def test_every_kind_has_a_good_example() -> None:
    assert set(GOOD_EVENTS) == {k.value for k in Kind}


@pytest.mark.parametrize("case", sorted(GOOD_EVENTS) + sorted(ALSO_GOOD))
def test_good_events_are_accepted_by_schema_and_models(case: str) -> None:
    event = GOOD_EVENTS.get(case) or ALSO_GOOD[case]
    assert schema_accepts(event), [e.message for e in VALIDATOR.iter_errors(event)]
    assert model_accepts(event)


@pytest.mark.parametrize("case", sorted(BAD_EVENTS))
def test_bad_events_are_rejected_by_schema_and_models(case: str) -> None:
    event = BAD_EVENTS[case]
    assert not schema_accepts(event), f"schema accepted: {case}"
    assert not model_accepts(event), f"models accepted: {case}"


@pytest.mark.parametrize(
    ("case", "event"),
    [
        ("end before start", with_changes(SESSION, start="2026-09-25T15:00:00-04:00", end="2026-09-25T14:00:00-04:00")),
        ("February 30", with_changes(SESSION, start="2026-02-30T10:00:00Z", end="2026-03-01T10:00:00Z")),
        # Valid text, but 04:30 UTC on 10000-01-01 does not exist, so the hub could not store it.
        (
            "past year 9999 in UTC",
            with_changes(SESSION, start="9999-12-31T23:30:00-05:00", end="9999-12-31T23:45:00-05:00"),
        ),
        (
            "before year 1000 in UTC",
            with_changes(SESSION, start="1000-01-01T00:30:00+01:00", end="1000-01-01T00:45:00+01:00"),
        ),
    ],
)
def test_rules_only_the_models_can_check(case: str, event: dict[str, Any]) -> None:
    # JSON Schema cannot compare two fields, know month lengths or convert to UTC; the hub models reject these.
    assert schema_accepts(event), case
    assert not model_accepts(event), case


def test_end_equal_to_start_is_allowed() -> None:
    assert model_accepts(with_changes(SESSION, end=SESSION["start"]))


def test_offsets_are_compared_as_real_times() -> None:
    # 14:30 at -04:00 is 18:30 UTC, which is after 18:00 UTC.
    assert model_accepts(with_changes(SESSION, start="2026-09-25T18:00:00Z", end="2026-09-25T14:30:00-04:00"))


def test_whole_number_floats_are_stored_as_integers() -> None:
    event = Event.model_validate(with_changes(GOOD_EVENTS["steps"], seq=7.0, data={"count": 8421.0}))
    assert event.seq == 7 and type(event.seq) is int
    assert event.data["count"] == 8421 and type(event.data["count"]) is int


def test_web_domains_are_stored_in_lowercase() -> None:
    assert Event.model_validate(copy.deepcopy(GOOD_EVENTS["web"])).data["domain"] == "www.youtube.com"


def test_malformed_values_give_validation_errors_not_crashes() -> None:
    # A TypeError here would surface as HTTP 500 instead of a clean rejection.
    for data in ({"stage": ["deep"]}, {"stage": {"a": 1}}, {"measured": [True]}):
        with pytest.raises(ValidationError):
            Event.model_validate(with_changes(GOOD_EVENTS["sleep"], data=data))
    for data in ({"text": "toast", "meal_type": ["dinner"]}, {"items": "roti"}, {"text": ["toast"]}):
        with pytest.raises(ValidationError):
            Event.model_validate(with_changes(GOOD_EVENTS["meal"], data=data))


# --- deduplication keys --------------------------------------------------------------------------------------


def test_external_id_wins_and_replaces() -> None:
    event = Event.model_validate(copy.deepcopy(GOOD_EVENTS["steps"]))
    assert event.dedup_key() == "ext:steps:2026-09-25"
    assert event.replaces_existing


def test_seq_is_used_when_there_is_no_external_id() -> None:
    event = Event.model_validate(copy.deepcopy(SESSION))
    assert event.dedup_key() == "seq:42"
    assert not event.replaces_existing


def test_stateless_events_in_the_same_second_get_different_keys() -> None:
    close_instagram = with_changes(GOOD_EVENTS["app_close"], app="Instagram", start="2026-09-25T18:09:10Z")
    open_tiktok = with_changes(GOOD_EVENTS["app_open"], app="TikTok", start="2026-09-25T18:09:10Z")
    keys = {Event.model_validate(e).dedup_key() for e in (close_instagram, open_tiktok)}
    assert len(keys) == 2
    assert all(key.startswith("content:") for key in keys)


def test_resending_the_same_stateless_event_gives_the_same_key() -> None:
    first = Event.model_validate(copy.deepcopy(GOOD_EVENTS["app_open"]))
    same_moment_other_offset = with_changes(GOOD_EVENTS["app_open"], start="2026-09-25T14:03:10-04:00")
    assert first.dedup_key() == Event.model_validate(same_moment_other_offset).dedup_key()


# --- batches -------------------------------------------------------------------------------------------------


def test_a_single_event_is_wrapped_into_a_batch() -> None:
    events, rejected = parse_batch(copy.deepcopy(GOOD_EVENTS["app_open"]))
    assert [e.kind for e in events] == [Kind.APP_OPEN] and not rejected


def test_one_bad_event_does_not_block_the_rest() -> None:
    payload = {"events": [copy.deepcopy(SESSION), BAD_EVENTS["sleep with stage as a list"], copy.deepcopy(SESSION)]}
    events, rejected = parse_batch(payload)
    assert len(events) == 2
    assert [(r.index, r.seq) for r in rejected] == [(1, 42)]
    assert "stage" in rejected[0].reason


def test_events_for_another_device_are_rejected() -> None:
    events, rejected = parse_batch(copy.deepcopy(SESSION), expected_device_id="windows-desk")
    assert not events
    assert rejected[0].reason == "device_id does not match this token's device"


def test_oversized_batches_fail_before_any_validation() -> None:
    with pytest.raises(BatchTooLargeError):
        parse_batch({"events": [{"not": "validated"}] * (MAX_BATCH_EVENTS + 1)})


@pytest.mark.parametrize("payload", [[], "text", {"events": []}, {"events": "nope"}, {"events": [], "extra": 1}])
def test_malformed_bodies_are_refused(payload: Any) -> None:
    with pytest.raises(MalformedBatchError):
        parse_batch(payload)


def test_non_object_items_are_rejected_one_by_one() -> None:
    events, rejected = parse_batch({"events": ["oops", copy.deepcopy(SESSION)]})
    assert len(events) == 1 and rejected[0].index == 0


def test_the_strict_batch_model_matches_the_limits() -> None:
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": []})
    with pytest.raises(ValidationError):
        EventBatch.model_validate({"events": [copy.deepcopy(SESSION)] * (MAX_BATCH_EVENTS + 1)})


def test_the_hub_can_build_events_from_datetimes() -> None:
    parsed = Event.model_validate(copy.deepcopy(SESSION))
    assert Event(**parsed.model_dump()) == parsed


def test_ingest_result_shape() -> None:
    result = IngestResult(accepted=3, duplicates=1, last_seq=44)
    assert result.model_dump() == {
        "accepted": 3, "replaced": 0, "duplicates": 1, "rejected": [], "last_seq": 44, "nudge": None,
    }
