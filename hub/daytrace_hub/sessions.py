"""DT-13: sessionizer: raw events to clean sessions.

Sessions are a pure function of the stored events: build_sessions() turns events into non-overlapping
sessions per device for a time window, and sessions_for() loads what it needs from the database. Nothing is
stored, so sessions can never go stale after a resend, a replaced event or a revoked device; the sessions
table stays available as a cache if the stats engine (DT-38) needs one.

The rules, in order:
1. Spans (app_session, window, web) become intervals. iPhone app_open / app_close pairs become intervals and
   end early if the screen turned off first. An open with no close ends at the device's next screen event
   (another open or close, screen on or off), or after 30 minutes, and is marked estimated.
2. Desktop heartbeats (window, web) with the same app and title merge when at most 5 s apart.
3. Overlaps on one device never double count: at any moment the most recently started session owns the screen.
4. AFK periods are cut out.
5. Pieces of the same app that touch are joined again, everything is clipped to the window (a day window,
   local midnight to local midnight, splits sessions at local midnight), and boundaries are snapped to whole
   seconds, so every total built from sessions adds up exactly.
"""
from __future__ import annotations

import bisect
import heapq
import itertools
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .categories import Categorizer
from .db import utc_text

OPEN_WITHOUT_CLOSE = timedelta(minutes=30)
MAX_PAIRED = timedelta(hours=6)  # an open and close further apart than this is a missed close, not a session
HEARTBEAT_GAP = timedelta(seconds=5)
SPAN_KINDS = ("app_session", "window", "web")
HEARTBEAT_KINDS = ("window", "web")
# Events that say something about what is on the screen. Only these can end an iPhone open with no close;
# a calendar entry or a steps total arriving from the same phone says nothing about the app.
SCREEN_POINT_KINDS = ("app_open", "app_close", "screen_on", "screen_off")
SCREEN_SPAN_KINDS = (*SPAN_KINDS, "afk")
SECOND = timedelta(seconds=1)


@dataclass(frozen=True)
class StoredEvent:
    """One row of the events table, with times parsed."""

    id: int
    device_id: str
    kind: str
    source: str
    start: datetime
    end: datetime | None
    app: str | None
    app_id: str | None
    title: str | None
    category: str | None
    data: dict[str, object]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> StoredEvent:
        return cls(
            id=row["id"],
            device_id=row["device_id"],
            kind=row["kind"],
            source=row["source"],
            start=parse_utc(row["start_utc"]),
            end=parse_utc(row["end_utc"]) if row["end_utc"] is not None else None,
            app=row["app"],
            app_id=row["app_id"],
            title=row["title"],
            category=row["category"],
            data=json.loads(row["data"]),
        )


@dataclass(frozen=True)
class Session:
    device_id: str
    start: datetime
    end: datetime
    app: str | None
    app_id: str | None
    title: str | None
    category: str | None
    kind: str  # "app" or "web"
    estimated: bool = False  # the end was inferred (an iPhone open with no close)
    event_ids: tuple[int, ...] = field(default=(), compare=False)

    @property
    def identity(self) -> tuple[str | None, str | None, str | None, str]:
        return (self.app, self.app_id, self.title, self.kind)

    @property
    def seconds(self) -> int:
        """Whole seconds (boundaries are snapped to whole seconds by build_sessions)."""
        return round((self.end - self.start).total_seconds())


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text)  # Python 3.11+ reads the trailing Z


def snap(moment: datetime) -> datetime:
    """The nearest whole second. Rounding is monotonic, so sessions that did not overlap still do not."""
    whole = moment.replace(microsecond=0)
    return whole + SECOND if moment.microsecond >= 500_000 else whole


# --- step 1: events to intervals ------------------------------------------------------------------------------


def _span_session(event: StoredEvent) -> Session:
    assert event.end is not None
    is_web = event.kind == "web"
    domain = event.data.get("domain") if is_web else None
    return Session(
        device_id=event.device_id,
        start=event.start,
        end=event.end,
        app=str(domain) if is_web and domain is not None else event.app,
        app_id=event.app_id,
        title=event.title,
        category=event.category,
        kind="web" if is_web else "app",
        event_ids=(event.id,),
    )


def _paired_sessions(events: Sequence[StoredEvent]) -> list[Session]:
    """iPhone Shortcuts send app_open and app_close; pair them per app (events sorted by time)."""
    screen = [e for e in events if e.kind in SCREEN_POINT_KINDS]
    boundary_times = [e.start for e in screen]
    screen_off_times = [e.start for e in screen if e.kind == "screen_off"]
    by_app: dict[str | None, list[StoredEvent]] = defaultdict(list)
    for event in screen:
        if event.kind in ("app_open", "app_close"):
            by_app[event.app].append(event)

    sessions: list[Session] = []
    for app_events in by_app.values():
        for position, opened in enumerate(app_events):
            if opened.kind != "app_open":
                continue
            following = app_events[position + 1] if position + 1 < len(app_events) else None
            close = following if following is not None and following.kind == "app_close" else None
            off_index = bisect.bisect_right(screen_off_times, opened.start)
            screen_off = screen_off_times[off_index] if off_index < len(screen_off_times) else None
            if close is not None and close.start - opened.start <= MAX_PAIRED:
                end, estimated, ids = close.start, False, (opened.id, close.id)
                if screen_off is not None and screen_off < end:
                    end = screen_off  # the screen went dark before the close fired
            else:
                next_index = bisect.bisect_right(boundary_times, opened.start)
                limit = opened.start + OPEN_WITHOUT_CLOSE
                end = min(boundary_times[next_index], limit) if next_index < len(boundary_times) else limit
                estimated, ids = True, (opened.id,)
            if end > opened.start:
                sessions.append(
                    Session(
                        device_id=opened.device_id,
                        start=opened.start,
                        end=end,
                        app=opened.app,
                        app_id=opened.app_id,
                        title=opened.title,
                        category=opened.category,
                        kind="app",
                        estimated=estimated,
                        event_ids=ids,
                    )
                )
    return sessions


# --- step 2: heartbeats ---------------------------------------------------------------------------------------


def merge_heartbeats(sessions: Iterable[Session], gap: timedelta = HEARTBEAT_GAP) -> list[Session]:
    """Join consecutive readings of the same app and title that are at most `gap` apart."""
    merged: list[Session] = []
    for session in sorted(sessions, key=lambda s: (s.start, s.end)):
        last = merged[-1] if merged else None
        if last is not None and last.identity == session.identity and session.start - last.end <= gap:
            merged[-1] = replace(
                last,
                end=max(last.end, session.end),
                estimated=last.estimated or session.estimated,
                event_ids=last.event_ids + session.event_ids,
            )
        else:
            merged.append(session)
    return merged


# --- step 3: overlaps -----------------------------------------------------------------------------------------


def resolve_overlaps(sessions: Sequence[Session]) -> list[Session]:
    """Make one device's sessions non-overlapping: at any moment the most recently started one owns the time.

    (Ties go to the one from the later event.) An app that was covered by a newer one and is still running
    afterwards continues after it, so no time is lost and none is counted twice.
    """
    if not sessions:
        return []
    ordered = sorted(sessions, key=lambda s: (s.start, max(s.event_ids, default=0)))
    points = sorted({s.start for s in ordered} | {s.end for s in ordered})
    active: list[tuple[float, int, int, Session]] = []  # min-heap on (-start, -event id): newest on top
    pieces: list[tuple[datetime, datetime, int]] = []
    next_index = 0
    for left, right in itertools.pairwise(points):
        while next_index < len(ordered) and ordered[next_index].start <= left:
            session = ordered[next_index]
            heapq.heappush(active, (-session.start.timestamp(), -max(session.event_ids, default=0), next_index, session))
            next_index += 1
        while active and active[0][3].end <= left:
            heapq.heappop(active)
        if active:
            owner = active[0][2]
            if pieces and pieces[-1][2] == owner and pieces[-1][1] == left:
                pieces[-1] = (pieces[-1][0], right, owner)
            else:
                pieces.append((left, right, owner))
    return [replace(ordered[owner], start=start, end=end) for start, end, owner in pieces]


# --- steps 4 and 5: AFK, joining, clipping --------------------------------------------------------------------


def subtract(sessions: Iterable[Session], gaps: Iterable[tuple[datetime, datetime]]) -> list[Session]:
    """Remove the given periods (AFK) from sessions, splitting them where needed."""
    holes = sorted(gaps)
    hole_ends = list(itertools.accumulate((end for _, end in holes), max))  # running max, for bisect
    result: list[Session] = []
    for session in sessions:
        pieces = [(session.start, session.end)]
        first = bisect.bisect_right(hole_ends, session.start)  # earlier holes all end before this session
        for hole_start, hole_end in holes[first:]:
            if hole_start >= session.end:
                break
            next_pieces = []
            for start, end in pieces:
                if hole_start > start:
                    next_pieces.append((start, min(end, hole_start)))
                if hole_end < end:
                    next_pieces.append((max(start, hole_end), end))
            pieces = [(s, e) for s, e in next_pieces if e > s]
        result.extend(replace(session, start=start, end=end) for start, end in pieces)
    return result


def join_touching(sessions: Iterable[Session]) -> list[Session]:
    """Pieces of the same app that touch exactly (split by an overlap or a cut) become one session again."""
    return merge_heartbeats(sessions, gap=timedelta(0))


def clip(sessions: Iterable[Session], start: datetime, end: datetime) -> list[Session]:
    """Clip to the window and snap to whole seconds; slivers under half a second disappear."""
    clipped = (replace(s, start=snap(max(s.start, start)), end=snap(min(s.end, end))) for s in sessions)
    return [s for s in clipped if s.end > s.start]


# --- all steps ------------------------------------------------------------------------------------------------


def build_sessions(events: Iterable[StoredEvent], start: datetime, end: datetime) -> list[Session]:
    """Clean, non-overlapping sessions per device within [start, end), sorted by device and time."""
    by_device: dict[str, list[StoredEvent]] = defaultdict(list)
    for event in events:
        by_device[event.device_id].append(event)
    result: list[Session] = []
    for device_id in sorted(by_device):
        device_events = sorted(by_device[device_id], key=lambda e: (e.start, e.id))
        spans = [e for e in device_events if e.kind in SPAN_KINDS and e.end is not None]
        heartbeats = merge_heartbeats(_span_session(e) for e in spans if e.kind in HEARTBEAT_KINDS)
        exact = [_span_session(e) for e in spans if e.kind not in HEARTBEAT_KINDS]  # usage stats are exact
        intervals = [*heartbeats, *exact, *_paired_sessions(device_events)]
        afk = [(e.start, e.end) for e in device_events if e.kind == "afk" and e.end is not None]
        cleaned = join_touching(subtract(resolve_overlaps(intervals), afk))
        result.extend(join_touching(clip(cleaned, start, end)))
    return result


_COLUMNS = "id, device_id, kind, source, start_utc, end_utc, app, app_id, title, category, data"


def load_events(
    conn: sqlite3.Connection, start: datetime, end: datetime, device_ids: Sequence[str] | None = None
) -> list[StoredEvent]:
    """Every event that can affect sessions or lanes in [start, end), with the context the rules need.

    Two index-friendly queries: spans that overlap the window (events_by_end), and point events from up to
    6 hours before (an open whose close lands in the window) to 6 hours after (a close for an open inside it).
    """
    device_filter, device_params = "", []
    if device_ids is not None:
        device_filter = f" AND device_id IN ({', '.join('?' for _ in device_ids)})"
        device_params = list(device_ids)
    # INDEXED BY: without statistics SQLite may pick the start index for spans and scan all history.
    spans = conn.execute(
        f"SELECT {_COLUMNS} FROM events INDEXED BY events_by_end WHERE end_utc > ? AND start_utc < ?{device_filter}",
        [utc_text(start - HEARTBEAT_GAP), utc_text(end + HEARTBEAT_GAP), *device_params],
    ).fetchall()
    points = conn.execute(
        f"SELECT {_COLUMNS} FROM events INDEXED BY events_by_start"
        f" WHERE start_utc >= ? AND start_utc < ? AND end_utc IS NULL{device_filter}",
        [utc_text(start - MAX_PAIRED), utc_text(end + MAX_PAIRED), *device_params],
    ).fetchall()
    events = [StoredEvent.from_row(row) for row in (*spans, *points)]
    return sorted(events, key=lambda e: (e.start, e.id))


def with_categories(sessions: Iterable[Session], categorizer: Categorizer) -> list[Session]:
    """Sessions with their resolved category (user override, collector hint, built-in list, AI, other)."""
    return [
        replace(s, category=categorizer.category(s.app, s.app_id, s.kind, s.category)) for s in sessions
    ]


def sessions_for(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    device_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> list[Session]:
    """Categorized sessions in [start, end). Nothing after `now` (default: the current time) is counted.

    This is the one entry point for everything built on sessions (timeline, stats, goals, insights), so they
    all agree on the numbers and the categories.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("session windows must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    until = min(end, now or datetime.now(UTC))
    if until <= start:
        return []
    sessions = build_sessions(load_events(conn, start, end, device_ids), start, until)
    return with_categories(sessions, Categorizer.from_db(conn))
