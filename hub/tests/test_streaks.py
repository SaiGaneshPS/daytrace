"""Tests for DT-53: streaks, goals and achievements, on 14 seeded days and on small hand-made histories.

The claims: the seeded 5-day focus streak that breaks once is reported as it was built; today is at_risk (with
what is left) until it qualifies, and a limit only qualifies once the day is over; a day with no data from what a
rule needs neither extends nor breaks a streak; days follow the time zone, midnight and DST; goals change the
rules they drive; badges are earned once and kept; every streak and badge says its rule and the days that counted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time as clock
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import streaks
from daytrace_hub.api import insights as insights_api
from daytrace_hub.api import streaks as streaks_api
from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.app import create_app
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.seed import seed

TZ_NAME = "America/Toronto"
TZ = ZoneInfo(TZ_NAME)
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)  # a Friday evening; the seed covers the 14 days up to now
FIRST = date(2026, 9, 12)
LOCAL = ("127.0.0.1", 50000)
PHONE = ("192.168.1.40", 50000)
STREAKS = ["focus_flame", "screens_down", "logged_it", "balanced", "synced"]


@pytest.fixture
def demo(tmp_path: Path) -> Settings:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    seed(settings, 14, TZ, NOW)
    streaks._cache.clear()
    return settings


def at(moment: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (streaks_api, insights_api, timeline_api):
        monkeypatch.setattr(module, "current_time", lambda: moment.astimezone(UTC))


@pytest.fixture
def hub(demo: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    at(NOW, monkeypatch)
    with TestClient(create_app(demo), client=LOCAL, base_url="http://localhost:8767") as client:
        yield client
    streaks._cache.clear()


def found(settings: Settings, moment: datetime = NOW, tz: ZoneInfo = TZ) -> streaks.Evaluation:
    return streaks.evaluation(Database(settings.database_path), tz, str(tz), moment)


def track(evaluation: streaks.Evaluation, streak_id: str) -> streaks.Track:
    return next(item for item in evaluation.streaks if item.id == streak_id)


def statuses(item: streaks.Track) -> dict[date, str]:
    return {day.day: day.status for day in item.days}


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def add(settings: Settings, device: str, kind: str, start: datetime, minutes: float | None = None,
        app: str | None = None, category: str | None = None, data: dict[str, Any] | None = None) -> None:
    end = start + timedelta(minutes=minutes) if minutes is not None else None
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, kind, source, start_utc, end_utc, utc_offset_min, app, app_id, category, data, received_at)"
            " VALUES (?, ?, ?, 'seed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (device, f"content:{device}-{kind}-{start.isoformat()}", kind, utc(start), utc(end) if end else None,
             int(start.utcoffset().total_seconds() // 60) if start.utcoffset() else 0, app, app and f"app.{app.lower()}",
             category, json.dumps(data or {}), utc(start)),
        )


def delete_day(settings: Settings, devices: tuple[str, ...], day: date) -> None:
    start, end = datetime.combine(day, time(0), tzinfo=TZ), datetime.combine(day + timedelta(days=1), time(0), tzinfo=TZ)
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute(f"DELETE FROM events WHERE device_id IN ({', '.join('?' for _ in devices)}) AND start_utc < ? AND COALESCE(end_utc, start_utc) > ?",
                     (*devices, utc(end), utc(start)))


# --- the rules ---------------------------------------------------------------------------------------------------


def test_the_rules_file_is_complete_and_consistent() -> None:
    rules = streaks.load_rules()
    assert [item.id for item in rules.streaks] == STREAKS
    assert set(rules.goals) == {"focus_target", "social_cap", "bedtime"}
    assert [item.id for item in rules.achievements] == ["first_sync", "full_set", "streak_7", "streak_30", "focus_1000", "perfect_week"]
    for goal in rules.goals.values():
        assert goal.low <= goal.parse(goal.default) <= goal.high and "{target}" in goal.rule
    for rule in rules.streaks:
        assert rule.measure in streaks.MEASURES and rule.needs and rule.rule
    assert streaks.clock_minutes("23:30") == 330 and streaks.clock_minutes("00:30") == 390 and streaks.clock_text(390) == "00:30"


# --- the seeded fortnight ----------------------------------------------------------------------------------------


def test_the_seeded_focus_streak_is_five_days_and_breaks_once(demo: Settings) -> None:
    flame = track(found(demo), "focus_flame")
    assert flame.rule == "240 or more focused minutes in a day"  # the demo student's goal
    assert flame.best == [date(2026, 9, 17) + timedelta(days=i) for i in range(5)]  # 17 to 21 September
    assert statuses(flame)[date(2026, 9, 22)] == "missed"  # the morning after a 130-minute night
    assert flame.current == [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]  # today met already
    assert flame.today.status == "met" and flame.today.remaining is None
    assert [len(run) for run in flame.runs] == [1, 1, 5, 3]


def test_every_seeded_streak(demo: Settings) -> None:
    evaluation = found(demo)
    assert (evaluation.first, evaluation.today) == (FIRST, NOW.date())  # from the first day with screen data
    summary = {item.id: (len(item.current), len(item.best), item.today.status) for item in evaluation.streaks}
    assert summary == {
        "focus_flame": (3, 5, "met"),
        "screens_down": (3, 5, "met"),  # no phone after 11 pm on the good nights
        "logged_it": (14, 14, "met"),
        "balanced": (1, 1, "at_risk"),  # under the 60-minute cap so far; a limit counts once the day is over
        "synced": (14, 14, "met"),
    }
    assert statuses(track(evaluation, "screens_down"))[FIRST] == "no_data"  # nothing from the phone the night before


def test_a_limit_is_at_risk_until_the_day_is_over_then_missed_once_passed(demo: Settings) -> None:
    balanced = track(found(demo), "balanced")
    assert balanced.today.status == "at_risk" and balanced.current == [date(2026, 9, 24)]
    assert balanced.today.remaining == pytest.approx(60 - balanced.today.value, abs=0.01)  # the room left
    add(demo, "seed-android", "app_session", NOW - timedelta(minutes=100), 90, "Instagram", "social")
    balanced = track(found(demo), "balanced")
    assert balanced.today.status == "missed" and balanced.current == []  # past the cap: today can't count any more


def test_a_target_not_reached_yet_is_at_risk_with_what_is_left(demo: Settings) -> None:
    morning = NOW.replace(hour=10)
    flame = track(found(demo, morning), "focus_flame")
    assert flame.today.status == "at_risk"
    assert flame.today.remaining == pytest.approx(240 - flame.today.value, abs=0.01) and flame.today.remaining > 0
    assert flame.current == [date(2026, 9, 23), date(2026, 9, 24)]  # yesterday's count, still alive


def test_just_after_midnight(demo: Settings) -> None:
    evaluation = found(demo, datetime(2026, 9, 26, 0, 5, tzinfo=TZ))
    assert evaluation.today == date(2026, 9, 26)
    flame = track(evaluation, "focus_flame")
    assert flame.today.status == "no_data" and len(flame.current) == 3  # no computer yet today: the run stands
    down = track(evaluation, "screens_down")
    assert down.today.status == "no_data" and len(down.current) == 3  # the phone hasn't said anything about tonight yet
    add(demo, "seed-android", "app_session", datetime(2026, 9, 26, 0, 1, tzinfo=TZ), 3, "Instagram", "social")
    down = track(found(demo, datetime(2026, 9, 26, 0, 5, tzinfo=TZ)), "screens_down")
    assert down.today.status == "at_risk" and down.today.remaining == 12  # on the phone after midnight: 12 of 15 left
    balanced = track(evaluation, "balanced")
    assert statuses(balanced)[date(2026, 9, 25)] == "met"  # yesterday ended under the cap: now it counts


def test_a_day_without_the_needed_device_neither_extends_nor_breaks(demo: Settings) -> None:
    delete_day(demo, ("seed-windows", "seed-mac"), date(2026, 9, 22))  # the computers stayed off on the day it broke
    flame = track(found(demo), "focus_flame")
    assert statuses(flame)[date(2026, 9, 22)] == "no_data"
    assert len(flame.current) == 8 and date(2026, 9, 22) not in flame.current  # 17 to 21 and 23 to 25, one streak
    assert statuses(track(found(demo), "logged_it"))[date(2026, 9, 22)] == "met"  # the phone still logged meals


def test_a_device_that_did_not_sync_misses_the_synced_streak(demo: Settings) -> None:
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('android-9', 'Old phone', 'android', ?)",
                     (utc(datetime(2026, 9, 20, tzinfo=TZ)),))
    synced = track(found(demo), "synced")
    days = statuses(synced)
    assert days[date(2026, 9, 19)] == "met" and days[date(2026, 9, 21)] == "missed"  # paired on the 20th, silent since
    assert synced.today.status == "at_risk" and synced.today.remaining == 1  # one device to go today
    assert synced.today.target == 5 and synced.today.value == 4


# --- time zones and DST ------------------------------------------------------------------------------------------


@pytest.fixture
def empty(tmp_path: Path) -> Settings:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    Database(settings.database_path).initialize()
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('android-1', 'Phone', 'android', ?)",
                     (utc(datetime(2026, 1, 1, tzinfo=UTC)),))
    streaks._cache.clear()
    return settings


def test_days_follow_the_time_zone(empty: Settings) -> None:
    for offset in range(4):  # a phone used every day at noon in Toronto
        add(empty, "android-1", "app_session", datetime(2026, 9, 20 + offset, 12, 0, tzinfo=TZ), 20, "Maps", "other")
    add(empty, "android-1", "meal", datetime(2026, 9, 21, 23, 30, tzinfo=TZ), data={"items": ["noodles"]})
    moment = datetime(2026, 9, 23, 18, 0, tzinfo=TZ)
    toronto = statuses(track(found(empty, moment), "logged_it"))
    tokyo_zone = ZoneInfo("Asia/Tokyo")  # 23:30 in Toronto is 12:30 the next day in Tokyo
    tokyo = statuses(track(found(empty, moment, tokyo_zone), "logged_it"))
    assert toronto[date(2026, 9, 21)] == "met" and toronto[date(2026, 9, 22)] == "missed"
    assert tokyo[date(2026, 9, 22)] == "met" and tokyo[date(2026, 9, 21)] == "missed"
    assert found(empty, moment, tokyo_zone).today == date(2026, 9, 24)


def test_a_dst_change_is_one_day_among_the_others(empty: Settings) -> None:
    for offset in range(5):  # Toronto falls back on Sunday 1 November 2026: that day is 25 hours long
        day = date(2026, 10, 30) + timedelta(days=offset)
        add(empty, "android-1", "app_session", datetime.combine(day, time(12), tzinfo=TZ), 20, "Maps", "other")
        add(empty, "android-1", "meal", datetime.combine(day, time(19), tzinfo=TZ), data={"items": ["soup"]})
    logged = track(found(empty, datetime(2026, 11, 3, 21, 0, tzinfo=TZ)), "logged_it")
    assert [day.day for day in logged.days] == [date(2026, 10, 30) + timedelta(days=i) for i in range(5)]
    assert logged.current == [day.day for day in logged.days]  # five days in a row, the long one included


# --- the API -----------------------------------------------------------------------------------------------------


def test_the_streaks_endpoint(hub: TestClient) -> None:
    body = hub.get("/api/v1/streaks", params={"tz": TZ_NAME}).json()
    assert (body["tz"], body["date"], body["since"]) == (TZ_NAME, "2026-09-25", "2026-09-12")
    by_id = {item["id"]: item for item in body["streaks"]}
    assert list(by_id) == STREAKS
    flame = by_id["focus_flame"]
    assert (flame["current"], flame["best"], flame["today"]) == (3, 5, "met")
    assert flame["best_dates"] == ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21"]
    assert flame["counted"] == ["2026-09-23", "2026-09-24", "2026-09-25"] and flame["needs"]
    assert len(flame["days"]) == 14 and flame["days"][-1]["date"] == "2026-09-25"  # since the first day, 30 asked
    balanced = by_id["balanced"]
    assert balanced["today"] == "at_risk" and balanced["remaining"]["unit"] == "minutes" and balanced["remaining"]["value"] > 0
    assert balanced["rule"] == "60 minutes or less in social apps in a day"
    few = hub.get("/api/v1/streaks", params={"tz": TZ_NAME, "days": 5}).json()
    assert [day["date"] for day in few["streaks"][0]["days"]] == [f"2026-09-{d}" for d in range(21, 26)]
    assert hub.get("/api/v1/streaks", params={"days": 0}).status_code == 422


def test_goals_and_changing_one(hub: TestClient) -> None:
    goals = {item["id"]: item for item in hub.get("/api/v1/goals", params={"tz": TZ_NAME}).json()["goals"]}
    assert (goals["focus_target"]["target"], goals["focus_target"]["default"]) == (240, 120)
    assert goals["focus_target"]["today"]["status"] == "met" and goals["focus_target"]["today"]["progress"] == 100
    social = goals["social_cap"]["today"]
    assert social["status"] == "at_risk" and social["progress"] == round(100 * social["value"] / 60)
    bedtime = goals["bedtime"]
    assert (bedtime["target"], bedtime["min"], bedtime["max"]) == ("23:30", "20:00", "03:00")
    assert bedtime["today"]["status"] == "met" and bedtime["today"]["value"] <= "23:30"  # asleep by then last night
    changed = hub.put("/api/v1/goals/focus_target", params={"tz": TZ_NAME}, json={"target": 350})
    assert changed.status_code == 200 and changed.json()["target"] == 350
    flame = next(item for item in hub.get("/api/v1/streaks", params={"tz": TZ_NAME}).json()["streaks"] if item["id"] == "focus_flame")
    assert flame["rule"] == "350 or more focused minutes in a day" and flame["best"] == 3  # the past judged again
    later = hub.put("/api/v1/goals/bedtime", json={"target": "00:15"})
    assert later.status_code == 200 and later.json()["target"] == "00:15"


@pytest.mark.parametrize(("goal", "target"), [
    ("focus_target", 5), ("focus_target", 1000), ("focus_target", "lots"), ("social_cap", "01:00"),
    ("bedtime", "25:00"), ("bedtime", "12:00"), ("bedtime", 330), ("bedtime", "late"),
])
def test_a_goal_out_of_range_is_refused(hub: TestClient, goal: str, target: Any) -> None:
    response = hub.put(f"/api/v1/goals/{goal}", json={"target": target})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"]


def test_only_the_dashboard_can_change_goals(hub: TestClient, demo: Settings) -> None:
    assert hub.put("/api/v1/goals/sleep_more", json={"target": 5}).status_code == 404
    with Database(demo.database_path).connect() as conn, transaction(conn):
        collector = register_device(conn, device_id="android-7", name="Phone", device_type="android")
        viewer = register_device(conn, device_id="viewer-7", name="Phone browser", device_type="viewer")
    with TestClient(hub.app, client=PHONE) as phone:
        assert phone.get("/api/v1/streaks").status_code == 401
        assert phone.get("/api/v1/achievements").status_code == 401
        as_collector = {"Authorization": f"Bearer {collector}"}
        assert phone.get("/api/v1/goals", headers=as_collector).status_code == 200  # reading is fine
        assert phone.put("/api/v1/goals/social_cap", json={"target": 45}, headers=as_collector).status_code == 403
        as_viewer = {"Authorization": f"Bearer {viewer}"}
        assert phone.put("/api/v1/goals/social_cap", json={"target": 45}, headers=as_viewer).status_code == 200


def test_achievements_are_earned_once_and_kept(hub: TestClient, demo: Settings) -> None:
    body = hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()
    badges = {item["id"]: item for item in body["achievements"]}
    assert badges["first_sync"]["earned_on"] == "2026-09-12"
    assert badges["full_set"]["unlocked"] and badges["full_set"]["earned_on"] == "2026-09-12"  # all four sent data
    assert badges["streak_7"]["earned_on"] == "2026-09-18"  # meals and syncing every day since the 12th
    assert badges["streak_7"]["dates"] == [f"2026-09-{d}" for d in range(12, 19)]
    assert badges["focus_1000"]["earned_on"] == "2026-09-15"  # 346 + 210 + 350 + 164 focused minutes
    assert not badges["streak_30"]["unlocked"] and badges["streak_30"]["progress"] == {"value": 14, "target": 30, "unit": "days"}
    assert not badges["perfect_week"]["unlocked"] and badges["perfect_week"]["progress"]["target"] == 7
    assert body["unlocked"] == 4 and all(item["rule"] for item in body["achievements"])
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("DELETE FROM events")  # the data behind every badge is gone later
    again = hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()
    assert again["unlocked"] == 4
    for badge in ("first_sync", "full_set", "streak_7", "focus_1000"):  # once earned, kept: when, and the days
        kept = next(item for item in again["achievements"] if item["id"] == badge)
        assert {key: kept[key] for key in ("earned_on", "unlocked_at", "dates")} == {key: badges[badge][key] for key in ("earned_on", "unlocked_at", "dates")}


def test_a_perfect_week(empty: Settings) -> None:
    """Every goal met every day of a whole week: focus on a computer, little social, asleep early."""
    with Database(empty.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('windows-1', 'PC', 'windows', ?)",
                     (utc(datetime(2026, 1, 1, tzinfo=UTC)),))
    for offset in range(8):
        day = date(2026, 9, 14) + timedelta(days=offset)
        add(empty, "windows-1", "window", datetime.combine(day, time(9), tzinfo=TZ), 150, "Code", "work")
        add(empty, "android-1", "app_session", datetime.combine(day, time(13), tzinfo=TZ), 10, "Instagram", "social")
        add(empty, "android-1", "sleep", datetime.combine(day - timedelta(days=1), time(22, 45), tzinfo=TZ), 480,
            data={"stage": "asleep", "measured": True})
    evaluation = found(empty, datetime(2026, 9, 21, 21, 0, tzinfo=TZ))
    assert all(day.status == "met" for goal in evaluation.goals.values() for day in goal.days[:7])
    badges = {item.rule.id: item for item in streaks.achievements(Database(empty.database_path), evaluation, NOW)}
    assert badges["perfect_week"].earned is not None
    assert badges["perfect_week"].earned.earned_on == date(2026, 9, 20)  # Monday 14 to Sunday 20 September
    assert badges["full_set"].earned is None and badges["full_set"].progress.value == 2  # no Mac, no iPhone


def test_the_streaks_answer_quickly_and_are_reused(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    started = clock.perf_counter()
    hub.get("/api/v1/streaks", params={"tz": TZ_NAME})
    first = clock.perf_counter() - started
    assert first < 3.0, f"{first:.2f} s"  # well under a second on a desktop; a slow CI runner gets room
    calls = []
    real = streaks.evaluate
    monkeypatch.setattr(streaks, "evaluate", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    for path in ("/api/v1/goals", "/api/v1/achievements", "/api/v1/streaks"):
        assert hub.get(path, params={"tz": TZ_NAME}).status_code == 200
    assert calls == []  # all three reuse the first evaluation


# --- the bug review's cases --------------------------------------------------------------------------------------


def add_windows(settings: Settings) -> None:
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('windows-1', 'PC', 'windows', ?)",
                     (utc(datetime(2026, 1, 1, tzinfo=UTC)),))


def goal(evaluation: streaks.Evaluation, goal_id: str) -> streaks.Track:
    return evaluation.goals[goal_id]


def test_a_bedtime_estimate_is_not_met_while_the_night_can_change(empty: Settings) -> None:
    evening = datetime(2026, 9, 22, 21, 0, tzinfo=TZ)
    for start, minutes in ((evening, 20), (evening + timedelta(minutes=50), 120), (evening + timedelta(hours=3, minutes=10), 45)):
        add(empty, "android-1", "app_session", start, minutes, "Instagram", "social")  # on the phone until 00:55
    one_am = goal(found(empty, datetime(2026, 9, 23, 1, 0, tzinfo=TZ)), "bedtime").today
    assert one_am.status == "at_risk"  # the 21:20 quiet stretch is the longest so far, but the night isn't over
    add(empty, "android-1", "app_session", datetime(2026, 9, 23, 8, 0, tzinfo=TZ), 10, "Maps", "other")
    noon = goal(found(empty, datetime(2026, 9, 23, 12, 30, tzinfo=TZ)), "bedtime").today
    assert (noon.status, streaks.clock_text(noon.value)) == ("missed", "00:55")  # asleep after the last use


def test_a_bedtime_is_read_by_the_wall_clock_on_a_dst_night(empty: Settings) -> None:
    """Toronto falls back at 02:00 on 1 November 2026: sleep from 01:30 EST is 01:30, not an hour later."""
    start = datetime(2026, 11, 1, 1, 30, tzinfo=TZ, fold=1)  # the second 01:30, after the clocks went back
    add(empty, "android-1", "app_session", datetime(2026, 10, 31, 20, 0, tzinfo=TZ), 30, "Maps", "other")
    add(empty, "android-1", "sleep", start, 420, data={"stage": "asleep", "measured": True})
    with Database(empty.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO goals (goal_id, target, updated_at) VALUES ('bedtime', ?, ?)", (json.dumps("02:00"), utc(NOW)))
    bedtime = goal(found(empty, datetime(2026, 11, 1, 9, 0, tzinfo=TZ)), "bedtime").today
    assert streaks.clock_text(bedtime.value) == "01:30" and bedtime.status == "met"  # health data: final at once


def test_a_day_the_phone_sent_nothing_is_no_data_for_logged_it(empty: Settings) -> None:
    for day in (21, 23):  # the phone's battery died on the 22nd
        add(empty, "android-1", "app_session", datetime(2026, 9, day, 12, 0, tzinfo=TZ), 20, "Maps", "other")
        add(empty, "android-1", "meal", datetime(2026, 9, day, 13, 0, tzinfo=TZ), data={"items": ["rice"]})
    logged = track(found(empty, datetime(2026, 9, 23, 18, 0, tzinfo=TZ)), "logged_it")
    assert statuses(logged)[date(2026, 9, 22)] == "no_data"
    assert logged.current == [date(2026, 9, 21), date(2026, 9, 23)]  # not broken by a day nobody saw


def test_screens_down_needs_the_phone_that_evening_and_on_the_day(empty: Settings) -> None:
    add(empty, "android-1", "app_session", datetime(2026, 9, 21, 10, 0, tzinfo=TZ), 20, "Maps", "other")  # silent after
    add(empty, "android-1", "app_session", datetime(2026, 9, 22, 10, 0, tzinfo=TZ), 20, "Maps", "other")
    add(empty, "android-1", "app_session", datetime(2026, 9, 22, 20, 0, tzinfo=TZ), 20, "Maps", "other")  # used that evening
    add(empty, "android-1", "app_session", datetime(2026, 9, 23, 9, 0, tzinfo=TZ), 20, "Maps", "other")
    days = statuses(track(found(empty, datetime(2026, 9, 23, 18, 0, tzinfo=TZ)), "screens_down"))
    assert days[date(2026, 9, 22)] == "no_data"  # nothing from the phone since 10:00 the day before
    assert days[date(2026, 9, 23)] == "met"  # used at 20:00 and again in the morning, nothing in between: a real 0


def test_a_busy_database_keeps_the_stored_badges(hub: TestClient, demo: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    first = {item["id"]: item for item in hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()["achievements"]}
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("DELETE FROM achievements WHERE achievement_id = 'focus_1000'")  # earned, but not recorded yet
        conn.execute("DELETE FROM events WHERE kind = 'meal'")  # the data behind the logged-meals run is gone
    calls = []

    def busy(conn: Any) -> Any:
        calls.append(conn)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(streaks, "transaction", busy)
    again = {item["id"]: item for item in hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()["achievements"]}
    assert len(calls) == 1  # only to record the one not stored, which fails
    assert again["streak_7"] == first["streak_7"]  # the stored ones as they were
    assert again["focus_1000"]["unlocked"] and again["focus_1000"]["earned_on"] == "2026-09-15"  # shown, recorded later
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("DELETE FROM events")  # nothing left to earn any badge from
    kept = {item["id"]: item for item in hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()["achievements"]}
    assert kept["streak_7"] == first["streak_7"] and kept["full_set"] == first["full_set"]  # only the stored rows say so


def test_reading_badges_takes_no_write_lock_once_they_are_stored(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    hub.get("/api/v1/achievements", params={"tz": TZ_NAME})

    def refused(conn: Any) -> Any:
        raise AssertionError("no write needed")

    monkeypatch.setattr(streaks, "transaction", refused)
    assert hub.get("/api/v1/achievements", params={"tz": TZ_NAME}).json()["unlocked"] == 4


def test_re_seeding_clears_the_badges_it_replaced(hub: TestClient, demo: Settings) -> None:
    hub.get("/api/v1/achievements", params={"tz": TZ_NAME})
    seed(demo, 14, TZ, NOW + timedelta(days=30))
    with Database(demo.database_path).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM achievements").fetchone()[0] == 0


def test_a_perfect_week_with_the_laptop_off_on_sunday(empty: Settings) -> None:
    add_windows(empty)
    for offset in range(8):
        day = date(2026, 9, 14) + timedelta(days=offset)
        if day.weekday() != 6:
            add(empty, "windows-1", "window", datetime.combine(day, time(9), tzinfo=TZ), 150, "Code", "work")
        add(empty, "android-1", "app_session", datetime.combine(day, time(13), tzinfo=TZ), 10, "Instagram", "social")
        add(empty, "android-1", "sleep", datetime.combine(day - timedelta(days=1), time(22, 45), tzinfo=TZ), 480,
            data={"stage": "asleep", "measured": True})
    evaluation = found(empty, datetime(2026, 9, 21, 21, 0, tzinfo=TZ))
    assert goal(evaluation, "focus_target").days[6].status == "no_data"  # Sunday: no computer
    badge = next(item for item in streaks.achievements(Database(empty.database_path), evaluation, NOW) if item.rule.id == "perfect_week")
    assert badge.earned is not None and badge.earned.earned_on == date(2026, 9, 20)


def test_wrapped_streaks_for_a_week_older_than_the_history(empty: Settings) -> None:
    add(empty, "android-1", "app_session", datetime(2025, 3, 5, 12, 0, tzinfo=TZ), 20, "Maps", "other")
    add(empty, "android-1", "meal", datetime(2025, 3, 5, 13, 0, tzinfo=TZ), data={"items": ["soup"]})
    add(empty, "android-1", "app_session", datetime(2026, 9, 20, 12, 0, tzinfo=TZ), 20, "Maps", "other")
    moment = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
    assert found(empty, moment).first == date(2025, 9, 25)  # 366 days, today included
    week = {item.id: item for item in streaks.week_streaks(Database(empty.database_path), TZ, TZ_NAME, moment, date(2025, 3, 3), date(2025, 3, 9))}
    assert (week["logged_it"].met, week["logged_it"].dates) == (1, [date(2025, 3, 5)])
    assert week["logged_it"].days_with_data == 1  # the phone sent data that day only


def test_rules_with_a_typo_are_refused() -> None:
    raw = json.loads(streaks.resources.files("daytrace_hub").joinpath("data", "streak_rules.json").read_text(encoding="utf-8"))

    def broken(change: Any) -> str:
        copy = json.loads(json.dumps(raw))
        change(copy)
        with pytest.raises(ValueError, match="streak_rules.json") as error:
            streaks.parse_rules(copy)
        return str(error.value)

    assert "unknown kind" in broken(lambda r: r["streaks"][1].update(kind="at_mots"))
    assert "no target" in broken(lambda r: r["streaks"][1].pop("target"))
    assert "unknown goal" in broken(lambda r: r["streaks"][0].update(goal="focus_tagret"))
    assert "twice" in broken(lambda r: r["streaks"].append(dict(r["streaks"][0])))
    assert "unknown measure" in broken(lambda r: r["goals"][0].update(measure="focus"))
    assert "unknown kind" in broken(lambda r: r["achievements"][0].update(kind="first"))


def test_a_new_minute_reads_only_the_days_that_can_still_change(demo: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    read: list[date] = []
    for name, measure in list(streaks.MEASURES.items()):
        monkeypatch.setitem(streaks.MEASURES, name, lambda stats, day, measure=measure: read.append(day) or measure(stats, day))
    found(demo)
    assert len(set(read)) == 14
    read.clear()
    found(demo, NOW + timedelta(minutes=1))  # no new data: the past days' readings are final and kept
    assert set(read) == {NOW.date()}
    read.clear()
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("UPDATE goals SET target = '300' WHERE goal_id = 'focus_target'")
    assert track(found(demo, NOW + timedelta(minutes=1)), "focus_flame").rule.startswith("300")
    assert set(read) <= {NOW.date()}  # a new target judges again without reading again


def test_a_long_history_is_read_a_month_at_a_time(empty: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    for offset in range(0, 70, 7):
        add(empty, "android-1", "app_session", datetime(2026, 7, 1, 12, 0, tzinfo=TZ) + timedelta(days=offset), 20, "Maps", "other")
    made = []

    class Counted(streaks.Stats):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            made.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(streaks, "Stats", Counted)
    evaluation = found(empty, datetime(2026, 9, 10, 12, 0, tzinfo=TZ))
    assert len(evaluation.streaks[0].days) == 72 and len(made) == 3  # 31 + 31 + 10 days, each with its own Stats


def test_requests_together_share_one_evaluation(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = streaks.evaluate

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        clock.sleep(0.2)  # slow enough that the others arrive while it works
        return real(*args, **kwargs)

    monkeypatch.setattr(streaks, "evaluate", counted)
    codes: list[int] = []
    threads = [threading.Thread(target=lambda path=path: codes.append(hub.get(path, params={"tz": TZ_NAME}).status_code))
               for path in ("/api/v1/streaks", "/api/v1/goals", "/api/v1/achievements")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert codes == [200, 200, 200] and len(calls) == 1
