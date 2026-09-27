"""Tests for DT-11: ingest API, auth and idempotency (DT-42: meals read from plain text)."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub import __version__
from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.api.events import MAX_BODY_BYTES, ingest, meal_items_from_text, meal_type_at, store_events
from daytrace_hub.auth import AuthenticatedDevice, hash_token, register_device
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event
from daytrace_hub.stats import Stats

PAYLOADS = Path(__file__).resolve().parents[2] / "ios" / "shortcuts" / "payloads"

SESSION = {
    "device_id": "android-1",
    "seq": 1,
    "kind": "app_session",
    "source": "usagestats",
    "start": "2026-09-25T14:03:10-04:00",
    "end": "2026-09-25T14:21:44-04:00",
    "app": "Instagram",
    "app_id": "com.instagram.android",
}


def session(seq: int, **changes: Any) -> dict[str, Any]:
    event = copy.deepcopy(SESSION)
    event["seq"] = seq
    event.update(changes)
    return event


@pytest.fixture
def tokens(db: Database) -> dict[str, str]:
    with db.connect() as conn:
        return {
            "android-1": register_device(conn, device_id="android-1", name="Galaxy phone", device_type="android"),
            "iphone-1": register_device(conn, device_id="iphone-1", name="iPhone", device_type="ios"),
            "viewer-1": register_device(conn, device_id="viewer-1", name="Phone browser", device_type="viewer"),
        }


def post(client: TestClient, token: str | None, body: Any, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if isinstance(body, bytes | str):
        return client.post("/api/v1/events", content=body, headers=headers, **kwargs)
    return client.post("/api/v1/events", json=body, headers=headers, **kwargs)


def stored(db: Database, device_id: str = "android-1") -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM events WHERE device_id = ? ORDER BY id", (device_id,)).fetchall()
    return [dict(row) for row in rows]


# --- health ---------------------------------------------------------------------------------------------------


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "profile": "personal", "version": __version__, "local": True}


# --- accepting events -----------------------------------------------------------------------------------------


def test_a_valid_batch_is_stored(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], {"events": [session(1), session(2), session(3)]})
    assert response.status_code == 200
    assert response.json() == {
        "accepted": 3, "replaced": 0, "duplicates": 0, "rejected": [], "last_seq": 3, "nudge": None, "meals": [],
    }
    rows = stored(db)
    assert [r["dedup_key"] for r in rows] == ["seq:1", "seq:2", "seq:3"]
    first = rows[0]
    assert first["start_utc"] == "2026-09-25T18:03:10.000000Z"  # stored in UTC
    assert first["end_utc"] == "2026-09-25T18:21:44.000000Z"
    assert first["utc_offset_min"] == -240  # the offset it was sent with
    assert first["app"] == "Instagram"
    assert json.loads(first["data"]) == {}


def test_a_single_event_object_is_accepted(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], session(7))
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["last_seq"] == 7


def test_the_example_shortcut_payloads_are_accepted(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    for name in ("app-event.json", "health-sync.json", "meal.json"):
        body = json.loads((PAYLOADS / name).read_text(encoding="utf-8"))
        response = post(client, tokens["iphone-1"], body)
        assert response.status_code == 200, name
        assert response.json()["rejected"] == [], name
        assert response.json()["accepted"] >= 1, name
    assert stored(db, "iphone-1")


# --- resending is safe ----------------------------------------------------------------------------------------


def test_resending_a_batch_stores_nothing_twice(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    batch = {"events": [session(1), session(2)]}
    post(client, tokens["android-1"], batch)
    response = post(client, tokens["android-1"], batch)
    assert response.json()["accepted"] == 0
    assert response.json()["duplicates"] == 2
    assert len(stored(db)) == 2


def test_the_same_event_twice_in_one_batch_counts_once(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], {"events": [session(1), session(1)]})
    assert (response.json()["accepted"], response.json()["duplicates"]) == (1, 1)


def test_shortcut_events_without_seq_are_deduplicated_by_content(
    client: TestClient, db: Database, tokens: dict[str, str]
) -> None:
    open_event = {"device_id": "iphone-1", "kind": "app_open", "source": "shortcuts",
                  "start": "2026-09-25T14:03:10-04:00", "app": "Instagram"}
    same_second_other_app = {**open_event, "app": "TikTok"}
    first = post(client, tokens["iphone-1"], {"events": [open_event, same_second_other_app]})
    assert first.json()["accepted"] == 2  # two apps in the same second are two events
    again = post(client, tokens["iphone-1"], {**open_event, "start": "2026-09-25T18:03:10Z"})  # same instant
    assert again.json()["duplicates"] == 1
    assert again.json()["last_seq"] is None
    assert len(stored(db, "iphone-1")) == 2


def test_an_external_id_event_replaces_the_stored_copy(
    client: TestClient, db: Database, tokens: dict[str, str]
) -> None:
    morning = {"device_id": "iphone-1", "external_id": "steps:2026-09-25", "kind": "steps", "source": "healthkit",
               "start": "2026-09-25T00:00:00-04:00", "end": "2026-09-25T09:00:00-04:00", "data": {"count": 2100}}
    evening = {**morning, "end": "2026-09-25T21:00:00-04:00", "data": {"count": 8421}}
    assert post(client, tokens["iphone-1"], morning).json()["accepted"] == 1
    response = post(client, tokens["iphone-1"], evening)
    assert (response.json()["accepted"], response.json()["replaced"]) == (0, 1)
    rows = stored(db, "iphone-1")
    assert len(rows) == 1
    assert json.loads(rows[0]["data"]) == {"count": 8421}
    assert rows[0]["end_utc"] == "2026-09-26T01:00:00.000000Z"
    assert rows[0]["updated_at"] is not None
    # Sending the evening number again changes nothing, so it is a duplicate, not a replacement.
    again = post(client, tokens["iphone-1"], evening)
    assert (again.json()["replaced"], again.json()["duplicates"]) == (0, 1)


def test_a_reused_seq_with_a_different_event_is_reported_not_dropped(
    client: TestClient, db: Database, tokens: dict[str, str]
) -> None:
    # A collector that restarted its numbering (e.g. after a reinstall) must find out, not lose events.
    post(client, tokens["android-1"], session(1))
    response = post(client, tokens["android-1"], {"events": [session(2), session(1, app="TikTok", app_id="com.zhiliaoapp.musically")]})
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 0
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["index"] == 1
    assert body["rejected"][0]["seq"] == 1
    assert body["rejected"][0]["code"] == "seq_conflict"  # tells the collector to renumber, not to drop it
    assert "already used for a different event" in body["rejected"][0]["reason"]
    assert stored(db)[0]["app"] == "Instagram"


def test_a_resend_with_only_a_new_category_is_still_a_duplicate(client: TestClient, tokens: dict[str, str]) -> None:
    post(client, tokens["android-1"], session(1))
    response = post(client, tokens["android-1"], session(1, category="social"))
    assert response.json()["duplicates"] == 1
    assert response.json()["rejected"] == []


# --- bad events never block good ones -------------------------------------------------------------------------


def test_one_bad_event_is_rejected_and_the_rest_are_stored(
    client: TestClient, db: Database, tokens: dict[str, str]
) -> None:
    bad = session(2, end="2026-09-25T13:00:00-04:00")  # ends before it starts
    response = post(client, tokens["android-1"], {"events": [session(1), bad, session(3)]})
    body = response.json()
    assert response.status_code == 200
    assert body["accepted"] == 2
    assert [(r["index"], r["seq"]) for r in body["rejected"]] == [(1, 2)]
    assert [r["seq"] for r in stored(db)] == [1, 3]


def test_events_for_another_device_are_rejected(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], {"events": [session(1), session(2, device_id="iphone-1")]})
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"][0]["index"] == 1
    assert body["rejected"][0]["code"] == "wrong_device"
    assert "does not match" in body["rejected"][0]["reason"]
    assert stored(db, "iphone-1") == []


def test_rejections_from_validation_and_storage_are_merged_in_order(client: TestClient, tokens: dict[str, str]) -> None:
    post(client, tokens["android-1"], session(5))
    batch = [session(5, app="Other"), {"kind": "nope"}, session(6)]
    body = post(client, tokens["android-1"], {"events": batch}).json()
    assert [(r["index"], r["code"]) for r in body["rejected"]] == [(0, "seq_conflict"), (1, "invalid")]


@pytest.mark.parametrize(
    ("bad_part", "reason"),
    [
        (b'"data": {"note": "\\ud83d"}', "unicode"),  # half of an emoji
        (b'"title": "chat \\udc00"', "unicode"),
        (b'"data": {"x": 1e400}', "finite"),
        (b'"data": {"x": [1, {"y": -1e999}]}', "finite"),
    ],
)
def test_events_the_database_or_json_cannot_hold_are_rejected_alone(
    client: TestClient, db: Database, tokens: dict[str, str], bad_part: bytes, reason: str
) -> None:
    good = json.dumps(session(1)).encode()
    bad = json.dumps(session(2))[:-1].encode() + b", " + bad_part + b"}"
    response = post(client, tokens["android-1"], b'{"events": [' + good + b", " + bad + b"]}")
    body = response.json()
    assert response.status_code == 200
    assert body["accepted"] == 1
    assert body["rejected"][0]["index"] == 1
    assert body["rejected"][0]["code"] == "invalid"
    assert reason in body["rejected"][0]["reason"].lower()
    assert [r["seq"] for r in stored(db)] == [1]


def test_oversized_data_is_rejected_alone(client: TestClient, tokens: dict[str, str]) -> None:
    body = post(client, tokens["android-1"], {"events": [session(1), session(2, data={"note": "x" * 17_000})]}).json()
    assert body["accepted"] == 1
    assert "at most 16 KB" in body["rejected"][0]["reason"]


def test_a_slow_retry_of_an_older_copy_does_not_overwrite_a_newer_one(
    client: TestClient, db: Database, tokens: dict[str, str]
) -> None:
    morning = {"device_id": "android-1", "seq": 10, "external_id": "steps:2026-09-25", "kind": "steps",
               "source": "health_connect", "start": "2026-09-25T00:00:00-04:00",
               "end": "2026-09-25T09:00:00-04:00", "data": {"count": 2100}}
    evening = {**morning, "seq": 20, "end": "2026-09-25T21:00:00-04:00", "data": {"count": 8421}}
    post(client, tokens["android-1"], morning)
    assert post(client, tokens["android-1"], evening).json()["replaced"] == 1
    retry = post(client, tokens["android-1"], morning).json()  # the morning request's response was lost
    assert (retry["replaced"], retry["duplicates"]) == (0, 1)
    assert json.loads(stored(db)[0]["data"]) == {"count": 8421}


def test_replaced_events_reach_the_nudge_hook(
    client: TestClient, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daytrace_hub.api import events as events_api

    seen: list[list[Any]] = []
    monkeypatch.setattr(events_api, "pick_nudge", lambda database, device_id, changed, **_: seen.append(changed))
    base = {"device_id": "iphone-1", "external_id": "cal:1", "kind": "calendar_event", "source": "calendar",
            "start": "2026-09-25T15:00:00-04:00", "end": "2026-09-25T16:00:00-04:00", "title": "Study"}
    post(client, tokens["iphone-1"], base)
    post(client, tokens["iphone-1"], {**base, "end": "2026-09-25T17:00:00-04:00"})  # the event was moved
    assert [len(changed) for changed in seen] == [1, 1]
    assert seen[1][0].end.hour == 17


def test_store_events_refuses_to_run_outside_a_transaction(db: Database, tokens: dict[str, str]) -> None:
    from daytrace_hub.api.events import store_events
    from daytrace_hub.models import Event

    with db.connect() as conn, pytest.raises(RuntimeError, match="inside a transaction"):
        store_events(conn, "android-1", [(0, Event.model_validate(session(1)))])


# --- auth -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Basic abc", "Token dt_abc", "Bearer dt_not-a-real-token", "Bearer a b"],
)
def test_missing_or_bad_tokens_get_401(client: TestClient, tokens: dict[str, str], authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.post("/api/v1/events", json=session(1), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_bearer_scheme_is_case_insensitive(client: TestClient, tokens: dict[str, str]) -> None:
    response = client.post("/api/v1/events", json=session(1), headers={"Authorization": f"bearer {tokens['android-1']}"})
    assert response.status_code == 200


def test_a_revoked_token_gets_401(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE devices SET revoked_at = '2026-09-25T20:00:00.000000Z' WHERE device_id = 'android-1'")
    assert post(client, tokens["android-1"], session(1)).status_code == 401


def test_only_a_hash_of_the_token_is_stored(db: Database, tokens: dict[str, str]) -> None:
    with db.connect() as conn:
        row = conn.execute("SELECT token_hash FROM devices WHERE device_id = 'android-1'").fetchone()
    assert row["token_hash"] == hash_token(tokens["android-1"])
    assert tokens["android-1"] not in row["token_hash"]
    assert tokens["android-1"].startswith("dt_")


def test_viewer_tokens_cannot_send_events(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["viewer-1"], {**session(1), "device_id": "viewer-1"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_auth_is_checked_before_the_body_is_read(client: TestClient) -> None:
    response = post(client, None, b"{not json")
    assert response.status_code == 401


def test_last_seen_is_recorded(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    post(client, tokens["android-1"], session(1))
    with db.connect() as conn:
        assert conn.execute("SELECT last_seen FROM devices WHERE device_id = 'android-1'").fetchone()[0]


def test_a_last_seen_in_the_future_is_corrected(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    # The hub clock was a day fast and has been fixed.
    with db.connect() as conn:
        conn.execute("UPDATE devices SET last_seen = '2999-01-01T00:00:00.000000Z' WHERE device_id = 'android-1'")
    post(client, tokens["android-1"], session(1))
    with db.connect() as conn:
        assert conn.execute("SELECT last_seen FROM devices WHERE device_id = 'android-1'").fetchone()[0] < "2999"


def test_a_busy_database_does_not_break_auth_for_reads(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    import time

    with db.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")  # the tracker or seed generator is writing
        started = time.monotonic()
        response = client.get("/api/v1/devices/android-1/cursor", headers={"Authorization": f"Bearer {tokens['android-1']}"})
        elapsed = time.monotonic() - started
        writer.execute("ROLLBACK")
    assert response.status_code == 200
    assert elapsed < 2  # the last_seen write gave up quickly instead of waiting the full busy timeout


def test_a_busy_database_answers_503_busy_for_writes(
    client: TestClient, db: Database, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daytrace_hub import db as db_module

    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_MS", 200)
    with db.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        response = post(client, tokens["android-1"], session(1))
        writer.execute("ROLLBACK")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "busy"
    assert response.headers["retry-after"] == "1"


def test_unexpected_errors_still_use_the_error_shape(
    settings: Any, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daytrace_hub.api import events as events_api
    from daytrace_hub.app import create_app

    def boom(*_: Any) -> None:
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(events_api, "ingest", boom)
    with TestClient(create_app(settings), client=("127.0.0.1", 1), raise_server_exceptions=False) as test_client:
        response = post(test_client, tokens["android-1"], session(1))
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "the hub hit an unexpected error", "details": []}
    }


def test_the_network_check_comes_before_auth(settings: Any, tokens: dict[str, str]) -> None:
    from daytrace_hub.app import create_app

    with TestClient(create_app(settings), client=("8.8.8.8", 1)) as outside:
        response = outside.post("/api/v1/events", json=session(1), headers={"Authorization": f"Bearer {tokens['android-1']}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_network"


# --- malformed and oversized bodies ---------------------------------------------------------------------------


def test_more_than_500_events_get_413(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], {"events": [session(i) for i in range(501)]})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "batch_too_large"
    assert stored(db) == []


def test_exactly_500_events_are_fine(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], {"events": [session(i) for i in range(500)]})
    assert response.status_code == 200
    assert response.json()["accepted"] == 500


def test_a_body_over_the_size_limit_gets_413(client: TestClient, tokens: dict[str, str]) -> None:
    body = json.dumps({**session(1), "data": {"note": "x" * MAX_BODY_BYTES}})
    response = post(client, tokens["android-1"], body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"  # split by size, unlike batch_too_large


def test_a_single_event_of_the_largest_allowed_size_always_fits(client: TestClient, tokens: dict[str, str]) -> None:
    # Worst case: every text field full of characters that a strict JSON encoder escapes to \\uXXXX.
    event = session(1, app="应" * 200, app_id="应" * 200, title="应" * 500, data={"note": "应" * 5000})
    body = json.dumps(event, ensure_ascii=True)
    assert len(body) < MAX_BODY_BYTES
    response = post(client, tokens["android-1"], body)
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_utf16_bodies_are_refused(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], json.dumps(session(1)).encode("utf-16"))
    assert response.status_code == 400
    assert "UTF-8" in response.json()["error"]["message"]


def test_a_utf8_byte_order_mark_is_fine(client: TestClient, tokens: dict[str, str]) -> None:
    response = post(client, tokens["android-1"], b"\xef\xbb\xbf" + json.dumps(session(1)).encode())
    assert response.status_code == 200


def test_a_chunked_body_without_content_length_is_capped_too(client: TestClient, tokens: dict[str, str]) -> None:
    def chunks() -> Any:
        yield b'{"device_id": "android-1", "data": {"note": "'
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536
        yield b'"}}'

    response = client.post(
        "/api/v1/events", content=chunks(), headers={"Authorization": f"Bearer {tokens['android-1']}"}
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"", "empty"),
        (b"   ", "empty"),
        (b"{not json", "not valid JSON"),
        (b"\xff\xfe\x00garbage", "UTF-8"),
        (b'{"device_id": "android-1", "seq": NaN}', "not valid JSON"),
        (b"[1, 2, 3]", "send one event object"),
        (b'"hello"', "send one event object"),
        (b'{"events": []}', "empty"),
        (b'{"events": {"kind": "app_open"}}', "send one event object"),
        (b'{"events": [], "extra": 1}', "nothing else"),
    ],
)
def test_malformed_bodies_get_400(client: TestClient, tokens: dict[str, str], body: bytes, fragment: str) -> None:
    response = post(client, tokens["android-1"], body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert fragment in response.json()["error"]["message"]


def test_deeply_nested_json_gets_400_not_500(client: TestClient, tokens: dict[str, str]) -> None:
    body = b'{"data": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    response = post(client, tokens["android-1"], body)
    assert response.status_code in (400, 413)


# --- cursor ---------------------------------------------------------------------------------------------------


def test_the_cursor_returns_the_highest_seq(client: TestClient, tokens: dict[str, str]) -> None:
    headers = {"Authorization": f"Bearer {tokens['android-1']}"}
    assert client.get("/api/v1/devices/android-1/cursor", headers=headers).json() == {
        "device_id": "android-1", "last_seq": None,
    }
    post(client, tokens["android-1"], {"events": [session(4), session(9), session(2)]})
    assert client.get("/api/v1/devices/android-1/cursor", headers=headers).json()["last_seq"] == 9


def test_a_device_cannot_read_another_devices_cursor(client: TestClient, tokens: dict[str, str]) -> None:
    response = client.get("/api/v1/devices/iphone-1/cursor", headers={"Authorization": f"Bearer {tokens['android-1']}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_the_cursor_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/v1/devices/android-1/cursor").status_code == 401


# --- error shape for everything else --------------------------------------------------------------------------


def test_unknown_paths_use_the_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_methods_use_the_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/events")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


# --- DT-42: meals from plain text -------------------------------------------------------------------------------

LUNCHTIME = "2026-09-25T13:10:00-04:00"


def meal(text: str = "two rotis and dal", start: str = LUNCHTIME, seq: int | None = None, **extra: Any) -> dict[str, Any]:
    data = {"text": text, **extra.pop("data", {})}
    event = {"device_id": "iphone-1", "kind": "meal", "source": "shortcuts", "start": start, "data": data, **extra}
    if seq is not None:
        event["seq"] = seq
    return event


def parsed_of(db: Database) -> list[Any]:
    return [json.loads(row["data"]).get("parsed") for row in stored(db, "iphone-1")]


def test_a_meal_sent_as_text_is_read_stored_and_returned(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    body = post(client, tokens["iphone-1"], meal()).json()  # the fake model answers without JSON: the text is split
    reading = {"items": ["two rotis", "dal"], "meal_type": "lunch", "items_by": "text", "type_by": "time"}
    assert body["meals"] == [{"index": 0, **reading}]
    assert parsed_of(db) == [reading]
    with db.connect() as conn:
        meals = Stats(conn, ZoneInfo("America/Toronto"), "America/Toronto").meals(date(2026, 9, 25))
    assert [(m["items"], m["meal_type"], m["text"]) for m in meals] == [(["two rotis", "dal"], "lunch", "two rotis and dal")]


def test_the_local_model_lists_the_items_when_it_can(client: TestClient, db: Database, tokens: dict[str, str], fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text('{"items": ["2 rotis", "dal"]}')
    body = post(client, tokens["iphone-1"], meal()).json()
    assert body["meals"][0]["items"] == ["2 rotis", "dal"] and body["meals"][0]["items_by"] == "ai"
    chat = fake_llm.chats()[-1]["body"]
    assert chat["messages"][-1]["content"] == "two rotis and dal"
    assert chat["response_format"]["json_schema"]["name"] == "meal_items"
    assert "never estimate calories" in chat["messages"][0]["content"]


@pytest.mark.parametrize("reply", [
    '{"items": ["two rotis", "dal", "chicken curry"]}',  # a food the note doesn't name
    '{"items": []}', '{"items": ["two rotis", 3]}', '{"foods": ["dal"]}',
])
def test_a_model_answer_that_doesnt_hold_up_is_not_used(client: TestClient, tokens: dict[str, str], fake_llm: FakeModelServer, reply: str) -> None:
    fake_llm.reply_text(reply)
    reading = post(client, tokens["iphone-1"], meal()).json()["meals"][0]
    assert (reading["items"], reading["items_by"]) == (["two rotis", "dal"], "text")


def test_a_meal_the_text_names_keeps_that_type(client: TestClient, tokens: dict[str, str]) -> None:
    reading = post(client, tokens["iphone-1"], meal("Dinner: pasta and a salad")).json()["meals"][0]
    assert (reading["items"], reading["meal_type"], reading["type_by"]) == (["pasta", "a salad"], "dinner", "text")


@pytest.mark.parametrize(("text", "items"), [
    ("two rotis and dal", ["two rotis", "dal"]),
    ("Lunch: rice, dal & a salad", ["rice", "dal", "a salad"]),
    ("I had eggs and toast for breakfast.", ["eggs", "toast"]),
    ("coffee with milk", ["coffee with milk"]),
    ("oats + berries; tea", ["oats", "berries", "tea"]),
    ("pizza\nsoda", ["pizza", "soda"]),
    ("and, , and", []),
])
def test_meal_text_is_split_into_foods(text: str, items: list[str]) -> None:
    assert meal_items_from_text(text) == items


@pytest.mark.parametrize(("start", "meal_type"), [
    ("2026-09-25T03:59:00-04:00", "snack"), ("2026-09-25T04:00:00-04:00", "breakfast"),
    ("2026-09-25T10:59:00-04:00", "breakfast"), ("2026-09-25T11:00:00-04:00", "lunch"),
    ("2026-09-25T15:30:00-04:00", "snack"), ("2026-09-25T17:00:00-04:00", "dinner"),
    ("2026-09-25T22:00:00-04:00", "snack"),
])
def test_the_meal_type_follows_the_hour_on_the_devices_clock(start: str, meal_type: str) -> None:
    assert meal_type_at(datetime.fromisoformat(start)) == meal_type


def test_a_meal_sent_in_utc_takes_the_hubs_own_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeline_api, "resolve_tz", lambda name: (ZoneInfo("America/Toronto"), "America/Toronto"))
    assert meal_type_at(datetime.fromisoformat("2026-09-25T17:30:00+00:00")) == "lunch"  # 13:30 in Toronto


@pytest.mark.parametrize("keying", [{}, {"seq": 7}, {"external_id": "meal-1"}])
def test_a_resent_meal_is_the_same_meal_and_is_not_read_again(client: TestClient, db: Database, tokens: dict[str, str],
                                                            fake_llm: FakeModelServer, keying: dict[str, Any]) -> None:
    fake_llm.reply_text('{"items": ["2 rotis", "dal"]}')
    first = post(client, tokens["iphone-1"], meal(**keying)).json()
    again = post(client, tokens["iphone-1"], meal(**keying)).json()
    assert (again["accepted"], again["duplicates"], again["rejected"]) == (0, 1, [])
    assert again["meals"] == first["meals"]  # told the same thing, from what was stored
    assert len(fake_llm.chats()) == 1 and len(stored(db, "iphone-1")) == 1


def test_a_changed_meal_under_its_external_id_is_read_afresh(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    post(client, tokens["iphone-1"], meal(external_id="meal-1"))
    body = post(client, tokens["iphone-1"], meal("porridge", external_id="meal-1")).json()
    assert body["replaced"] == 1 and body["meals"][0]["items"] == ["porridge"]
    assert parsed_of(db) == [{"items": ["porridge"], "items_by": "text", "meal_type": "lunch", "type_by": "time"}]


def test_a_meal_stored_before_it_could_be_read_is_still_the_same_meal(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    with db.connect() as conn, transaction(conn):
        store_events(conn, "iphone-1", [(0, Event.model_validate(meal(seq=3)))])  # no reading, as before DT-42
    body = post(client, tokens["iphone-1"], meal(seq=3)).json()
    assert (body["duplicates"], body["rejected"]) == (1, [])  # not a seq conflict
    assert parsed_of(db) == [None]


def test_a_meal_refused_as_a_seq_conflict_is_not_reported(client: TestClient, tokens: dict[str, str]) -> None:
    post(client, tokens["iphone-1"], meal(seq=4))
    body = post(client, tokens["iphone-1"], {"events": [meal("porridge", seq=4), meal("tea", seq=5)]}).json()
    assert [r["code"] for r in body["rejected"]] == ["seq_conflict"]
    assert [m["index"] for m in body["meals"]] == [1]


def test_a_meal_with_its_own_items_and_type_is_left_as_it_is(client: TestClient, db: Database, tokens: dict[str, str], fake_llm: FakeModelServer) -> None:
    body = post(client, tokens["iphone-1"], meal(data={"items": ["dal"], "meal_type": "dinner"})).json()
    assert body["meals"] == [] and parsed_of(db) == [None] and fake_llm.chats() == []
    body = post(client, tokens["iphone-1"], meal("soup", data={"items": ["soup"]})).json()
    assert body["meals"] == [{"index": 0, "items": ["soup"], "meal_type": "lunch", "items_by": "event", "type_by": "time"}]
    assert fake_llm.chats() == []  # only the type was missing


def test_a_collector_cannot_send_the_hubs_reading(client: TestClient, tokens: dict[str, str]) -> None:
    body = post(client, tokens["iphone-1"], meal(data={"parsed": {"items": ["cake"]}})).json()
    assert body["accepted"] == 0 and "written by the hub" in body["rejected"][0]["reason"]


def test_without_a_model_the_text_is_split(db: Database, tokens: dict[str, str]) -> None:
    device = AuthenticatedDevice(device_id="iphone-1", name="iPhone", device_type="ios")
    result = ingest(db, device, meal("toast and jam"), llm=None)
    assert [(m.items, m.items_by) for m in result.meals] == [(["toast", "jam"], "text")]


def test_a_model_that_is_down_is_not_waited_for(client: TestClient, tokens: dict[str, str], fake_llm: FakeModelServer) -> None:
    fake_llm.down = True
    reading = post(client, tokens["iphone-1"], meal()).json()["meals"][0]
    assert reading["items_by"] == "text"
