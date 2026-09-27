"""Tests for DT-53: streaks, goals and achievements, on 14 seeded days and on small hand-made histories.

The claims: the seeded 5-day focus streak that breaks once is reported as it was built; today is at_risk (with
what is left) until it qualifies, and a limit only qualifies once the day is over; a day with no data from what a
rule needs neither extends nor breaks a streak; days follow the time zone, midnight and DST; goals change the
rules they drive; badges are earned once and kept; every streak and badge says its rule and the days that counted.
"""
from __future__ import annotations

import json
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
    assert down.today.status == "at_risk" and down.today.remaining == 15  # tonight's late hours are still going
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


def test_the_streaks_answer_quickly_and_are_reused(hub: TestClient) -> None:
    started = clock.perf_counter()
    hub.get("/api/v1/streaks", params={"tz": TZ_NAME})
    first = clock.perf_counter() - started
    started = clock.perf_counter()
    for path in ("/api/v1/goals", "/api/v1/achievements", "/api/v1/streaks"):
        assert hub.get(path, params={"tz": TZ_NAME}).status_code == 200
    reused = clock.perf_counter() - started
    assert first < 3.0, f"{first:.2f} s"  # well under a second on a desktop; a slow CI runner gets room
    assert reused < first + 0.5  # the three reuse one evaluation
