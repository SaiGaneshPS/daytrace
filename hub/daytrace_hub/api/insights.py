"""DT-41: insights and Wrapped endpoints. DT-31 starts it with GET /insights/day, the numbers behind the Today tab.

Every number comes from the stats engine (stats.py), so the dashboard never works one out itself and every page
agrees: the timeline's lanes, the hero cards and the story all count the same pieces.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..db import Database
from ..stats import DESK_TYPES, PHONE_TYPES, Stats
from . import API_PREFIX, ApiError
from .timeline import EARLIEST, LATEST, current_time, minutes, resolve_tz

router = APIRouter(prefix=API_PREFIX, tags=["insights"])
TOP_APPS = 10


class AppMinutes(BaseModel):
    app: str
    category: str = Field(description="The category with the most of this app's time that day.")
    minutes: float


class DaySummary(BaseModel):
    date: date
    tz: str
    in_progress: bool = Field(description="True while the day is not over: the numbers are the day so far.")
    screen_minutes: float | None = Field(description="Screen time, per device and added up (as the timeline); null without screen data.")
    phone_minutes: float | None
    computer_minutes: float | None
    focused_minutes: float | None = Field(description="Work or study in blocks of 10+ minutes with no phone distraction.")
    focus_score: int | None = Field(description="0 to 100; null on a day with neither work nor distraction.")
    pickups: int | None
    switches_per_hour: float | None
    sleep_minutes: float | None = Field(description="Last night's sleep (the night ending this morning).")
    sleep_estimated: bool
    steps: int | None = Field(description="The day's steps; the largest total when two phones sent one.")
    top_apps: list[AppMinutes] = Field(description=f"Up to {TOP_APPS} apps and sites with the most time, most first.")
    estimated: bool = Field(description="True when any of this was inferred (an iPhone app without a close, a guessed night).")


def day_steps(stats: Stats, day: date) -> int | None:
    """The day's steps: per device, the step counts that started this day added up; the largest device total wins
    (two phones syncing one Health account send the same steps twice)."""
    window = stats.day(day)
    by_device: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, object, object]] = set()
    for event in window.events:
        if event.kind != "steps" or not window.start <= event.start < window.end:
            continue
        key = (event.device_id, event.start, event.end)
        if key in seen:
            continue
        seen.add(key)
        count = event.data.get("count")
        if isinstance(count, int) and count >= 0:
            by_device[event.device_id] += count
    return max(by_device.values()) if by_device else None


def top_apps(stats: Stats, day: date) -> list[AppMinutes]:
    """The apps and sites with the most time, from the same pieces totals(group_by="app") counts."""
    seconds: dict[str, int] = defaultdict(int)
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for piece in stats.day(day).pieces:
        key = piece.app or piece.app_id or "unknown"
        seconds[key] += piece.seconds
        by_category[key][piece.category or "other"] += piece.seconds
    ranked = sorted(seconds.items(), key=lambda item: (-item[1], item[0]))[:TOP_APPS]
    return [
        AppMinutes(app=key, category=max(by_category[key].items(), key=lambda item: (item[1], item[0]))[0],
                   minutes=minutes(total))
        for key, total in ranked if total > 0
    ]


def summarize(stats: Stats, day: date, tz_name: str) -> DaySummary:
    iso = day.isoformat()
    totals = stats.totals(day)
    has_screen = iso not in totals["missing_days"] and stats.day(day).until > stats.day(day).start
    phones = stats.totals(day, device_types=PHONE_TYPES)
    desks = stats.totals(day, device_types=DESK_TYPES)
    focus, score = stats.focused_minutes(day), stats.focus_score(day)
    pickups, switches = stats.pickups(day), stats.switches_per_hour(day)
    sleep = stats.sleep_estimate(day)
    return DaySummary(
        date=day,
        tz=tz_name,
        in_progress=not stats.day(day).over,
        screen_minutes=totals["total_minutes"] if has_screen else None,
        phone_minutes=phones["total_minutes"] if has_screen else None,
        computer_minutes=desks["total_minutes"] if has_screen else None,
        focused_minutes=focus["value"],
        focus_score=score["value"],
        pickups=pickups["value"],
        switches_per_hour=switches["value"],
        sleep_minutes=sleep["value"],
        sleep_estimated=bool(sleep["value"] is not None and not sleep.get("measured", False)),
        steps=day_steps(stats, day),
        top_apps=top_apps(stats, day) if has_screen else [],
        estimated=bool(totals["estimated"] or (sleep["value"] is not None and not sleep.get("measured", False))),
    )


@router.get("/insights/day", response_model=DaySummary, summary="One day's numbers for the Today tab")
def day_summary(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    day: Annotated[date | None, Query(alias="date", description="YYYY-MM-DD; default: today in tz")] = None,
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> DaySummary:
    zone, zone_name = resolve_tz(tz)
    now = current_time()
    day = day or now.astimezone(zone).date()
    if not EARLIEST <= day <= LATEST:
        raise ApiError(400, "bad_request", f"date must be between {EARLIEST} and {LATEST}")
    with database.connect() as conn:
        return summarize(Stats(conn, zone, zone_name, now), day, zone_name)
