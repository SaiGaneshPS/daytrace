"""Tests for DT-41: the Insights tabs (GET /insights/{tab}) and the week's Wrapped (GET /wrapped), on 14 seeded days.

The claims: every tab answers with chart-ready series whose numbers add up to the tab's totals and agree with the
stats engine (so with Today and every other page), missing days are null rather than zero, ranges are checked, the
answers are cached until the data changes, and each tab answers in well under a second. Wrapped's lines are the
local model's, every number checked against the week's facts, or plain ones when the model can't be used.
"""
from __future__ import annotations

import json
import os
import time as clock
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub.api import insights as insights_api
from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.api.insights import RangeInfo
from daytrace_hub.app import create_app
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.seed import seed
from daytrace_hub.stats import Stats
from daytrace_hub.story import (
    MIN_DAYS_TO_COMPARE,
    WRAPPED_LINES,
    Fact,
    clean_lines,
    template_wrapped,
    week_facts,
    wrapped_problems,
)

TZ_NAME = "America/Toronto"
TZ = ZoneInfo(TZ_NAME)
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)  # a Friday evening; the seed covers the 14 days up to now
TODAY = NOW.date()
FIRST = TODAY - timedelta(days=13)
LAST_WEEK = date(2026, 9, 14)  # the last whole week: Monday 14 to Sunday 20 September
TABS = ["overview", "apps", "devices", "focus", "sleep", "food", "calendar"]
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def demo(tmp_path: Path) -> Settings:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    seed(settings, 14, TZ, NOW)
    return settings


@pytest.fixture
def hub(demo: Settings, fake_llm: FakeModelServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(insights_api, "current_time", lambda: NOW.astimezone(UTC))
    monkeypatch.setattr(timeline_api, "current_time", lambda: NOW.astimezone(UTC))
    insights_api._cache.clear()
    with TestClient(create_app(demo, llm=fake_llm.llm()), client=LOCAL, base_url="http://localhost:8767") as client:
        yield client
    insights_api._cache.clear()


def tab(client: TestClient, name: str, span: str = "14d") -> dict[str, Any]:
    response = client.get(f"/api/v1/insights/{name}", params={"range": span, "tz": TZ_NAME})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def stats_of(demo: Settings) -> Iterator[Callable[[], Stats]]:
    """Makes a Stats on its own connection (a fresh read), open until the test ends."""
    stack = ExitStack()

    def make() -> Stats:
        return Stats(stack.enter_context(Database(demo.database_path).connect()), TZ, TZ_NAME, NOW)

    yield make
    stack.close()


def metric(body: dict[str, Any], metric_id: str) -> Any:
    return next(item["value"] for item in body["metrics"] if item["id"] == metric_id)


def close(total: float, parts: list[float], items: int) -> bool:
    """Each part is rounded to 2 decimals on its own, so a sum can be off by up to half a hundredth per part."""
    return abs(total - sum(parts)) <= 0.005 * max(items, 1) + 1e-9


# --- every tab ---------------------------------------------------------------------------------------------------


def test_every_tab_answers_with_its_metrics_and_series(hub: TestClient) -> None:
    kinds = {
        "overview": {"screen_by_device": "stacked", "categories": "donut", "focus_by_day": "trend", "hours": "heatmap",
                     "phone_vs_computer": "stacked"},
        "apps": {"top_apps": "bars", "treemap": "treemap", "by_category": "stacked", "switches": "trend", "leaderboard": "leaderboard"},
        "devices": {"share": "donut", "by_day": "stacked", "hours": "heatmap", "flow": "sankey", "handoffs": "sankey", "sync": "strip"},
        "focus": {"score": "gauge", "focus_by_day": "stacked", "switches_by_hour": "bars", "late_vs_focus": "scatter",
                  "switches_by_day": "trend", "distraction_hours": "heatmap"},
        "sleep": {"sleep_by_night": "stacked", "schedule": "trend", "late_night": "bars"},
        "food": {"meals_by_day": "stacked", "meal_times": "scatter", "top_items": "bars"},
        "calendar": {"plan_by_day": "stacked", "blocks": "bars", "hours": "heatmap"},
    }
    for name, expected in kinds.items():
        body = tab(hub, name)
        assert body["tab"] == name and body["tz"] == TZ_NAME
        assert body["range"] == {"first": FIRST.isoformat(), "last": TODAY.isoformat(), "days": 14, "label": "Last 14 days"}
        assert {key: series["kind"] for key, series in body["series"].items()} == expected
        assert body["meta"]["source"] == "seed" and body["in_progress"] is True
        for item in body["metrics"]:
            assert item["label"] and item["unit"] and item["explain"], item
        for series in body["series"].values():
            assert series["title"] and series["unit"] and series["explain"]
            for line in series.get("lines") or []:
                assert len(line["values"]) == len(series["x"])  # one value per label


def test_the_overview_adds_up_and_agrees_with_the_stats_engine(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "overview")
    stats = stats_of()
    totals = stats.totals(FIRST, TODAY)
    total = metric(body, "screen_time")
    assert total == totals["total_minutes"]
    donut = [item["value"] for item in body["series"]["categories"]["items"]]
    assert close(total, donut, len(donut))
    stacked = body["series"]["screen_by_device"]
    by_day = {item["key"]: item["minutes"] for item in stats.totals(FIRST, TODAY, group_by="day")["items"]}
    for index, label in enumerate(stacked["x"]):
        parts = [line["values"][index] or 0 for line in stacked["lines"]]
        assert close(by_day[label], parts, len(parts)), label  # each day's stack is that day's screen time
    heat = [cell["value"] for cell in body["series"]["hours"]["cells"]]
    assert close(total, heat, len(heat))
    scores = [stats.focus_score(FIRST + timedelta(days=i))["value"] for i in range(14)]
    assert body["series"]["focus_by_day"]["lines"][0]["values"] == [float(score) if score is not None else None for score in scores]
    assert metric(body, "daily_average") == round(totals["total_seconds"] / 14 / 60, 2)


def test_the_apps_and_devices_tabs_add_up(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    stats = stats_of()
    total = stats.totals(FIRST, TODAY)["total_minutes"]
    apps = tab(hub, "apps")
    tree = apps["series"]["treemap"]["items"]
    assert close(total, [category["value"] for category in tree], len(tree))
    for category in tree:
        assert close(category["value"], [child["value"] for child in category["children"]], len(category["children"]))
    top = stats.top_apps(FIRST, TODAY, limit=15)
    assert [(item["name"], item["value"], item["category"]) for item in apps["series"]["top_apps"]["items"]] == [
        (app["app"], app["minutes"], app["category"]) for app in top]
    assert metric(apps, "top_app") == top[0]["app"]
    devices = tab(hub, "devices")
    links = [link["value"] for link in devices["series"]["flow"]["links"]]
    assert close(total, links, len(links))
    share = [item["value"] for item in devices["series"]["share"]["items"]]
    assert close(total, share, len(share))
    assert set(devices["series"]["flow"]["nodes"]) >= {"Work", "Social"}  # categories are nodes too


def test_the_focus_tab_has_the_scores_the_split_and_the_late_night_pattern(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "focus")
    stats = stats_of()
    scores = [stats.focus_score(FIRST + timedelta(days=i))["value"] for i in range(14)]
    known = [score for score in scores if score is not None]
    assert metric(body, "focus_score") == round(sum(known) / len(known))
    assert body["series"]["score"]["value"] == metric(body, "focus_score") and body["series"]["score"]["max"] == 100
    best = max(known)
    assert metric(body, "best_score") == best
    scatter = body["series"]["late_vs_focus"]
    assert scatter["stats"]["n"] == len(scatter["points"]) >= 3
    assert scatter["stats"]["rho"] is not None and scatter["stats"]["rho"] < 0  # the seeded pattern: later nights, less focus
    assert "Correlation, not cause" in scatter["note"]
    lines = {line["name"]: line["values"] for line in body["series"]["focus_by_day"]["lines"]}
    assert set(lines) == {"Focused", "Other work or study", "Distracted"}


def test_the_sleep_food_and_calendar_tabs_agree_with_the_stats_engine(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    stats = stats_of()
    days = [FIRST + timedelta(days=i) for i in range(14)]
    sleep = tab(hub, "sleep")
    nights = [stats.sleep_estimate(day)["value"] for day in days]
    known = [night for night in nights if night is not None]
    assert metric(sleep, "sleep") == round(sum(known) / len(known), 2)
    assert metric(sleep, "nights") == len(known)
    stacked = {line["name"]: line["values"] for line in sleep["series"]["sleep_by_night"]["lines"]}
    assert [m if m is not None else e for m, e in zip(stacked["Measured"], stacked["Estimated"], strict=True)] == nights
    bedtimes = [value for value in sleep["series"]["schedule"]["lines"][0]["values"] if value is not None]
    assert bedtimes and all(180 <= value <= 600 for value in bedtimes)  # asleep between 21:00 and 04:00
    food = tab(hub, "food")
    meals = stats.meals(FIRST, TODAY)
    assert metric(food, "meals") == len(meals) > 0
    per_day = sum(value or 0 for line in food["series"]["meals_by_day"]["lines"] for value in line["values"])
    assert per_day == len(meals) == len(food["series"]["meal_times"]["points"])
    calendar = tab(hub, "calendar")
    plans = [stats.planned_vs_actual(day) for day in days]
    assert metric(calendar, "planned") == round(sum(plan["totals_seconds"]["planned"] for plan in plans) / 60, 2)
    assert metric(calendar, "events") == sum(len(plan["blocks"]) for plan in plans)


def test_a_device_that_sent_nothing_that_day_is_a_gap_not_zero(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    quiet = TODAY - timedelta(days=3)
    start, end = datetime.combine(quiet, datetime.min.time(), tzinfo=TZ), datetime.combine(quiet + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    with Database(demo.database_path).connect() as conn, transaction(conn):  # the desk PC stayed off that day
        conn.execute("DELETE FROM events WHERE device_id = 'seed-windows' AND start_utc < ? AND COALESCE(end_utc, start_utc) >= ?", (utc(end), utc(start)))
    day = (quiet - FIRST).days
    by_day = {total["key"]: total["minutes"] for total in stats_of().totals(FIRST, TODAY, group_by="day")["items"]}
    for name, key in (("overview", "screen_by_device"), ("devices", "by_day")):
        lines = {line["key"]: line["values"] for line in tab(hub, name)["series"][key]["lines"]}
        assert lines["seed-windows"][day] is None, name  # no data from it, which is not 0 minutes
        assert lines["seed-android"][day] is not None and lines["seed-windows"][day + 1] is not None
        assert close(by_day[quiet.isoformat()], [values[day] or 0 for values in lines.values()], len(lines))


def test_device_names_that_repeat_are_told_apart(hub: TestClient, demo: Settings) -> None:
    add_device(demo, "android-9", "Galaxy phone (demo)", "android")  # the phone, paired again with its old name
    add_device(demo, "windows-9", "work", "windows")  # a computer named like the Work category
    add_event(demo, datetime(2026, 9, 23, 12, 0, tzinfo=TZ), device="android-9")
    add_event(demo, datetime(2026, 9, 23, 14, 0, tzinfo=TZ), device="windows-9", kind="window", app="Code", category="work")
    body = tab(hub, "devices")
    flow = body["series"]["flow"]
    assert len({node.casefold() for node in flow["nodes"]}) == len(flow["nodes"])  # a Sankey refuses a name twice
    assert all(link["source"] != link["target"] and {link["source"], link["target"]} <= set(flow["nodes"]) for link in flow["links"])
    assert {"Galaxy phone (demo) (android-9)", "Galaxy phone (demo) (seed-android)", "work (windows-9)", "Work"} <= set(flow["nodes"])
    names = [line["name"] for line in body["series"]["by_day"]["lines"]]
    assert len(set(names)) == len(names)
    rows = body["series"]["hours"]["y"]
    assert len(set(rows)) == len(rows)
    assert insights_api.distinct({"a": "Pixel", "b": "pixel", "c": "Work", "d": "Mac"}, taken=["work"]) == {
        "a": "Pixel (a)", "b": "pixel (b)", "c": "Work (c)", "d": "Mac"}


def test_overlapping_events_are_planned_time_once_in_the_heatmap(hub: TestClient, demo: Settings) -> None:
    day = datetime(2026, 9, 23, 10, 0, tzinfo=TZ)
    add_event(demo, day, minutes=60, kind="calendar_event", app=None, category=None, title="Standup and planning")
    add_event(demo, day + timedelta(minutes=30), minutes=60, device="seed-iphone", kind="calendar_event", app=None, category=None, title="Study block")
    body = tab(hub, "calendar")
    cells = [cell["value"] for cell in body["series"]["hours"]["cells"]]
    assert close(metric(body, "planned"), cells, len(cells))  # 10:00 to 11:30 is 90 minutes, not 120


# --- ranges and missing days -------------------------------------------------------------------------------------


def test_days_without_data_are_null_not_zero(hub: TestClient) -> None:
    body = tab(hub, "overview", f"{(FIRST - timedelta(days=3)).isoformat()}..{TODAY.isoformat()}")
    for line in body["series"]["screen_by_device"]["lines"]:
        assert line["values"][:3] == [None, None, None]  # before the data starts
    future = tab(hub, "overview", f"{(TODAY + timedelta(days=1)).isoformat()}..{(TODAY + timedelta(days=2)).isoformat()}")
    assert metric(future, "screen_time") is None and future["in_progress"] is False  # nothing has happened yet
    past = tab(hub, "overview", f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}")
    assert past["in_progress"] is False


@pytest.mark.parametrize("span", ["2026-09-01..2026-09-05", f"{(TODAY + timedelta(days=1)).isoformat()}..{(TODAY + timedelta(days=3)).isoformat()}"])
def test_counts_are_null_where_nothing_could_be_seen(hub: TestClient, span: str) -> None:
    """Before recording began and on days still to come, "0 meals" or "0 events" would claim what nobody saw."""
    food, calendar, sleep = tab(hub, "food", span), tab(hub, "calendar", span), tab(hub, "sleep", span)
    assert (metric(food, "meals"), metric(food, "days_with_meals")) == (None, None)
    assert all(value is None for line in food["series"]["meals_by_day"]["lines"] for value in line["values"])
    assert (metric(calendar, "planned"), metric(calendar, "events")) == (None, None)
    assert (metric(sleep, "nights"), metric(sleep, "estimated_nights"), metric(sleep, "sleep")) == (None, None, None)


def test_counts_are_zero_on_a_day_that_was_seen(hub: TestClient, demo: Settings) -> None:
    fasting = TODAY - timedelta(days=2)
    start, end = datetime.combine(fasting, datetime.min.time(), tzinfo=TZ), datetime.combine(fasting + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    with Database(demo.database_path).connect() as conn, transaction(conn):  # the devices sent data, but no meal was logged
        conn.execute("DELETE FROM events WHERE kind = 'meal' AND start_utc >= ? AND start_utc < ?", (utc(start), utc(end)))
    body = tab(hub, "food", f"{fasting.isoformat()}..{fasting.isoformat()}")
    assert (metric(body, "meals"), metric(body, "days_with_meals")) == (0, 0)


@pytest.mark.parametrize(("span", "first", "last"), [
    ("today", TODAY, TODAY),
    ("7d", TODAY - timedelta(days=6), TODAY),
    ("92d", TODAY - timedelta(days=91), TODAY),
    ("2026-09-01..2026-09-10", date(2026, 9, 1), date(2026, 9, 10)),
])
def test_ranges(hub: TestClient, span: str, first: date, last: date) -> None:
    body = tab(hub, "apps", span)
    assert (body["range"]["first"], body["range"]["last"]) == (first.isoformat(), last.isoformat())


@pytest.mark.parametrize("span", ["0d", "93d", "2026-09-10..2026-09-01", "2026-02-30..2026-03-01", "last week", "2026-01-01..2026-06-01"])
def test_bad_ranges_are_refused(hub: TestClient, span: str) -> None:
    response = hub.get("/api/v1/insights/overview", params={"range": span, "tz": TZ_NAME})
    assert response.status_code == 400, span


def test_an_unknown_tab_or_an_unpaired_phone_is_refused(hub: TestClient) -> None:
    assert hub.get("/api/v1/insights/money").status_code == 422
    with TestClient(hub.app, client=("192.168.1.40", 50000)) as phone:
        assert phone.get("/api/v1/insights/overview").status_code == 401
        assert phone.get("/api/v1/wrapped").status_code == 401


# --- caching and speed -------------------------------------------------------------------------------------------


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def add_event(settings: Settings, when: datetime, minutes: int = 30, device: str = "seed-android", kind: str = "app_session",
              app: str | None = "Extra", category: str | None = "social", title: str | None = None) -> None:
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, seq, kind, source, start_utc, end_utc, utc_offset_min, app, app_id, title, category, data, received_at)"
            " VALUES (?, ?, NULL, ?, 'seed', ?, ?, -240, ?, ?, ?, ?, '{}', ?)",
            (device, f"content:extra-{device}-{kind}-{when.isoformat()}", kind, utc(when), utc(when + timedelta(minutes=minutes)),
             app, f"app.{app.lower()}" if app else None, title, category, "2026-09-25T20:00:00.000000Z"),
        )


def add_device(settings: Settings, device_id: str, name: str, device_type: str) -> None:
    with Database(settings.database_path).connect() as conn, transaction(conn):
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES (?, ?, ?, ?)",
                     (device_id, name, device_type, utc(datetime(2026, 9, 1, tzinfo=TZ))))


def test_answers_are_cached_until_the_data_changes(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats]) -> None:
    span = f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}"
    first = tab(hub, "overview", span)
    second = tab(hub, "overview", span)
    assert (first["cached"], second["cached"]) == (False, True)
    assert second["metrics"] == first["metrics"]
    add_event(demo, datetime(2026, 9, 20, 10, 0, tzinfo=TZ))  # new data inside the range
    third = tab(hub, "overview", span)
    assert third["cached"] is False
    assert metric(third, "screen_time") == pytest.approx(metric(first, "screen_time") + 30, abs=0.05)


def test_a_range_with_today_is_worked_out_again_after_a_minute(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(insights_api, "monotonic", lambda: moment[0])
    assert tab(hub, "apps", "7d")["cached"] is False
    moment[0] += insights_api.LIVE_CACHE_SECONDS - 1
    assert tab(hub, "apps", "7d")["cached"] is True
    moment[0] += 2
    assert tab(hub, "apps", "7d")["cached"] is False  # today moves on even when no event arrives


def test_a_range_not_begun_is_worked_out_again_after_a_minute(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(insights_api, "monotonic", lambda: moment[0])
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    assert tab(hub, "overview", f"{tomorrow}..{tomorrow}")["cached"] is False
    assert tab(hub, "overview", f"{tomorrow}..{tomorrow}")["cached"] is True
    moment[0] += insights_api.LIVE_CACHE_SECONDS + 1
    assert tab(hub, "overview", f"{tomorrow}..{tomorrow}")["cached"] is False  # after midnight it has begun


def test_a_cache_hit_survives_another_request_evicting_it(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    span = f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}"
    tab(hub, "overview", span)

    class Evicting:
        """The cache's lock, with another request filling the cache (and evicting everything) whenever it is let go."""

        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            insights_api._cache.clear()

    monkeypatch.setattr(insights_api, "_cache_guard", Evicting())
    assert tab(hub, "overview", span)["cached"] is True  # found and used in one hold of the lock


def test_the_data_version_sees_a_seed_run_and_not_last_seen(hub: TestClient, demo: Settings) -> None:
    span = f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}"
    tab(hub, "overview", span)
    database = Database(demo.database_path)
    with database.connect() as conn:
        version, rows = insights_api.data_version(conn), conn.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()
    seed(demo, 14, TZ, NOW)  # from another process, it deletes its events and inserts as many again
    with database.connect() as conn:
        assert tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()) == tuple(rows)  # ids reused
        assert insights_api.data_version(conn) > version
    assert tab(hub, "overview", span)["cached"] is False
    with database.connect() as conn, transaction(conn):
        version = insights_api.data_version(conn)
        conn.execute("UPDATE devices SET last_seen = ?", (utc(NOW),))  # written on every request: changes no number
    with database.connect() as conn:
        assert insights_api.data_version(conn) == version
    assert tab(hub, "overview", span)["cached"] is True
    with database.connect() as conn, transaction(conn):
        conn.execute("UPDATE devices SET name = 'Renamed' WHERE device_id = 'seed-android'")
    assert tab(hub, "overview", span)["cached"] is False


def test_every_tab_answers_quickly_on_two_weeks_of_data(hub: TestClient) -> None:
    for name in TABS:
        started = clock.perf_counter()
        tab(hub, name, "14d")
        elapsed = clock.perf_counter() - started
        assert elapsed < 2.0, f"{name} took {elapsed:.2f} s"  # well under 1 s on a desktop; a slow CI runner gets room


# --- the stats engine's new splits -------------------------------------------------------------------------------


def test_the_crosstab_adds_up_to_the_totals_every_way(demo: Settings, stats_of: Callable[[], Stats]) -> None:
    stats = stats_of()
    total = stats.totals(FIRST, TODAY)["total_seconds"]
    for row, column in [("device", "category"), ("day", "hour"), ("app", "category"), ("hour", "device")]:
        table = stats.crosstab(FIRST, TODAY, row, column)
        assert table["total_seconds"] == total
        assert sum(seconds for cells in table["cells"].values() for seconds in cells.values()) == total, (row, column)
    with pytest.raises(ValueError):
        stats.crosstab(FIRST, TODAY, "app", "app")


def test_the_same_meal_from_two_devices_is_listed_once(demo: Settings, stats_of: Callable[[], Stats]) -> None:
    before = len(stats_of().meals(TODAY))
    with Database(demo.database_path).connect() as conn, transaction(conn):
        meal = conn.execute("SELECT * FROM events WHERE kind = 'meal' AND start_utc >= ? ORDER BY start_utc LIMIT 1",
                            ("2026-09-25T04:00:00",)).fetchone()
        conn.execute(  # the same record, sent by a second phone on the same health account
            "INSERT INTO events (device_id, dedup_key, seq, kind, source, start_utc, end_utc, utc_offset_min, data, received_at)"
            " VALUES ('seed-android', 'content:meal-copy', NULL, 'meal', ?, ?, ?, ?, ?, ?)",
            (meal["source"], meal["start_utc"], meal["end_utc"], meal["utc_offset_min"], meal["data"], meal["received_at"]),
        )
    assert len(stats_of().meals(TODAY)) == before > 0


# --- Wrapped -----------------------------------------------------------------------------------------------------


def good_lines(stats_of: Callable[[], Stats]) -> str:
    stats = stats_of()
    facts = {fact.label: fact.value for fact in week_facts(stats, LAST_WEEK, LAST_WEEK + timedelta(days=6))}
    return "\n".join([
        f"You spent {facts['screen time this week']} minutes on screens this week.",
        f"That is about {facts['average screen time a day']} minutes a day.",
        f"You picked up your phone {facts['phone pickups this week']} times.",
    ])


def test_wrapped_is_last_week_by_default_with_three_checked_lines(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats], fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text(good_lines(stats_of))
    body = hub.get("/api/v1/wrapped", params={"tz": TZ_NAME}).json()
    assert (body["week"], body["first"], body["last"]) == ("2026-W38", "2026-09-14", "2026-09-20")
    assert body["in_progress"] is False and body["fallback"] is False and body["model"]
    assert len(body["lines"]) == 3 and body["lines"][0].startswith("You spent ")
    assert body["top_apps"] and body["metrics"][0]["id"] == "screen_time"
    streaks = {item["id"]: item for item in body["streaks"]}  # DT-53: each streak's days in the week
    assert list(streaks) == ["focus_flame", "screens_down", "logged_it", "balanced", "synced"]
    assert (streaks["focus_flame"]["met"], streaks["focus_flame"]["longest"]) == (5, 4)  # 14, 17 to 20 September
    assert streaks["logged_it"]["met"] == streaks["logged_it"]["days_with_data"] == 7
    again = hub.get("/api/v1/wrapped", params={"tz": TZ_NAME, "week": "2026-W38"}).json()
    assert again["cached"] is True and again["lines"] == body["lines"]
    assert len(fake_llm.chats()) == 1  # the model wrote once


def test_wrapped_lines_with_a_wrong_number_are_retried_then_replaced(hub: TestClient, demo: Settings, stats_of: Callable[[], Stats], fake_llm: FakeModelServer) -> None:
    wrong = "You spent 9999 minutes on screens.\nYou did well.\nKeep it up."
    fake_llm.reply_text(wrong)
    fake_llm.reply_text(wrong)
    body = hub.get("/api/v1/wrapped", params={"tz": TZ_NAME}).json()
    assert body["fallback"] is True and body["model"] is None
    assert "9999" in body["reason"]
    stats = stats_of()
    assert body["lines"] == template_wrapped(week_facts(stats, LAST_WEEK, LAST_WEEK + timedelta(days=6)), LAST_WEEK)
    assert len(fake_llm.chats()) == 2  # asked once more, told what was wrong


def test_the_week_so_far_and_bad_weeks(hub: TestClient, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("One.\nTwo.\nThree.")
    body = hub.get("/api/v1/wrapped", params={"tz": TZ_NAME, "week": "2026-W39"}).json()
    assert body["in_progress"] is True and body["first"] == "2026-09-21"
    for week in ["2026-39", "2026-W60", "2026-W00"]:
        assert hub.get("/api/v1/wrapped", params={"week": week}).status_code == 400, week


def test_a_week_to_come_is_not_in_progress(hub: TestClient, fake_llm: FakeModelServer) -> None:
    body = hub.get("/api/v1/wrapped", params={"tz": TZ_NAME, "week": "2026-W41"}).json()
    assert body["in_progress"] is False and body["fallback"] is True  # as /insights says for days not begun
    assert len(body["lines"]) == WRAPPED_LINES and body["lines"][0].startswith("Nothing was recorded")
    assert fake_llm.chats() == []  # nothing to write about


def test_a_week_is_compared_with_the_one_before_only_when_it_has_enough_days(demo: Settings, stats_of: Callable[[], Stats]) -> None:
    stats = stats_of()
    labels = [fact.label for fact in week_facts(stats, LAST_WEEK, LAST_WEEK + timedelta(days=6))]
    assert not any("week before" in label for label in labels)  # the seed has only 2 days of the week before
    this_week = TODAY - timedelta(days=TODAY.weekday())
    labels = [fact.label for fact in week_facts(stats, this_week, this_week + timedelta(days=6))]
    assert "average screen time a day the week before" in labels  # last week has all 7, this one 4 whole days so far
    assert MIN_DAYS_TO_COMPARE <= 4


def test_a_week_just_begun_is_not_compared_with_the_one_before(demo: Settings) -> None:
    monday = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)  # a few hours of Monday against a whole week
    with Database(demo.database_path).connect() as conn:
        facts = week_facts(Stats(conn, TZ, TZ_NAME, monday), monday.date(), monday.date() + timedelta(days=6))
    assert facts and not any("week before" in fact.label for fact in facts)


def test_the_plain_lines_are_always_three_and_pass_the_number_check(demo: Settings, stats_of: Callable[[], Stats]) -> None:
    days = [LAST_WEEK + timedelta(days=i) for i in range(7)]
    facts = week_facts(stats_of(), LAST_WEEK, LAST_WEEK + timedelta(days=6))
    lines = template_wrapped(facts, LAST_WEEK)
    assert len(lines) == WRAPPED_LINES and wrapped_problems(lines, facts, days) == []
    screen_only = [fact for fact in facts if fact.label.startswith(("screen time this", "average screen", "time in ", "time on "))]
    lines = template_wrapped(screen_only, LAST_WEEK)  # no focus, sleep or pickups: a week of phone videos
    assert len(lines) == WRAPPED_LINES and wrapped_problems(lines, screen_only, days) == []
    shortest = [Fact("screen time this week", 30, "minutes"), Fact("average screen time a day", 30, "minutes")]
    assert len(template_wrapped(shortest, LAST_WEEK)) == WRAPPED_LINES
    assert len(template_wrapped([], LAST_WEEK)) == WRAPPED_LINES


def test_clean_lines_takes_off_numbering_but_not_a_number() -> None:
    reply = "<think>plan</think>\n1. You spent 2.5 hours in Code.\n2) Two.\n- Three\n• Four\n2.5 hours of focus on Tuesday.\n\"Quoted.\""
    assert clean_lines(reply) == ["You spent 2.5 hours in Code.", "Two.", "Three", "Four", "2.5 hours of focus on Tuesday.", "Quoted."]


def test_the_openapi_schema_has_one_meta_model(hub: TestClient) -> None:
    schemas = hub.app.openapi()["components"]["schemas"]
    assert "Meta" in schemas and not any(name.endswith("__Meta") for name in schemas)  # the dashboard's types keep "Meta"


# --- the overview's extras (DT-34) --------------------------------------------------------------------------------


def test_phone_and_computer_add_up_to_each_day(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "overview")
    split = body["series"]["phone_vs_computer"]
    assert split["kind"] == "stacked" and [line["key"] for line in split["lines"]] == ["phone", "computer"]
    stats = stats_of()
    by_day = {item["key"]: item["minutes"] for item in stats.totals(FIRST, TODAY, group_by="day")["items"]}
    for index, label in enumerate(split["x"]):
        parts = [line["values"][index] or 0 for line in split["lines"]]
        assert close(by_day[label], parts, 2), label  # the seed has phones and computers only
    phones = stats.totals(FIRST, TODAY, device_types=frozenset({"android", "ios"}))["total_minutes"]
    assert close(phones, [value or 0 for value in split["lines"][0]["values"]], 14)


def test_the_best_and_toughest_days_say_why(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "overview")
    stats = stats_of()
    days = [FIRST + timedelta(days=i) for i in range(14)]
    focused = [(stats.focused_minutes(day)["value"] or 0, day) for day in days]
    late = [(stats.late_night_minutes(day)["value"] or 0, day) for day in days]
    assert metric(body, "best_day") == max(focused)[1].isoformat() and metric(body, "best_day_focused") == max(focused)[0]
    assert metric(body, "toughest_day") == max(late)[1].isoformat() and metric(body, "toughest_day_late") == max(late)[0]
    reasons = {item["id"]: item["explain"] for item in body["metrics"]}
    assert "most focused time" in reasons["best_day"] and "most screen time after 11 pm" in reasons["toughest_day"]
    # The card shows the minutes as 6h 31m: its reason doesn't repeat them as raw decimals (390.83) beside it.
    assert f"{max(focused)[0]:g}" not in reasons["best_day"] and f"{max(late)[0]:g}" not in reasons["toughest_day"]


def test_changes_compare_whole_days_with_the_days_before(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "overview", "7d")  # 19 to 25 September; the 25th is still going
    changes = {change["id"]: change for change in body["changes"]}
    assert set(changes) == {"daily_average", "focused_time", "focus_score", "pickups", "sleep", "late_night"}
    stats = stats_of()
    now_days = [TODAY - timedelta(days=i) for i in range(1, 7)]  # 19 to 24: whole days only
    before_days = [TODAY - timedelta(days=i) for i in range(7, 14)]  # the 7 days before the range: 12 to 18
    now = sum(stats.focused_minutes(day)["value"] for day in now_days) / 6
    then = sum(stats.focused_minutes(day)["value"] for day in before_days) / 7
    focus = changes["focused_time"]
    assert focus["now"] == pytest.approx(now, abs=0.01) and focus["before"] == pytest.approx(then, abs=0.01)
    assert focus["change_pct"] == round(100 * (now - then) / then) and focus["better"] == "up"
    assert focus["direction"] == ("up" if now > then else "down") and focus["days"] == 6
    assert changes["late_night"]["better"] == "down" and changes["daily_average"]["better"] == "down"
    assert tab(hub, "overview", "today")["changes"] == []  # a day still going is never compared
    single = tab(hub, "overview", f"{(TODAY - timedelta(days=1)).isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}")
    assert {change["days"] for change in single["changes"]} == {1}  # a past day against the one before
    assert tab(hub, "apps")["changes"] == []  # only the overview compares


def detail(client: TestClient, app: str, span: str = "7d") -> dict[str, Any]:
    response = client.get("/api/v1/insights/apps/detail", params={"app": app, "range": span, "tz": TZ_NAME})
    assert response.status_code == 200, response.text
    return response.json()


DASHBOARD_FIXTURES = Path(__file__).resolve().parents[2] / "dashboard" / "e2e" / "fixtures"


def fixture_answers(client: TestClient) -> dict[str, dict[str, Any]]:
    """What the dashboard's e2e tests mock the hub with, by file: the Overview (DT-34), the Apps and Devices tab with
    one app's detail (DT-55), and the Focus and Sleep tab (DT-56), for 14 seeded days."""
    def fresh(body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "cached": False}

    apps = {span: fresh(tab(client, "apps", span)) for span in ("14d", "7d")}
    leader = apps["7d"]["series"]["leaderboard"]["items"][0]["name"]
    return {
        "insights-overview.json": {span: fresh(tab(client, "overview", span)) for span in ("14d", "7d", "today")},
        "insights-apps-devices.json": {
            "apps": apps,
            "devices": {span: fresh(tab(client, "devices", span)) for span in ("14d", "7d")},
            "detail": {"7d": fresh(detail(client, leader))},
        },
        "insights-focus-sleep.json": {
            "focus": {span: fresh(tab(client, "focus", span)) for span in ("14d", "7d")},
            "sleep": {span: fresh(tab(client, "sleep", span)) for span in ("14d", "7d")},
        },
    }


def test_the_dashboards_fixtures_are_the_hubs_answers(hub: TestClient) -> None:
    """The dashboard's e2e tests mock the hub with these files, so they must be what the hub answers now. After
    changing a tab, write them again with DAYTRACE_WRITE_FIXTURES=1."""
    for name, answers in fixture_answers(hub).items():
        path = DASHBOARD_FIXTURES / name
        if os.environ.get("DAYTRACE_WRITE_FIXTURES") == "1":
            path.write_text(json.dumps(answers, indent=1) + "\n", encoding="utf-8", newline="\n")
        assert json.loads(path.read_text(encoding="utf-8")) == answers, f"{name} is stale: run with DAYTRACE_WRITE_FIXTURES=1"


def test_device_lines_say_what_kind_of_device_they_are(hub: TestClient) -> None:
    # The dashboard colors a device by its kind, and two of a kind apart: it needs the kind, not a guess from the id.
    lines = tab(hub, "overview")["series"]["screen_by_device"]["lines"]
    assert {line["key"]: line["device_type"] for line in lines} == {
        "seed-windows": "windows", "seed-mac": "macos", "seed-android": "android", "seed-iphone": "ios"}
    assert all(line["device_type"] is None for line in tab(hub, "overview")["series"]["phone_vs_computer"]["lines"])


def test_a_night_still_going_is_not_compared(hub: TestClient, demo: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # At 00:30 on the 26th the 25th is over, but its night (23:00 to 03:00) is not: it is left out, like a day still
    # going, or the late-night average would drop only because the night isn't finished.
    after_midnight = datetime(2026, 9, 26, 0, 30, tzinfo=TZ)
    monkeypatch.setattr(insights_api, "current_time", lambda: after_midnight.astimezone(UTC))
    insights_api._cache.clear()
    late = {change["id"]: change for change in tab(hub, "overview", "7d")["changes"]}["late_night"]  # 20 to 26
    with Database(demo.database_path).connect() as conn:
        stats = Stats(conn, TZ, TZ_NAME, after_midnight)
        whole = [stats.late_night_minutes(date(2026, 9, day))["value"] for day in range(20, 25)]  # 20 to 24
        still_going = stats.late_night_minutes(date(2026, 9, 25))
    assert still_going["in_progress"] and still_going["value"] is not None
    assert late["now"] == pytest.approx(sum(whole) / len(whole), abs=0.01)


@pytest.mark.parametrize(("span", "compared"), [("2026-09-16..2026-09-22", True), ("2026-09-15..2026-09-21", False)])
def test_a_week_is_compared_only_with_4_days_on_each_side(hub: TestClient, span: str, compared: bool) -> None:
    # The seed begins on the 12th: the days before 16 to 22 have 4 days with data (12 to 15), and before 15 to 21 only
    # 3. Like Wrapped, a week needs 4 on each side.
    assert bool(tab(hub, "overview", span)["changes"]) is compared


def test_a_range_with_no_whole_day_skips_the_days_before(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[RangeInfo] = []
    real = insights_api.TabBuilder.__init__

    def counting(self: Any, stats: Stats, conn: Any, span: RangeInfo) -> None:
        built.append(span)
        real(self, stats, conn, span)

    monkeypatch.setattr(insights_api.TabBuilder, "__init__", counting)
    assert tab(hub, "overview", "today")["changes"] == []
    assert [span.first for span in built] == [TODAY]  # only today's: the day before was never worked out


# --- DT-55: the Apps and Devices tab -----------------------------------------------------------------------------


def test_the_treemap_adds_up_to_the_overview_total(hub: TestClient) -> None:
    for span in ("7d", "14d", "today"):
        treemap = tab(hub, "apps", span)["series"]["treemap"]["items"]
        total = metric(tab(hub, "overview", span), "screen_time")
        assert close(total, [item["value"] for item in treemap], len(treemap)), span
        for category in treemap:  # and each category box is its apps (and its "Other apps")
            assert close(category["value"], [child["value"] for child in category["children"]], len(category["children"]))


def test_the_leaderboard_is_in_order_with_each_apps_week(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    board = tab(hub, "apps", "14d")["series"]["leaderboard"]
    items = board["items"]
    assert board["kind"] == "leaderboard" and len(items) == insights_api.LEADERS
    assert [item["value"] for item in items] == sorted((item["value"] for item in items), reverse=True)
    stats = stats_of()
    top = stats.top_apps(FIRST, TODAY, limit=insights_api.LEADERS)
    assert [(item["name"], item["value"], item["category"]) for item in items] == [(app["app"], app["minutes"], app["category"]) for app in top]
    week = [TODAY - timedelta(days=6 - i) for i in range(7)]  # the 7 days up to the range's last day
    assert board["x"] == [day.isoformat() for day in week]
    for item in items[:3]:
        expected = [stats.totals(day, group_by="app")["items"] for day in week]
        assert item["spark"] == [next((row["minutes"] for row in rows if row["key"] == item["name"]), 0) for rows in expected]


def test_a_leaders_change_is_its_day_average_against_the_days_before(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    items = tab(hub, "apps", "7d")["series"]["leaderboard"]["items"]  # 19 to 25; whole days 19 to 24, before 12 to 18
    stats = stats_of()
    now_days = [TODAY - timedelta(days=i) for i in range(1, 7)]
    before_days = [TODAY - timedelta(days=i) for i in range(7, 14)]

    def average(app: str, days: list[date]) -> float:
        return sum(next((row["seconds"] for row in stats.totals(day, group_by="app")["items"] if row["key"] == app), 0) for day in days) / len(days) / 60

    for item in items[:4]:
        change = item["change"]
        assert change["now"] == pytest.approx(average(item["name"], now_days), abs=0.01)
        assert change["before"] == pytest.approx(average(item["name"], before_days), abs=0.01)
        assert change["better"] == {"social": "down", "video": "down", "games": "down", "work": "up", "study": "up"}.get(item["category"], "neutral")
    assert all(item["change"] is None for item in tab(hub, "apps", "today")["series"]["leaderboard"]["items"])


def test_handoffs_add_up_over_the_range(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    body = tab(hub, "devices", "14d")
    stats = stats_of()
    days = [FIRST + timedelta(days=i) for i in range(14)]
    assert metric(body, "handoffs") == sum(stats.handoffs(day)["value"] for day in days)
    flow = body["series"]["handoffs"]
    assert flow["kind"] == "sankey" and 0 < len(flow["links"]) <= insights_api.HANDOFF_LINKS
    assert [link["value"] for link in flow["links"]] == sorted((link["value"] for link in flow["links"]), reverse=True)
    assert {link["source"] for link in flow["links"]} | {link["target"] for link in flow["links"]} == set(flow["nodes"])
    assert all(link["target"].startswith("then ") and not link["source"].startswith("then ") for link in flow["links"])  # no cycles


def test_the_sync_strip_shows_gaps_as_no_data(hub: TestClient, demo: Settings) -> None:
    # A spare phone paired on 1 September never sends anything: its row is all 0 (no data), and it has no line in
    # the charts, never a 0 there. A seeded phone that sent nothing on the 20th has a 0 on the strip and a gap
    # (null) in its line that day.
    add_device(demo, "android-9", "Spare phone", "android")
    with Database(demo.database_path).connect() as conn, transaction(conn):
        day_start, day_end = datetime(2026, 9, 20, tzinfo=TZ), datetime(2026, 9, 21, tzinfo=TZ)
        conn.execute("DELETE FROM events WHERE device_id = 'seed-iphone' AND start_utc < ? AND (end_utc > ? OR (end_utc IS NULL AND start_utc >= ?))",
                     (utc(day_end), utc(day_start), utc(day_start)))  # its opens and closes have no end
    insights_api._cache.clear()
    body = tab(hub, "devices", "14d")
    strip = body["series"]["sync"]
    assert strip["kind"] == "strip" and strip["x"] == [(FIRST + timedelta(days=i)).isoformat() for i in range(14)]
    rows = {name: {cell["x"]: cell["value"] for cell in strip["cells"] if cell["y"] == y} for y, name in enumerate(strip["y"])}
    assert rows["Spare phone"] == dict.fromkeys(range(14), 0)
    gap = strip["x"].index("2026-09-20")
    assert rows["iPhone (demo)"][gap] == 0 and all(value == 1 for x, value in rows["iPhone (demo)"].items() if x != gap)
    lines = {line["key"]: line["values"] for line in body["series"]["by_day"]["lines"]}
    assert lines["seed-iphone"][gap] is None and "android-9" not in lines
    seen = {item["id"]: item["value"] for item in body["metrics"] if item["id"].startswith("last_seen:")}
    assert set(seen) == {f"last_seen:{device}" for device in ("seed-windows", "seed-mac", "seed-android", "seed-iphone", "android-9")}


def test_one_apps_detail_agrees_with_the_tabs(hub: TestClient) -> None:
    board = tab(hub, "apps", "7d")["series"]["leaderboard"]
    leader = board["items"][0]
    body = detail(hub, leader["name"])
    assert (body["app"], body["category"]) == (leader["name"], leader["category"])
    assert metric(body, "total") == pytest.approx(leader["value"], abs=0.01)
    assert body["series"]["daily"]["lines"][0]["values"] == leader["spark"]  # the 7d range is the sparkline's week
    parts = {name: body["series"][name] for name in ("hours", "devices")}
    assert close(metric(body, "total"), parts["hours"]["lines"][0]["values"], 24)
    assert close(metric(body, "total"), [item["value"] for item in parts["devices"]["items"]], len(parts["devices"]["items"]))
    longest = body["longest"]
    assert longest and 0 < longest["minutes"] <= max(value or 0 for value in leader["spark"])
    assert metric(body, "longest") == longest["minutes"] and metric(body, "days_used") == sum(1 for value in leader["spark"] if value)


def test_an_app_with_no_time_and_days_with_no_data(hub: TestClient) -> None:
    body = detail(hub, "An app nobody uses")
    assert metric(body, "total") == 0 and metric(body, "days_used") == 0 and body["longest"] is None
    assert body["series"]["daily"]["lines"][0]["values"] == [0] * 7  # days with data: 0 for it
    before = detail(hub, "An app nobody uses", "2026-09-01..2026-09-05")  # before recording began: unknown
    assert before["series"]["daily"]["lines"][0]["values"] == [None] * 5 and metric(before, "total") is None
    assert before["series"]["hours"]["lines"][0]["values"] == [None] * 24  # unknown hours too, not 24 zeros
    assert hub.get("/api/v1/insights/apps/detail", params={"app": "", "range": "7d"}).status_code == 422


def test_an_apps_detail_is_cached_until_the_data_changes(hub: TestClient, demo: Settings) -> None:
    span = f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}"
    first, second = detail(hub, "TikTok", span), detail(hub, "TikTok", span)
    assert (first["cached"], second["cached"]) == (False, True)
    add_event(demo, datetime(2026, 9, 22, 13, 0, tzinfo=TZ), minutes=30, app="TikTok")
    third = detail(hub, "TikTok", span)
    assert third["cached"] is False and metric(third, "total") > metric(first, "total")


def test_the_longest_stretch_runs_over_midnight(hub: TestClient, demo: Settings) -> None:
    # 23:00 on the 22nd to 01:30 on the 23rd on one phone: one stretch of 2h 30m, not a day's 1h and the next day's 1h 30m.
    add_event(demo, datetime(2026, 9, 22, 23, 0, tzinfo=TZ), minutes=150, app="Night owl", category="video")
    body = detail(hub, "Night owl")
    assert metric(body, "longest") == 150 and body["longest"]["minutes"] == 150
    assert body["longest"]["start"].startswith("2026-09-23T03:00") and body["longest"]["device_id"] == "seed-android"  # 23:00 Toronto
    assert body["series"]["daily"]["lines"][0]["values"][3:5] == [60, 90]  # each day still has its own part


def test_last_heard_from_is_fresh_on_a_cached_answer(hub: TestClient, demo: Settings) -> None:
    span = f"{FIRST.isoformat()}..{(TODAY - timedelta(days=1)).isoformat()}"  # over, so kept until the data changes
    first = tab(hub, "devices", span)
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute("UPDATE devices SET last_seen = '2026-09-25T20:59:00.000000Z' WHERE device_id = 'seed-android'")
    second = tab(hub, "devices", span)
    assert (first["cached"], second["cached"]) == (False, True)  # contact alone doesn't change the data
    assert metric(first, "last_seen:seed-android") is None and metric(second, "last_seen:seed-android") == "2026-09-25T20:59:00.000000Z"


def test_the_sankey_says_each_nodes_category(hub: TestClient) -> None:
    flow = tab(hub, "devices")["series"]["handoffs"]
    assert len(flow["node_categories"]) == len(flow["nodes"])
    names = {"comms": "Chat and calls"}
    for node, category in zip(flow["nodes"], flow["node_categories"], strict=True):
        assert node.endswith(f" · {names.get(category, category.capitalize())}")
    assert any(item["name"] == "Chat and calls" for item in tab(hub, "apps")["series"]["treemap"]["items"])  # as the dashboard says


def test_the_leaderboard_reads_the_ranges_own_split(hub: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[date, date, str, str]] = []
    real = Stats.crosstab

    def counting(self: Stats, first: date, last: date | None, row: Any, column: Any) -> dict[str, Any]:
        calls.append((first, last or first, row, column))
        return real(self, first, last, row, column)

    monkeypatch.setattr(Stats, "crosstab", counting)
    tab(hub, "apps", "14d")
    by_day = [call for call in calls if call[2:] == ("day", "app") and call[1] == TODAY]
    assert by_day == [(FIRST, TODAY, "day", "app")]  # once, for the range: the sparklines' week is inside it
    calls.clear()
    insights_api._cache.clear()
    tab(hub, "apps", "today")
    assert (TODAY - timedelta(days=6), TODAY, "day", "app") in calls  # a range shorter than the week reads the days before


# --- DT-56: the Focus and Sleep tab ------------------------------------------------------------------------------


def test_distractions_by_hour_add_up_to_the_distracting_categories(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    heat = tab(hub, "focus", "14d")["series"]["distraction_hours"]
    assert heat["x"] == insights_api.HOURS and heat["y"] == insights_api.WEEKDAYS
    stats = stats_of()  # the focus score's distracted time: overlaps once
    distracted = sum(stats.focus_score(FIRST + timedelta(days=i)).get("distracted_seconds", 0) for i in range(14)) / 60
    assert close(distracted, [cell["value"] for cell in heat["cells"]], len(heat["cells"]))
    evenings = sum(cell["value"] for cell in heat["cells"] if cell["x"] >= 21)
    assert evenings > 0  # the seeded late-night scrolling is there


def test_switches_by_day_are_the_stats_engines(hub: TestClient, stats_of: Callable[[], Stats]) -> None:
    line = tab(hub, "focus", "14d")["series"]["switches_by_day"]["lines"][0]["values"]
    stats = stats_of()
    assert line == [stats.switches_per_hour(FIRST + timedelta(days=i))["value"] for i in range(14)]


def test_the_trend_line_is_theil_sen_and_only_with_a_correlation() -> None:
    pairs = [{"late_minutes": x, "focus_score": y} for x, y in ((0, 80), (60, 60), (120, 40))]
    assert insights_api.trend_line(pairs, rho=-1.0) == {"slope": -0.3333, "intercept": 80.0}  # exactly 80 - x / 3
    assert insights_api.trend_line(pairs, rho=None) == {"slope": None, "intercept": None}  # no correlation: no line
    # One odd night (a short late night, then a great day) would turn a least-squares line up; the medians aren't swung.
    odd = [{"late_minutes": x, "focus_score": y} for x, y in ((0, 80), (30, 70), (60, 60), (90, 50), (120, 40), (10, 20))]
    assert insights_api.trend_line(odd, rho=-0.6)["slope"] < 0


def test_a_missing_correlation_says_why(hub: TestClient) -> None:
    few = tab(hub, "focus", f"{(TODAY - timedelta(days=2)).isoformat()}..{TODAY.isoformat()}")["series"]["late_vs_focus"]
    assert few["stats"]["rho"] is None and few["stats"]["n"] < 3 and few["reason"].startswith("It needs 3 nights")
    assert few["stats"]["slope"] is None  # and no line without it
    assert tab(hub, "focus", "14d")["series"]["late_vs_focus"]["reason"] is None


def test_the_focus_lines_have_keys(hub: TestClient) -> None:
    lines = tab(hub, "focus", "14d")["series"]["focus_by_day"]["lines"]
    assert [line["key"] for line in lines] == ["focused", "rest", "distracted"]  # the calendar finds its line by key, not its name


def test_distraction_on_two_devices_at_once_counts_once(hub: TestClient, demo: Settings) -> None:
    span = "2026-09-01..2026-09-02"  # before the seed: only what is added here
    at = datetime(2026, 9, 1, 21, 0, tzinfo=TZ)  # a Tuesday
    add_event(demo, at, minutes=60, device="seed-android", app="YouTube", category="video")
    add_event(demo, at, minutes=60, device="seed-windows", app="YouTube", category="video")
    cells = {(cell["x"], cell["y"]): cell["value"] for cell in tab(hub, "focus", span)["series"]["distraction_hours"]["cells"]}
    assert cells == {(21, 1): 60}


def test_estimated_screen_time_marks_the_focus_charts(hub: TestClient, demo: Settings) -> None:
    # An iPhone app opened on the 22nd and never seen closing: its time is inferred, and so is everything made from it.
    with Database(demo.database_path).connect() as conn, transaction(conn):
        conn.execute(
            "INSERT INTO events (device_id, dedup_key, seq, kind, source, start_utc, end_utc, utc_offset_min, app, app_id, title, category, data, received_at)"
            " VALUES ('seed-iphone', 'content:open-no-close', NULL, 'app_open', 'seed', ?, NULL, -240, 'Instagram', 'app.instagram', NULL, 'social', '{}', ?)",
            (utc(datetime(2026, 9, 22, 10, 0, tzinfo=TZ)), "2026-09-25T20:00:00.000000Z"),
        )
    series = tab(hub, "focus", "14d")["series"]
    assert series["switches_by_day"]["estimated"] and series["score"]["estimated"] and series["late_vs_focus"]["estimated"]



def test_the_late_night_pattern_has_its_line_size_and_caveat(hub: TestClient) -> None:
    body = tab(hub, "focus", "14d")
    scatter = body["series"]["late_vs_focus"]
    stats = scatter["stats"]
    assert stats["n"] == len(scatter["points"]) >= 10 and stats["rho"] < -0.5  # the seeded pattern: later nights, less focus
    assert stats["slope"] < 0 and 0 <= stats["intercept"] <= 100
    assert scatter["note"].startswith("Correlation, not cause")
    explain = body["series"]["score"]["explain"]
    assert "100 × focused time ÷ (work or study time + distracted time)" in explain  # the formula, for the gauge's tooltip
