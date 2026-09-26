"""Tests for DT-15: the seed generator."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from scipy.stats import spearmanr

from daytrace_hub import __main__ as cli
from daytrace_hub.api.timeline import build_timeline, day_window
from daytrace_hub.db import Database
from daytrace_hub.models import Event
from daytrace_hub.seed import DEVICES, SOURCE, generate, plan_days, seed
from daytrace_hub.sessions import Session, sessions_for

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)  # a Friday evening
FOCUS_GOAL_MINUTES = 240  # the goal the streak is designed around (DT-53)


@pytest.fixture
def demo(tmp_path: Path) -> Database:
    database = Database(tmp_path / "daytrace-demo.db")
    seed(database, "demo", 14, TZ, NOW)
    return database


def day_sessions(db: Database, day: date, devices: list[str] | None = None) -> list[Session]:
    start, end = day_window(day, TZ)
    with db.connect() as conn:
        return sessions_for(conn, start, end, devices, now=NOW.astimezone(UTC))


def work_minutes(db: Database, day: date) -> float:
    return sum(s.seconds for s in day_sessions(db, day, ["seed-windows"]) if s.category == "work") / 60


def late_night_minutes(db: Database, day: date) -> float:
    """Phone use from 23:00 on `day` to 03:00 the next morning (the stats engine's late-night window)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ) + timedelta(hours=23)
    with db.connect() as conn:
        sessions = sessions_for(conn, start, start + timedelta(hours=4), ["seed-android"], now=NOW.astimezone(UTC))
    return sum(s.seconds for s in sessions) / 60


# --- the plan -------------------------------------------------------------------------------------------------


def test_the_plan_has_a_five_day_streak_that_breaks_after_a_late_night() -> None:
    plans = plan_days(NOW.date(), 14)
    assert [p.focus for p in plans[5:10]] == ["good"] * 5
    assert plans[10].focus == "poor" and plans[10].late_before == 130
    assert all(p.focus == "good" for p in plans[11:])
    assert [p.late_before for p in plans[1:5]] == [90, 20, 130, 45]
    assert all(plans[i].late_after == plans[i + 1].late_before for i in range(13))


def test_every_event_is_valid_seed_data_and_nothing_is_in_the_future() -> None:
    events = generate(14, TZ, NOW)
    for raw in events:
        event = Event.model_validate(raw)
        assert event.source.value == SOURCE
        assert event.device_id in DEVICES
        assert event.start < NOW
        assert event.end is None or event.end <= NOW


def test_numbered_collectors_get_unique_increasing_seq_and_shortcuts_none() -> None:
    events = generate(14, TZ, NOW)
    for device in ("seed-windows", "seed-mac", "seed-android", "seed-browser"):
        seqs = [e["seq"] for e in events if e["device_id"] == device]
        assert seqs == list(range(len(seqs)))
    assert all("seq" not in e for e in events if e["device_id"] == "seed-iphone")


def test_generation_is_repeatable() -> None:
    assert generate(14, TZ, NOW) == generate(14, TZ, NOW)


def test_today_stops_at_now_and_steps_so_far_are_scaled() -> None:
    morning = NOW.replace(hour=10)
    events = generate(3, TZ, morning)
    assert all(datetime.fromisoformat(e["start"]) < morning for e in events)
    steps_today = next(e for e in events if e["kind"] == "steps" and e["start"].startswith("2026-09-25"))
    steps_yesterday = next(e for e in events if e["kind"] == "steps" and e["start"].startswith("2026-09-24"))
    assert steps_today["data"]["count"] < steps_yesterday["data"]["count"]


def test_day_counts_are_checked() -> None:
    with pytest.raises(ValueError, match="between 1 and 90"):
        generate(0, TZ, NOW)
    with pytest.raises(ValueError, match="between 1 and 90"):
        generate(91, TZ, NOW)


# --- the stored demo data -------------------------------------------------------------------------------------


def test_the_personal_profile_is_never_seeded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="real data"):
        seed(Database(tmp_path / "daytrace-personal.db"), "personal", 14, TZ, NOW)


def test_seeding_again_creates_no_duplicates(demo: Database) -> None:
    with demo.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = seed(demo, "demo", 14, TZ, NOW)
    with demo.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    assert before == after == again.total
    assert devices == len(DEVICES)


def test_seeding_a_day_later_shifts_the_window_without_conflicts(demo: Database) -> None:
    result = seed(demo, "demo", 14, TZ, NOW + timedelta(days=1))
    assert (result.first_day, result.last_day) == (date(2026, 9, 13), date(2026, 9, 26))
    with demo.connect() as conn:
        first = conn.execute("SELECT MIN(start_utc) FROM events WHERE kind = 'app_session'").fetchone()[0]
    assert first >= "2026-09-13"


def test_seeding_keeps_real_events_from_real_devices(demo: Database) -> None:
    from daytrace_hub.api.events import store_events
    from daytrace_hub.auth import register_device
    from daytrace_hub.db import transaction

    with demo.connect() as conn:
        register_device(conn, device_id="android-1", name="Real phone", device_type="android")
        with transaction(conn):
            store_events(conn, "android-1", [(0, Event.model_validate({
                "device_id": "android-1", "seq": 0, "kind": "app_session", "source": "usagestats",
                "start": "2026-09-25T10:00:00-04:00", "end": "2026-09-25T10:05:00-04:00", "app": "Instagram"}))])
    seed(demo, "demo", 14, TZ, NOW)
    with demo.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE device_id = 'android-1'").fetchone()[0] == 1


def test_a_seeded_day_has_four_device_lanes_and_every_lane_type(demo: Database) -> None:
    timeline = build_timeline(demo, date(2026, 9, 24), TZ, "America/Toronto")
    counted = [lane.device_type for lane in timeline.lanes if lane.counted]
    assert counted == ["windows", "macos", "android", "ios"]
    assert any(not lane.counted for lane in timeline.lanes)  # the browser extension's lane
    assert timeline.totals.seconds == sum(lane.seconds for lane in timeline.lanes if lane.counted)
    assert timeline.totals.any_screen_seconds < timeline.totals.seconds  # the phone during the evening show
    assert timeline.calendar and timeline.sleep and timeline.meals
    assert timeline.meta.source == "seed"


def test_every_category_chart_has_data(demo: Database) -> None:
    categories: set[str] = set()
    for offset in range(14):
        categories |= {s.category for s in day_sessions(demo, date(2026, 9, 12) + timedelta(days=offset))
                       if s.category is not None}
    assert categories >= {"work", "study", "social", "video", "comms", "games", "health", "other"}


def test_late_nights_lower_next_day_focus(demo: Database) -> None:
    days = [date(2026, 9, 12) + timedelta(days=offset) for offset in range(14)]
    late = [late_night_minutes(demo, day) for day in days[:-1]]
    focus = [work_minutes(demo, day) for day in days[1:]]
    rho, p_value = spearmanr(late, focus)
    assert rho < -0.6
    assert p_value < 0.05


def test_the_focus_streak_is_five_days_and_breaks_once(demo: Database) -> None:
    met = [work_minutes(demo, date(2026, 9, 12) + timedelta(days=offset)) >= FOCUS_GOAL_MINUTES for offset in range(14)]
    assert met[5:10] == [True] * 5  # 17 to 21 September
    assert met[10] is False  # the morning after a 130-minute night
    assert met[11:] == [True, True, True]


def test_the_study_block_sometimes_has_tiktok_in_it(demo: Database) -> None:
    study = [s for s in day_sessions(demo, date(2026, 9, 22)) if s.app == "TikTok" and s.device_id == "seed-iphone"]
    assert any(15 <= s.start.astimezone(TZ).hour < 17 for s in study)


# --- the command line and the sample script -------------------------------------------------------------------


def test_the_seed_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    assert cli.main(["seed", "--profile", "demo", "--days", "3", "--tz", "America/Toronto"]) == 0
    out = capsys.readouterr().out
    assert "Seeded the demo profile" in out and "seed-windows" in out
    assert (tmp_path / "daytrace-demo.db").exists()


@pytest.mark.parametrize("args", [["--profile", "personal"], ["--days", "0"], ["--tz", "Mars/Olympus"], ["--tz", "America"]])
def test_the_seed_command_refuses_bad_requests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]) -> None:
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    assert cli.main(["seed", *args]) == 2


def test_the_sample_scripts_read_the_token_from_the_environment() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("send-sample-events.ps1", "send-sample-events.sh"):
        text = (scripts / name).read_text(encoding="utf-8")
        assert "DAYTRACE_TOKEN" in text
        assert "Implemented in DT-15" not in text
