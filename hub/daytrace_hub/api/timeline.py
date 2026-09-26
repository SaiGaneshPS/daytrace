"""DT-13: GET /api/v1/timeline.

One day in one time zone: a lane of sessions per device, plus calendar, sleep and meal lanes. The day runs
from local midnight to local midnight in `tz`, so DST days are 23 or 25 hours long and sessions are split at
local midnight. Every number is in whole seconds first; minutes are rounded from those for display.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..db import Database
from ..sessions import Session, StoredEvent, build_sessions, load_events
from . import API_PREFIX, ApiError

LANE_ORDER = {"windows": 0, "macos": 1, "android": 2, "ios": 3, "browser": 4, "viewer": 5}
# Browser-extension lanes show which sites were open; that time is already inside the desktop lane.
DETAIL_ONLY_TYPES = frozenset({"browser"})
EARLIEST, LATEST = date(1970, 1, 1), date(9998, 12, 31)

router = APIRouter(prefix=API_PREFIX, tags=["timeline"])


class TimelineSession(BaseModel):
    start: datetime
    end: datetime
    seconds: int
    minutes: float
    app: str | None
    app_id: str | None
    title: str | None
    category: str | None
    kind: Literal["app", "web"]
    estimated: bool = Field(description="True when the end was inferred (an iPhone open with no close).")


class Lane(BaseModel):
    device_id: str
    device_type: str
    name: str
    counted: bool = Field(description="False for browser-extension lanes, whose time is inside the desktop lane.")
    seconds: int
    minutes: float
    sessions: list[TimelineSession]


class CalendarEntry(BaseModel):
    start: datetime
    end: datetime
    title: str | None
    all_day: bool
    device_id: str


class SleepEntry(BaseModel):
    start: datetime
    end: datetime
    minutes: float
    stage: str | None
    estimated: bool
    device_id: str


class MealEntry(BaseModel):
    time: datetime
    items: list[str] | None
    text: str | None
    meal_type: str | None
    device_id: str


class Totals(BaseModel):
    seconds: int = Field(description="Sum of the counted lanes' seconds (screen time per device, added up).")
    minutes: float
    by_device: dict[str, float] = Field(description="Minutes per counted device.")
    any_screen_seconds: int = Field(description="Time at least one screen was in use (overlaps counted once).")
    any_screen_minutes: float


class TimeRange(BaseModel):
    start: datetime
    end: datetime
    tz: str


class Meta(BaseModel):
    unit: Literal["minutes"] = "minutes"
    range: TimeRange
    source: Literal["real", "seed", "mixed"]
    estimated: bool


class Timeline(BaseModel):
    date: date
    tz: str
    lanes: list[Lane]
    calendar: list[CalendarEntry]
    sleep: list[SleepEntry]
    meals: list[MealEntry]
    totals: Totals
    meta: Meta


def minutes(seconds: float) -> float:
    return round(seconds / 60, 2)


def resolve_tz(name: str | None) -> tuple[tzinfo, str]:
    """An IANA zone such as America/Toronto, or the hub computer's current offset when none is given."""
    if name is None:
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        return local, str(local)
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(400, "bad_request", f"unknown time zone {name!r}; use an IANA name such as America/Toronto") from None


def day_window(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Local midnight to the next local midnight, in UTC (23 or 25 hours long on DST days)."""
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return start, end


def union_seconds(sessions: list[Session]) -> int:
    """Seconds covered by at least one session (sessions from different devices may overlap)."""
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for session in sorted(sessions, key=lambda s: s.start):
        if current_end is None or session.start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = session.start, session.end
        else:
            current_end = max(current_end, session.end)
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return round(total)


def build_timeline(database: Database, day: date, tz: tzinfo, tz_name: str) -> Timeline:
    start, end = day_window(day, tz)
    with database.connect() as conn:
        events = load_events(conn, start, end)
        devices = {row["device_id"]: row for row in conn.execute("SELECT device_id, name, device_type FROM devices")}
    sessions = build_sessions(events, start, end)
    by_id = {event.id: event for event in events}

    grouped: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        grouped[session.device_id].append(session)
    lanes: list[Lane] = []
    for device_id, device_sessions in grouped.items():
        device = devices.get(device_id)
        device_type = device["device_type"] if device else "unknown"
        shown = [_shown(s, tz) for s in device_sessions]
        seconds = sum(s.seconds for s in shown)
        lanes.append(
            Lane(
                device_id=device_id,
                device_type=device_type,
                name=device["name"] if device else device_id,
                counted=device_type not in DETAIL_ONLY_TYPES,
                seconds=seconds,
                minutes=minutes(seconds),
                sessions=shown,
            )
        )
    lanes.sort(key=lambda lane: (LANE_ORDER.get(lane.device_type, 99), lane.device_id))

    calendar = _calendar([e for e in events if _on_calendar(e, start, end)], tz)
    sleep = _sleep([e for e in events if _slept(e, start, end)], tz)
    meals = [_meal(e, tz) for e in events if _ate(e, start, end)]

    counted = [lane for lane in lanes if lane.counted]
    counted_ids = {lane.device_id for lane in counted}
    total = sum(lane.seconds for lane in counted)
    any_screen = union_seconds([s for s in sessions if s.device_id in counted_ids])

    shown_lane_events = {e.id for e in events if _on_calendar(e, start, end) or _slept(e, start, end) or _ate(e, start, end)}
    used = {i for s in sessions for i in s.event_ids} | shown_lane_events
    sources = {by_id[i].source for i in used if i in by_id}
    source: Literal["real", "seed", "mixed"] = (
        "seed" if sources == {"seed"} else "mixed" if "seed" in sources else "real"
    )
    return Timeline(
        date=day,
        tz=tz_name,
        lanes=lanes,
        calendar=calendar,
        sleep=sleep,
        meals=meals,
        totals=Totals(
            seconds=total,
            minutes=minutes(total),
            by_device={lane.device_id: lane.minutes for lane in counted},
            any_screen_seconds=any_screen,
            any_screen_minutes=minutes(any_screen),
        ),
        meta=Meta(
            range=TimeRange(start=start.astimezone(tz), end=end.astimezone(tz), tz=tz_name),
            source=source,
            estimated=any(s.estimated for s in sessions) or any(entry.estimated for entry in sleep),
        ),
    )


def _shown(session: Session, tz: tzinfo) -> TimelineSession:
    return TimelineSession(
        start=session.start.astimezone(tz),
        end=session.end.astimezone(tz),
        seconds=session.seconds,
        minutes=minutes(session.seconds),
        app=session.app,
        app_id=session.app_id,
        title=session.title,
        category=session.category,
        kind=session.kind,  # type: ignore[arg-type]
        estimated=session.estimated,
    )


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _on_calendar(e: StoredEvent, start: datetime, end: datetime) -> bool:
    """Calendar events that overlap the day."""
    return e.kind == "calendar_event" and e.end is not None and e.start < end and e.end > start


def _slept(e: StoredEvent, start: datetime, end: datetime) -> bool:
    """Sleep that ended on this day: "last night's sleep" belongs to the morning, at full length."""
    return e.kind == "sleep" and e.end is not None and start < e.end <= end


def _ate(e: StoredEvent, start: datetime, end: datetime) -> bool:
    return e.kind == "meal" and start <= e.start < end


def _meal(e: StoredEvent, tz: tzinfo) -> MealEntry:
    items = e.data.get("items")
    return MealEntry(
        time=e.start.astimezone(tz),
        items=[str(item) for item in items] if isinstance(items, list) else None,
        text=_text(e.data, "text"),
        meal_type=_text(e.data, "meal_type"),
        device_id=e.device_id,
    )


def _calendar(events: list[StoredEvent], tz: tzinfo) -> list[CalendarEntry]:
    """The same event synced to two phones is listed once."""
    seen: set[tuple[str | None, datetime, datetime]] = set()
    entries: list[CalendarEntry] = []
    for e in events:
        assert e.end is not None
        key = (e.title, e.start, e.end)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            CalendarEntry(
                start=e.start.astimezone(tz),
                end=e.end.astimezone(tz),
                title=e.title,
                all_day=e.data.get("all_day") is True,
                device_id=e.device_id,
            )
        )
    return sorted(entries, key=lambda entry: entry.start)


def _sleep(events: list[StoredEvent], tz: tzinfo) -> list[SleepEntry]:
    entries = []
    for e in events:
        assert e.end is not None
        entries.append(
            SleepEntry(
                start=e.start.astimezone(tz),
                end=e.end.astimezone(tz),
                minutes=minutes((e.end - e.start).total_seconds()),
                stage=_text(e.data, "stage"),
                estimated=e.data.get("measured") is False,
                device_id=e.device_id,
            )
        )
    return sorted(entries, key=lambda entry: entry.start)


@router.get("/timeline", response_model=Timeline, summary="One day's lanes: devices, calendar, sleep, meals")
def get_timeline(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    day: Annotated[date | None, Query(alias="date", description="YYYY-MM-DD; default: today in tz")] = None,
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto")] = None,
) -> Timeline:
    zone, zone_name = resolve_tz(tz)
    day = day or datetime.now(zone).date()
    if not EARLIEST <= day <= LATEST:
        raise ApiError(400, "bad_request", f"date must be between {EARLIEST} and {LATEST}")
    return build_timeline(database, day, zone, zone_name)
