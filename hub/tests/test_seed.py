"""Tests for DT-15: the seed generator."""
from __future__ import annotations

import itertools
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from scipy.stats import spearmanr

from daytrace_hub import __main__ as cli
from daytrace_hub.api.timeline import build_timeline, day_window
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database
from daytrace_hub.models import Event
from daytrace_hub.seed import DEVICES, SOURCE, SeedRefused, generate, plan_days, seed
from daytrace_hub.sessions import Session, sessions_for

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=TZ)  # a Friday evening
FOCUS_GOAL_MINUTES = 240  # the goal the streak is designed around (DT-53)
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def demo_settings(tmp_path: Path) -> Settings:
    return Settings(profile=get_profile("demo"), data_dir=tmp_path)


@pytest.fixture
def demo(tmp_path: Path) -> Database:
    settings = demo_settings(tmp_path)
    seed(settings, 14, TZ, NOW)
    return Database(settings.database_path)


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


# --- physically possible data ---------------------------------------------------------------------------------


def impossible(events: list[dict[str, Any]]) -> list[str]:
    """Everything a single person could not have done: two things at once in the hands or at the desk, phone
    use while asleep, desk windows while the desk is AFK, or an invalid event."""
    problems: list[str] = []
    parsed = [Event.model_validate(e) for e in events]
    lanes: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    open_at: dict[str | None, datetime] = {}
    sleep, afk = [], []
    for e in parsed:
        where = f"{e.device_id} {e.kind} {e.app} {e.start.isoformat()}"
        if e.kind == "window":
            lanes["desk"].append((e.start, e.end, where))  # type: ignore[arg-type]
        elif e.kind == "app_session":
            lanes["hand"].append((e.start, e.end, where))  # type: ignore[arg-type]
        elif e.kind == "app_open":
            open_at[e.app] = e.start
        elif e.kind == "app_close":
            lanes["hand"].append((open_at.pop(e.app), e.start, where))
        elif e.kind == "sleep":
            sleep.append((e.start, e.end))
        elif e.kind == "afk":
            afk.append((e.start, e.end))
    for lane, spans in lanes.items():
        spans.sort()
        problems += [f"{lane}: {b[2]} overlaps {a[2]}" for a, b in itertools.pairwise(spans) if b[0] < a[1]]
    problems += [f"asleep but on the phone: {where}" for s, e, where in lanes["hand"]
                 for start, end in sleep if s < end and e > start]
    problems += [f"desk AFK but a window is open: {where}" for s, e, where in lanes["desk"]
                 if "seed-windows" in where for start, end in afk if s < end and e > start]
    return problems


@pytest.mark.parametrize(
    ("zone", "day"),
    [
        ("America/Toronto", "2026-03-10"), ("America/Toronto", "2026-11-03"), ("America/Toronto", "2026-06-20"),
        ("America/St_Johns", "2026-11-02"), ("America/Santiago", "2026-09-09"), ("America/Santiago", "2026-04-07"),
        ("Africa/Cairo", "2026-04-27"), ("Asia/Kolkata", "2026-01-15"), ("Australia/Lord_Howe", "2026-10-06"),
        ("UTC", "2026-02-28"), ("America/Havana", "2026-03-11"), ("Asia/Beirut", "2026-04-01"),
    ],
)
def test_the_data_is_physically_possible_in_every_zone_and_season(zone: str, day: str) -> None:
    tz = ZoneInfo(zone)
    now = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=tz) + timedelta(hours=23, minutes=59)
    assert impossible(generate(14, tz, now)) == []


def test_the_data_is_physically_possible_on_many_days() -> None:
    for offset in range(0, 365, 23):
        now = NOW + timedelta(days=offset)
        assert impossible(generate(14, TZ, now)) == [], now


# --- the plan -------------------------------------------------------------------------------------------------


def test_the_plan_has_a_five_day_streak_that_breaks_after_a_late_night() -> None:
    plans = plan_days(NOW.date(), 14)
    assert [p.focus for p in plans[5:10]] == ["good"] * 5
    assert plans[10].focus == "poor" and plans[10].late_before == 130
    assert all(p.focus == "good" for p in plans[11:])
    assert [p.late_before for p in plans[1:5]] == [90, 20, 130, 45]
    assert all(plans[i].late_after == plans[i + 1].late_before for i in range(13))


def test_every_event_is_valid_seed_data_and_nothing_is_in_the_future() -> None:
    for raw in generate(14, TZ, NOW):
        event = Event.model_validate(raw)
        assert event.source.value == SOURCE
        assert event.device_id in DEVICES
        assert event.start <= NOW
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
    assert all(datetime.fromisoformat(e["start"]) <= morning for e in events)
    steps_today = next(e for e in events if e["kind"] == "steps" and e["start"].startswith("2026-09-25"))
    steps_yesterday = next(e for e in events if e["kind"] == "steps" and e["start"].startswith("2026-09-24"))
    assert steps_today["data"]["count"] < steps_yesterday["data"]["count"]


def test_an_iphone_app_open_at_now_is_closed_at_now(tmp_path: Path) -> None:
    events = generate(14, TZ, NOW + timedelta(days=30))
    pair = next((o, c) for o, c in itertools.pairwise(events)
                if o["kind"] == "app_open" and c["kind"] == "app_close" and o["device_id"] == "seed-iphone")
    middle = datetime.fromisoformat(pair[0]["start"]) + (datetime.fromisoformat(pair[1]["start"])
                                                          - datetime.fromisoformat(pair[0]["start"])) / 2
    cut = generate(14, TZ, middle)
    closes = [e for e in cut if e["kind"] == "app_close" and datetime.fromisoformat(e["start"]) == middle]
    assert len(closes) == 1  # moved to now, so today's seed data has no unclosed (estimated) session


def test_day_counts_are_checked() -> None:
    with pytest.raises(SeedRefused, match="between 1 and 90"):
        generate(0, TZ, NOW)
    with pytest.raises(SeedRefused, match="between 1 and 90"):
        generate(91, TZ, NOW)


# --- the stored demo data -------------------------------------------------------------------------------------


def test_profiles_with_real_data_are_never_seeded(tmp_path: Path) -> None:
    personal = Settings(profile=get_profile("personal"), data_dir=tmp_path)
    with pytest.raises(SeedRefused, match="real data"):
        seed(personal, 14, TZ, NOW)
    assert not personal.database_path.exists()


def test_seeding_again_creates_no_duplicates(tmp_path: Path, demo: Database) -> None:
    with demo.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = seed(demo_settings(tmp_path), 14, TZ, NOW)
    with demo.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    assert before == after == again.total
    assert devices == len(DEVICES)


def test_seeding_a_day_later_shifts_the_window_without_conflicts(tmp_path: Path, demo: Database) -> None:
    result = seed(demo_settings(tmp_path), 14, TZ, NOW + timedelta(days=1))
    assert (result.first_day, result.last_day) == (date(2026, 9, 13), date(2026, 9, 26))
    with demo.connect() as conn:
        first_use = conn.execute("SELECT MIN(start_utc) FROM events WHERE kind != 'sleep'").fetchone()[0]
        first_sleep = conn.execute("SELECT MIN(start_utc) FROM events WHERE kind = 'sleep'").fetchone()[0]
    assert first_use >= "2026-09-13"
    assert first_sleep >= "2026-09-12T"  # the first night's sleep starts the evening before the first day


def test_seeding_keeps_real_events_from_real_devices(tmp_path: Path, demo: Database) -> None:
    from daytrace_hub.api.events import store_events
    from daytrace_hub.auth import register_device
    from daytrace_hub.db import transaction

    with demo.connect() as conn:
        register_device(conn, device_id="android-1", name="Real phone", device_type="android")
        with transaction(conn):
            store_events(conn, "android-1", [(0, Event.model_validate({
                "device_id": "android-1", "seq": 0, "kind": "app_session", "source": "usagestats",
                "start": "2026-09-25T10:00:00-04:00", "end": "2026-09-25T10:05:00-04:00", "app": "Instagram"}))])
    seed(demo_settings(tmp_path), 14, TZ, NOW)
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
    assert timeline.meta.estimated is False


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


# --- the command line and the sample scripts ------------------------------------------------------------------


def test_the_seed_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    assert cli.main(["seed", "--profile", "demo", "--days", "3", "--tz", "America/Toronto"]) == 0
    out = capsys.readouterr().out
    assert "Seeded the demo profile" in out and "seed-windows" in out
    assert (tmp_path / "daytrace-demo.db").exists()


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (["--profile", "personal"], {}, "real data"),
        (["--days", "0"], {}, "between 1 and 90"),
        (["--tz", "Mars/Olympus"], {}, "unknown time zone"),
        (["--tz", "America"], {}, "unknown time zone"),
        ([], {"DAYTRACE_MDNS": "maybe"}, "DAYTRACE_MDNS"),
        ([], {"DAYTRACE_DATA_DIR": "relative"}, "absolute path"),
    ],
)
def test_the_seed_command_explains_what_went_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, args: list[str], env: dict[str, str], message: str
) -> None:
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["seed", *args]) == 2
    assert message in capsys.readouterr().err


def test_the_seed_command_reports_a_busy_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    from daytrace_hub import db as db_module

    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db_module, "BUSY_TIMEOUT_MS", 200)
    Database(tmp_path / "daytrace-demo.db").initialize()
    holder = sqlite3.connect(tmp_path / "daytrace-demo.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # the demo hub (or another seed) is writing
    try:
        assert cli.main(["seed", "--days", "2", "--tz", "UTC"]) == 2
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "busy" in capsys.readouterr().err


def test_the_sample_scripts_keep_the_token_off_command_lines_and_fail_on_rejections() -> None:
    ps1 = (SCRIPTS / "send-sample-events.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")  # .editorconfig: Windows PowerShell 5.1 needs the BOM
    for name in ("send-sample-events.ps1", "send-sample-events.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8-sig")
        assert "DAYTRACE_TOKEN" in text
        assert "rejected" in text  # a non-zero exit when the hub refuses the events
    sh = (SCRIPTS / "send-sample-events.sh").read_text(encoding="utf-8")
    assert "-H @-" in sh and "--fail-with-body" not in sh and "python3" not in sh
