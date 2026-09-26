"""DT-16: the desktop tracker's shared loop. The Windows probe (tracker/windows.py) and the macOS one (DT-17)
plug into it.

Every 2 seconds a probe reads the foreground app, its window title and how long it has been since the last
keyboard or mouse input. The loop turns readings into spans on the hub computer's own device:

- The same app and title in a row extend one `window` span; a change ends it and starts the next. A title that
  keeps changing (a clock or a counter in it, a new title less than 10 s after the last one) relabels the span
  instead of splitting it.
- No input for 3 minutes is AFK. It starts at the last input, and every span that ran past that moment (the open
  one, or one a window that came to the front by itself started) is cut back to it. Only input ends AFK.
- A video or presentation holding the screen on (a display request) keeps the screen time going without input,
  for up to 3 hours; when the request ends or the 3 hours are up, AFK starts there, not back at the last input.
  The lock screen is AFK straight away.
- A pause between two readings far longer than the poll, on either clock (the computer slept, or the process was
  paused; macOS's monotonic clock stops during sleep), or the wall clock stepping back, ends the open span at the
  last reading, and nothing is ever recorded before the moment tracking resumed.
- Spans are written through the ingest path (api.events.store_events) under a stable external_id with a seq that
  only grows, so each flush replaces the hub's copy: the timeline is a few seconds behind at most, and one span is
  one row. A span cut back after it was written is written again. Titles pass through the redaction hook first
  (DT-44 fills it in).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Protocol

from pydantic import ValidationError

from ..db import Database, transaction, utc_text
from ..models import Event

logger = logging.getLogger("daytrace_hub.tracker")

POLL_SECONDS = 2.0
AFK_AFTER_SECONDS = 180.0
MAX_WATCHING_SECONDS = 3 * 3600.0  # a screen kept on by a video longer than this without input is AFK anyway
FLUSH_EVERY_SECONDS = 4.0  # the timeline is at most about this far behind
SLEEP_GAP_SECONDS = 30.0  # a pause this long between readings means the computer slept
CLOCK_BACK_SECONDS = 1.0  # the wall clock going back more than this is a clock change
TITLE_SETTLE = timedelta(seconds=10)  # a new title sooner than this after the last one relabels the span
RECENT = timedelta(seconds=AFK_AFTER_SECONDS + 60)  # how far back AFK can still cut spans
MAX_APP, MAX_TITLE = 200, 500  # the event schema's limits


@dataclass(frozen=True)
class Reading:
    """What the screen showed at one moment."""

    app: str | None  # a display name, such as "Visual Studio Code"
    app_id: str | None  # the program, such as "Code.exe" (a bundle id on macOS)
    title: str | None
    idle_seconds: float  # since the last keyboard or mouse input
    locked: bool = False
    display_required: bool = False  # a video or presentation is keeping the screen on


class Probe(Protocol):
    def read(self) -> Reading | None:
        """The current reading, or None when nothing can be read right now."""


Redactor = Callable[[Reading], Reading]
Sink = Callable[[list[dict[str, Any]]], None]


def no_redaction(reading: Reading) -> Reading:
    return reading


def replace_reading(reading: Reading, **changes: Any) -> Reading:
    """For redaction rules (DT-44): the same reading with some fields changed."""
    return replace(reading, **changes)


def clean_text(text: str | None, limit: int) -> str | None:
    """Valid Unicode (a broken UTF-16 title can hold half an emoji) and within the schema's length."""
    if text is None:
        return None
    text = text.encode("utf-8", "replace").decode("utf-8").strip()
    return text[:limit] or None


@dataclass
class Span:
    kind: str  # "window" or "afk"
    start: datetime
    end: datetime
    number: int  # the span's place in this run, for its external_id
    app: str | None = None
    app_id: str | None = None
    title: str | None = None
    title_changed: datetime = field(default=datetime.min.replace(tzinfo=UTC))

    @property
    def identity(self) -> tuple[str | None, str | None, str | None]:
        return (self.app, self.app_id, self.title)

    def end_at(self, moment: datetime) -> None:
        """End here, but never before the start (a clock change must not make a span end before it began)."""
        self.end = max(self.start, moment)


class Tracker:
    """Readings in, spans out. `step()` is one poll; `run()` polls until stopped; `close()` ends what is open."""

    def __init__(
        self,
        probe: Probe,
        sink: Sink,
        device_id: str,
        *,
        last_seq: int = 0,
        redact: Redactor = no_redaction,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = POLL_SECONDS,
        afk_after: float = AFK_AFTER_SECONDS,
        flush_every: float = FLUSH_EVERY_SECONDS,
    ) -> None:
        self.probe = probe
        self.sink = sink
        self.device_id = device_id
        self.redact = redact
        self.wall = wall
        self.monotonic = monotonic
        self.poll_seconds = poll_seconds
        self.afk_after = afk_after
        self.flush_every = flush_every
        self._seq = last_seq  # the last seq this device used; each write takes the next
        started = wall()
        self._run_id = int(started.timestamp() * 1000)
        self._numbers = 0
        self.current: Span | None = None
        self._floor = started  # nothing is recorded before this (moves on after a sleep or a clock change)
        self._held_until: datetime | None = None  # the last moment only a display request kept AFK away
        self._recent: list[Span] = []  # ended spans AFK may still cut back
        self._pending: list[Span] = []  # ended or cut-back spans not written yet
        self._written: set[int] = set()  # spans the hub has a copy of (among current and recent)
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._last_flush: float | None = None
        self.wait: Callable[[threading.Event, float], object] = lambda stop, seconds: stop.wait(seconds)

    # --- one poll --------------------------------------------------------------------------------------------

    def step(self) -> None:
        now, mono = self.wall(), self.monotonic()
        if self._last_wall is not None and self._last_mono is not None:
            wall_gap = (now - self._last_wall).total_seconds()
            limit = max(SLEEP_GAP_SECONDS, 10 * self.poll_seconds)
            if max(mono - self._last_mono, wall_gap) > limit or wall_gap < -CLOCK_BACK_SECONDS:
                self._interrupted(now)  # slept, paused, or the clock changed: nothing happened in between
        reading = self._read()
        if reading is not None:
            self._apply(reading, now)
            self._last_wall, self._last_mono = now, mono
        if self._last_flush is None or mono - self._last_flush >= self.flush_every or self._pending:
            self.flush()
            self._last_flush = mono

    def _interrupted(self, now: datetime) -> None:
        self._end(self._last_wall or now)
        self._floor = now
        self._held_until = None

    def _read(self) -> Reading | None:
        try:
            reading = self.probe.read()
            return None if reading is None else self.redact(reading)
        except Exception:  # a probe must never stop the tracker (odd windows, elevated apps)
            logger.exception("the tracker could not read the screen this time")
            return None

    def _apply(self, reading: Reading, now: datetime) -> None:
        now = max(now, self._floor)
        last_input = min(now, now - timedelta(seconds=max(0.0, reading.idle_seconds)))
        current = self.current
        if current is not None and current.kind == "afk":
            # Only input ends AFK: a video starting or a call coming in while you are away does not.
            if reading.locked or reading.idle_seconds >= self.afk_after or last_input <= current.start:
                current.end_at(now)
                return
            back = max(current.start, last_input)
            self._end(back)
            self._begin_window(reading, back, now)
            return
        watching = reading.display_required and reading.idle_seconds < MAX_WATCHING_SECONDS
        if reading.locked or (reading.idle_seconds >= self.afk_after and not watching):
            # AFK from the last input, or from the last moment a display request was holding it off.
            start = self._held_until if self._held_until is not None else last_input
            start = min(now, max(start, self._floor))
            self._cut_back(start)
            self._held_until = None
            self._numbers += 1
            self.current = Span("afk", start, now, self._numbers)
            return
        self._held_until = now if reading.idle_seconds >= self.afk_after else None  # watching, not typing
        app, app_id = clean_text(reading.app, MAX_APP), clean_text(reading.app_id, MAX_APP)
        title = clean_text(reading.title, MAX_TITLE)
        if current is not None and current.identity == (app, app_id, title):
            current.end_at(now)
            return
        if current is not None and (current.app, current.app_id) == (app, app_id) and now - current.title_changed < TITLE_SETTLE:
            current.title, current.title_changed = title, now  # the title keeps changing: relabel, don't split
            current.end_at(now)
            return
        if current is not None:
            self._end(now)
        self._begin_window(reading, now, now)

    def _begin_window(self, reading: Reading, start: datetime, now: datetime) -> None:
        self._numbers += 1
        self.current = Span(
            "window", start, max(start, now), self._numbers, clean_text(reading.app, MAX_APP),
            clean_text(reading.app_id, MAX_APP), clean_text(reading.title, MAX_TITLE), title_changed=start,
        )

    def _end(self, at: datetime) -> None:
        """End the open span at `at` (never before it started)."""
        if self.current is None:
            return
        self.current.end_at(at)
        self._pending.append(self.current)
        self._recent.append(self.current)
        self.current = None

    def _cut_back(self, moment: datetime) -> None:
        """Nothing was in use after `moment`: end the open span there, and cut back ended spans that ran past it."""
        self._end(moment)
        for span in self._recent:
            if span.end > moment:
                span.end_at(moment)
                if span not in self._pending:
                    self._pending.append(span)

    # --- writing ---------------------------------------------------------------------------------------------

    def flush(self) -> None:
        """Write what ended or was cut back since the last flush, and the span still open. Kept for the next try
        if it fails. A span the hub already has is written even when it was cut back to nothing, so the hub never
        keeps an older, longer copy."""
        spans = [*self._pending, *([self.current] if self.current is not None else [])]
        due = [span for span in spans if span.end > span.start or span.number in self._written]
        if due:
            try:
                self.sink([self._event(span) for span in due])
            except Exception:  # a busy database or a full disk: keep the spans and try again
                logger.exception("the tracker could not save its spans; it will try again")
                return
            self._written.update(span.number for span in due)
        self._pending = []
        horizon = (self._last_wall or self.wall()) - RECENT
        self._recent = [span for span in self._recent if span.end >= horizon]
        alive = {span.number for span in self._recent} | ({self.current.number} if self.current else set())
        self._written &= alive

    def _event(self, span: Span) -> dict[str, Any]:
        self._seq += 1
        event: dict[str, Any] = {
            "device_id": self.device_id,
            "seq": self._seq,  # grows on every write, so the hub keeps the newest copy of the span
            "external_id": f"tracker:{self._run_id}:{span.number}",
            "kind": span.kind,
            "source": "tracker",
            "start": span.start.astimezone().isoformat(),  # the offset in force here, as every collector sends
            "end": span.end.astimezone().isoformat(),
        }
        if span.kind == "window":
            event.update({key: value for key, value in (("app", span.app), ("app_id", span.app_id), ("title", span.title))
                          if value is not None})
        return event

    # --- running ---------------------------------------------------------------------------------------------

    def close(self) -> None:
        """End the open span at the last reading and write everything."""
        self._end(self._last_wall or self.wall())
        self.flush()

    def run(self, stop: threading.Event) -> None:
        """Poll until `stop` is set, then close (always, even after an error). Polls are scheduled from the
        monotonic clock; after a sleep or a slow write the schedule starts again from now instead of catching up
        with a burst of polls."""
        next_poll = self.monotonic()
        try:
            while not stop.is_set():
                try:
                    self.step()
                except Exception:  # anything unexpected: log it and keep tracking
                    logger.exception("the tracker hit an unexpected problem; it keeps going")
                next_poll += self.poll_seconds
                now = self.monotonic()
                if next_poll <= now:
                    next_poll = now + self.poll_seconds
                self.wait(stop, next_poll - now)
        finally:
            self.close()


# --- the hub's own desktop device -----------------------------------------------------------------------------


def tracker_device(conn: sqlite3.Connection, device_type: str, name: str) -> str:
    """The hub computer's own device (no token: it writes through the ingest path directly, never over the
    network). Created once, then reused; a revoked one is replaced by a new one. Seed devices (seed-...) have no
    token either and are never taken over. Call inside a transaction."""
    from ..api.devices import next_device_id

    row = conn.execute(
        "SELECT device_id FROM devices WHERE token_hash IS NULL AND device_type = ? AND revoked_at IS NULL"
        " AND device_id NOT LIKE 'seed-%' ORDER BY paired_at LIMIT 1",
        (device_type,),
    ).fetchone()
    if row is not None:
        return str(row[0])
    device_id = next_device_id(conn, device_type)
    conn.execute(
        "INSERT INTO devices (device_id, name, device_type, token_hash, paired_at) VALUES (?, ?, ?, NULL, ?)",
        (device_id, name, device_type, utc_text(datetime.now(UTC))),
    )
    return device_id


class DatabaseSink:
    """Writes tracker events through the same path as events from phones (validation, dedup, replacement). If the
    device was revoked (or deleted) while tracking, the events go to a new tracker device from then on."""

    def __init__(self, database: Database, device_type: str, name: str) -> None:
        self.database = database
        self.device_type = device_type
        self.name = name
        with database.connect() as conn, transaction(conn):
            self.device_id = tracker_device(conn, device_type, name)

    def __call__(self, events: list[dict[str, Any]]) -> None:
        from ..api.events import store_events

        with self.database.connect() as conn, transaction(conn):
            row = conn.execute("SELECT revoked_at FROM devices WHERE device_id = ?", (self.device_id,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                self.device_id = tracker_device(conn, self.device_type, self.name)
                logger.info("the tracker's device was revoked; tracking continues as %s", self.device_id)
            parsed: list[tuple[int, Event]] = []
            for index, event in enumerate(events):
                try:
                    parsed.append((index, Event.model_validate({**event, "device_id": self.device_id})))
                except ValidationError as error:  # one odd window never blocks the rest
                    logger.warning("the tracker dropped a span the hub would refuse: %s", error.errors()[0]["msg"])
            store_events(conn, self.device_id, parsed)

    def last_seq(self) -> int:
        """The highest seq this device has used (0 for none), so a restart continues the numbering."""
        from ..api.events import last_seq

        with self.database.connect() as conn:
            return last_seq(conn, self.device_id) or 0


class AlreadyTracking(RuntimeError):
    """Another tracker already writes to this profile's database."""


@contextmanager
def single_tracker(lock_path: Path) -> Iterator[None]:
    """Only one tracker per profile: two would record every span twice. An OS file lock, released even if the
    process dies."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[str] = lock_path.open("a+")
    try:
        try:
            _lock(handle)
        except OSError:
            raise AlreadyTracking(f"another Daytrace tracker is already running for this profile ({lock_path})") from None
        yield
    finally:
        handle.close()  # closing releases the lock


def _lock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def platform_probe() -> tuple[Probe, str] | None:
    """This computer's probe and device type, or None where there is none yet (macOS arrives with DT-17)."""
    if sys.platform == "win32":
        from .windows import WindowsProbe

        return WindowsProbe(), "windows"
    return None


class TrackerService:
    """The tracker as a background thread inside the hub (or in the foreground for `daytrace-hub tracker`)."""

    def __init__(self, database: Database, probe: Probe, device_type: str, name: str, lock_path: Path,
                 redact: Redactor = no_redaction) -> None:
        self.database = database
        self.probe = probe
        self.device_type = device_type
        self.name = name
        self.lock_path = lock_path
        self.redact = redact
        self.device_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tracker(self) -> Tracker:
        sink = DatabaseSink(self.database, self.device_type, self.name)
        self.device_id = sink.device_id
        return Tracker(self.probe, sink, sink.device_id, last_seq=sink.last_seq(), redact=self.redact)

    def run_forever(self) -> None:
        """Track in this thread until stop() is called (or Ctrl+C). A database busy at the start (a seed or a
        migration holding it) is waited for, not given up on."""
        with single_tracker(self.lock_path):
            delay = 1.0
            while not self._stop.is_set():
                try:
                    tracker = self._tracker()
                except sqlite3.OperationalError as error:
                    logger.warning("the tracker is waiting for the database (%s)", error)
                    self._stop.wait(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                tracker.run(self._stop)
                return

    def start(self) -> None:
        def body() -> None:
            try:
                self.run_forever()
            except AlreadyTracking as error:
                logger.warning("%s; this hub will not track the desktop too", error)
            except Exception:  # the hub keeps serving even if tracking fails
                logger.exception("the desktop tracker stopped")

        self._thread = threading.Thread(target=body, name="daytrace-tracker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
