"""DT-13: GET /api/v1/timeline.

One day in one time zone: a lane of sessions per device, plus calendar, sleep and meal lanes. The day runs
from local midnight to local midnight in `tz`, so DST days are 23 or 25 hours long and sessions are split at
local midnight. Every number is whole seconds first; minutes are rounded from those for display only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, available_timezones

import tzlocal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..categories import Categorizer
from ..db import Database
from ..sessions import Session, StoredEvent, build_sessions, load_events, snap, with_categories
from . import API_PREFIX, ApiError

LANE_ORDER = {"windows": 0, "macos": 1, "android": 2, "ios": 3, "browser": 4, "viewer": 5}
# Browser-extension lanes show which sites were open; that time is already inside the desktop lane.
DETAIL_ONLY_TYPES = frozenset({"browser"})
# Sleep stages that are not sleep: time in bed awake, or awake in the night.
NOT_ASLEEP = frozenset({"in_bed", "awake"})
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
    category: str = Field(description="One of the categories in GET /categories; 'other' when unknown.")
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
    seconds: int = Field(description="Screen time per counted device, added up. Exactly the sum of the lanes.")
    minutes: float
    by_device_seconds: dict[str, int]
    by_device: dict[str, float] = Field(description="Minutes per counted device (rounded for display).")
    any_screen_seconds: int = Field(description="Time at least one screen was in use (overlaps counted once).")
    any_screen_minutes: float
    sleep_seconds: int = Field(description="Sleep that ended this day; overlapping stages and copies counted once.")
    sleep_minutes: float


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


@lru_cache(maxsize=1)
def known_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def local_zone_name() -> str:
    """The hub computer's IANA time zone (for example America/St_Johns), or UTC if it cannot be found."""
    try:
        name = tzlocal.get_localzone_name()
    except Exception:  # noqa: BLE001 - tzlocal raises different errors on each OS; UTC is the safe answer
        return "UTC"
    return name if name in known_zones() else "UTC"


def resolve_tz(name: str | None) -> tuple[tzinfo, str]:
    """An IANA zone such as America/Toronto; the hub computer's own zone when none is given."""
    name = local_zone_name() if name is None else name.strip()
    if name not in known_zones():  # also refuses folders such as "America", which crash ZoneInfo on Windows
        raise ApiError(400, "bad_request", f"unknown time zone {name!r}; use an IANA name such as America/Toronto")
    return ZoneInfo(name), name


def day_window(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Local midnight to the next local midnight, in UTC (23 or 25 hours long on DST days)."""
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    return start, end


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    """Seconds covered by at least one interval. With whole-second boundaries this is a whole number."""
    total = timedelta(0)
    current: tuple[datetime, datetime] | None = None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = (start, end)
        else:
            current = (current[0], max(current[1], end))
    if current is not None:
        total += current[1] - current[0]
    return round(total.total_seconds())


def current_time() -> datetime:
    return datetime.now(UTC)


def build_timeline(database: Database, day: date, tz: tzinfo, tz_name: str) -> Timeline:
    start, end = day_window(day, tz)
    until = min(end, current_time())  # nothing that has not happened yet
    with database.connect() as conn:
        events = load_events(conn, start, end)
        devices = {row["device_id"]: row for row in conn.execute("SELECT device_id, name, device_type FROM devices")}
        categorizer = Categorizer.from_db(conn)
    sessions = with_categories(build_sessions(events, start, until), categorizer) if until > start else []

    grouped: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        grouped[session.device_id].append(session)
    lanes = sorted(
        (_lane(device_id, device_sessions, devices.get(device_id), tz) for device_id, device_sessions in grouped.items()),
        key=lambda lane: (LANE_ORDER.get(lane.device_type, 99), lane.device_id),
    )

    calendar_events = [e for e in events if e.kind == "calendar_event" and e.end is not None and e.start < end and e.end > start]
    sleep_events = _unique_sleep(e for e in events if e.kind == "sleep" and e.end is not None and start < e.end <= end)
    meal_events = [e for e in events if e.kind == "meal" and start <= e.start < end]
    sleep = [_sleep_entry(e, tz) for e in sleep_events]

    counted = [lane for lane in lanes if lane.counted]
    counted_ids = {lane.device_id for lane in counted}
    total = sum(lane.seconds for lane in counted)
    any_screen = union_seconds([(s.start, s.end) for s in sessions if s.device_id in counted_ids])
    asleep = union_seconds(
        [(snap(e.start), snap(e.end)) for e in sleep_events if e.end is not None and _stage(e) not in NOT_ASLEEP]
    )
    return Timeline(
        date=day,
        tz=tz_name,
        lanes=lanes,
        calendar=_calendar(calendar_events, tz),
        sleep=sleep,
        meals=[_meal(e, tz) for e in meal_events],
        totals=Totals(
            seconds=total,
            minutes=minutes(total),
            by_device_seconds={lane.device_id: lane.seconds for lane in counted},
            by_device={lane.device_id: lane.minutes for lane in counted},
            any_screen_seconds=any_screen,
            any_screen_minutes=minutes(any_screen),
            sleep_seconds=asleep,
            sleep_minutes=minutes(asleep),
        ),
        meta=Meta(
            range=TimeRange(start=start.astimezone(tz), end=end.astimezone(tz), tz=tz_name),
            source=_source(events, sessions, [*calendar_events, *sleep_events, *meal_events], start, end),
            estimated=any(s.estimated for s in sessions) or any(entry.estimated for entry in sleep),
        ),
    )


def _lane(device_id: str, sessions: list[Session], device: object, tz: tzinfo) -> Lane:
    device_type = device["device_type"] if device else "unknown"  # type: ignore[index]
    shown = [_shown(s, tz) for s in sessions]
    seconds = sum(s.seconds for s in shown)
    return Lane(
        device_id=device_id,
        device_type=device_type,
        name=device["name"] if device else device_id,  # type: ignore[index]
        counted=device_type not in DETAIL_ONLY_TYPES,
        seconds=seconds,
        minutes=minutes(seconds),
        sessions=shown,
    )


def _source(
    events: list[StoredEvent], sessions: list[Session], lane_events: list[StoredEvent], start: datetime, end: datetime
) -> Literal["real", "seed", "mixed"]:
    """seed, real or mixed, over every event that shaped the day: sessions, AFK cuts and the lanes."""
    by_id = {e.id: e for e in events}
    used = {i for s in sessions for i in s.event_ids} | {e.id for e in lane_events}
    used |= {e.id for e in events if e.kind == "afk" and e.end is not None and e.start < end and e.end > start}
    sources = {by_id[i].source for i in used if i in by_id}
    return "seed" if sources == {"seed"} else "mixed" if "seed" in sources else "real"


def _shown(session: Session, tz: tzinfo) -> TimelineSession:
    return TimelineSession(
        start=session.start.astimezone(tz),
        end=session.end.astimezone(tz),
        seconds=session.seconds,
        minutes=minutes(session.seconds),
        app=session.app,
        app_id=session.app_id,
        title=session.title,
        category=session.category or "other",
        kind=session.kind,  # type: ignore[arg-type]
        estimated=session.estimated,
    )


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _stage(e: StoredEvent) -> str | None:
    return _text(e.data, "stage")


def _unique_sleep(events: object) -> list[StoredEvent]:
    """The same night synced from two phones is listed once."""
    seen: set[tuple[datetime, datetime | None, str | None]] = set()
    unique: list[StoredEvent] = []
    for e in events:  # type: ignore[attr-defined]
        key = (e.start, e.end, _stage(e))
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return sorted(unique, key=lambda e: e.start)


def _sleep_entry(e: StoredEvent, tz: tzinfo) -> SleepEntry:
    assert e.end is not None
    return SleepEntry(
        start=e.start.astimezone(tz),
        end=e.end.astimezone(tz),
        minutes=minutes((e.end - e.start).total_seconds()),
        stage=_stage(e),
        estimated=e.data.get("measured") is False,
        device_id=e.device_id,
    )


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


@router.get("/timeline", response_model=Timeline, summary="One day's lanes: devices, calendar, sleep, meals")
def get_timeline(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    day: Annotated[date | None, Query(alias="date", description="YYYY-MM-DD; default: today in tz")] = None,
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> Timeline:
    zone, zone_name = resolve_tz(tz)
    day = day or current_time().astimezone(zone).date()
    if not EARLIEST <= day <= LATEST:
        raise ApiError(400, "bad_request", f"date must be between {EARLIEST} and {LATEST}")
    return build_timeline(database, day, zone, zone_name)
