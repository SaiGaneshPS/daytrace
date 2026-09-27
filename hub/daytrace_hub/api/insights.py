"""DT-41: insights and Wrapped endpoints. DT-31 starts it with GET /insights/day, the numbers behind the Today tab.

Every number comes from the stats engine (stats.py), so the dashboard never works one out itself and every page
agrees: the timeline's lanes, the hero cards and the story all count the same pieces.
"""
from __future__ import annotations

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
    in_progress: bool = Field(description="True while the day is going (it has begun and not ended): the numbers so far.")
    screen_minutes: float | None = Field(description="Screen time, per device and added up (as the timeline); null without screen data.")
    screen_estimated: bool = Field(description="True when some screen time was inferred (an iPhone app without a close).")
    phone_minutes: float | None
    computer_minutes: float | None
    focused_minutes: float | None = Field(description="Work or study in blocks of 10+ minutes with no phone distraction.")
    focus_score: int | None = Field(description="0 to 100; null on a day with neither work nor distraction.")
    work_or_study_minutes: float | None = Field(description="Time in work or study apps and sites, any device (overlaps once); focused time is part of it.")
    distracted_minutes: float | None = Field(description="Time in social, video or game apps and sites, any device (overlaps once).")
    pickups: int | None
    switches_per_hour: float | None
    sleep_minutes: float | None = Field(description="Last night's sleep (the night ending this morning).")
    sleep_estimated: bool
    steps: int | None = Field(description="The day's steps; the largest total when two phones sent one.")
    top_apps: list[AppMinutes] = Field(description=f"Up to {TOP_APPS} apps and sites with the most time, most first.")
    estimated: bool = Field(description="True when any of this was inferred (an iPhone app without a close, a guessed night).")


def summarize(stats: Stats, day: date, tz_name: str) -> DaySummary:
    iso = day.isoformat()
    totals = stats.totals(day)
    has_screen = iso not in totals["missing_days"] and stats.day(day).until > stats.day(day).start
    phones = stats.totals(day, device_types=PHONE_TYPES)
    desks = stats.totals(day, device_types=DESK_TYPES)
    focus, score = stats.focused_minutes(day), stats.focus_score(day)
    pickups, switches = stats.pickups(day), stats.switches_per_hour(day)
    sleep = stats.sleep_estimate(day)
    window = stats.day(day)
    return DaySummary(
        date=day,
        tz=tz_name,
        in_progress=window.until > window.start and not window.over,  # not a day that hasn't begun
        screen_minutes=totals["total_minutes"] if has_screen else None,
        screen_estimated=bool(totals["estimated"]),
        phone_minutes=phones["total_minutes"] if has_screen else None,
        computer_minutes=desks["total_minutes"] if has_screen else None,
        focused_minutes=focus["value"],
        focus_score=score["value"],
        # The focus score's own parts, so a chart of focus against distraction shows the numbers behind the score.
        work_or_study_minutes=minutes(score["work_or_study_seconds"]) if "work_or_study_seconds" in score else None,
        distracted_minutes=minutes(score["distracted_seconds"]) if "distracted_seconds" in score else None,
        pickups=pickups["value"],
        switches_per_hour=switches["value"],
        sleep_minutes=sleep["value"],
        sleep_estimated=bool(sleep["value"] is not None and not sleep.get("measured", False)),
        steps=stats.steps(day),
        top_apps=[AppMinutes(**app) for app in stats.top_apps(day, TOP_APPS)] if has_screen else [],
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
