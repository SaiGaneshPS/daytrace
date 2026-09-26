"""Tests for DT-38: the stats engine. Hand-computed answers on a small day, and consistency on seeded data."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.api.events import store_events
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event
from daytrace_hub.seed import seed
from daytrace_hub.stats import Stats

TORONTO = "America/Toronto"
ZONE = ZoneInfo(TORONTO)
DAY = date(2026, 9, 25)
NEXT = date(2026, 9, 26)
LATER = datetime(2030, 1, 1, tzinfo=UTC)


def add(db: Database, device_id: str, device_type: str, events: list[dict[str, Any]]) -> None:
    """Pair a device (once) and store its events through the real ingest path."""
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
            register_device(conn, device_id=device_id, name=device_id, device_type=device_type)
        with transaction(conn):
            store_events(conn, device_id, [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)])


def at(clock: str, day: date = DAY) -> str:
    return f"{day.isoformat()}T{clock}-04:00"  # Toronto in September


def span(kind: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "source": extra.pop("source", "tracker"), "start": start, "end": end, **extra}


@pytest.fixture
def day(db: Database) -> Database:
    """One hand-made day (all times Toronto). Every expected number in the tests below is worked out from it.

    Desk (windows-1): VS Code 09:00-09:25, Edge 09:25-09:40 (the extension saw youtube.com 09:25-09:30 and
    docs.google.com 09:30-09:40), VS Code 09:40-10:00, Minecraft 22:30-23:30.
    Phone (android-1): Instagram 09:50-09:52, WhatsApp 12:00-12:05 and 12:05:30-12:06, Instagram 23:10-23:40.
    Calendar: "Study: stats" 09:00-10:00. A paired iPhone sends nothing.
    """
    code = {"app": "Visual Studio Code", "app_id": "Code.exe"}
    add(db, "windows-1", "windows", [
        span("window", at("09:00:00"), at("09:25:00"), title="stats.py", **code),
        span("window", at("09:25:00"), at("09:40:00"), app="Microsoft Edge", app_id="msedge.exe"),
        span("window", at("09:40:00"), at("10:00:00"), title="stats.py", **code),
        span("window", at("22:30:00"), at("23:30:00"), app="Minecraft", app_id="Minecraft.exe"),
    ])
    add(db, "browser-1", "browser", [
        span("web", at("09:25:00"), at("09:30:00"), source="browser", app_id="msedge.exe", data={"domain": "youtube.com"}, seq=1),
        span("web", at("09:30:00"), at("09:40:00"), source="browser", app_id="msedge.exe", data={"domain": "docs.google.com"}, seq=2),
    ])
    phone = {"source": "usagestats"}
    add(db, "android-1", "android", [
        span("app_session", at("09:50:00"), at("09:52:00"), app="Instagram", app_id="com.instagram.android", seq=1, **phone),
        span("app_session", at("12:00:00"), at("12:05:00"), app="WhatsApp", app_id="com.whatsapp", seq=2, **phone),
        span("app_session", at("12:05:30"), at("12:06:00"), app="WhatsApp", app_id="com.whatsapp", seq=3, **phone),
        span("app_session", at("23:10:00"), at("23:40:00"), app="Instagram", app_id="com.instagram.android", seq=4, **phone),
        span("calendar_event", at("09:00:00"), at("10:00:00"), source="calendar", title="Study: stats", seq=5, data={"all_day": False}),
    ])
    with db.connect() as conn:
        register_device(conn, device_id="iphone-1", name="iPhone", device_type="ios")
    return db


@contextmanager
def stats(db: Database, now: datetime = LATER) -> Iterator[Stats]:
    with db.connect() as conn:
        yield Stats(conn, ZONE, TORONTO, now=now)


# --- totals -------------------------------------------------------------------------------------------------------

# windows-1: 25 + 15 + 20 + 60 = 120 min; android-1: 2 + 5 + 0.5 + 30 = 37.5 min; the browser lane is not counted.
TOTAL = 9450


def items(result: dict[str, Any]) -> dict[str, int]:
    return {item["key"]: item["seconds"] for item in result["items"]}


def test_totals_by_app_credit_browser_time_to_sites(day: Database) -> None:
    with stats(day) as s:
        result = s.totals(DAY, group_by="app")
    assert items(result) == {
        "Minecraft": 3600, "Visual Studio Code": 2700, "Instagram": 1920, "docs.google.com": 600, "WhatsApp": 330,
        "youtube.com": 300,
    }  # all 15 minutes of Edge went to the two sites, so Edge itself has none left
    assert result["total_seconds"] == TOTAL
    assert result["items"][0]["key"] == "Minecraft"  # largest first


def test_every_grouping_adds_up_to_the_same_total(day: Database) -> None:
    with stats(day) as s:
        by = {group: s.totals(DAY, group_by=group) for group in ("app", "category", "device", "hour", "day")}
    assert items(by["category"]) == {"games": 3600, "work": 3300, "social": 1920, "comms": 330, "video": 300}
    assert items(by["device"]) == {"windows-1": 7200, "android-1": 2250}
    assert items(by["hour"]) == {"09": 3720, "12": 330, "22": 1800, "23": 3600}
    assert items(by["day"]) == {"2026-09-25": TOTAL}
    for result in by.values():
        assert sum(item["seconds"] for item in result["items"]) == result["total_seconds"] == TOTAL
    assert list(items(by["hour"])) == ["09", "12", "22", "23"]  # hours and days in order, not by size


def test_days_and_devices_without_data_are_missing_not_zero(day: Database) -> None:
    with stats(day) as s:
        result = s.totals(DAY, NEXT, group_by="day")
    assert items(result) == {"2026-09-25": TOTAL}  # no "2026-09-26": 0
    assert result["missing_days"] == ["2026-09-26"]
    assert result["missing"] == {
        "iphone-1": ["2026-09-25", "2026-09-26"],  # paired, never sent anything
        "windows-1": ["2026-09-26"],
        "android-1": ["2026-09-26"],
    }


def test_results_carry_the_documented_meta(day: Database) -> None:
    with stats(day) as s:
        result = s.totals(DAY)
    assert result["unit"] == "minutes"
    assert result["range"] == {"start": "2026-09-25T00:00:00-04:00", "end": "2026-09-26T00:00:00-04:00", "tz": TORONTO}
    assert result["source"] == "real"
    assert result["estimated"] is False


def test_nothing_after_now_is_counted(day: Database) -> None:
    with stats(day, now=datetime(2026, 9, 25, 9, 10, tzinfo=ZONE)) as s:
        assert s.totals(DAY)["total_seconds"] == 600  # ten minutes of VS Code so far


def test_ranges_are_checked(day: Database) -> None:
    with stats(day) as s:
        with pytest.raises(ValueError, match="group_by"):
            s.totals(DAY, group_by="colour")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="before"):
            s.totals(NEXT, DAY)
        with pytest.raises(ValueError, match="at most"):
            s.totals(DAY, DAY + timedelta(days=400))


# --- focus ---------------------------------------------------------------------------------------------------------


def test_focused_minutes(day: Database) -> None:
    # Work: 09:00-09:25 and 09:30-10:00 (Docs then VS Code). Instagram on the phone 09:50-09:52 cuts the second
    # stretch into 09:30-09:50 (20 min, counts) and 09:52-10:00 (8 min, too short). 25 + 20 = 45 minutes.
    with stats(day) as s:
        result = s.focused_minutes(DAY)
    assert result["value"] == 45.0
    assert result["seconds"] == 2700
    assert [(b["start"][11:16], b["end"][11:16], b["minutes"]) for b in result["blocks"]] == [
        ("09:00", "09:25", 25.0), ("09:30", "09:50", 20.0),
    ]


def test_focus_score_follows_its_documented_formula(day: Database) -> None:
    # focused 45 min; work or study 55 min (09:00-09:25, 09:30-10:00); distracted 77 min: YouTube 5, Instagram 2,
    # and Minecraft 22:30-23:30 with Instagram 23:10-23:40 on top (70 once). round(100 x 45 / (55 + 77)) = 34.
    with stats(day) as s:
        result = s.focus_score(DAY)
    assert result["value"] == 34
    assert (result["focused_seconds"], result["work_or_study_seconds"], result["distracted_seconds"]) == (2700, 3300, 4620)
    assert result["unit"] == "score"
    assert result["formula"] == "round(100 x focused / (work_or_study + distracted))"


def test_a_day_without_data_has_no_focus_rather_than_zero(day: Database) -> None:
    with stats(day) as s:
        assert s.focused_minutes(NEXT)["value"] is None and s.focused_minutes(NEXT)["missing"] is True
        assert s.focus_score(NEXT)["value"] is None


def test_a_day_with_neither_work_nor_distraction_has_no_score(db: Database) -> None:
    add(db, "android-1", "android", [span("app_session", at("12:00:00"), at("12:30:00"), app="WhatsApp",
                                          app_id="com.whatsapp", source="usagestats", seq=1)])
    with stats(db) as s:
        result = s.focus_score(DAY)
    assert result["value"] is None and result["missing"] is False
    assert result["reason"] == "no work, study or distraction time this day"


def test_switches_per_hour(day: Database) -> None:
    # Desk: VS Code -> youtube.com -> docs.google.com -> VS Code (3 switches in the 09 hour); the phone's two
    # WhatsApp sessions are the same app, and everything else is more than 5 minutes apart. Screen time with
    # overlaps counted once: 120 min desk + 5.5 min WhatsApp + 10 min Instagram after Minecraft = 135.5 min.
    with stats(day) as s:
        result = s.switches_per_hour(DAY)
    assert result["switches"] == 3
    assert result["by_hour"] == {"09": 3}
    assert result["active_seconds"] == 8130
    assert result["value"] == round(3 / (8130 / 3600), 1) == 1.3


def test_pickups(day: Database) -> None:
    # Instagram at 09:50, WhatsApp at 12:00 (the 12:05:30 session follows a 30 s rest: the same pickup), and
    # Instagram at 23:10. The paired iPhone sent nothing: missing, not zero.
    with stats(day) as s:
        result = s.pickups(DAY)
    assert result["value"] == 3
    assert result["by_device"] == {"android-1": 3}
    assert result["missing_devices"] == ["iphone-1"]


def test_late_night_minutes(day: Database) -> None:
    # 23:00-03:00: Minecraft 23:00-23:30 and Instagram 23:10-23:40, overlapping: 23:00-23:40 once = 40 minutes.
    with stats(day) as s:
        result = s.late_night_minutes(DAY)
    assert result["value"] == 40.0
    assert result["by_device"] == {"windows-1": 1800, "android-1": 1800}
    assert result["range"]["start"] == "2026-09-25T23:00:00-04:00"
    assert result["range"]["end"] == "2026-09-26T03:00:00-04:00"


def test_sleep_is_estimated_from_the_longest_idle_stretch(day: Database) -> None:
    # 21:00 to noon: the screens were busy 22:30-23:40, so the longest quiet stretch is 23:40 to 12:00.
    with stats(day) as s:
        result = s.sleep_estimate(NEXT)
    assert (result["method"], result["measured"], result["estimated"]) == ("idle_gap", False, True)
    assert (result["start"], result["end"]) == ("2026-09-25T23:40:00-04:00", "2026-09-26T12:00:00-04:00")
    assert result["value"] == 740.0


def test_measured_sleep_wins_and_is_counted_once(day: Database) -> None:
    night = {"source": "health_connect", "data": {"stage": "asleep", "measured": True}}
    add(day, "android-1", "android", [
        span("sleep", at("23:45:00"), at("07:15:00", NEXT), external_id="sleep:1", **night),
        span("sleep", at("23:45:00"), at("07:15:00", NEXT), external_id="sleep:1-copy", **night),  # synced twice
        span("sleep", at("03:00:00", NEXT), at("03:20:00", NEXT), external_id="awake:1", source="health_connect",
             data={"stage": "awake"}),  # awake in the night is not sleep
    ])
    with stats(day) as s:
        result = s.sleep_estimate(NEXT)
    assert (result["method"], result["measured"], result["estimated"]) == ("health", True, False)
    assert result["value"] == 450.0  # 23:45 to 07:15


def test_no_sleep_is_invented_for_a_night_without_data(day: Database) -> None:
    with stats(day) as s:
        assert s.sleep_estimate(date(2026, 9, 28))["value"] is None


def test_planned_vs_actual(day: Database) -> None:
    # "Study: stats" 09:00-10:00: on plan 25 (VS Code) + 10 (Docs) + 10 + 8 (VS Code) = 53 min; off plan 5
    # (YouTube) + 2 (Instagram on the phone while VS Code was open: the distraction wins) = 7 min. 53 + 7 = 60.
    with stats(day) as s:
        result = s.planned_vs_actual(DAY)
    block = result["blocks"][0]
    assert block["title"] == "Study: stats"
    assert (block["planned_seconds"], block["on_plan_seconds"], block["off_plan_seconds"], block["other_seconds"],
            block["idle_seconds"]) == (3600, 3180, 420, 0, 0)
    assert block["on_plan_pct"] == 88 and result["value"] == 88
    assert result["totals_minutes"]["planned"] == 60.0


def test_a_block_still_running_counts_up_to_now(day: Database) -> None:
    with stats(day, now=datetime(2026, 9, 25, 9, 30, tzinfo=ZONE)) as s:
        block = s.planned_vs_actual(DAY)["blocks"][0]
    assert block["in_progress"] is True
    assert (block["planned_seconds"], block["on_plan_seconds"], block["off_plan_seconds"]) == (1800, 1500, 300)


# --- correlation ---------------------------------------------------------------------------------------------------


def test_correlation_needs_three_pairs(day: Database) -> None:
    with stats(day) as s:
        result = s.correlation(DAY, NEXT)
    assert (result["rho"], result["p"], result["n"]) == (None, None, 0)  # the next day has no data at all
    assert result["caveat"].startswith("Correlation, not cause")


# --- on seeded data --------------------------------------------------------------------------------------------------


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Database, datetime, list[date]]:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    now = datetime(2026, 9, 26, 20, 0, tzinfo=ZONE)
    database = Database(settings.database_path)
    database.initialize()
    seed(settings, 14, ZONE, now)
    monkeypatch.setattr(timeline_api, "current_time", lambda: now)
    days = [now.date() - timedelta(days=i) for i in range(13, -1, -1)]
    return database, now, days


def test_seeded_totals_agree_with_the_timeline_and_with_each_other(seeded: tuple[Database, datetime, list[date]]) -> None:
    database, now, days = seeded
    with stats(database, now) as s:
        by = {group: s.totals(days[0], days[-1], group_by=group) for group in ("app", "category", "device", "hour", "day")}
        timeline_total = sum(timeline_api.build_timeline(database, d, ZONE, TORONTO).totals.seconds for d in days)
    totals = {group: result["total_seconds"] for group, result in by.items()}
    assert set(totals.values()) == {timeline_total}
    for result in by.values():
        assert sum(item["seconds"] for item in result["items"]) == timeline_total
    assert by["app"]["source"] == "seed"
    assert "seed-browser" not in items(by["device"])  # detail only, never counted twice
    assert "Microsoft Edge" not in items(by["app"]) or items(by["app"])["Microsoft Edge"] < items(by["app"])["youtube.com"]


def test_seeded_late_nights_go_with_less_focus(seeded: tuple[Database, datetime, list[date]]) -> None:
    database, now, days = seeded
    with stats(database, now) as s:
        result = s.correlation(days[0], days[-1])
    assert result["n"] >= 10
    assert result["rho"] is not None and result["rho"] < -0.5  # the pattern the seed data was built with
    assert result["p"] is not None and result["p"] < 0.05


def test_seeded_days_have_every_stat(seeded: tuple[Database, datetime, list[date]]) -> None:
    database, now, days = seeded
    yesterday = days[-2]
    with stats(database, now) as s:
        assert s.focused_minutes(yesterday)["value"] >= 0
        assert 0 <= s.focus_score(yesterday)["value"] <= 100
        assert s.pickups(yesterday)["value"] > 0
        assert s.sleep_estimate(yesterday)["method"] == "health"  # the seed phone syncs measured sleep
        assert s.switches_per_hour(yesterday)["value"] > 0
        assert s.planned_vs_actual(yesterday)["totals_seconds"]["planned"] >= 0
