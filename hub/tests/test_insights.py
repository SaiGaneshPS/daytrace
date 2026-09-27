"""Tests for DT-41: the Insights tabs (GET /insights/{tab}) and the week's Wrapped (GET /wrapped), on 14 seeded days.

The claims: every tab answers with chart-ready series whose numbers add up to the tab's totals and agree with the
stats engine (so with Today and every other page), missing days are null rather than zero, ranges are checked, the
answers are cached until the data changes, and each tab answers in well under a second. Wrapped's lines are the
local model's, every number checked against the week's facts, or plain ones when the model can't be used.
"""
from __future__ import annotations

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
        "overview": {"screen_by_device": "stacked", "categories": "donut", "focus_by_day": "trend", "hours": "heatmap"},
        "apps": {"top_apps": "bars", "treemap": "treemap", "by_category": "stacked", "switches": "trend"},
        "devices": {"share": "donut", "by_day": "stacked", "hours": "heatmap", "flow": "sankey"},
        "focus": {"score": "gauge", "focus_by_day": "stacked", "switches_by_hour": "bars", "late_vs_focus": "scatter"},
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
    assert body["streaks"] == []  # DT-53 fills these
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
