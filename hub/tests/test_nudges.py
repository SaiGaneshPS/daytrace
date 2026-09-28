"""Tests for DT-43: the nudge rules, their cooldown and choices, the nudge in the ingest response, the desktop
tracker's own nudges, and the desktop notifications (never a real one: conftest refuses notify._run).

The clock is pinned (api.timeline.current_time) and the hub's zone is Toronto; 25 September 2026 is a Friday."""

from __future__ import annotations

import base64
import math
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import notify, nudges, streaks
from daytrace_hub.api import timeline as timeline_api
from daytrace_hub.auth import register_device
from daytrace_hub.db import Database, transaction
from daytrace_hub.story import duration
from daytrace_hub.tracker.base import DatabaseSink

TZ = ZoneInfo("America/Toronto")
TZ_NAME = "America/Toronto"
FRIDAY = datetime(2026, 9, 25, tzinfo=TZ)


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return (FRIDAY + timedelta(days=days)).replace(hour=hour, minute=minute)


class Clock:
    def __init__(self) -> None:
        self.now = at(12)

    def set(self, moment: datetime) -> None:
        self.now = moment


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    found = Clock()
    monkeypatch.setattr(timeline_api, "current_time", lambda: found.now.astimezone(UTC))
    monkeypatch.setattr(nudges, "hub_zone", lambda: (TZ, TZ_NAME))
    nudges._evaluations.clear()
    yield found
    nudges._evaluations.clear()


@pytest.fixture
def tokens(db: Database) -> dict[str, str]:
    with db.connect() as conn:
        return {
            "android-1": register_device(conn, device_id="android-1", name="Galaxy phone", device_type="android"),
            "iphone-1": register_device(conn, device_id="iphone-1", name="iPhone", device_type="ios"),
            "windows-1": register_device(conn, device_id="windows-1", name="Desktop", device_type="windows"),
        }


_seq: dict[str, int] = {}


def send(client: TestClient, tokens: dict[str, str], device: str, *events: dict[str, Any]) -> dict[str, Any]:
    body = []
    for event in events:
        _seq[device] = _seq.get(device, 0) + 1
        body.append({"device_id": device, "seq": _seq[device], **event})
    response = client.post("/api/v1/events", json={"events": body}, headers={"Authorization": f"Bearer {tokens[device]}"})
    assert response.status_code == 200, response.text
    assert response.json()["rejected"] == [], response.json()["rejected"]
    return response.json()


def app(name: str, app_id: str, start: datetime, end: datetime, kind: str = "app_session") -> dict[str, Any]:
    source = "tracker" if kind == "window" else "usagestats"
    return {"kind": kind, "source": source, "start": start.isoformat(), "end": end.isoformat(), "app": name, "app_id": app_id}


def tiktok(start: datetime, end: datetime) -> dict[str, Any]:
    return app("TikTok", "com.zhiliaoapp.musically", start, end)


def instagram(start: datetime, end: datetime) -> dict[str, Any]:
    return app("Instagram", "com.instagram.android", start, end)


def code(start: datetime, end: datetime) -> dict[str, Any]:
    return app("Visual Studio Code", "Code.exe", start, end, kind="window")


def event(title: str, start: datetime, end: datetime, all_day: bool = False) -> dict[str, Any]:
    return {"kind": "calendar_event", "source": "calendar", "external_id": f"cal:{'_'.join(title.split())[:60]}:{start.isoformat()}",
            "start": start.isoformat(), "end": end.isoformat(), "title": title, "data": {"all_day": True} if all_day else {}}


def logged(db: Database) -> list[tuple[str, str | None]]:
    with db.connect() as conn:
        return [(row["rule"], row["device_id"]) for row in conn.execute("SELECT rule, device_id FROM nudge_log ORDER BY id")]


# --- R1: focus blocks, the cooldown -------------------------------------------------------------------------------


def test_tiktok_during_a_study_event_gets_a_nudge_and_the_cooldown_holds(
    client: TestClient, tokens: dict[str, str], db: Database, clock: Clock
) -> None:
    clock.set(at(15, 30))
    send(client, tokens, "android-1", event("Study: calculus", at(15), at(17)))
    first = send(client, tokens, "android-1", tiktok(at(15, 25), at(15, 30)))["nudge"]
    assert first is not None
    assert (first["rule"], first["title"]) == ("focus_block", "Time to focus")
    assert first["body"] == 'TikTok during "Study: calculus", which runs until 17:00.'
    assert datetime.fromisoformat(first["created_at"]) == at(15, 30)

    clock.set(at(15, 45))  # 15 minutes on, from either phone: resting
    assert send(client, tokens, "android-1", tiktok(at(15, 40), at(15, 45)))["nudge"] is None
    assert send(client, tokens, "iphone-1", tiktok(at(15, 44), at(15, 45)))["nudge"] is None
    clock.set(at(15, 51))  # 21 minutes on: again
    again = send(client, tokens, "iphone-1", tiktok(at(15, 46), at(15, 51)))["nudge"]
    assert again is not None and again["rule"] == "focus_block"
    assert logged(db) == [("focus_block", "android-1"), ("focus_block", "iphone-1")]  # every nudge is logged


@pytest.mark.parametrize(("title", "all_day", "app_event", "nudged"), [
    ("Deep work", False, "tiktok", True),
    ("Exam revision", False, "tiktok", True),
    ("Homework", False, "tiktok", True),
    ("Study", False, "code", False),  # focused, not distracted
    ("Lunch with Sam", False, "tiktok", False),  # not a focus event
    ("Workout", False, "tiktok", False),  # "work" only as a whole word
    ("Exam week", True, "tiktok", False),  # an all-day event is a label, not a block of time
])
def test_only_a_distracting_app_in_a_focus_event_nudges(
    client: TestClient, tokens: dict[str, str], clock: Clock, title: str, all_day: bool, app_event: str, nudged: bool
) -> None:
    clock.set(at(10, 30))
    send(client, tokens, "android-1", event(title, at(10), at(12), all_day=all_day))
    activity = tiktok(at(10, 25), at(10, 30)) if app_event == "tiktok" else code(at(10, 10), at(10, 30))
    device = "android-1" if app_event == "tiktok" else "windows-1"
    found = send(client, tokens, device, activity)["nudge"]
    assert (found is not None and found["rule"] == "focus_block") is nudged


def test_activity_that_is_not_happening_now_nudges_no_one(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(16, 30))
    send(client, tokens, "android-1", event("Study", at(15), at(17)))
    # A phone catching up: TikTok that ended 20 minutes ago, and yesterday's.
    assert send(client, tokens, "android-1", tiktok(at(16), at(16, 10)), tiktok(at(15), at(15, 30)))["nudge"] is None
    assert send(client, tokens, "android-1", tiktok(at(15, 10, days=-1), at(15, 20, days=-1)))["nudge"] is None
    assert logged(db) == []


def test_an_app_known_by_its_id_alone_still_nudges(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(10, 30))
    send(client, tokens, "android-1", event("Study", at(10), at(12)))
    by_id = {"kind": "app_session", "source": "usagestats", "start": at(10, 25).isoformat(), "end": at(10, 30).isoformat(),
             "app_id": "com.zhiliaoapp.musically"}
    found = send(client, tokens, "android-1", by_id)["nudge"]
    assert found is not None and found["body"].startswith('com.zhiliaoapp.musically during "Study"')


def test_a_long_name_never_cuts_what_the_nudge_says(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(10, 30))
    send(client, tokens, "android-1", event("Study " + "very " * 40 + "long", at(10), at(12)))
    long_app = app("Tik" * 60, "com.zhiliaoapp.musically", at(10, 25), at(10, 30))
    found = send(client, tokens, "android-1", long_app)["nudge"]
    assert found is not None and len(found["body"]) <= 240
    assert found["body"].endswith("..., which runs until 12:00.") or found["body"].endswith('...", which runs until 12:00.')
    assert found["body"].startswith("Tik" * 12)


def test_one_nudge_at_a_time_whatever_the_rule(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(23, 40))
    send(client, tokens, "android-1", instagram(at(21), at(22, 30)))  # 90 minutes: over the social goal too
    clock.set(at(23, 45))
    assert send(client, tokens, "android-1", instagram(at(23, 40), at(23, 45)))["nudge"]["rule"] == "late_scroll"
    clock.set(at(23, 45).replace(second=4))  # the next request, 4 seconds on
    assert send(client, tokens, "android-1", instagram(at(23, 45), at(23, 45).replace(second=4)))["nudge"] is None
    clock.set(at(23, 51))  # 6 minutes on: the social limit may speak (late_scroll still rests)
    found = send(client, tokens, "android-1", instagram(at(23, 46), at(23, 51)))["nudge"]
    assert found is not None and found["rule"] == "social_cap"
    assert [rule for rule, _ in logged(db)] == ["late_scroll", "social_cap"]


def test_a_goal_changed_this_minute_is_the_one_used(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(10, 30)))  # 90 minutes
    with db.connect() as conn, transaction(conn):
        conn.execute("INSERT INTO nudge_log (rule, device_id, title, body, created_at) VALUES ('social_cap', NULL, 't', 'b', ?)",
                     ("2026-09-25T17:30:00.000000Z",))  # resting until 13:50 Toronto
    clock.set(at(14, 0).replace(second=10))
    with db.connect() as conn:  # worked out, and kept, this minute
        nudges._evaluation(nudges.Moment(db, conn, clock.now.astimezone(UTC), TZ, TZ_NAME, []), history=False)
    with db.connect() as conn, transaction(conn):
        streaks.save_target(conn, streaks.load_rules().goals["social_cap"], 120)
    clock.set(at(14, 0).replace(second=40))  # the same minute
    assert send(client, tokens, "android-1", instagram(at(13, 59), at(14, 0).replace(second=40)))["nudge"] is None  # 91 < 120


def test_just_over_the_goal_in_whole_minutes(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(9, 55)))
    clock.set(at(14, 0).replace(second=24))
    edge = instagram(at(13, 55), at(14, 0).replace(second=24))  # 55 + 5.4 = 60.4 minutes: "1 hour", not over
    assert send(client, tokens, "android-1", edge)["nudge"] is None
    clock.set(at(14, 1))
    found = send(client, tokens, "android-1", instagram(at(14, 0).replace(second=24), at(14, 1)))["nudge"]  # 61
    assert found is not None and found["body"].startswith("1 hour 1 minute in social apps today, over your goal of 1 hour.")


def test_a_redacted_or_unnamed_app_nudges_no_one(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(10, 30))
    send(client, tokens, "android-1", event("Study", at(10), at(12)))
    unnamed = {"kind": "app_session", "source": "usagestats", "start": at(10, 25).isoformat(), "end": at(10, 30).isoformat()}
    assert send(client, tokens, "android-1", unnamed)["nudge"] is None


# --- R2: late-night scrolling --------------------------------------------------------------------------------------


def test_late_scrolling_names_the_first_event_of_the_next_day(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    send(client, tokens, "android-1", event("Brunch", at(9, days=1), at(10, days=1)), event("Movie", at(14, days=1), at(16, days=1)))
    clock.set(at(23, 45))
    found = send(client, tokens, "android-1", instagram(at(23, 40), at(23, 45)))["nudge"]
    assert found is not None and found["rule"] == "late_scroll" and found["title"] == "Past your bedtime"
    assert found["body"] == ('It\'s 23:45, past your 23:30 bedtime, and Instagram is open. Your day starts with "Brunch" at 09:00.')
    clock.set(at(0, 30, days=1))  # after midnight the next day is today
    found = send(client, tokens, "android-1", tiktok(at(0, 25, days=1), at(0, 30, days=1)))["nudge"]
    assert found is not None and found["body"].startswith("It's 00:30, past your 23:30 bedtime, and TikTok is open.")
    assert found["body"].endswith('Your day starts with "Brunch" at 09:00.')
    clock.set(at(4, 30, days=1))  # the night is over
    assert send(client, tokens, "android-1", tiktok(at(4, 25, days=1), at(4, 30, days=1)))["nudge"] is None


@pytest.mark.parametrize(("night", "only_event", "said"), [
    # Clocks go back on 1 November 2026: that day has 25 hours, and its last one still belongs to it.
    (datetime(2026, 10, 31, 23, 45, tzinfo=TZ), datetime(2026, 11, 1, 23, 30, tzinfo=TZ), 'Your day starts with "Late show" at 23:30.'),
    # Clocks go forward on 8 March 2026: that day has 23 hours, and 00:30 on the 9th is the day after.
    (datetime(2026, 3, 7, 23, 45, tzinfo=TZ), datetime(2026, 3, 9, 0, 30, tzinfo=TZ), "Nothing is on your calendar in the morning"),
])
def test_the_next_day_is_right_on_clock_change_days(client: TestClient, tokens: dict[str, str], clock: Clock,
                                                   night: datetime, only_event: datetime, said: str) -> None:
    send(client, tokens, "android-1", event("Late show", only_event, only_event + timedelta(minutes=20)))
    clock.set(night)
    found = send(client, tokens, "android-1", instagram(night - timedelta(minutes=5), night))["nudge"]
    assert found is not None and found["rule"] == "late_scroll" and said in found["body"]


def test_late_scrolling_follows_the_bedtime_goal_and_a_clear_morning(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(23, 10))
    assert send(client, tokens, "android-1", instagram(at(23, 5), at(23, 10)))["nudge"] is None  # before 23:30
    with db.connect() as conn, transaction(conn):
        streaks.save_target(conn, streaks.load_rules().goals["bedtime"], "23:00")
    clock.set(at(23, 12))
    found = send(client, tokens, "android-1", instagram(at(23, 10), at(23, 12)))["nudge"]
    assert found is not None and found["body"] == (
        "It's 23:12, past your 23:00 bedtime, and Instagram is open. Nothing is on your calendar in the morning, but sleep still counts.")
    clock.set(at(23, 50))
    assert send(client, tokens, "windows-1", code(at(23, 30), at(23, 50)))["nudge"] is None  # work late is not scrolling


# --- R3: over the social goal --------------------------------------------------------------------------------------


def test_the_social_limit_uses_the_goal_and_the_streaks_pages_numbers(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(9, 40)))  # 40 minutes: under the hour
    clock.set(at(14, 1))
    assert send(client, tokens, "android-1", instagram(at(13, 56), at(14, 1)))["nudge"] is None
    send(client, tokens, "android-1", instagram(at(11), at(11, 30)))
    clock.set(at(14, 3))
    found = send(client, tokens, "android-1", instagram(at(14, 1), at(14, 3)))["nudge"]
    goal = streaks.evaluation(db, TZ, TZ_NAME, at(14, 3)).goals["social_cap"]
    assert goal.today.value is not None and goal.target is not None and goal.today.value > goal.target
    assert found is not None and found["rule"] == "social_cap" and found["title"] == "Over your social limit"
    assert found["body"] == f"{duration(goal.today.value)} in social apps today, over your goal of 1 hour. Instagram can wait."
    assert duration(goal.today.value) == "1 hour 17 minutes"  # 40 + 5 + 30 + 2


def test_video_over_the_social_limit_is_not_a_social_nudge(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(10, 30)))
    clock.set(at(14, 5))
    assert send(client, tokens, "android-1", app("YouTube", "com.google.android.youtube", at(14), at(14, 5)))["nudge"] is None


# --- R4: a streak at risk --------------------------------------------------------------------------------------------


def focus_days(client: TestClient, tokens: dict[str, str], minutes_today: int) -> None:
    """Three days of 130 focused minutes (the focus goal is 120), then some today."""
    for days in (-3, -2, -1):
        send(client, tokens, "windows-1", code(at(9, days=days), at(11, 10, days=days)))
    send(client, tokens, "windows-1", code(at(9), at(9) + timedelta(minutes=minutes_today)))


def test_a_streak_at_risk_in_the_evening_says_the_real_amount_left(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(19, 30))
    focus_days(client, tokens, 100)
    clock.set(at(19, 45))
    assert send(client, tokens, "windows-1", code(at(19, 40), at(19, 45)))["nudge"] is None  # before 20:00
    clock.set(at(20, 30))
    found = send(client, tokens, "windows-1", code(at(20, 25), at(20, 30)))["nudge"]
    track = next(item for item in streaks.evaluation(db, TZ, TZ_NAME, at(20, 30)).streaks if item.id == "focus_flame")
    assert track.today.status == "at_risk" and track.today.remaining is not None and len(track.current) == 3
    left = math.ceil(track.today.remaining)
    assert left == 20  # 120 - 100 (the 5-minute windows are too short to count as focus)
    assert found is not None and found["rule"] == "streak_at_risk" and found["title"] == "Keep your Focus flame"
    assert found["body"] == f"{left} more focused minutes keeps your 3-day Focus flame streak going."


def test_no_streak_nudge_that_cannot_be_done_before_midnight(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(19))
    focus_days(client, tokens, 5)  # 120 minutes still to go (5 minutes is too short to count)
    clock.set(at(23, 50))
    assert send(client, tokens, "windows-1", code(at(23, 45), at(23, 50)))["nudge"] is None  # 10 minutes left today
    clock.set(at(21, 30))
    found = send(client, tokens, "windows-1", code(at(21, 25), at(21, 30)))["nudge"]  # 150 minutes left
    assert found is not None and found["body"] == "120 more focused minutes keeps your 3-day Focus flame streak going."


def test_the_social_goal_needs_only_today(client: TestClient, tokens: dict[str, str], clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    whole: list[int] = []
    real = streaks.evaluation
    monkeypatch.setattr(streaks, "evaluation", lambda *args, **kwargs: whole.append(1) or real(*args, **kwargs))
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(10, 30)))
    clock.set(at(14, 1))
    assert send(client, tokens, "android-1", instagram(at(13, 56), at(14, 1)))["nudge"]["rule"] == "social_cap"
    assert whole == []  # the whole history was never read


def test_the_history_is_read_only_for_a_streak_that_can_still_be_kept(db: Database, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    kept = streaks.Track("focus_flame", "Focus flame", "", "focused_minutes", "at_least", "minutes", 120.0, "",
                         [streaks.DayResult(at(12).date(), "met", 130.0, 120.0, None)])
    asked: list[bool] = []

    def evaluation(moment: nudges.Moment, history: bool) -> Any:
        asked.append(history)
        return type("Found", (), {"streaks": [kept], "goals": {}})()

    monkeypatch.setattr(nudges, "_evaluation", evaluation)
    with db.connect() as conn:
        moment = nudges.Moment(db, conn, at(20, 30).astimezone(UTC), TZ, TZ_NAME, [nudges.Activity("Code", "work")])
        assert nudges.streak_at_risk(moment) is None
    assert asked == [False]  # today said nothing could be kept: the history wasn't read


def test_no_streak_nudge_once_it_is_kept_or_with_no_run_to_keep(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(19))
    focus_days(client, tokens, 125)  # kept today
    clock.set(at(20, 30))
    assert send(client, tokens, "windows-1", code(at(20, 25), at(20, 30)))["nudge"] is None


def test_a_new_streak_has_no_run_to_lose(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(20, 30))
    send(client, tokens, "windows-1", code(at(9), at(10)))  # only today: nothing to keep yet
    assert send(client, tokens, "windows-1", code(at(20, 25), at(20, 30)))["nudge"] is None


# --- choices, the log, and never breaking an ingest -----------------------------------------------------------------


def test_the_rules_switch_on_and_off(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    settings = client.get("/api/v1/nudges").json()
    assert [(rule["id"], rule["enabled"]) for rule in settings["rules"]] == [
        ("focus_block", True), ("late_scroll", True), ("streak_at_risk", True), ("social_cap", True)]
    assert (settings["desktop"], settings["cooldown_minutes"], settings["recent"]) == (True, 20, [])
    saved = client.put("/api/v1/nudges", json={"disabled": ["focus_block"], "desktop": False}).json()
    assert [rule["id"] for rule in saved["rules"] if not rule["enabled"]] == ["focus_block"] and saved["desktop"] is False

    clock.set(at(15, 30))
    send(client, tokens, "android-1", event("Study", at(15), at(17)))
    assert send(client, tokens, "android-1", tiktok(at(15, 25), at(15, 30)))["nudge"] is None
    client.put("/api/v1/nudges", json={"disabled": [], "desktop": True})
    clock.set(at(15, 31))
    found = send(client, tokens, "android-1", tiktok(at(15, 30), at(15, 31)))["nudge"]
    assert found is not None
    recent = client.get("/api/v1/nudges").json()["recent"]
    assert [(item["rule"], item["device_id"], item["body"]) for item in recent] == [("focus_block", "android-1", found["body"])]

    refused = client.put("/api/v1/nudges", json={"disabled": ["be_nice"], "desktop": True})
    assert refused.status_code == 400 and "be_nice" in refused.json()["error"]["message"]


@pytest.mark.parametrize("body", [
    {"disable": ["late_scroll"], "desktop": True},  # a typo
    {"desktop": False},  # half a choice
    {"disabled": ["late_scroll"]},
    {"disabled": ["focus_block"] * 5, "desktop": True},  # more than there are rules
])
def test_a_typo_or_half_a_choice_is_refused_not_saved(client: TestClient, db: Database, body: dict[str, Any]) -> None:
    client.put("/api/v1/nudges", json={"disabled": ["late_scroll"], "desktop": False})
    assert client.put("/api/v1/nudges", json=body).status_code == 422
    with db.connect() as conn:
        assert nudges.load_choices(conn) == nudges.Choices(("late_scroll",), False)  # untouched


def test_stored_choices_are_read_leniently(db: Database) -> None:
    with db.connect() as conn, transaction(conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('nudges', ?)", ('{"disabled": ["late_scroll", "gone"], "desktop": "yes"}',))
    with db.connect() as conn:
        assert nudges.load_choices(conn) == nudges.Choices(("late_scroll",), True)
        conn.execute("UPDATE settings SET value = 'not json' WHERE key = 'nudges'")
        assert nudges.load_choices(conn) == nudges.Choices()


def test_a_failing_rule_never_breaks_an_ingest(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: nudges.Moment) -> None:
        raise RuntimeError("a bug in a rule")

    monkeypatch.setitem(nudges.CHECKS, "focus_block", broken)
    clock.set(at(15, 30))
    result = send(client, tokens, "android-1", tiktok(at(15, 25), at(15, 30)))
    assert (result["accepted"], result["nudge"]) == (1, None)


def test_two_requests_at_once_nudge_once(db: Database, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(15, 30))
    now = at(15, 30).astimezone(UTC)
    with db.connect() as conn:
        assert nudges._fire(conn, "focus_block", "android-1", ("Time to focus", "one"), now) is not None
        assert nudges._fire(conn, "focus_block", "iphone-1", ("Time to focus", "two"), now) is None  # rechecked inside
    assert logged(db) == [("focus_block", "android-1")]


# --- the desktop tracker's own nudges --------------------------------------------------------------------------------


def test_the_desktop_tracker_shows_its_own_nudge_when_desktop_notifications_are_on(
    client: TestClient, tokens: dict[str, str], db: Database, clock: Clock
) -> None:
    clock.set(at(15, 30))
    send(client, tokens, "android-1", event("Study for the exam", at(15), at(17)))
    shown: list[Any] = []

    def show(nudge: Any) -> bool:
        shown.append(nudge)
        return True

    sink = DatabaseSink(db, "windows", "This PC", nudge=show)
    steam = {"kind": "window", "source": "tracker", "seq": 1, "start": at(15, 20).isoformat(), "end": at(15, 30).isoformat(),
             "app": "Steam", "app_id": "steam.exe"}
    sink([steam])
    wait_for(lambda: shown)
    assert [(item.rule, item.body) for item in shown] == [("focus_block", 'Steam during "Study for the exam", which runs until 17:00.')]
    assert logged(db) == [("focus_block", sink.device_id)]

    client.put("/api/v1/nudges", json={"disabled": [], "desktop": False})
    clock.set(at(16))
    sink([{**steam, "seq": 2, "start": at(15, 55).isoformat(), "end": at(16).isoformat()}])
    time.sleep(0.2)
    assert len(shown) == 1 and len(logged(db)) == 1  # off: not shown, not logged


def wait_for(done: Callable[[], object], seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_desktop_nudge_that_could_not_be_shown_is_taken_back(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(15, 30))
    send(client, tokens, "android-1", event("Study", at(15), at(17)))
    tried: list[Any] = []

    def fails(nudge: Any) -> bool:
        tried.append(nudge)
        return False  # no desktop session, say

    sink = DatabaseSink(db, "windows", "This PC", nudge=fails)
    sink([{"kind": "window", "source": "tracker", "seq": 1, "start": at(15, 20).isoformat(), "end": at(15, 30).isoformat(),
           "app": "Steam", "app_id": "steam.exe"}])
    wait_for(lambda: tried and not logged(db))  # nobody saw it: not logged, and no rule resting
    found = send(client, tokens, "android-1", tiktok(at(15, 29), at(15, 30)))["nudge"]
    assert found is not None and found["rule"] == "focus_block"  # the phone still gets it


def test_a_nudge_that_goes_wrong_never_looks_like_a_failed_save(db: Database, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(nudges, "pick_nudge", broken)
    clock.set(at(15, 30))
    sink = DatabaseSink(db, "windows", "This PC", nudge=lambda nudge: True)
    sink([{"kind": "window", "source": "tracker", "seq": 1, "start": at(15, 20).isoformat(), "end": at(15, 30).isoformat(), "app": "Steam"}])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (sink.device_id,)).fetchone()[0] == 1  # saved


def test_desktop_nudges_off_read_nothing_more(db: Database, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    with db.connect() as conn, transaction(conn):
        nudges.save_choices(conn, nudges.Choices((), desktop=False))
    opened = []
    real = Database.connect

    def counting(self: Database) -> Any:
        opened.append(1)
        return real(self)

    monkeypatch.setattr(Database, "connect", counting)
    monkeypatch.setattr(nudges, "activities", lambda *_: pytest.fail("off: no need to look at the events"))
    clock.set(at(15, 30))
    assert nudges.pick_nudge(db, "windows-1", [], desktop=True) is None
    assert len(opened) == 1  # one connection, the choices read once

# --- desktop notifications -----------------------------------------------------------------------------------------


HOSTILE = '$(Start-Process calc) "]]></text><text>`$env:USERNAME'


def test_a_windows_toast_never_puts_the_words_in_the_script() -> None:
    found = notify.command(HOSTILE, f"body {HOSTILE}", platform="win32")
    assert found is not None
    argv, extra = found
    assert argv[:2] == ["powershell.exe", "-NoProfile"] and "-EncodedCommand" in argv
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert script == notify.WINDOWS_SCRIPT  # always the same script ...
    assert all(HOSTILE not in part for part in argv)  # ... and the words only ever in its environment
    assert extra == {"DAYTRACE_TOAST_TITLE": HOSTILE, "DAYTRACE_TOAST_BODY": f"body {HOSTILE}", "DAYTRACE_TOAST_APP": notify.POWERSHELL_APP_ID}
    assert "CreateTextNode($env:DAYTRACE_TOAST_TITLE)" in script  # as text in the XML, never parsed as markup
    assert "-ExecutionPolicy" not in argv  # an encoded command needs no change to the execution policy


def test_a_mac_notification_gets_the_words_as_arguments() -> None:
    found = notify.command(HOSTILE, "body", platform="darwin")
    assert found is not None
    argv, extra = found
    assert argv[0] == "osascript" and argv[-2:] == [HOSTILE, "body"] and extra == {}
    assert [argv[i + 1] for i, part in enumerate(argv[:-2]) if part == "-e"] == list(notify.MAC_SCRIPT)


def test_linux_uses_notify_send_only_when_it_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.command("t", "b", platform="linux") is None
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    assert notify.command("t", "b", platform="linux") == (["/usr/bin/notify-send", "--app-name=Daytrace", "--", "t", "b"], {})


def test_showing_a_notification_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(notify, "command", lambda title, body: (["notifier", title, body], {"X": "1"}))
    monkeypatch.setattr(notify, "_run", lambda argv, extra: ran.append((argv, extra)))
    assert notify.show("Time to focus", "TikTok during Study", wait=True) is True
    assert ran == [(["notifier", "Time to focus", "TikTok during Study"], {"X": "1"})]

    def fails(argv: list[str], extra: dict[str, str]) -> None:
        raise OSError("no such program")

    monkeypatch.setattr(notify, "_run", fails)
    assert notify.show("Time to focus", "body", wait=True) is False


# --- redaction and delete-all ----------------------------------------------------------------------------------------


def test_applying_redaction_rules_hides_the_words_of_logged_nudges(client: TestClient, tokens: dict[str, str], db: Database, clock: Clock) -> None:
    clock.set(at(15, 30))
    send(client, tokens, "android-1", event("Study: Project Falcon", at(15), at(17)))
    assert "Falcon" in send(client, tokens, "android-1", tiktok(at(15, 25), at(15, 30)))["nudge"]["body"]
    saved = client.put("/api/v1/privacy/redaction", json={"disabled": [], "custom": [{"name": "Work", "words": ["Falcon"]}]})
    assert saved.status_code == 200, saved.text
    assert client.post("/api/v1/privacy/redaction/apply", json={"confirm": True}).status_code == 200
    recent = client.get("/api/v1/nudges").json()["recent"]
    assert [(item["rule"], item["title"], item["body"]) for item in recent] == [("focus_block", "[redacted]", "[redacted]")]


def test_delete_all_forgets_the_numbers_nudges_worked_out(client: TestClient, tokens: dict[str, str], clock: Clock) -> None:
    clock.set(at(14))
    send(client, tokens, "android-1", instagram(at(9), at(10, 30)))
    clock.set(at(14, 1))
    assert send(client, tokens, "android-1", instagram(at(13, 56), at(14, 1)))["nudge"]["rule"] == "social_cap"
    assert nudges._evaluations
    response = client.post("/api/v1/privacy/delete", json={"confirm": "delete all my daytrace data", "keep_redaction_rules": True})
    assert response.status_code == 200, response.text
    assert not nudges._evaluations
