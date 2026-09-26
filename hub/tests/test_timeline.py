"""Tests for DT-13: the timeline endpoint."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.api.events import store_events
from daytrace_hub.api.timeline import day_window, local_zone_name
from daytrace_hub.auth import register_device
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event

TORONTO = "America/Toronto"
PHONE = ("192.168.1.50", 40000)
LATER = datetime(2030, 1, 1, tzinfo=UTC)  # tests look back at days that are fully over


@pytest.fixture(autouse=True)
def _days_are_in_the_past(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeline_api, "current_time", lambda: LATER)


def add(db: Database, device_id: str, device_type: str, events: list[dict[str, Any]], name: str | None = None) -> str:
    """Pair a device (once) and store its events through the real ingest path."""
    with db.connect() as conn:
        exists = conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        token = "" if exists else register_device(conn, device_id=device_id, name=name or device_id, device_type=device_type)
        with transaction(conn):
            parsed = [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)]
            store_events(conn, device_id, parsed)
    return token


def span(kind: str, start: str, end: str, app: str | None = None, source: str = "tracker", **extra: Any) -> dict[str, Any]:
    event = {"kind": kind, "source": source, "start": start, "end": end, **extra}
    if app is not None:
        event["app"] = app
    return event


def point(kind: str, start: str, app: str | None = None, source: str = "shortcuts", **extra: Any) -> dict[str, Any]:
    event = {"kind": kind, "source": source, "start": start, **extra}
    if app is not None:
        event["app"] = app
    return event


def get(client: TestClient, date: str, tz: str = TORONTO, **headers: str) -> dict[str, Any]:
    response = client.get("/api/v1/timeline", params={"date": date, "tz": tz}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def seed_day(db: Database) -> None:
    """One realistic morning on four devices (all times Toronto, -04:00)."""
    add(db, "windows-1", "windows", [
        span("window", "2026-09-25T09:00:00-04:00", "2026-09-25T09:40:00-04:00", "Code", title="stats.py"),
        span("afk", "2026-09-25T09:40:00-04:00", "2026-09-25T09:55:00-04:00"),
        span("window", "2026-09-25T09:55:00-04:00", "2026-09-25T10:30:00-04:00", "msedge"),
    ], name="Desk PC")
    add(db, "mac-1", "macos", [
        span("window", "2026-09-25T13:00:00-04:00", "2026-09-25T13:45:00-04:00", "Figma"),
    ])
    add(db, "android-1", "android", [
        span("app_session", "2026-09-25T08:00:00-04:00", "2026-09-25T08:25:00-04:00", "Instagram", source="usagestats", seq=1),
        span("app_session", "2026-09-25T10:00:00-04:00", "2026-09-25T10:10:00-04:00", "WhatsApp", source="usagestats", seq=2),
    ])
    add(db, "iphone-1", "ios", [
        point("app_open", "2026-09-25T21:00:00-04:00", "TikTok"),
        point("app_close", "2026-09-25T21:20:00-04:00", "TikTok"),
    ])


# --- lanes and totals -----------------------------------------------------------------------------------------


def test_a_day_on_four_devices(client: TestClient, db: Database) -> None:
    seed_day(db)
    body = get(client, "2026-09-25")
    assert [lane["device_id"] for lane in body["lanes"]] == ["windows-1", "mac-1", "android-1", "iphone-1"]
    windows = body["lanes"][0]
    assert windows["name"] == "Desk PC"
    assert [s["app"] for s in windows["sessions"]] == ["Code", "msedge"]  # the AFK gap is not screen time
    assert windows["seconds"] == (40 + 35) * 60
    assert body["totals"]["by_device"] == {"windows-1": 75.0, "mac-1": 45.0, "android-1": 35.0, "iphone-1": 20.0}
    assert body["totals"]["seconds"] == (75 + 45 + 35 + 20) * 60
    assert body["totals"]["minutes"] == 175.0


def test_lane_and_total_seconds_add_up_exactly(client: TestClient, db: Database) -> None:
    seed_day(db)
    body = get(client, "2026-09-25")
    for lane in body["lanes"]:
        assert lane["seconds"] == sum(s["seconds"] for s in lane["sessions"])
        assert lane["minutes"] == round(lane["seconds"] / 60, 2)
    assert body["totals"]["seconds"] == sum(lane["seconds"] for lane in body["lanes"] if lane["counted"])
    assert body["totals"]["seconds"] == sum(body["totals"]["by_device_seconds"].values())


def test_any_screen_time_never_exceeds_total_screen_time(client: TestClient, db: Database) -> None:
    # Ten 1.4 s sessions: boundaries are snapped to whole seconds before anything is added up.
    base = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
    add(db, "android-1", "android", [
        span("app_session", (base + timedelta(seconds=5 * n)).isoformat(),
             (base + timedelta(seconds=5 * n, milliseconds=1400)).isoformat(), "X", source="usagestats", seq=n)
        for n in range(10)
    ])
    totals = get(client, "2026-09-25")["totals"]
    assert totals["any_screen_seconds"] <= totals["seconds"]
    assert totals["seconds"] == 10


def test_time_on_two_screens_at_once_counts_once_in_any_screen(client: TestClient, db: Database) -> None:
    seed_day(db)  # WhatsApp on the phone 10:00-10:10 while Edge was open on the PC
    totals = get(client, "2026-09-25")["totals"]
    assert totals["any_screen_seconds"] == totals["seconds"] - 10 * 60


def test_session_times_are_shown_in_the_requested_time_zone(client: TestClient, db: Database) -> None:
    seed_day(db)
    first = get(client, "2026-09-25")["lanes"][0]["sessions"][0]
    assert first["start"] == "2026-09-25T09:00:00-04:00"
    assert first["minutes"] == 40.0
    assert first["kind"] == "app"
    assert first["estimated"] is False


def test_the_day_follows_the_requested_time_zone(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [
        span("app_session", "2026-09-25T22:00:00-04:00", "2026-09-25T22:30:00-04:00", "Instagram", source="usagestats", seq=1),
    ])
    assert get(client, "2026-09-25")["totals"]["seconds"] == 1800  # Toronto evening
    assert get(client, "2026-09-26", tz="UTC")["totals"]["seconds"] == 1800  # already the 26th in UTC
    assert get(client, "2026-09-25", tz="UTC")["totals"]["seconds"] == 0


def test_a_session_across_local_midnight_is_split_between_the_days(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [
        span("app_session", "2026-09-25T23:45:00-04:00", "2026-09-26T00:20:00-04:00", "YouTube", source="usagestats", seq=1),
    ])
    assert get(client, "2026-09-25")["totals"]["seconds"] == 15 * 60
    assert get(client, "2026-09-26")["totals"]["seconds"] == 20 * 60


def test_browser_lanes_are_shown_but_not_added_to_totals(client: TestClient, db: Database) -> None:
    seed_day(db)
    add(db, "browser-1", "browser", [
        span("web", "2026-09-25T09:55:00-04:00", "2026-09-25T10:15:00-04:00", source="browser", data={"domain": "youtube.com"}),
    ])
    body = get(client, "2026-09-25")
    browser = next(lane for lane in body["lanes"] if lane["device_id"] == "browser-1")
    assert browser["counted"] is False
    assert browser["sessions"][0]["app"] == "youtube.com"
    assert browser["sessions"][0]["kind"] == "web"
    assert "browser-1" not in body["totals"]["by_device"]
    assert body["totals"]["seconds"] == 175 * 60


def test_an_empty_day(client: TestClient) -> None:
    body = get(client, "2026-09-25")
    assert body["lanes"] == [] and body["calendar"] == [] and body["sleep"] == [] and body["meals"] == []
    assert body["totals"] == {
        "seconds": 0, "minutes": 0.0, "by_device_seconds": {}, "by_device": {},
        "any_screen_seconds": 0, "any_screen_minutes": 0.0, "sleep_seconds": 0, "sleep_minutes": 0.0,
    }
    assert body["meta"]["source"] == "real"


def test_a_revoked_devices_history_still_shows(client: TestClient, db: Database) -> None:
    seed_day(db)
    assert client.delete("/api/v1/devices/mac-1").status_code == 204
    assert "mac-1" in [lane["device_id"] for lane in get(client, "2026-09-25")["lanes"]]


# --- daylight saving time -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2026-11-01", 25), ("2026-09-25", 24)])
def test_dst_days_are_23_or_25_hours_long(day: str, hours: int) -> None:
    start, end = day_window(datetime.fromisoformat(day).date(), ZoneInfo(TORONTO))
    assert end - start == timedelta(hours=hours)


def test_a_session_across_the_spring_forward_counts_real_time(client: TestClient, db: Database) -> None:
    # 01:30 EST to 03:30 EDT on 2026-03-08 is one real hour, not two.
    add(db, "android-1", "android", [
        span("app_session", "2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00", "YouTube", source="usagestats", seq=1),
    ])
    body = get(client, "2026-03-08")
    assert body["totals"]["seconds"] == 3600
    assert body["meta"]["range"]["start"] == "2026-03-08T00:00:00-05:00"
    assert body["meta"]["range"]["end"] == "2026-03-09T00:00:00-04:00"


def test_a_whole_fall_back_day_holds_25_hours_of_use(client: TestClient, db: Database) -> None:
    add(db, "windows-1", "windows", [
        span("window", "2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00", "Code"),
    ])
    assert get(client, "2026-11-01")["totals"]["seconds"] == 25 * 3600


# --- calendar, sleep and meals --------------------------------------------------------------------------------


def test_calendar_entries_are_listed_once_even_from_two_phones(client: TestClient, db: Database) -> None:
    study = span("calendar_event", "2026-09-25T15:00:00-04:00", "2026-09-25T17:00:00-04:00", source="calendar",
                 title="Study: algorithms", external_id="cal:1")
    add(db, "android-1", "android", [study])
    add(db, "iphone-1", "ios", [study])
    add(db, "iphone-1", "ios", [span("calendar_event", "2026-09-25T00:00:00-04:00", "2026-09-26T00:00:00-04:00",
                                     source="calendar", title="Holiday", data={"all_day": True}, external_id="cal:2")])
    calendar = get(client, "2026-09-25")["calendar"]
    assert [(c["title"], c["all_day"]) for c in calendar] == [("Holiday", True), ("Study: algorithms", False)]


def test_sleep_shows_on_the_morning_it_ended_at_full_length(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [
        span("sleep", "2026-09-24T23:40:00-04:00", "2026-09-25T07:05:00-04:00", source="healthkit",
             data={"stage": "asleep", "measured": True}, external_id="sleep:1"),
    ])
    today = get(client, "2026-09-25")["sleep"]
    assert [(s["minutes"], s["stage"], s["estimated"]) for s in today] == [(445.0, "asleep", False)]
    assert get(client, "2026-09-24")["sleep"] == []


def test_estimated_sleep_is_flagged(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [
        span("sleep", "2026-09-25T00:30:00-04:00", "2026-09-25T07:00:00-04:00", source="health_connect",
             data={"measured": False}, external_id="sleep:2"),
    ])
    body = get(client, "2026-09-25")
    assert body["sleep"][0]["estimated"] is True
    assert body["meta"]["estimated"] is True


def test_meals_are_listed(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [point("meal", "2026-09-25T19:30:00-04:00", data={"items": ["roti", "dal"], "meal_type": "dinner"})])
    meals = get(client, "2026-09-25")["meals"]
    assert meals == [{"time": "2026-09-25T19:30:00-04:00", "items": ["roti", "dal"], "text": None,
                      "meal_type": "dinner", "device_id": "iphone-1"}]


# --- accuracy signals -----------------------------------------------------------------------------------------


def test_seed_afk_that_changes_the_numbers_makes_the_source_mixed(client: TestClient, db: Database) -> None:
    add(db, "windows-1", "windows", [
        span("window", "2026-09-25T10:00:00-04:00", "2026-09-25T11:00:00-04:00", "Code"),
        span("afk", "2026-09-25T10:00:00-04:00", "2026-09-25T10:59:00-04:00", source="seed"),
    ])
    body = get(client, "2026-09-25")
    assert body["totals"]["seconds"] == 60
    assert body["meta"]["source"] == "mixed"


def test_overlapping_sleep_stages_and_copies_are_counted_once(client: TestClient, db: Database) -> None:
    night = [
        span("sleep", "2026-09-24T23:00:00-04:00", "2026-09-25T07:00:00-04:00", source="healthkit",
             data={"stage": "in_bed"}, external_id="sleep:bed"),
        span("sleep", "2026-09-24T23:20:00-04:00", "2026-09-25T06:50:00-04:00", source="healthkit",
             data={"stage": "asleep"}, external_id="sleep:asleep"),
    ]
    add(db, "iphone-1", "ios", night)
    add(db, "android-1", "android", night)  # the same night from the other phone
    body = get(client, "2026-09-25")
    assert [s["stage"] for s in body["sleep"]] == ["in_bed", "asleep"]
    assert body["totals"]["sleep_seconds"] == 450 * 60  # asleep only, once


def test_the_source_says_when_numbers_come_from_seed_data(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [
        span("app_session", "2026-09-25T08:00:00-04:00", "2026-09-25T08:10:00-04:00", "Instagram", source="seed", seq=1),
    ])
    assert get(client, "2026-09-25")["meta"]["source"] == "seed"
    add(db, "android-1", "android", [
        span("app_session", "2026-09-25T09:00:00-04:00", "2026-09-25T09:10:00-04:00", "Instagram", source="usagestats", seq=2),
    ])
    assert get(client, "2026-09-25")["meta"]["source"] == "mixed"


def test_an_inferred_iphone_session_marks_the_day_as_estimated(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [point("app_open", "2026-09-25T21:00:00-04:00", "TikTok")])
    body = get(client, "2026-09-25")
    session = body["lanes"][0]["sessions"][0]
    assert (session["seconds"], session["estimated"]) == (30 * 60, True)
    assert body["meta"]["estimated"] is True


def test_nothing_after_now_is_counted(client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.fromisoformat("2026-09-25T21:21:39-04:00")
    monkeypatch.setattr(timeline_api, "current_time", lambda: now.astimezone(UTC))
    add(db, "iphone-1", "ios", [point("app_open", "2026-09-25T21:19:39-04:00", "TikTok")])  # no close yet
    body = get(client, "2026-09-25")
    assert body["totals"]["seconds"] == 120  # not 30 minutes into the future
    assert get(client, "2026-09-26")["totals"]["seconds"] == 0


def test_a_close_hours_after_midnight_still_pairs_for_the_first_day(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [
        point("app_open", "2026-09-25T23:00:00-04:00", "TikTok"),
        point("app_close", "2026-09-26T00:45:00-04:00", "TikTok"),
    ])
    today = get(client, "2026-09-25")
    assert today["totals"]["seconds"] == 3600
    assert today["meta"]["estimated"] is False
    assert get(client, "2026-09-26")["totals"]["seconds"] == 45 * 60


def test_a_health_sync_does_not_end_an_iphone_session(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [
        point("app_open", "2026-09-25T14:50:00-04:00", "TikTok"),
        span("calendar_event", "2026-09-25T14:51:00-04:00", "2026-09-25T15:30:00-04:00", source="calendar",
             title="Call", external_id="cal:9"),
    ])
    assert get(client, "2026-09-25")["totals"]["seconds"] == 30 * 60


def test_the_screen_turning_off_ends_a_paired_session(client: TestClient, db: Database) -> None:
    add(db, "iphone-1", "ios", [
        point("app_open", "2026-09-25T10:00:00-04:00", "YouTube"),
        point("screen_off", "2026-09-25T10:04:00-04:00"),
        point("app_close", "2026-09-25T15:00:00-04:00", "YouTube"),
    ])
    session = get(client, "2026-09-25")["lanes"][0]["sessions"][0]
    assert (session["seconds"], session["estimated"]) == (240, False)


def test_old_history_is_not_loaded_for_a_day(db: Database) -> None:
    from daytrace_hub.sessions import load_events

    add(db, "android-1", "android", [
        span("app_session", "2026-09-20T10:00:00-04:00", "2026-09-20T10:10:00-04:00", "Old", source="usagestats", seq=1),
        span("app_session", "2026-09-25T10:00:00-04:00", "2026-09-25T10:10:00-04:00", "Today", source="usagestats", seq=2),
    ])
    start, end = day_window(date(2026, 9, 25), ZoneInfo(TORONTO))
    with db.connect() as conn:
        assert [e.app for e in load_events(conn, start, end)] == ["Today"]
        plan = " ".join(str(tuple(row)) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM events INDEXED BY events_by_end WHERE end_utc > ? AND start_utc < ?",
            ("2026-09-25", "2026-09-26"),
        ))
    assert "events_by_end" in plan


# --- parameters and access ------------------------------------------------------------------------------------


def test_date_and_time_zone_default_to_today_here(client: TestClient) -> None:
    response = client.get("/api/v1/timeline")
    assert response.status_code == 200
    body = response.json()
    zone = local_zone_name()
    assert body["tz"] == zone  # an IANA name the dashboard can send back
    assert body["date"] == LATER.astimezone(ZoneInfo(zone)).date().isoformat()


def test_the_default_zone_follows_dst_on_other_dates(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeline_api, "local_zone_name", lambda: "America/St_Johns")
    summer = client.get("/api/v1/timeline", params={"date": "2026-07-15"}).json()["meta"]["range"]
    winter = client.get("/api/v1/timeline", params={"date": "2026-01-15"}).json()["meta"]["range"]
    assert summer["start"].endswith("-02:30")
    assert winter["start"].endswith("-03:30")


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"date": "2026-09-25", "tz": "Mars/Olympus"}, 400),
        ({"date": "2026-09-25", "tz": "../../etc/passwd"}, 400),
        ({"date": "2026-09-25", "tz": "America"}, 400),  # a tzdata folder: a crash on Windows before
        ({"date": "2026-09-25", "tz": " "}, 400),
        ({"date": "2026-02-30", "tz": TORONTO}, 422),
        ({"date": "yesterday", "tz": TORONTO}, 422),
        ({"date": "0001-01-01", "tz": TORONTO}, 400),
    ],
)
def test_bad_parameters(client: TestClient, params: dict[str, str], status: int) -> None:
    response = client.get("/api/v1/timeline", params=params)
    assert response.status_code == status
    assert "error" in response.json()


def test_phones_need_a_token_and_viewers_can_read(client: TestClient, db: Database) -> None:
    with db.connect() as conn:
        token = register_device(conn, device_id="viewer-1", name="Phone browser", device_type="viewer")
    with TestClient(client.app, client=PHONE) as phone:
        assert phone.get("/api/v1/timeline").status_code == 401
        assert phone.get("/api/v1/timeline", headers={"Authorization": f"Bearer {token}"}).status_code == 200
