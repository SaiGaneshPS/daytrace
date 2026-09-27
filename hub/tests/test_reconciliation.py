"""Tests for DT-59: every number the hub shows agrees with every other, on 14 seeded days.

The claims: screen time split every way (app, category, device, hour, day) adds up to one total, which is the
timeline's blocks, the day summary's, the Overview's and every chart's that is cut from it; category shares add up
to 100%; each streak day's reading is the stats engine's own number, judged against its target; a night guessed from
the phone is marked estimated wherever it shows; and every answer about a range says its unit, range, source and
whether it is estimated. A deliberate off-by-one in stats.py (or a stale dashboard fixture) fails one of these.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub.api import insights as insights_api
from daytrace_hub.api import streaks as streaks_api
from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.app import create_app
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.seed import seed
from daytrace_hub.stats import Stats

TZ_NAME = "America/Toronto"
TZ = ZoneInfo(TZ_NAME)
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)
TODAY = NOW.date()
FIRST = TODAY - timedelta(days=13)
DAYS = [FIRST + timedelta(days=i) for i in range(14)]
TABS = ["overview", "apps", "devices", "focus", "sleep", "food", "calendar"]
GROUPINGS = ["app", "category", "device", "hour", "day"]


@pytest.fixture
def demo(tmp_path: Path) -> Settings:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    seed(settings, 14, TZ, NOW)
    return settings


@pytest.fixture
def hub(demo: Settings, fake_llm: FakeModelServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for module in (insights_api, timeline_api, streaks_api):
        monkeypatch.setattr(module, "current_time", lambda: NOW.astimezone(UTC))
    insights_api._cache.clear()
    with TestClient(create_app(demo, llm=fake_llm.llm()), client=("127.0.0.1", 50000), base_url="http://localhost:8767") as client:
        yield client
    insights_api._cache.clear()


@pytest.fixture
def stats_of(demo: Settings) -> Iterator[Callable[[], Stats]]:
    stack = ExitStack()
    yield lambda: Stats(stack.enter_context(Database(demo.database_path).connect()), TZ, TZ_NAME, NOW)
    stack.close()


def get(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/v1{path}", params={"tz": TZ_NAME, **params})
    assert response.status_code == 200, response.text
    return response.json()


def metric(body: dict[str, Any], metric_id: str) -> Any:
    return next(item["value"] for item in body["metrics"] if item["id"] == metric_id)


def close(total: float, parts: list[float | None], rounded: int) -> bool:
    """Parts rounded to hundredths on their own add up to the total (rounded too) within half a hundredth each."""
    return abs(total - sum(part or 0 for part in parts)) <= 0.005 * (rounded + 1) + 1e-9


# --- screen time, every way ------------------------------------------------------------------------------------


def test_each_day_every_split_is_the_timelines_blocks(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    stats = stats_of()
    for day in DAYS:
        totals = {group: stats.totals(day, group_by=group) for group in GROUPINGS}
        total = totals["app"]["total_seconds"]
        for group, found in totals.items():
            assert sum(item["seconds"] for item in found["items"]) == total, (day, group)  # every split, the same seconds
        timeline = get(hub, "/timeline", date=day.isoformat())
        blocks = sum(session["seconds"] for lane in timeline["lanes"] if lane["counted"] for session in lane["sessions"])
        assert blocks == timeline["totals"]["seconds"] == total, day  # the timeline's blocks are the same time
        summary = get(hub, "/insights/day", date=day.isoformat())
        assert summary["screen_minutes"] == totals["app"]["total_minutes"] == timeline["totals"]["minutes"], day
        by_device = {item["key"]: item["seconds"] for item in totals["device"]["items"]}
        assert {lane["device_id"]: lane["seconds"] for lane in timeline["lanes"] if lane["counted"] and lane["seconds"]} == {
            key: seconds for key, seconds in by_device.items() if seconds}, day


def test_the_range_total_is_every_charts_total(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    for span in ("14d", "7d"):
        overview = get(hub, "/insights/overview", range=span)
        apps = get(hub, "/insights/apps", range=span)
        devices = get(hub, "/insights/devices", range=span)
        first = FIRST if span == "14d" else TODAY - timedelta(days=6)
        stats = stats_of()
        total = stats.totals(first, TODAY)["total_minutes"]
        assert metric(overview, "screen_time") == total, span
        charts = {
            "screen by device": [value for line in overview["series"]["screen_by_device"]["lines"] for value in line["values"]],
            "categories": [item["value"] for item in overview["series"]["categories"]["items"]],
            "hours": [cell["value"] for cell in overview["series"]["hours"]["cells"]],
            "treemap": [item["value"] for item in apps["series"]["treemap"]["items"]],
            "categories by day": [value for line in apps["series"]["by_category"]["lines"] for value in line["values"]],
            "device share": [item["value"] for item in devices["series"]["share"]["items"]],
            "devices by day": [value for line in devices["series"]["by_day"]["lines"] for value in line["values"]],
            "device metrics": [item["value"] for item in devices["metrics"] if item["id"].startswith("device:")],
        }
        for name, parts in charts.items():
            assert close(total, parts, len(parts)), (span, name, total, sum(part or 0 for part in parts))
        phone_and_computer = [value for line in overview["series"]["phone_vs_computer"]["lines"] for value in line["values"]]
        assert close(total, phone_and_computer, len(phone_and_computer)), span  # the seed has only phones and computers


def test_category_shares_of_the_screen_time_add_up_to_100(hub: TestClient) -> None:
    for span in ("14d", "7d", "today"):
        overview = get(hub, "/insights/overview", range=span)
        total = metric(overview, "screen_time")  # the hero number, not the donut's own sum
        shares = [100 * item["value"] / total for item in overview["series"]["categories"]["items"]]
        assert abs(sum(shares) - 100) <= 0.1, (span, sum(shares))


# --- streaks and goals -----------------------------------------------------------------------------------------


def test_each_streak_day_is_the_stats_engines_reading(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    body = get(hub, "/streaks", days=366)  # every day judged, so a run can be counted from the days
    stats = stats_of()
    readings: dict[str, Callable[[Any], float | None]] = {
        "focus_flame": lambda day: stats.focused_minutes(day)["value"],
        "logged_it": lambda day: float(len(stats.meals(day))),
    }
    streaks = {streak["id"]: streak for streak in body["streaks"]}
    for streak_id, reading in readings.items():
        streak = streaks[streak_id]
        for entry in streak["days"]:
            day = datetime.fromisoformat(entry["date"]).date()
            if entry["value"] is None:
                assert entry["status"] == "no_data", (streak_id, day)
                continue
            assert entry["value"] == pytest.approx(reading(day), abs=0.01), (streak_id, day)
            if day < TODAY:  # a whole day is met or missed by its reading against the target
                assert entry["status"] == ("met" if entry["value"] >= streak["target"] else "missed"), (streak_id, day)
        met = [entry["date"] for entry in streak["days"] if entry["status"] == "met"]
        assert set(streak["counted"]) <= set(met) and streak["current"] == len(streak["counted"])
        run = 0
        for entry in reversed(streak["days"][:-1] if streak["today"] == "at_risk" else streak["days"]):
            if entry["status"] == "no_data":
                continue  # a day without data neither extends nor breaks a run
            if entry["status"] != "met":
                break
            run += 1
        assert streak["current"] == run, streak_id  # the current run is the met days in a row up to today
    social = get(hub, "/goals")["goals"]
    cap = next(goal for goal in social if goal["id"] == "social_cap")
    assert cap["today"]["value"] == pytest.approx(stats.totals(TODAY, category="social")["total_minutes"], abs=0.01)


# --- estimated wherever a night is guessed ---------------------------------------------------------------------


def test_a_night_guessed_from_the_phone_is_estimated_everywhere(hub: TestClient, demo: Settings) -> None:
    # No health app data for last night (the night ending this morning): sleep is worked out from the phone.
    night_start, night_end = datetime(2026, 9, 24, 18, 0, tzinfo=TZ), datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
    with Database(demo.database_path).connect() as conn, transaction(conn):
        removed = conn.execute("DELETE FROM events WHERE kind = 'sleep' AND start_utc >= ? AND start_utc < ?",
                               (night_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), night_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"))).rowcount
    assert removed
    insights_api._cache.clear()
    summary = get(hub, "/insights/day", date=TODAY.isoformat())
    assert summary["sleep_minutes"] is not None and summary["sleep_estimated"] and summary["estimated"] and summary["meta"]["estimated"]
    timeline = get(hub, "/timeline", date=TODAY.isoformat())
    assert timeline["sleep"] == []  # the timeline shows only recorded sleep: a guessed night is never drawn as a record
    overview = get(hub, "/insights/overview", range="7d")
    assert next(item for item in overview["metrics"] if item["id"] == "sleep")["estimated"]
    sleep = get(hub, "/insights/sleep", range="7d")
    assert metric(sleep, "estimated_nights") == 1 and sleep["series"]["sleep_by_night"]["estimated"]
    assert sleep["series"]["sleep_by_night"]["lines"][1]["values"][-1] is not None  # drawn on the "Estimated" line
    goals = get(hub, "/goals")
    bedtime = next(goal for goal in goals["goals"] if goal["id"] == "bedtime")
    assert bedtime["today"]["estimated"] and goals["meta"]["estimated"]  # the bedtime goal is judged on a guessed night
    story = get(hub, "/story", date=TODAY.isoformat())
    assert story["meta"]["estimated"]


def test_measured_nights_are_not_estimated(hub: TestClient) -> None:
    assert not get(hub, "/insights/day", date=TODAY.isoformat())["sleep_estimated"]  # the seed's health app sent every night
    assert metric(get(hub, "/insights/sleep", range="14d"), "estimated_nights") == 0
    assert not next(goal for goal in get(hub, "/goals")["goals"] if goal["id"] == "bedtime")["today"]["estimated"]


# --- every range answer says what its numbers are --------------------------------------------------------------

RANGE_ANSWERS: list[tuple[str, str, dict[str, Any]]] = [
    ("story", "/story", {"date": TODAY.isoformat()}),
    ("timeline", "/timeline", {"date": TODAY.isoformat()}),
    ("day summary", "/insights/day", {"date": TODAY.isoformat()}),
    *[(f"{tab} tab", f"/insights/{tab}", {"range": "7d"}) for tab in TABS],
    ("app detail", "/insights/apps/detail", {"app": "TikTok", "range": "7d"}),
    ("wrapped", "/wrapped", {}),
    ("streaks", "/streaks", {}),
    ("goals", "/goals", {}),
    ("achievements", "/achievements", {}),
]


@pytest.mark.parametrize(("name", "path", "params"), RANGE_ANSWERS, ids=[name for name, _, _ in RANGE_ANSWERS])
def test_every_range_answer_says_its_unit_range_source_and_estimated(hub: TestClient, name: str, path: str, params: dict[str, Any]) -> None:
    meta = get(hub, path, **params)["meta"]
    assert set(meta) >= {"unit", "range", "source", "estimated"}, name
    # A unit for the headline numbers; none only where each number has its own (the goals, the day's story facts).
    assert (meta["unit"] is None) if name in {"goals", "story"} else (isinstance(meta["unit"], str) and meta["unit"]), (name, meta["unit"])
    assert set(meta["range"]) == {"start", "end", "tz"} and meta["range"]["tz"] == TZ_NAME
    assert datetime.fromisoformat(meta["range"]["start"]) < datetime.fromisoformat(meta["range"]["end"])
    assert meta["source"] == "seed"  # the seed's data only
    assert isinstance(meta["estimated"], bool)


def test_the_schema_promises_the_meta_on_every_range_answer(hub: TestClient) -> None:
    spec = hub.get("/openapi.json").json()
    meta = spec["components"]["schemas"]["Meta"]
    assert set(meta["required"]) >= {"range", "source", "estimated"} and set(meta["properties"]) >= {"unit", "range", "source", "estimated"}
    paths = {path for _, path, _ in RANGE_ANSWERS} - {f"/insights/{tab}" for tab in TABS} | {"/insights/{tab}"}
    for path in paths:
        response = spec["paths"][f"/api/v1{path}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        model = spec["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
        assert model["properties"]["meta"] == {"$ref": "#/components/schemas/Meta"} and "meta" in model["required"], path


def test_an_answer_says_which_days_its_tools_read(hub: TestClient, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", {"first_day": "2026-09-19", "last_day": "2026-09-24", "group_by": "day"})
    fake_llm.reply_text("You were on a screen every one of those days.")
    response = hub.post("/api/v1/ask", json={"question": "How much screen time did I have this week?", "tz": TZ_NAME})
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]
    assert (meta["range"]["start"][:10], meta["range"]["end"][:10]) == ("2026-09-19", "2026-09-25") and meta["source"] == "seed"
    fake_llm.reply_text("OFF_TOPIC")
    declined = hub.post("/api/v1/ask", json={"question": "What is the capital of France?", "tz": TZ_NAME}).json()
    assert declined["declined"] and declined["meta"] is None  # nothing was read


def test_the_ranges_behind_streaks_and_goals_start_the_evening_before(hub: TestClient) -> None:
    goals = get(hub, "/goals")["meta"]["range"]
    assert goals["start"].startswith((TODAY - timedelta(days=1)).isoformat())  # last night's bedtime began yesterday
    streaks = get(hub, "/streaks", days=3)
    assert streaks["meta"]["range"]["start"].startswith((datetime.fromisoformat(streaks["since"]).date() - timedelta(days=1)).isoformat())


def open_without_close(settings: Settings, when: datetime, app: str, category: str) -> None:
    """An iPhone app seen opening and never closing: its time is inferred (estimated)."""
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, seq, kind, source, start_utc, end_utc, utc_offset_min, app, app_id, title, category, data, received_at)"
            " VALUES ('seed-iphone', ?, NULL, 'app_open', 'seed', ?, NULL, -240, ?, ?, NULL, ?, '{}', ?)",
            (f"content:open-{app}-{when.isoformat()}", when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z"), app,
             f"app.{app.lower()}", category, "2026-09-25T20:00:00.000000Z"),
        )


def test_a_streak_is_estimated_by_the_days_behind_its_numbers(hub: TestClient, demo: Settings) -> None:
    # Study on the iPhone with no close seen on the 18th, a day of Focus flame's best run (17 to 21 September), and
    # not among the 3 days listed: the streak's numbers rest on it, so the streak is estimated.
    open_without_close(demo, datetime(2026, 9, 18, 12, 0, tzinfo=TZ), "Anki", "study")
    body = get(hub, "/streaks", days=3)
    flame = next(streak for streak in body["streaks"] if streak["id"] == "focus_flame")
    assert "2026-09-18" in flame["best_dates"]
    assert not any(day["estimated"] for day in flame["days"])  # the listed days were all measured ...
    assert flame["estimated"] and body["meta"]["estimated"]  # ... but the best run wasn't
    history = get(hub, "/streaks", days=366)
    estimated_days = {day["date"] for streak in history["streaks"] for day in streak["days"] if day["estimated"]}
    badges = get(hub, "/achievements")
    earned = {day for badge in badges["achievements"] for day in badge["dates"]}
    assert badges["meta"]["estimated"] == bool(estimated_days & earned)  # badges rest on the same days


def test_one_clock_per_request(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(streaks_api, "current_time", lambda: calls.append(1) or NOW.astimezone(UTC))
    for path in ("/streaks", "/goals", "/achievements"):
        calls.clear()
        get(hub, path)
        assert len(calls) == 1, path  # the meta is of the same moment as the numbers


def test_range_sources_are_read_by_index(demo: Settings) -> None:
    with Database(demo.database_path).connect() as conn:
        for query in (timeline_api.RANGE_SPAN_SOURCES, timeline_api.RANGE_POINT_SOURCES):
            plan = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}", ("2026-09-20", "2026-09-21")))
            assert "USING INDEX" in plan and ">" in plan and "end_utc=?" not in plan, plan  # a range, not every point event


def test_focus_is_estimated_only_by_what_it_is_made_of(demo: Settings, stats_of: Callable[[], Stats]) -> None:
    # An iPhone map opened and never seen closing: screen time is estimated that day, focused time isn't.
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, seq, kind, source, start_utc, end_utc, utc_offset_min, app, app_id, title, category, data, received_at)"
            " VALUES ('seed-iphone', 'content:map-open', NULL, 'app_open', 'seed', ?, NULL, -240, 'Maps', 'com.apple.maps', NULL, 'other', '{}', ?)",
            ("2026-09-22T18:00:00.000000Z", "2026-09-25T20:00:00.000000Z"),
        )
    stats = stats_of()
    day = TODAY - timedelta(days=3)  # the 22nd
    assert stats.totals(day)["estimated"] and stats.day_estimated(day)
    assert not stats.focused_minutes(day)["estimated"]


@pytest.mark.parametrize(("sources", "said"), [({"seed"}, "seed"), ({"seed", "tracker"}, "mixed"), ({"tracker"}, "real"), (set(), "real")])
def test_one_rule_for_where_the_data_came_from(sources: set[str], said: str) -> None:
    assert timeline_api.source_of(sources) == said
