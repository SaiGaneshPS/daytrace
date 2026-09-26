"""DT-13: sessionizer: raw events to clean sessions.

Sessions are a pure function of the stored events: build_sessions() turns one device's events into
non-overlapping sessions for a time window, and sessions_for() loads what it needs from the database.
Nothing is stored, so sessions can never go stale after a resend, a replaced event or a revoked device; the
sessions table stays available as a cache if the stats engine (DT-38) ever needs one.

The rules, in order:
1. Spans (app_session, window, web) become intervals. iPhone app_open / app_close pairs become intervals; an
   open with no close ends at the device's next event, or after 30 minutes, and is marked estimated.
2. Desktop heartbeats (window, web) with the same app and title merge when at most 5 s apart.
3. Overlaps on one device never double count: at any moment the most recently started session owns the screen.
4. AFK periods are cut out.
5. Pieces of the same app that touch are joined again, and everything is clipped to the window, so a day
   window (local midnight to local midnight, DST aware) splits sessions at local midnight.
"""
from __future__ import annotations

import heapq
import itertools
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .db import utc_text

OPEN_WITHOUT_CLOSE = timedelta(minutes=30)
MAX_PAIRED = timedelta(hours=6)  # an open and close further apart than this is a missed close, not a session
HEARTBEAT_GAP = timedelta(seconds=5)
LOOKBACK = MAX_PAIRED  # events this far before a window can still produce sessions inside it
LOOKAHEAD = OPEN_WITHOUT_CLOSE  # an open near the end of a window needs the device's next event
SPAN_KINDS = ("app_session", "window", "web")
HEARTBEAT_KINDS = ("window", "web")


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
        """Whole seconds, so totals always equal the sum of what is shown."""
        return round((self.end - self.start).total_seconds())


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text)  # Python 3.11+ reads the trailing Z


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
    sessions: list[Session] = []
    for index, opened in enumerate(events):
        if opened.kind != "app_open":
            continue
        close: StoredEvent | None = None
        for later in events[index + 1:]:
            if later.app != opened.app:
                continue
            if later.kind == "app_open":
                break  # opened again before any close: this open was never closed
            if later.kind == "app_close":
                close = later
                break
        if close is not None and close.start - opened.start <= MAX_PAIRED:
            end, estimated, ids = close.start, False, (opened.id, close.id)
        else:
            following = next((e.start for e in events[index + 1:] if e.start > opened.start), None)
            limit = opened.start + OPEN_WITHOUT_CLOSE
            end = min(following, limit) if following is not None else limit
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
    active: list[tuple[datetime, int, int, Session]] = []  # max-heap by (start, event id) via negation
    pieces: list[tuple[datetime, datetime, int]] = []
    next_index = 0
    for left, right in itertools.pairwise(points):
        while next_index < len(ordered) and ordered[next_index].start <= left:
            session = ordered[next_index]
            rank = (session.start, max(session.event_ids, default=0))
            heapq.heappush(active, (_negate(rank[0]), -rank[1], next_index, session))
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


def _negate(moment: datetime) -> float:
    return -moment.timestamp()


# --- steps 4 and 5: AFK, joining, clipping --------------------------------------------------------------------


def subtract(sessions: Iterable[Session], gaps: Iterable[tuple[datetime, datetime]]) -> list[Session]:
    """Remove the given periods (AFK) from sessions, splitting them where needed."""
    holes = sorted(gaps)
    result: list[Session] = []
    for session in sessions:
        pieces = [(session.start, session.end)]
        for hole_start, hole_end in holes:
            if hole_end <= session.start or hole_start >= session.end:
                continue
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
    clipped = (replace(s, start=max(s.start, start), end=min(s.end, end)) for s in sessions)
    return [s for s in clipped if s.end > s.start and s.seconds > 0]


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
        result.extend(clip(cleaned, start, end))
    return result


def load_events(
    conn: sqlite3.Connection, start: datetime, end: datetime, device_ids: Sequence[str] | None = None
) -> list[StoredEvent]:
    """Every event that can affect sessions or lanes in [start, end), with the context the rules need."""
    query = (
        "SELECT id, device_id, kind, source, start_utc, end_utc, app, app_id, title, category, data FROM events"
        " WHERE start_utc < ? AND COALESCE(end_utc, start_utc) > ?"
    )
    params: list[object] = [utc_text(end + LOOKAHEAD), utc_text(start - LOOKBACK)]
    if device_ids is not None:
        query += f" AND device_id IN ({', '.join('?' for _ in device_ids)})"
        params.extend(device_ids)
    return [StoredEvent.from_row(row) for row in conn.execute(query + " ORDER BY start_utc, id", params)]


def sessions_for(
    conn: sqlite3.Connection, start: datetime, end: datetime, device_ids: Sequence[str] | None = None
) -> list[Session]:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("session windows must be timezone-aware")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    return build_sessions(load_events(conn, start, end, device_ids), start, end)
