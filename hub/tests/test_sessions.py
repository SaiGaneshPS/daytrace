"""Tests for DT-13: sessionizer rules."""
from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daytrace_hub.sessions import (
    Session,
    StoredEvent,
    build_sessions,
    merge_heartbeats,
    resolve_overlaps,
    sessions_for,
)

DAY = datetime(2026, 9, 25, tzinfo=UTC)
WINDOW = (DAY, DAY + timedelta(days=1))
_ids = iter(range(1, 1_000_000))


def at(clock: str, day: datetime = DAY) -> datetime:
    parts = [int(p) for p in clock.split(":")]
    return day + timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2] if len(parts) > 2 else 0)


def ev(kind: str, start: str, end: str | None = None, app: str | None = "Instagram", device: str = "d1",
       title: str | None = None, data: dict[str, Any] | None = None, **extra: Any) -> StoredEvent:
    return StoredEvent(
        id=extra.get("id", next(_ids)),
        device_id=device,
        kind=kind,
        source=extra.get("source", "tracker"),
        start=at(start),
        end=at(end) if end else None,
        app=app,
        app_id=extra.get("app_id"),
        title=title,
        category=extra.get("category"),
        data=data or {},
    )


def run(*events: StoredEvent, window: tuple[datetime, datetime] = WINDOW) -> list[Session]:
    return build_sessions(events, *window)


def spans(sessions: list[Session]) -> list[tuple[str | None, str, str]]:
    return [(s.app, s.start.strftime("%H:%M:%S"), s.end.strftime("%H:%M:%S")) for s in sessions]


def total(sessions: list[Session]) -> int:
    return sum(s.seconds for s in sessions)


# --- spans and pairs ------------------------------------------------------------------------------------------


def test_a_usage_span_becomes_one_exact_session() -> None:
    sessions = run(ev("app_session", "10:00:00", "10:18:34"))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:18:34")]
    assert sessions[0].seconds == 18 * 60 + 34
    assert not sessions[0].estimated


def test_an_iphone_open_and_close_become_a_session() -> None:
    sessions = run(ev("app_open", "10:00"), ev("app_close", "10:12"))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:12:00")]
    assert not sessions[0].estimated


def test_an_open_without_close_ends_at_the_next_event_and_is_estimated() -> None:
    sessions = run(ev("app_open", "10:00"), ev("app_open", "10:07", app="TikTok"), ev("app_close", "10:20", app="TikTok"))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:07:00"), ("TikTok", "10:07:00", "10:20:00")]
    assert [s.estimated for s in sessions] == [True, False]


def test_an_open_without_close_or_later_event_lasts_30_minutes() -> None:
    assert spans(run(ev("app_open", "10:00"))) == [("Instagram", "10:00:00", "10:30:00")]


def test_an_open_whose_next_event_is_far_away_still_stops_at_30_minutes() -> None:
    sessions = run(ev("app_open", "10:00"), ev("screen_off", "11:15", app=None))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:30:00")]


def test_a_screen_off_ends_an_open_session() -> None:
    assert spans(run(ev("app_open", "10:00"), ev("screen_off", "10:04", app=None))) == [
        ("Instagram", "10:00:00", "10:04:00")
    ]


def test_a_close_without_an_open_is_ignored() -> None:
    assert run(ev("app_close", "10:00")) == []


def test_opening_the_same_app_again_ends_the_unclosed_one() -> None:
    sessions = run(ev("app_open", "10:00"), ev("app_open", "10:05"), ev("app_close", "10:09"))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:09:00")]  # joined: same app, touching
    assert sessions[0].estimated  # part of it was inferred


def test_an_open_and_close_hours_apart_are_a_missed_close_not_a_session() -> None:
    sessions = run(ev("app_open", "08:00"), ev("app_close", "15:00"))
    assert spans(sessions) == [("Instagram", "08:00:00", "08:30:00")]
    assert sessions[0].estimated


# --- heartbeats -----------------------------------------------------------------------------------------------


def test_window_heartbeats_close_together_merge() -> None:
    sessions = run(
        ev("window", "10:00:00", "10:00:02", app="Code", title="stats.py"),
        ev("window", "10:00:05", "10:00:07", app="Code", title="stats.py"),  # 3 s gap
        ev("window", "10:00:12", "10:00:14", app="Code", title="stats.py"),  # 5 s gap, still merged
        ev("window", "10:00:20", "10:00:22", app="Code", title="stats.py"),  # 6 s gap, new session
    )
    assert spans(sessions) == [("Code", "10:00:00", "10:00:14"), ("Code", "10:00:20", "10:00:22")]
    assert len(sessions[0].event_ids) == 3


def test_heartbeats_with_another_title_stay_separate() -> None:
    sessions = run(
        ev("window", "10:00:00", "10:00:02", app="Code", title="stats.py"),
        ev("window", "10:00:03", "10:00:05", app="Code", title="db.py"),
    )
    assert [(s.title, s.seconds) for s in sessions] == [("stats.py", 2), ("db.py", 2)]


def test_phone_usage_stats_are_exact_and_never_padded() -> None:
    sessions = run(ev("app_session", "10:00:00", "10:01:00"), ev("app_session", "10:01:02", "10:02:00"))
    assert total(sessions) == 118  # the 2 s gap is not counted


def test_merge_heartbeats_joins_only_matching_neighbours() -> None:
    base = Session("d1", at("10:00"), at("10:01"), "A", None, None, None, "app", event_ids=(1,))
    other = Session("d1", at("10:01:01"), at("10:02"), "B", None, None, None, "app", event_ids=(2,))
    again = Session("d1", at("10:02:01"), at("10:03"), "A", None, None, None, "app", event_ids=(3,))
    assert [s.app for s in merge_heartbeats([base, other, again])] == ["A", "B", "A"]


# --- overlaps -------------------------------------------------------------------------------------------------


def test_a_newer_app_covers_an_older_one_which_continues_afterwards() -> None:
    sessions = run(ev("app_session", "10:00", "10:30", app="YouTube"), ev("app_session", "10:10", "10:20", app="WhatsApp"))
    assert spans(sessions) == [
        ("YouTube", "10:00:00", "10:10:00"),
        ("WhatsApp", "10:10:00", "10:20:00"),
        ("YouTube", "10:20:00", "10:30:00"),
    ]
    assert total(sessions) == 30 * 60  # never double counted


def test_an_overlapping_handover_is_not_double_counted() -> None:
    sessions = run(ev("app_session", "10:00", "10:30", app="YouTube"), ev("app_session", "10:20", "10:40", app="TikTok"))
    assert spans(sessions) == [("YouTube", "10:00:00", "10:20:00"), ("TikTok", "10:20:00", "10:40:00")]
    assert total(sessions) == 40 * 60


def test_the_same_app_overlapping_itself_becomes_one_session() -> None:
    sessions = run(ev("app_session", "10:00", "10:30"), ev("app_session", "10:15", "10:45"))
    assert spans(sessions) == [("Instagram", "10:00:00", "10:45:00")]


def test_devices_are_independent() -> None:
    sessions = run(ev("app_session", "10:00", "10:30", device="phone"), ev("window", "10:00", "10:30", device="pc"))
    assert sorted((s.device_id, s.seconds) for s in sessions) == [("pc", 1800), ("phone", 1800)]


def test_resolved_overlaps_never_double_count_or_lose_time() -> None:
    rng = random.Random(1234)
    for _ in range(300):
        raw = []
        for n in range(rng.randint(1, 12)):
            start = DAY + timedelta(minutes=rng.randint(0, 120))
            raw.append(Session("d1", start, start + timedelta(minutes=rng.randint(1, 40)), rng.choice("ABC"),
                               None, None, None, "app", event_ids=(n + 1,)))
        resolved = resolve_overlaps(raw)
        ordered = sorted(resolved, key=lambda s: s.start)
        assert all(a.end <= b.start for a, b in itertools.pairwise(ordered))
        covered = _union(raw)
        assert sum((s.end - s.start).total_seconds() for s in resolved) == covered


def _union(sessions: list[Session]) -> float:
    total_seconds, end = 0.0, None
    for s in sorted(sessions, key=lambda s: s.start):
        if end is None or s.start >= end:
            total_seconds += (s.end - s.start).total_seconds()
            end = s.end
        elif s.end > end:
            total_seconds += (s.end - end).total_seconds()
            end = s.end
    return total_seconds


# --- AFK and windows ------------------------------------------------------------------------------------------


def test_afk_periods_are_cut_out() -> None:
    sessions = run(ev("window", "10:00", "11:00", app="Code"), ev("afk", "10:20", "10:30", app=None))
    assert spans(sessions) == [("Code", "10:00:00", "10:20:00"), ("Code", "10:30:00", "11:00:00")]
    assert total(sessions) == 50 * 60


def test_afk_on_another_device_changes_nothing() -> None:
    sessions = run(ev("window", "10:00", "11:00", app="Code", device="pc"), ev("afk", "10:20", "10:30", app=None, device="mac"))
    assert total(sessions) == 60 * 60


def test_sessions_are_split_at_the_window_edges() -> None:
    late = ev("app_session", "23:50", "23:59:59")
    across = StoredEvent(late.id, "d1", "app_session", "usagestats", at("23:50"), at("00:10", DAY + timedelta(days=1)),
                         "Instagram", None, None, None, {})
    today = run(across)
    tomorrow = run(across, window=(DAY + timedelta(days=1), DAY + timedelta(days=2)))
    assert [s.seconds for s in today] == [600]
    assert [s.seconds for s in tomorrow] == [600]


def test_web_events_are_listed_by_domain() -> None:
    sessions = run(ev("web", "10:00", "10:05", app=None, data={"domain": "youtube.com"}, app_id="msedge"))
    assert (sessions[0].app, sessions[0].kind, sessions[0].app_id) == ("youtube.com", "web", "msedge")


def test_non_screen_events_make_no_sessions() -> None:
    assert run(
        ev("sleep", "01:00", "07:00", app=None, data={"stage": "asleep"}),
        ev("steps", "00:00", "21:00", app=None, data={"count": 8000}),
        ev("calendar_event", "15:00", "17:00", app=None, title="Study"),
        ev("meal", "19:00", app=None, data={"text": "dal"}),
        ev("screen_on", "09:00", app=None),
    ) == []


def test_sub_second_slivers_are_dropped() -> None:
    sliver = StoredEvent(next(_ids), "d1", "app_session", "usagestats", at("10:00"),
                         at("10:00") + timedelta(milliseconds=300), "X", None, None, None, {})
    assert run(sliver) == []


def test_session_windows_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sessions_for(None, datetime(2026, 9, 25), datetime(2026, 9, 26))  # type: ignore[arg-type]  # noqa: DTZ001
