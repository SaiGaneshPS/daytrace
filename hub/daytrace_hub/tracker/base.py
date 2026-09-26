"""DT-16: the desktop tracker's shared loop. The Windows probe (tracker/windows.py) and the macOS one (DT-17)
plug into it.

Every 2 seconds a probe reads the foreground app, its window title and how long it has been since the last
keyboard or mouse input. The loop turns readings into spans on the hub computer's own desktop device:

- The same app and title in a row extend one `window` span; a change ends it and starts the next. A title that
  changes again within 10 s (a clock or a counter in the title) relabels the span instead of splitting it.
- No input for 3 minutes is AFK: the window span ends at the last input and an `afk` span runs until input
  comes back (it starts there too). A video or presentation holding the screen on is not AFK (up to 3 hours
  without input). The lock screen is AFK straight away.
- A pause between two readings far longer than the poll (the computer slept, or the process was paused) ends
  the open span at the last reading, so sleep is never counted as screen time.
- Spans are written through the ingest path (api.events.store_events) under a stable external_id with a seq
  that only grows, so each flush replaces the hub's copy with the longer one: the timeline is a few seconds
  behind at most, and one span is one row. Titles pass through the redaction hook first (DT-44 fills it in).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
TITLE_SETTLE = timedelta(seconds=10)  # a title changing again sooner relabels the span instead of splitting it
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

    @property
    def identity(self) -> tuple[str | None, str | None, str | None]:
        return (self.app, self.app_id, self.title)


class Probe(Protocol):
    def read(self) -> Reading | None:
        """The current reading, or None when nothing can be read right now."""


Redactor = Callable[[Reading], Reading]
Sink = Callable[[list[dict[str, Any]]], None]


def no_redaction(reading: Reading) -> Reading:
    return reading


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

    @property
    def identity(self) -> tuple[str | None, str | None, str | None]:
        return (self.app, self.app_id, self.title)


class Tracker:
    """Readings in, spans out. `step()` is one poll; `run()` polls until stopped; `close()` ends what is open."""

    def __init__(
        self,
        probe: Probe,
        sink: Sink,
        device_id: str,
        *,
        first_seq: int = 0,
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
        self._seq = first_seq
        self._started = wall()
        self._run_id = int(self._started.timestamp() * 1000)
        self._numbers = 0
        self.current: Span | None = None
        self._closed: list[Span] = []
        self._written: set[int] = set()  # open spans the hub already has a copy of
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None
        self._last_flush: float | None = None

    # --- one poll --------------------------------------------------------------------------------------------

    def step(self) -> None:
        now, mono = self.wall(), self.monotonic()
        if self._last_mono is not None and mono - self._last_mono > max(SLEEP_GAP_SECONDS, 10 * self.poll_seconds):
            self._end(self._last_wall or now)  # slept: nothing happened in between
        reading = self._read()
        if reading is not None:
            self._apply(reading, now)
            self._last_wall, self._last_mono = now, mono
        if self._last_flush is None or mono - self._last_flush >= self.flush_every or self._closed:
            self.flush()
            self._last_flush = mono

    def _read(self) -> Reading | None:
        try:
            reading = self.probe.read()
            return None if reading is None else self.redact(reading)
        except Exception:  # a probe must never stop the tracker (odd windows, elevated apps)
            logger.exception("the tracker could not read the screen this time")
            return None

    def _away(self, reading: Reading) -> bool:
        if reading.locked:
            return True
        if reading.idle_seconds < self.afk_after:
            return False
        return not (reading.display_required and reading.idle_seconds < MAX_WATCHING_SECONDS)

    def _apply(self, reading: Reading, now: datetime) -> None:
        current = self.current
        # The last input, never before what is already recorded (or before this run started).
        floor = max(current.start if current else self._started, self._started)
        last_input = min(now, max(floor, now - timedelta(seconds=reading.idle_seconds)))
        if self._away(reading):
            if current is not None and current.kind == "afk":
                current.end = now
                return
            if current is not None:
                self._end(last_input)  # the window was in use until the last input
            self._begin("afk", last_input, now)
            return
        app, app_id, title = clean_text(reading.app, MAX_APP), clean_text(reading.app_id, MAX_APP), clean_text(reading.title, MAX_TITLE)
        if current is not None and current.kind == "afk":
            back = max(current.start, last_input)  # input came back at the last input
            self._end(back)
            self._begin("window", back, now, app, app_id, title)
            return
        if current is not None and current.identity == (app, app_id, title):
            current.end = now
            return
        if current is not None and (current.app, current.app_id) == (app, app_id) and now - current.start < TITLE_SETTLE:
            current.title, current.end = title, now  # the title changed again straight away: relabel, don't split
            return
        if current is not None:
            self._end(now)
        self._begin("window", now, now, app, app_id, title)

    def _begin(self, kind: str, start: datetime, end: datetime, app: str | None = None, app_id: str | None = None,
               title: str | None = None) -> None:
        self._numbers += 1
        self.current = Span(kind, start, max(start, end), self._numbers, app, app_id, title)

    def _end(self, at: datetime) -> None:
        """End the open span at `at` (never before it started)."""
        if self.current is None:
            return
        self.current.end = max(self.current.start, at)
        self._closed.append(self.current)
        self.current = None

    # --- writing ---------------------------------------------------------------------------------------------

    def flush(self) -> None:
        """Write what ended since the last flush and the span still open. Kept for the next try if it fails.

        A span that was written before is always written again, even when AFK cut it back to nothing: otherwise
        the hub would keep its older, longer copy."""
        spans = [*self._closed, *([self.current] if self.current is not None else [])]
        due = [span for span in spans if span.end > span.start or span.number in self._written]
        if not due:
            self._closed = []
            return
        try:
            self.sink([self._event(span) for span in due])
        except Exception:  # a busy database or a full disk: keep the spans and try again
            logger.exception("the tracker could not save its spans; it will try again")
            return
        self._written.update(span.number for span in due if span is self.current)
        self._closed = []

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
        """Poll until `stop` is set, then close. Each poll is scheduled from the monotonic clock (no drift)."""
        next_poll = self.monotonic()
        while not stop.is_set():
            self.step()
            next_poll += self.poll_seconds
            stop.wait(max(0.0, next_poll - self.monotonic()))
        self.close()


# --- the hub's own desktop device -----------------------------------------------------------------------------


def tracker_device(conn: sqlite3.Connection, device_type: str, name: str) -> str:
    """The hub computer's own device (no token: it writes through the ingest path directly, never over the
    network). Created once, then reused; a revoked one is replaced by a new one."""
    from ..api.devices import next_device_id

    row = conn.execute(
        "SELECT device_id FROM devices WHERE token_hash IS NULL AND device_type = ? AND revoked_at IS NULL"
        " ORDER BY paired_at LIMIT 1",
        (device_type,),
    ).fetchone()
    if row is not None:
        return str(row[0])
    with transaction(conn):
        device_id = next_device_id(conn, device_type)
        conn.execute(
            "INSERT INTO devices (device_id, name, device_type, token_hash, paired_at) VALUES (?, ?, ?, NULL, ?)",
            (device_id, name, device_type, utc_text(datetime.now(UTC))),
        )
    return device_id


class DatabaseSink:
    """Writes tracker events through the same path as events from phones (validation, dedup, replacement)."""

    def __init__(self, database: Database, device_id: str) -> None:
        self.database = database
        self.device_id = device_id

    def __call__(self, events: list[dict[str, Any]]) -> None:
        from ..api.events import store_events

        parsed: list[tuple[int, Event]] = []
        for index, event in enumerate(events):
            try:
                parsed.append((index, Event.model_validate(event)))
            except ValidationError as error:  # one odd window never blocks the rest
                logger.warning("the tracker dropped a span the hub would refuse: %s", error.errors()[0]["msg"])
        with self.database.connect() as conn, transaction(conn):
            store_events(conn, self.device_id, parsed)

    def first_seq(self) -> int:
        from ..api.events import last_seq

        with self.database.connect() as conn:
            return (last_seq(conn, self.device_id) or 0) + 1


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
        with self.database.connect() as conn:
            self.device_id = tracker_device(conn, self.device_type, self.name)
        sink = DatabaseSink(self.database, self.device_id)
        return Tracker(self.probe, sink, self.device_id, first_seq=sink.first_seq(), redact=self.redact)

    def run_forever(self) -> None:
        """Track in this thread until stop() (or Ctrl+C) is called."""
        with single_tracker(self.lock_path):
            tracker = self._tracker()
            try:
                tracker.run(self._stop)
            except KeyboardInterrupt:
                tracker.close()

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


def replace_reading(reading: Reading, **changes: Any) -> Reading:
    """For redaction rules (DT-44): the same reading with some fields changed."""
    return replace(reading, **changes)
