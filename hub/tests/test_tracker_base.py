"""Tests for DT-16: the desktop tracker's loop (spans, AFK, sleep, flushing) and its place in the hub."""
from __future__ import annotations

import sys
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import app as app_module
from daytrace_hub.app import create_app
from daytrace_hub.config import Settings, get_profile, load_settings
from daytrace_hub.db import Database
from daytrace_hub.sessions import sessions_for
from daytrace_hub.tracker.base import (
    AlreadyTracking,
    DatabaseSink,
    Reading,
    Tracker,
    TrackerService,
    replace_reading,
    single_tracker,
    tracker_device,
)

START = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)  # 09:00 in Toronto
CODE = Reading("Visual Studio Code", "Code.exe", "stats.py - daytrace", 0.0)
EDGE = Reading("Microsoft Edge", "msedge.exe", "Daytrace - Microsoft Edge", 0.0)
HALF_EMOJI = chr(0xD83D)  # half of a surrogate pair, as a broken UTF-16 window title can hold


class Clock:
    """Wall and monotonic time that only move when the test says so."""

    def __init__(self) -> None:
        self.now = START
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


class Script:
    """A probe that returns the reading it is given, every time it is asked."""

    def __init__(self) -> None:
        self.next: Reading | None | Exception = CODE

    def read(self) -> Reading | None:
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


class Recorder:
    """A sink that keeps the newest copy of each span, as the hub does (by external_id, the higher seq wins)."""

    def __init__(self) -> None:
        self.writes: list[list[dict[str, Any]]] = []
        self.fail = False

    def __call__(self, events: list[dict[str, Any]]) -> None:
        if self.fail:
            raise OSError("database is locked")
        self.writes.append(events)

    def spans(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in (e for write in self.writes for e in write):
            kept = latest.get(event["external_id"])
            if kept is None or event["seq"] > kept["seq"]:
                latest[event["external_id"]] = event
        return sorted(latest.values(), key=lambda e: e["start"])


def summary(spans: Iterable[dict[str, Any]]) -> list[tuple[str, str | None, str, str]]:
    def hhmmss(text: str) -> str:
        return datetime.fromisoformat(text).astimezone(UTC).strftime("%H:%M:%S")

    return [(s["kind"], s.get("app_id"), hhmmss(s["start"]), hhmmss(s["end"])) for s in spans]


@pytest.fixture
def rig() -> tuple[Tracker, Script, Clock, Recorder]:
    clock, probe, sink = Clock(), Script(), Recorder()
    tracker = Tracker(probe, sink, "windows-1", first_seq=100, wall=lambda: clock.now, monotonic=lambda: clock.mono)
    return tracker, probe, clock, sink


def poll(tracker: Tracker, clock: Clock, times: int = 1, every: float = 2.0) -> None:
    for _ in range(times):
        tracker.step()
        clock.advance(every)


class Steady:
    """A probe that always shows VS Code."""

    def read(self) -> Reading:
        return CODE


# --- spans -----------------------------------------------------------------------------------------------------------


def test_the_same_window_is_one_span_that_grows(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, _, clock, sink = rig
    poll(tracker, clock, 6)  # readings 13:00:00 to 13:00:10
    tracker.close()
    assert summary(sink.spans()) == [("window", "Code.exe", "13:00:00", "13:00:10")]
    copies = [e for write in sink.writes for e in write]
    assert len({e["external_id"] for e in copies}) == 1  # one span, written again as it grew
    assert [e["seq"] for e in copies] == sorted(e["seq"] for e in copies) and copies[0]["seq"] == 101
    assert (copies[-1]["app"], copies[-1]["title"], copies[-1]["source"]) == ("Visual Studio Code", "stats.py - daytrace", "tracker")


def test_switching_apps_ends_one_span_and_starts_the_next(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock, 3)  # Code until 13:00:04
    probe.next = EDGE
    poll(tracker, clock, 3)  # the switch is seen at 13:00:06
    tracker.close()
    assert summary(sink.spans()) == [
        ("window", "Code.exe", "13:00:00", "13:00:06"),
        ("window", "msedge.exe", "13:00:06", "13:00:10"),
    ]


def test_a_title_that_keeps_changing_relabels_instead_of_splitting(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock)  # 13:00:00
    for second in range(3):  # a clock in the title: a new title every 2 s
        probe.next = replace_reading(CODE, title=f"terminal 13:00:0{second}")
        poll(tracker, clock)
    tracker.close()
    spans = sink.spans()
    assert len(spans) == 1 and spans[0]["title"] == "terminal 13:00:02"


def test_a_title_change_after_it_settled_is_a_new_span(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock, 7)  # 13:00:00 to 13:00:12 on one file
    probe.next = replace_reading(CODE, title="sessions.py - daytrace")
    poll(tracker, clock, 2)
    tracker.close()
    assert [s["title"] for s in sink.spans()] == ["stats.py - daytrace", "sessions.py - daytrace"]


# --- away ------------------------------------------------------------------------------------------------------------


def poll_idle(tracker: Tracker, probe: Script, clock: Clock, last_input: datetime, until: datetime,
              showing: Reading = CODE) -> None:
    """Poll every 2 s up to `until`, with the idle time a real probe would report since `last_input`."""
    while clock.now <= until:
        probe.next = replace_reading(showing, idle_seconds=(clock.now - last_input).total_seconds())
        poll(tracker, clock)


def test_no_input_for_three_minutes_is_afk_from_the_last_input(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock, 31)  # typing until 13:01:00
    left = START + timedelta(minutes=1)  # the last input
    poll_idle(tracker, probe, clock, left, START + timedelta(minutes=5))  # AFK is noticed at 13:04:00, 3 min later
    back = clock.now - timedelta(seconds=1)  # input again at 13:05:01
    probe.next = replace_reading(CODE, idle_seconds=1.0)
    poll(tracker, clock, 2)
    tracker.close()
    assert back.strftime("%H:%M:%S") == "13:05:01"
    # The window was written up to 13:03:58 while it still looked like reading; AFK cut it back to 13:01:00.
    # The last reading is at 13:05:04, where close() ends the window.
    assert summary(sink.spans()) == [
        ("window", "Code.exe", "13:00:00", "13:01:00"),
        ("afk", None, "13:01:00", "13:05:01"),
        ("window", "Code.exe", "13:05:01", "13:05:04"),
    ]


def test_a_window_cut_back_to_nothing_is_rewritten_too(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock)  # typing in VS Code at 13:00:00, then a window takes the front at 13:00:02 by itself
    poll_idle(tracker, probe, clock, START, START + timedelta(minutes=4), showing=EDGE)
    tracker.close()
    edge = next(s for s in sink.spans() if s.get("app_id") == "msedge.exe")
    # Edge was written up to 13:02:58 while it looked like reading; no input ever happened in it, so it now ends
    # where it began, and the hub gets that copy too (not the older, longer one).
    assert edge["start"] == edge["end"]
    assert sink.spans()[-1]["kind"] == "afk"


def test_the_lock_screen_is_afk_at_once(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock, 3)
    probe.next = Reading(None, None, None, 0.5, locked=True)  # seen at 13:00:06; Win+L pressed at 13:00:05.5
    poll(tracker, clock, 3)
    tracker.close()
    spans = sink.spans()
    assert [s["kind"] for s in spans] == ["window", "afk"]
    assert spans[0]["end"] == spans[1]["start"]
    assert datetime.fromisoformat(spans[1]["start"]).astimezone(UTC) == START + timedelta(seconds=5.5)


def test_a_video_holding_the_screen_on_is_not_afk_for_hours(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    probe.next = replace_reading(EDGE, idle_seconds=1800.0, display_required=True)  # half an hour into a film
    poll(tracker, clock, 3)
    probe.next = replace_reading(EDGE, idle_seconds=4 * 3600.0, display_required=True)  # asleep in front of it
    poll(tracker, clock)
    tracker.close()
    assert [s["kind"] for s in sink.spans()] == ["window", "afk"]


def test_a_sleeping_computer_is_never_screen_time(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, _, clock, sink = rig
    poll(tracker, clock, 3)  # readings 13:00:00 to 13:00:04
    clock.advance(3600)  # the lid was closed for an hour
    poll(tracker, clock, 2)  # readings 14:00:06 and 14:00:08
    tracker.close()
    assert summary(sink.spans()) == [
        ("window", "Code.exe", "13:00:00", "13:00:04"),
        ("window", "Code.exe", "14:00:06", "14:00:08"),
    ]


# --- robustness ------------------------------------------------------------------------------------------------------


def test_a_probe_that_fails_never_stops_the_tracker(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    poll(tracker, clock, 2)
    probe.next = OSError("access is denied")  # an elevated window, say
    poll(tracker, clock)
    probe.next = CODE
    poll(tracker, clock)
    tracker.close()
    assert summary(sink.spans()) == [("window", "Code.exe", "13:00:00", "13:00:06")]


def test_titles_are_cleaned_and_cut_to_the_schema(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    probe.next = replace_reading(CODE, title=f"broken {HALF_EMOJI} emoji " + "x" * 600)
    poll(tracker, clock, 2)
    tracker.close()
    title = sink.spans()[0]["title"]
    assert len(title) == 500 and HALF_EMOJI not in title
    title.encode("utf-8")  # valid Unicode now


def test_the_redaction_hook_sees_every_reading_first() -> None:
    clock, probe, sink = Clock(), Script(), Recorder()
    probe.next = replace_reading(EDGE, title="MyBank - Account Summary - Microsoft Edge")

    def hide(reading: Reading) -> Reading:
        return replace_reading(reading, title=None) if "Bank" in (reading.title or "") else reading

    tracker = Tracker(probe, sink, "windows-1", redact=hide, wall=lambda: clock.now, monotonic=lambda: clock.mono)
    poll(tracker, clock, 2)
    tracker.close()
    assert "title" not in sink.spans()[0]


def test_spans_that_could_not_be_saved_are_kept_for_the_next_try(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, probe, clock, sink = rig
    sink.fail = True
    poll(tracker, clock, 3)
    probe.next = EDGE
    poll(tracker, clock, 3)  # the Code span ended while the database was busy
    sink.fail = False
    poll(tracker, clock)
    tracker.close()
    assert [s["app_id"] for s in sink.spans()] == ["Code.exe", "msedge.exe"]


def test_writes_are_batched_every_few_seconds(rig: tuple[Tracker, Script, Clock, Recorder]) -> None:
    tracker, _, clock, sink = rig
    poll(tracker, clock, 10)  # 20 seconds on one window
    assert 4 <= len(sink.writes) <= 6  # about every 4 s, not every 2 s poll


# --- in the hub ------------------------------------------------------------------------------------------------------


def test_spans_reach_the_timeline_through_the_ingest_path(db: Database) -> None:
    with db.connect() as conn:
        device_id = tracker_device(conn, "windows", "DESKTOP-TEST")
        assert tracker_device(conn, "windows", "DESKTOP-TEST") == device_id  # created once
        row = conn.execute("SELECT token_hash, device_type FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    assert device_id == "windows-1" and row["token_hash"] is None and row["device_type"] == "windows"
    clock, probe = Clock(), Script()
    sink = DatabaseSink(db, device_id)
    tracker = Tracker(probe, sink, device_id, first_seq=sink.first_seq(), wall=lambda: clock.now, monotonic=lambda: clock.mono)
    poll(tracker, clock, 31)  # a minute of VS Code
    tracker.close()
    with db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
        sessions = sessions_for(conn, START, START + timedelta(hours=1), now=START + timedelta(hours=1))
    assert rows == 1  # one span, replaced as it grew
    assert [(s.app, s.category, s.seconds) for s in sessions] == [("Visual Studio Code", "work", 60)]
    assert sink.first_seq() > 1  # a restart continues the numbering


def test_only_one_tracker_per_profile(tmp_path: Path) -> None:
    lock = tmp_path / "personal.db.tracker.lock"
    with single_tracker(lock), pytest.raises(AlreadyTracking), single_tracker(lock):
        pass
    with single_tracker(lock):  # free again once the first one stopped
        pass


def test_the_service_runs_in_the_background_and_stops_cleanly(db: Database, tmp_path: Path) -> None:
    service = TrackerService(db, Steady(), "windows", "DESKTOP-TEST", tmp_path / "t.lock")
    service.start()
    threading.Event().wait(0.3)
    service.stop()
    assert service.device_id == "windows-1"


def test_tracking_is_on_by_default_only_for_your_own_data(tmp_path: Path) -> None:
    base = {"DAYTRACE_DATA_DIR": str(tmp_path)}
    assert load_settings("personal", env=base).track_desktop is True
    assert load_settings("demo", env=base).track_desktop is False
    assert load_settings("personal", env={**base, "DAYTRACE_TRACKER": "off"}).track_desktop is False
    assert load_settings("shared-dev", env={**base, "DAYTRACE_TRACKER": "on"}).track_desktop is True
    with pytest.raises(ValueError, match="DAYTRACE_TRACKER"):
        load_settings("personal", env={**base, "DAYTRACE_TRACKER": "sometimes"})


def test_the_hub_starts_and_stops_its_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "platform_probe", lambda: (Steady(), "windows"))
    settings = Settings(profile=get_profile("personal"), data_dir=tmp_path, track_desktop=True)
    with TestClient(create_app(settings), client=("127.0.0.1", 50000), base_url="http://localhost:8765") as hub:
        assert hub.get("/api/v1/health").status_code == 200
        threading.Event().wait(0.3)
    with Database(settings.database_path).connect() as conn:
        assert conn.execute("SELECT device_id FROM devices WHERE token_hash IS NULL").fetchone()[0] == "windows-1"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows probe needs Windows")
def test_the_windows_probe_reads_without_crashing() -> None:
    from daytrace_hub.tracker.windows import WindowsProbe, display_name, idle_seconds

    reading = WindowsProbe().read()  # CI runs without a desktop: None or a locked reading is fine too
    assert reading is None or reading.idle_seconds >= 0
    assert idle_seconds() >= 0
    assert display_name(sys.executable)  # python.exe describes itself ("Python")
