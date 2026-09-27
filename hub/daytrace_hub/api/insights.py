"""DT-41: insights and Wrapped endpoints. DT-31 starts it with GET /insights/day, the numbers behind the Today tab;
DT-41 adds GET /insights/{tab} for the Insights tabs and GET /wrapped for the week in review.

Every number comes from the stats engine (stats.py), so the dashboard never works one out itself and every page
agrees: the timeline's lanes, the hero cards, the story and the charts all count the same pieces.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from functools import cached_property
from time import monotonic
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..db import Database, data_version
from ..llm import LLM
from ..stats import (
    DESK_TYPES,
    DISTRACTING,
    FOCUS_JOIN,
    PHONE_TYPES,
    PRODUCTIVE,
    GroupBy,
    Stats,
    app_key,
    merge,
)
from ..story import week_facts, week_wrapped
from . import API_PREFIX, ApiError
from .ai import FactOut, get_llm
from .streaks import WeekStreakOut, week_streak_highlights
from .timeline import EARLIEST, LATEST, Meta, current_time, minutes, resolve_tz

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
        top_apps=[AppMinutes(**app) for app in stats.top_apps(day, limit=TOP_APPS)] if has_screen else [],
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


# --- the Insights tabs (DT-41) -----------------------------------------------------------------------------------
#
# GET /insights/{tab}?range= gives each Insights tab its metrics (a number with its unit and a line saying what it
# means) and named series shaped for the charts (ECharts): trend and stacked lines, bars, a donut, a treemap, a
# heatmap, a Sankey, a scatter and a gauge. Everything comes from the stats engine's own splits (totals and
# crosstab), so a tab's charts add up to its totals, and they agree with Today and with each other (DT-59 checks).

Tab = Literal["overview", "apps", "devices", "focus", "sleep", "food", "calendar"]
SeriesKind = Literal["trend", "stacked", "bars", "donut", "treemap", "heatmap", "sankey", "scatter", "gauge", "leaderboard", "strip"]
MAX_RANGE_DAYS = 92
LIVE_CACHE_SECONDS = 60.0  # a range with today in it changes as time passes: worked out again this often
CACHE_SIZE = 64
TREEMAP_APPS = 8  # apps named inside each category; the rest are one "Other apps" box
COMPARE_MIN_DAYS = 4  # whole days with data each side needs before a change is shown (half a range under 8 days)
TOP_APP_BARS = 15
LEADERS = 10  # apps on the leaderboard
SPARK_DAYS = 7  # each leader's sparkline: the week up to the range's last day
HANDOFF_LINKS = 12  # the most common switches between devices drawn in the Sankey
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOURS = [f"{hour:02d}" for hour in range(24)]
MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack", "other"]
PLAN_PARTS = [("on_plan", "On plan"), ("off_plan", "Off plan"), ("other", "Other screen time"), ("idle", "No screen")]


class Metric(BaseModel):
    id: str
    label: str
    value: float | int | str | None = Field(description="null when there is no data for it in the range (missing, not zero)")
    unit: str
    explain: str
    estimated: bool = False


class Line(BaseModel):
    name: str
    key: str | None = Field(default=None, description="The id behind the name (a device id, a category).")
    category: str | None = None
    device_type: str | None = Field(default=None, description="A device's line: its kind (windows, android...).")
    values: list[float | None] = Field(description="One per x label; null where there is no data (not zero).")


class Change(BaseModel):
    """One number against the same number of days just before the range (DT-34's week-over-week chips)."""

    id: str
    label: str
    unit: str
    now: float
    before: float
    delta: float = Field(description="now minus before, in the unit.")
    change_pct: int | None = Field(description="The change as a percent of before; null when before is 0.")
    direction: Literal["up", "down", "same"]
    better: Literal["up", "down", "neutral"] = Field(
        description="Which way is good: more focus, less late-night screen time; neutral for an app that is neither work nor a distraction.")
    days: int = Field(description="Whole days with data on each side that were compared, at least.")


class Item(BaseModel):
    name: str
    value: float
    key: str | None = None
    category: str | None = None
    share: float | None = Field(default=None, description="A percent that goes with the item (a block's on-plan share).")
    children: list[Item] | None = None
    spark: list[float | None] | None = Field(default=None, description="leaderboard: minutes on each of the series' x days; "
                                             "null on a day no device sent screen data.")
    change: Change | None = Field(default=None, description="leaderboard: the day average against the days just before the range.")


class Cell(BaseModel):
    x: int = Field(description="Index into the series' x labels.")
    y: int = Field(description="Index into the series' y labels.")
    value: float


class Link(BaseModel):
    source: str
    target: str
    value: float


class Point(BaseModel):
    x: float
    y: float
    label: str


class Series(BaseModel):
    kind: SeriesKind
    title: str
    unit: str
    explain: str
    estimated: bool = False
    x: list[str] | None = Field(default=None, description="trend, stacked, bars and heatmap: the x labels.")
    y: list[str] | None = Field(default=None, description="heatmap and strip: the row labels.")
    lines: list[Line] | None = Field(default=None, description="trend and stacked: one per line; bars: one.")
    items: list[Item] | None = Field(default=None, description="donut, treemap, leaderboard and bars by name.")
    cells: list[Cell] | None = Field(default=None, description="heatmap; strip: 1 sent screen data that day, 0 paired but sent none, "
                                     "no cell when not paired (or the day hasn't begun).")
    nodes: list[str] | None = Field(default=None, description="sankey.")
    node_categories: list[str | None] | None = Field(default=None, description="sankey: each node's category key, in the nodes' order.")
    links: list[Link] | None = Field(default=None, description="sankey.")
    points: list[Point] | None = Field(default=None, description="scatter.")
    value: float | None = Field(default=None, description="gauge.")
    max: float | None = Field(default=None, description="gauge.")
    stats: dict[str, float | int | None] | None = Field(default=None, description="scatter: rho, p and n.")
    note: str | None = None


class RangeInfo(BaseModel):
    first: date
    last: date
    days: int
    label: str


class InsightsTab(BaseModel):
    tab: Tab
    tz: str
    range: RangeInfo
    in_progress: bool = Field(description="True when the range includes today, which is not over: numbers so far.")
    metrics: list[Metric]
    series: dict[str, Series]
    changes: list[Change] = Field(default_factory=list, description="overview: each day-average against the days just "
                                  "before the range, over whole days only; left out when either side has too few.")
    meta: Meta
    cached: bool = False


def parse_range(text: str, today: date) -> RangeInfo:
    """`today`, `7d` (the last 7 days, today included; 1 to 92 days) or `YYYY-MM-DD..YYYY-MM-DD`."""
    text = text.strip().lower()
    if text == "today":
        return RangeInfo(first=today, last=today, days=1, label="Today")
    counted = re.fullmatch(r"(\d{1,3})d", text)
    spelled = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", text)
    if counted:
        count = int(counted.group(1))
        if not 1 <= count <= MAX_RANGE_DAYS:
            raise ApiError(400, "bad_request", f"a range is 1 to {MAX_RANGE_DAYS} days")
        first, last, label = today - timedelta(days=count - 1), today, f"Last {count} days"
    elif spelled:
        try:
            first, last = date.fromisoformat(spelled.group(1)), date.fromisoformat(spelled.group(2))
        except ValueError:
            raise ApiError(400, "bad_request", "those dates don't exist") from None
        if last < first:
            raise ApiError(400, "bad_request", "the range ends before it starts")
        if (last - first).days + 1 > MAX_RANGE_DAYS:
            raise ApiError(400, "bad_request", f"a range is 1 to {MAX_RANGE_DAYS} days")
        label = f"{first.isoformat()} to {last.isoformat()}"
    else:
        raise ApiError(400, "bad_request", f"range must be today, a number of days such as 7d (up to {MAX_RANGE_DAYS}), or YYYY-MM-DD..YYYY-MM-DD")
    if not (EARLIEST <= first and last <= LATEST):
        raise ApiError(400, "bad_request", f"dates must be between {EARLIEST} and {LATEST}")
    return RangeInfo(first=first, last=last, days=(last - first).days + 1, label=label)


def _mean(values: list[float | int | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None


def _rounded(value: float | None, digits: int = 0) -> float | int | None:
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


def compare_days_needed(count: int) -> int:
    """Whole days with data each side of a comparison needs: 4, or half a range under 8 days (rounded up)."""
    return max(1, min(COMPARE_MIN_DAYS, -(-count // 2)))


def better_for(category: str | None) -> Literal["up", "down", "neutral"]:
    """Which way an app's time is better going: less for distractions, more for work and study."""
    return "down" if category in DISTRACTING else "up" if category in PRODUCTIVE else "neutral"


CATEGORY_NAMES = {"comms": "Chat and calls"}  # as the dashboard names them; the rest are the key, capitalized


def _category_name(key: str) -> str:
    return CATEGORY_NAMES.get(key, key.capitalize())


def make_change(change_id: str, label: str, unit: str, now: float, then: float, better: Literal["up", "down", "neutral"],
                days: int) -> Change:
    """A day average against the one before: the rule behind every change chip (the overview's and each app's)."""
    delta = now - then
    pct = round(100 * delta / then) if then else None
    same = abs(delta) < 0.5 or pct == 0
    return Change(id=change_id, label=label, unit=unit, now=round(now, 2), before=round(then, 2), delta=round(delta, 2),
                  change_pct=pct, direction="same" if same else "up" if delta > 0 else "down", better=better, days=days)


def distinct(labels: dict[str, str], taken: Iterable[str] = ()) -> dict[str, str]:
    """`labels` (key to label) made unique, ignoring case, among themselves and against `taken`: a label that is
    shared (two phones both called "Pixel 8", a computer called "Work" next to the Work category) gets its key.
    A chart can't tell two lines, rows or Sankey nodes with one name apart, and a Sankey can't link a node to
    itself."""
    shared = Counter(label.casefold() for label in labels.values())
    used = {label.casefold() for label in taken}
    unique: dict[str, str] = {}
    for key, label in labels.items():
        if shared[label.casefold()] > 1 or label.casefold() in used:
            label = f"{label} ({key})"
        base, number = label, 2
        while label.casefold() in used:
            label, number = f"{base} {number}", number + 1
        used.add(label.casefold())
        unique[key] = label
    return unique


class TabBuilder:
    """Builds one tab for one range from one Stats (which loads each day once for all the tab's charts). Each
    split of the range (totals, crosstab) is worked out once, however many charts use it."""

    def __init__(self, stats: Stats, conn: sqlite3.Connection, span: RangeInfo) -> None:
        self.stats = stats
        self.span = span
        self.days = [span.first + timedelta(days=i) for i in range(span.days)]
        self.labels = [day.isoformat() for day in self.days]
        rows = conn.execute("SELECT device_id, name, device_type FROM devices").fetchall()
        self.names = distinct({row["device_id"]: row["name"] for row in rows})
        self.types = {row["device_id"]: row["device_type"] for row in rows}
        self._totals: dict[tuple[str, frozenset[str] | None], dict[str, Any]] = {}
        self._crosstabs: dict[tuple[str, str], dict[str, Any]] = {}

    # -- shared pieces -------------------------------------------------------------------------------------------

    def device(self, device_id: str) -> str:
        return self.names.get(device_id, device_id)

    def totals(self, group_by: GroupBy = "app", device_types: frozenset[str] | None = None) -> dict[str, Any]:
        key = (group_by, device_types)
        if key not in self._totals:
            self._totals[key] = self.stats.totals(self.span.first, self.span.last, group_by=group_by, device_types=device_types)
        return self._totals[key]

    def crosstab(self, row: GroupBy, column: GroupBy) -> dict[str, Any]:
        if (row, column) not in self._crosstabs:
            self._crosstabs[(row, column)] = self.stats.crosstab(self.span.first, self.span.last, row, column)
        return self._crosstabs[(row, column)]

    @cached_property
    def screen_days(self) -> set[str]:
        """The days in the range with screen data (not a day that hasn't begun, nor one nobody sent anything for)."""
        return {item["key"] for item in self.totals("day")["items"]}

    @cached_property
    def device_days(self) -> dict[str, set[str]]:
        """Each day's devices that sent screen data: on a day a device sent none, its line has a gap, not 0."""
        return {label: self.stats.day(day).counted_devices_with_data for day, label in zip(self.days, self.labels, strict=True)}

    @cached_property
    def observed_days(self) -> set[str]:
        """The days the hub could have heard about (Stats.observed): meals and events are counted, 0 included, only
        on these."""
        return {label for day, label in zip(self.days, self.labels, strict=True) if self.stats.observed(day)}

    @cached_property
    def scores(self) -> list[dict[str, Any]]:
        return [self.stats.focus_score(day) for day in self.days]

    @cached_property
    def focused(self) -> list[float | None]:
        return [self.stats.focused_minutes(day)["value"] for day in self.days]

    @cached_property
    def pickups(self) -> list[float | None]:
        return [self.stats.pickups(day)["value"] for day in self.days]

    @cached_property
    def nights(self) -> list[dict[str, Any]]:
        return [self.stats.sleep_estimate(day) for day in self.days]

    @cached_property
    def late_nights(self) -> list[dict[str, Any]]:
        return [self.stats.late_night_minutes(day) for day in self.days]

    @cached_property
    def late(self) -> list[float | None]:
        return [night["value"] for night in self.late_nights]

    @cached_property
    def screen_by_day(self) -> list[float | None]:
        by_day = {item["key"]: item["minutes"] for item in self.totals("day")["items"]}
        return [by_day.get(label) for label in self.labels]

    def phone_and_computer(self) -> Series:
        """Each day's phone and computer minutes. A day neither kind sent screen data for is a gap in its line."""
        cells = self.crosstab("day", "device")["cells"]
        lines = []
        for name, kinds, category in (("Phone", PHONE_TYPES, "comms"), ("Computer", DESK_TYPES, "work")):
            values: list[float | None] = []
            for label in self.labels:
                devices = [device for device in self.device_days[label] | set(cells.get(label, {})) if self.types.get(device) in kinds]
                values.append(minutes(sum(cells.get(label, {}).get(device, 0) for device in devices)) if devices else None)
            lines.append(Line(name=name, key=name.lower(), category=category, values=values))
        return Series(kind="stacked", title="Phone and computer", unit="minutes", x=self.labels, lines=lines,
                      estimated=self.crosstab("day", "device")["estimated"],
                      explain="Minutes each day on phones and on computers; a gap is a day that kind of device sent nothing.")

    def best_and_toughest(self) -> list[Metric]:
        """The day with the most focused minutes, and the one with the most screen time after 11 pm (that night)."""
        def most(values: list[float | None]) -> tuple[date, float] | None:
            known = [(value, day) for value, day in zip(values, self.days, strict=True) if value]
            return (max(known)[1], max(known)[0]) if known else None  # a tie: the later day

        best, toughest = most(self.focused), most(self.late)
        return [
            Metric(id="best_day", label="Best day", value=best[0].isoformat() if best else None, unit="date",
                   explain="The day in the range with the most focused time." if best else "No focused time in the range."),
            Metric(id="best_day_focused", label="Its focused time", value=best[1] if best else None, unit="minutes",
                   explain="Work or study in blocks of 10 minutes or more that day."),
            Metric(id="toughest_day", label="Toughest day", value=toughest[0].isoformat() if toughest else None, unit="date",
                   explain=("The day in the range with the most screen time after 11 pm, that night." if toughest
                            else "No screen time after 11 pm in the range.")),
            Metric(id="toughest_day_late", label="Its late-night time", value=toughest[1] if toughest else None, unit="minutes",
                   explain="Screen time from 23:00 to 03:00 that night, on any device."),
        ]

    def compare(self, conn: sqlite3.Connection) -> list[Change]:
        """Each day-average against the same number of days just before the range, over whole days only (a day still
        going would look like less, and so would a night: 11 pm to 3 am runs into the next day), and only when both
        sides have enough days with data to say anything: 4, or half a range shorter than 8 days."""
        count = self.span.days
        needed = compare_days_needed(count)
        if sum(self.stats.day(day).over for day in self.days) < needed:
            return []  # not enough whole days here: nothing to compare, so the days before aren't worked out
        first = self.span.first - timedelta(days=count)
        before = TabBuilder(self.stats, conn, RangeInfo(first=first, last=self.span.first - timedelta(days=1), days=count, label=""))

        def days_over(builder: TabBuilder) -> list[bool]:
            return [builder.stats.day(day).over for day in builder.days]

        def by_day(values: Callable[[TabBuilder], list[float | None]]) -> Callable[[TabBuilder], Iterable[tuple[float | None, bool]]]:
            return lambda builder: zip(values(builder), days_over(builder), strict=True)

        def late_nights(builder: TabBuilder) -> Iterable[tuple[float | None, bool]]:
            return [(night["value"], over and not night.get("in_progress", False))
                    for night, over in zip(builder.late_nights, days_over(builder), strict=True)]

        rows: list[tuple[str, str, str, Literal["up", "down"], Callable[[TabBuilder], Iterable[tuple[float | None, bool]]]]] = [
            ("daily_average", "Screen time a day", "minutes", "down", by_day(lambda b: b.screen_by_day)),
            ("focused_time", "Focused time a day", "minutes", "up", by_day(lambda b: b.focused)),
            ("focus_score", "Focus score", "score", "up", by_day(lambda b: [score["value"] for score in b.scores])),
            ("pickups", "Phone pickups a day", "pickups", "down", by_day(lambda b: b.pickups)),
            ("sleep", "Sleep a night", "minutes", "up", by_day(lambda b: [night["value"] for night in b.nights])),
            ("late_night", "After 11 pm, a night", "minutes", "down", late_nights),
        ]
        changes = []
        for metric_id, label, unit, better, readings in rows:
            now_values = [value for value, whole in readings(self) if whole and value is not None]
            before_values = [value for value, whole in readings(before) if whole and value is not None]
            if len(now_values) < needed or len(before_values) < needed:
                continue
            changes.append(make_change(metric_id, label, unit, sum(now_values) / len(now_values), sum(before_values) / len(before_values),
                                       better, min(len(now_values), len(before_values))))
        return changes

    def top_apps(self, limit: int) -> list[dict[str, Any]]:
        """Stats.top_apps for the range, from the splits the tab already has."""
        main: dict[str, tuple[int, str]] = {}
        for category, apps in self.crosstab("category", "app")["cells"].items():
            for app, seconds in apps.items():
                main[app] = max(main.get(app, (seconds, category)), (seconds, category))
        return [{"app": item["key"], "minutes": item["minutes"], "category": main[item["key"]][1] if item["key"] in main else "other"}
                for item in self.totals("app")["items"][:limit] if item["seconds"] > 0]

    def whole_days_with_data(self, first: date, last: date) -> list[date]:
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        return [day for day in days if self.stats.day(day).over and self.stats.day(day).counted_devices_with_data]

    def app_changes(self, apps: list[dict[str, Any]]) -> dict[str, Change]:
        """Each app's minutes a day against the same number of days just before the range, over whole days with
        screen data on each side (a day with data and none of the app is 0 for it), with overview's rule for how
        many days that needs. Empty when either side has too few."""
        count = self.span.days
        needed = compare_days_needed(count)
        now_days = self.whole_days_with_data(self.span.first, self.span.last)
        if len(now_days) < needed:
            return {}
        before_first, before_last = self.span.first - timedelta(days=count), self.span.first - timedelta(days=1)
        before_days = self.whole_days_with_data(before_first, before_last)
        if len(before_days) < needed:
            return {}
        now_cells = self.crosstab("day", "app")["cells"]
        before_cells = self.stats.crosstab(before_first, before_last, "day", "app")["cells"]

        def average(cells: dict[str, dict[str, int]], days: list[date], app: str) -> float:
            return sum(cells.get(day.isoformat(), {}).get(app, 0) for day in days) / len(days) / 60

        return {
            app["app"]: make_change(app["app"], f"{app['app']} a day", "minutes", average(now_cells, now_days, app["app"]),
                                    average(before_cells, before_days, app["app"]), better_for(app["category"]),
                                    min(len(now_days), len(before_days)))
            for app in apps
        }

    def leaderboard(self) -> Series:
        """The top apps with each one's week (a sparkline over the 7 days up to the range's last day) and its change
        against the days before the range."""
        top = self.top_apps(LEADERS)
        first = self.span.last - timedelta(days=SPARK_DAYS - 1)
        days = [first + timedelta(days=i) for i in range(SPARK_DAYS)]
        # The range's own split has the week when the range covers it; a shorter range reads the days before too.
        cells = (self.crosstab("day", "app") if first >= self.span.first else self.stats.crosstab(first, self.span.last, "day", "app"))["cells"]
        seen = [self.stats.day(day).until > self.stats.day(day).start and bool(self.stats.day(day).counted_devices_with_data) for day in days]
        changes = self.app_changes(top)
        items = [
            Item(name=app["app"], key=app["app"], category=app["category"], value=app["minutes"],
                 spark=[minutes(cells.get(day.isoformat(), {}).get(app["app"], 0)) if known else None for day, known in zip(days, seen, strict=True)],
                 change=changes.get(app["app"]))
            for app in top
        ]
        return Series(kind="leaderboard", title="Top apps and sites", unit="minutes", estimated=self.totals("app")["estimated"],
                      x=[day.isoformat() for day in days], items=items,
                      explain=(f"The {LEADERS} apps and sites with the most time in the range, each with its last 7 days and its "
                               "change against the days just before the range (a day on average, whole days only)."))

    def handoffs(self) -> tuple[list[Metric], Series]:
        """Moves between devices (Stats.handoffs) over the range, as a Sankey from what you left to what you took up."""
        pairs: dict[tuple[str, str, str, str], int] = defaultdict(int)
        counts: list[int] = []
        for day in self.days:
            found = self.stats.handoffs(day)
            if found["value"] is None:
                continue
            counts.append(found["value"])
            for pair in found["pairs"]:
                pairs[(pair["from_device"], pair["from_category"], pair["to_device"], pair["to_category"])] += pair["count"]
        ranked = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:HANDOFF_LINKS]

        def side(device: str, category: str, then: bool) -> str:
            return f"{'then ' if then else ''}{self.device(device)} · {_category_name(category)}"

        nodes: list[str] = []
        node_categories: list[str | None] = []
        links: list[Link] = []
        for (from_device, from_category, to_device, to_category), count in ranked:
            source, target = side(from_device, from_category, False), side(to_device, to_category, True)
            for name, category in ((source, from_category), (target, to_category)):
                if name not in nodes:
                    nodes.append(name)
                    node_categories.append(category)
            links.append(Link(source=source, target=target, value=count))
        top = ranked[0][0] if ranked else None
        metrics = [
            Metric(id="handoffs", label="Device switches", value=sum(counts) if counts else None, unit="switches",
                   explain="Moving from one device to another within 5 minutes, say from the computer to the phone."),
            Metric(id="top_handoff", label="Most common switch",
                   value=f"{side(top[0], top[1], False)}, {side(top[2], top[3], True)}" if top else None, unit="text",
                   explain="The move between devices you made most often in the range."),
        ]
        series = Series(kind="sankey", title="Switching between devices", unit="switches",
                        explain=(f"What you left on one device (left) and what you took up on another within 5 minutes (right). "
                                 f"The {HANDOFF_LINKS} most common."),
                        nodes=nodes, node_categories=node_categories, links=links)
        return metrics, series

    def sync_strip(self, devices: list[str]) -> Series:
        """Each phone and computer, day by day: sent screen data (1), sent none although it was around (0: paired
        then, or between two days it sent data), or not around (no cell). A day at 0 is why that device's lines
        have a gap there: no data, not no use."""
        begun = [self.stats.day(day).until > self.stats.day(day).start for day in self.days]
        sent = [self.stats.day(day).counted_devices_with_data if on else set() for day, on in zip(self.days, begun, strict=True)]
        cells: list[Cell] = []
        for y, device in enumerate(devices):
            with_data = [x for x, found in enumerate(sent) if device in found]
            for x, day in enumerate(self.days):
                if not begun[x]:
                    continue
                if device in sent[x]:
                    cells.append(Cell(x=x, y=y, value=1))
                elif device in self.stats.expected(day) or (with_data and with_data[0] < x < with_data[-1]):
                    cells.append(Cell(x=x, y=y, value=0))
        return Series(kind="strip", title="When each device sent data", unit="days", x=self.labels, y=[self.device(d) for d in devices],
                      cells=cells, explain="A gap here is a day that device sent nothing, so its time that day is unknown, not zero.")

    def stacked(self, row: GroupBy, title: str, explain: str, name: Callable[[str], str] | None = None,
                category_of: Callable[[str], str] | None = None) -> Series:
        """Minutes per day, one line per `row` key. A day without screen data is a gap in every line; by device, so
        is a day that device sent nothing (another device's data says nothing about it)."""
        table = self.crosstab("day", row)
        cells = table["cells"]
        keys = sorted({key for day in cells.values() for key in day}, key=lambda key: -sum(day.get(key, 0) for day in cells.values()))

        def value(day: str, key: str) -> float | None:
            counted = cells.get(day, {})
            known = key in counted or (key in self.device_days[day] if row == "device" else day in self.screen_days)
            return minutes(counted.get(key, 0)) if known else None

        lines = [
            Line(name=name(key) if name else key, key=key, category=category_of(key) if category_of else None,
                 device_type=self.types.get(key) if row == "device" else None, values=[value(day, key) for day in self.labels])
            for key in keys
        ]
        return Series(kind="stacked", title=title, unit="minutes", explain=explain, estimated=table["estimated"], x=self.labels, lines=lines)

    def weekday_hours(self) -> Series:
        table = self.crosstab("day", "hour")
        grid: dict[tuple[int, int], int] = defaultdict(int)
        for label, hours in table["cells"].items():
            weekday = date.fromisoformat(label).weekday()
            for hour, seconds in hours.items():
                grid[(int(hour), weekday)] += seconds
        return Series(
            kind="heatmap", title="When screens were on", unit="minutes", estimated=table["estimated"],
            explain="Screen time in the range by weekday and hour of the day, added up (each device counted).",
            x=HOURS, y=WEEKDAYS, cells=[Cell(x=hour, y=weekday, value=minutes(seconds)) for (hour, weekday), seconds in sorted(grid.items())],
        )

    # -- the tabs ------------------------------------------------------------------------------------------------

    def overview_metrics(self) -> list[Metric]:
        """The overview's numbers without its charts (Wrapped shows these for the week)."""
        totals = self.totals("category")
        days_with = len(self.screen_days)
        focused, pickups, sleep, late = self.focused, self.pickups, self.nights, self.late
        scores = [score["value"] for score in self.scores]
        known_focus = [value for value in focused if value is not None]
        metrics = [
            Metric(id="screen_time", label="Screen time", value=totals["total_minutes"] if days_with else None, unit="minutes",
                   explain="All app and site time across devices, each device counted, away time removed.", estimated=totals["estimated"]),
            Metric(id="daily_average", label="A day, on average", value=minutes(totals["total_seconds"] / days_with) if days_with else None,
                   unit="minutes", explain="Screen time divided by the days that had any data.", estimated=totals["estimated"]),
            Metric(id="focused_time", label="Focused time", value=_rounded(sum(known_focus), 2) if known_focus else None, unit="minutes",
                   explain="Work or study in blocks of 10 minutes or more with no phone distraction."),
            Metric(id="focus_score", label="Focus score, on average", value=_rounded(_mean(scores)), unit="score",
                   explain="0 to 100: focused time against work, study and distraction together, averaged over the days."),
            Metric(id="pickups", label="Phone pickups a day", value=_rounded(_mean(pickups), 1), unit="pickups",
                   explain="A phone app coming into use after the phone rested for a minute or more."),
            Metric(id="sleep", label="Sleep a night", value=_rounded(_mean([night["value"] for night in sleep]), 2), unit="minutes",
                   explain="From your health app when it sends sleep; otherwise the longest stretch the phone was not used overnight.",
                   estimated=any(night["value"] is not None and not night.get("measured", False) for night in sleep)),
            Metric(id="late_night", label="After 11 pm, a night", value=_rounded(_mean(late), 2), unit="minutes",
                   explain="Screen time from 23:00 to 03:00, on any device (overlaps counted once)."),
        ]
        return metrics

    def overview(self) -> tuple[list[Metric], dict[str, Series]]:
        metrics = self.overview_metrics() + self.best_and_toughest()
        totals = self.totals("category")
        scores = [score["value"] for score in self.scores]
        series = {
            "screen_by_device": self.stacked("device", "Screen time by device", "Minutes each day on each device.", name=self.device),
            "categories": Series(kind="donut", title="By category", unit="minutes", estimated=totals["estimated"],
                                 explain="Where the screen time went, by kind of app or site.",
                                 items=[Item(name=_category_name(item["key"]), key=item["key"], category=item["key"], value=item["minutes"])
                                        for item in totals["items"] if item["seconds"]]),
            "focus_by_day": Series(kind="trend", title="Focus score", unit="score", x=self.labels,
                                   explain="Each day's focus score (0 to 100); a gap is a day with no work or distraction.",
                                   lines=[Line(name="Focus score", values=[float(score) if score is not None else None for score in scores])]),
            "hours": self.weekday_hours(),
            "phone_vs_computer": self.phone_and_computer(),
        }
        return metrics, series

    def apps(self) -> tuple[list[Metric], dict[str, Series]]:
        totals = self.totals("app")
        top = self.top_apps(TOP_APP_BARS)
        used = [item for item in totals["items"] if item["seconds"]]
        switches = [self.stats.switches_per_hour(day)["value"] for day in self.days]
        table = self.crosstab("category", "app")
        tree: list[Item] = []
        for category, apps in sorted(table["cells"].items(), key=lambda kv: -sum(kv[1].values())):
            ranked = sorted(apps.items(), key=lambda kv: (-kv[1], kv[0]))
            children = [Item(name=app, value=minutes(seconds), category=category) for app, seconds in ranked[:TREEMAP_APPS] if seconds]
            rest = sum(seconds for _, seconds in ranked[TREEMAP_APPS:])
            if rest:
                children.append(Item(name="Other apps", value=minutes(rest), category=category))
            tree.append(Item(name=_category_name(category), key=category, category=category, value=minutes(sum(apps.values())), children=children))
        metrics = [
            Metric(id="apps_used", label="Apps and sites used", value=len(used) if self.screen_days else None,
                   unit="apps", explain="Apps and sites with any screen time in the range."),
            Metric(id="top_app", label="Most used", value=top[0]["app"] if top else None, unit="app",
                   explain="The app or site with the most time in the range."),
            Metric(id="top_app_time", label="Time in it", value=top[0]["minutes"] if top else None, unit="minutes",
                   explain="Its screen time in the range, every device added up.", estimated=totals["estimated"]),
            Metric(id="switches", label="App switches an hour", value=_rounded(_mean(switches), 1), unit="switches per hour",
                   explain="Changes to a different app or site on a device within 5 minutes, per hour of screen time, averaged over the days."),
        ]
        series = {
            "top_apps": Series(kind="bars", title="Top apps and sites", unit="minutes", estimated=totals["estimated"],
                               explain="The apps and sites with the most time, colored by their main category.",
                               items=[Item(name=app["app"], value=app["minutes"], category=app["category"]) for app in top]),
            "treemap": Series(kind="treemap", title="Categories and their apps", unit="minutes", estimated=table["estimated"],
                              explain=f"Each category's time, with its top {TREEMAP_APPS} apps and sites inside.", items=tree),
            "by_category": self.stacked("category", "Categories by day", "Minutes each day in each category.",
                                        name=_category_name, category_of=lambda key: key),
            "switches": Series(kind="trend", title="App switches an hour", unit="switches per hour", x=self.labels,
                               explain="How often you jumped between apps, per hour of screen time.",
                               lines=[Line(name="Switches an hour", values=switches)]),
            "leaderboard": self.leaderboard(),
        }
        return metrics, series

    def devices(self) -> tuple[list[Metric], dict[str, Series]]:
        totals = self.totals("device")
        phones = self.totals("app", PHONE_TYPES)["total_seconds"]
        desks = self.totals("app", DESK_TYPES)["total_seconds"]
        hours = self.crosstab("device", "hour")
        flow = self.crosstab("device", "category")
        devices = totals["items"]
        metrics = [
            Metric(id=f"device:{item['key']}", label=self.device(item["key"]), value=item["minutes"], unit="minutes",
                   explain="Screen time on this device in the range.", estimated=totals["estimated"])
            for item in devices
        ]
        metrics.append(Metric(id="phone_share", label="On the phone", value=round(100 * phones / (phones + desks)) if phones + desks else None,
                              unit="percent", explain="The phones' share of phone and computer screen time together."))
        handoff_metrics, handoff_series = self.handoffs()
        metrics += handoff_metrics
        device_rows = [item["key"] for item in devices]
        strip_rows = device_rows + sorted({device for day in self.days for device in self.stats.expected(day)} - set(device_rows))
        metrics += [Metric(id=f"last_seen:{device}", label=self.device(device), value=None, unit="time",
                           explain="When the hub last heard from it (UTC); filled in fresh for each answer.") for device in strip_rows]
        categories = {category: _category_name(category) for category in sorted({category for row in flow["cells"].values() for category in row})}
        nodes = distinct({device: self.device(device) for device in device_rows}, taken=categories.values())
        series = {
            "share": Series(kind="donut", title="By device", unit="minutes", estimated=totals["estimated"],
                            explain="Screen time counted per device and added up, so an hour on both counts on both.",
                            items=[Item(name=self.device(item["key"]), key=item["key"], value=item["minutes"]) for item in devices if item["seconds"]]),
            "by_day": self.stacked("device", "Each device by day", "Minutes each day on each device.", name=self.device),
            "hours": Series(kind="heatmap", title="Devices through the day", unit="minutes", estimated=hours["estimated"],
                            explain="Screen time on each device by hour of the day, the whole range added up.",
                            x=HOURS, y=[self.device(device) for device in device_rows],
                            cells=[Cell(x=int(hour), y=row, value=minutes(seconds))
                                   for row, device in enumerate(device_rows) for hour, seconds in sorted(hours["cells"].get(device, {}).items())]),
            "flow": Series(kind="sankey", title="From device to category", unit="minutes", estimated=flow["estimated"],
                           explain="How each device's time splits into categories.",
                           nodes=[nodes[device] for device in device_rows] + list(categories.values()),
                           links=[Link(source=nodes[device], target=categories[category], value=minutes(seconds))
                                  for device in device_rows for category, seconds in sorted(flow["cells"].get(device, {}).items()) if seconds]),
            "handoffs": handoff_series,
            "sync": self.sync_strip(strip_rows),
        }
        return metrics, series

    def focus(self) -> tuple[list[Metric], dict[str, Series]]:
        stats, first, last = self.stats, self.span.first, self.span.last
        scores = self.scores
        focused = [stats.focused_minutes(day)["value"] for day in self.days]
        switches = [stats.switches_per_hour(day) for day in self.days]
        pairs = stats.correlation(first, last) if self.span.days >= 2 else None
        values = [score["value"] for score in scores]
        known = [(value, day) for value, day in zip(values, self.days, strict=True) if value is not None]
        best = max(known, key=lambda pair: (pair[0], pair[1])) if known else None
        by_hour: dict[str, int] = defaultdict(int)
        for day in switches:
            for hour, count in day.get("by_hour", {}).items():
                by_hour[hour] += count
        known_focus = [value for value in focused if value is not None]
        metrics = [
            Metric(id="focused_time", label="Focused time", value=_rounded(sum(known_focus), 2) if known_focus else None, unit="minutes",
                   explain="Work or study in blocks of 10 minutes or more with no phone distraction."),
            Metric(id="focus_score", label="Focus score, on average", value=_rounded(_mean(values)), unit="score",
                   explain="0 to 100: focused time against work, study and distraction together, averaged over the days."),
            Metric(id="best_day", label="Best day", value=best[1].isoformat() if best else None, unit="date",
                   explain="The day with the highest focus score in the range."),
            Metric(id="best_score", label="Its focus score", value=best[0] if best else None, unit="score",
                   explain="That day's focus score."),
        ]
        focus_lines = []
        for name, field in (("Focused", "focused"), ("Other work or study", "rest"), ("Distracted", "distracted_seconds")):
            row: list[float | None] = []
            for score, focused_value in zip(scores, focused, strict=True):
                if score.get("missing"):
                    row.append(None)
                elif field == "focused":
                    row.append(focused_value or 0.0)
                elif field == "rest":
                    row.append(minutes(max(0, score.get("work_or_study_seconds", 0) - score.get("focused_seconds", 0))))
                else:
                    row.append(minutes(score.get(field, 0)))
            focus_lines.append(Line(name=name, category={"Focused": "study", "Other work or study": "work", "Distracted": "social"}[name], values=row))
        series = {
            "score": Series(kind="gauge", title="Focus score", unit="score", value=_rounded(_mean(values)), max=100,
                            explain="The average of the days' focus scores, from 0 to 100."),
            "focus_by_day": Series(kind="stacked", title="Focus and distraction by day", unit="minutes", x=self.labels, lines=focus_lines,
                                   explain="Focused time, the rest of the work or study time, and time in social, video or game apps."),
            "switches_by_hour": Series(kind="bars", title="App switches through the day", unit="switches", x=HOURS,
                                       explain="App switches by hour of the day, the whole range added up.",
                                       lines=[Line(name="Switches", values=[float(by_hour.get(hour, 0)) for hour in HOURS])]),
        }
        if pairs is not None:
            series["late_vs_focus"] = Series(
                kind="scatter", title="Late nights and the next day's focus", unit="score",
                explain="Each point is a night: minutes on screens after 11 pm, and the next day's focus score.",
                points=[Point(x=pair["late_minutes"], y=pair["focus_score"], label=pair["night"]) for pair in pairs["pairs"]],
                stats={"rho": pairs["rho"], "p": pairs["p"], "n": pairs["n"]}, note=pairs["caveat"],
            )
        return metrics, series

    def sleep(self) -> tuple[list[Metric], dict[str, Series]]:
        stats = self.stats
        nights = [stats.sleep_estimate(day) for day in self.days]
        late = [stats.late_night_minutes(day) for day in self.days]
        values = [night["value"] for night in nights]
        # A night with health data or a phone sending is one the hub could see (method set), even with no answer yet.
        # Counts are over such nights: with none (a range before recording, or still to come) they are missing.
        seen = any(night.get("method") is not None for night in nights)
        estimated_nights = [night for night in nights if night["value"] is not None and not night.get("measured", False)]
        measured = [night["value"] if night["value"] is not None and night.get("measured", False) else None for night in nights]
        guessed = [night["value"] if night["value"] is not None and not night.get("measured", False) else None for night in nights]

        def since_six(moment: str | None, day: date) -> float | None:
            """Minutes after 18:00 the evening before `day`: bedtimes and wake times on one scale."""
            if not moment:
                return None
            evening = datetime.combine(day - timedelta(days=1), time(18), tzinfo=stats.tz)
            return minutes((datetime.fromisoformat(moment) - evening).total_seconds())

        metrics = [
            Metric(id="sleep", label="Sleep a night", value=_rounded(_mean(values), 2), unit="minutes",
                   explain="From your health app when it sends sleep; otherwise the longest stretch the phone was not used overnight.",
                   estimated=bool(estimated_nights)),
            Metric(id="nights", label="Nights with sleep", value=len([v for v in values if v is not None]) if seen else None, unit="nights",
                   explain="Nights in the range with sleep measured or estimated."),
            Metric(id="estimated_nights", label="Estimated nights", value=len(estimated_nights) if seen else None, unit="nights",
                   explain="Nights worked out from the phone's quiet time, with no health data."),
            Metric(id="late_night", label="After 11 pm, a night", value=_rounded(_mean([night["value"] for night in late]), 2), unit="minutes",
                   explain="Screen time from 23:00 to 03:00, on any device (overlaps counted once)."),
        ]
        series = {
            "sleep_by_night": Series(kind="stacked", title="Sleep each night", unit="minutes", x=self.labels, estimated=bool(estimated_nights),
                                     explain="The night ending on each morning: measured by your health app, or estimated from the phone.",
                                     lines=[Line(name="Measured", category="health", values=measured), Line(name="Estimated", category="other", values=guessed)]),
            "schedule": Series(kind="trend", title="Bedtime and wake time", unit="minutes after 18:00", x=self.labels,
                               explain="When sleep started and ended, as minutes after 18:00 the evening before (360 is midnight).",
                               lines=[Line(name="Asleep", values=[since_six(night.get("start"), day) for night, day in zip(nights, self.days, strict=True)]),
                                      Line(name="Awake", values=[since_six(night.get("end"), day) for night, day in zip(nights, self.days, strict=True)])]),
            "late_night": Series(kind="bars", title="After 11 pm", unit="minutes", x=self.labels,
                                 explain="Screen time from 23:00 on each day to 03:00 the next morning.",
                                 lines=[Line(name="After 11 pm", values=[night["value"] for night in late])]),
        }
        return metrics, series

    def food(self) -> tuple[list[Metric], dict[str, Series]]:
        meals = self.stats.meals(self.span.first, self.span.last)
        by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        items: dict[str, int] = defaultdict(int)
        points: list[Point] = []
        for meal in meals:
            kind = meal["meal_type"] if meal["meal_type"] in MEAL_TYPES else "other"
            by_day[meal["day"]][kind] += 1
            for item in meal["items"]:
                items[item.strip().lower()] += 1
            when = datetime.fromisoformat(meal["time"])
            points.append(Point(x=float(self.labels.index(meal["day"])), y=round(when.hour + when.minute / 60, 2),
                                label=", ".join(meal["items"]) or meal["text"] or kind))
        ranked = sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))
        observed = self.observed_days
        metrics = [
            Metric(id="meals", label="Meals logged", value=len(meals) if observed else None, unit="meals",
                   explain="Meals sent from your phone's health app or the Log Meal shortcut."),
            Metric(id="days_with_meals", label="Days with a meal logged", value=len(by_day) if observed else None, unit="days",
                   explain="Days in the range with at least one meal."),
            Metric(id="top_item", label="Most logged", value=ranked[0][0] if ranked else None, unit="item", explain="The food logged most often."),
        ]
        series = {
            "meals_by_day": Series(kind="stacked", title="Meals by day", unit="meals", x=self.labels,
                                   explain="Meals logged each day, by type; a gap is a day the hub heard nothing from.",
                                   lines=[Line(name=kind.capitalize(), key=kind,
                                               values=[float(by_day.get(day, {}).get(kind, 0)) if day in observed else None for day in self.labels])
                                          for kind in MEAL_TYPES if any(day.get(kind) for day in by_day.values())]),
            "meal_times": Series(kind="scatter", title="When you ate", unit="hour of the day", x=self.labels,
                                 explain="Each meal at its time of day (0 to 24), by day.", points=points),
            "top_items": Series(kind="bars", title="Most logged foods", unit="times",
                                explain="Foods by how often they were logged.",
                                items=[Item(name=name, value=float(count)) for name, count in ranked[:10]]),
        }
        return metrics, series

    def calendar(self) -> tuple[list[Metric], dict[str, Series]]:
        stats = self.stats
        plans = [stats.planned_vs_actual(day) for day in self.days]
        planned = sum(plan.get("totals_seconds", {}).get("planned", 0) for plan in plans)
        seen = [plan for plan in plans if not plan.get("missing")]
        on_plan = sum(plan["totals_seconds"]["on_plan"] for plan in seen)
        seen_planned = sum(plan["totals_seconds"]["planned"] for plan in seen)
        blocks = [(block, day) for plan, day in zip(plans, self.days, strict=True) for block in plan["blocks"]]
        busiest = max(zip(plans, self.days, strict=True), key=lambda pair: pair[0].get("totals_seconds", {}).get("planned", 0), default=None)
        observed = self.observed_days
        grid: dict[tuple[int, int], int] = defaultdict(int)
        for plan, day in zip(plans, self.days, strict=True):
            # Overlapping events are one stretch of planned time, as the Planned time total counts them.
            spans = [(datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])) for block in plan["blocks"]]
            for start, end in merge(spans):
                for hour, seconds in stats.split_by_hour(start, end):
                    grid[(int(hour), day.weekday())] += seconds
        metrics = [
            Metric(id="planned", label="Planned time", value=minutes(planned) if observed else None, unit="minutes",
                   explain="Time in calendar events (not all-day ones), overlaps counted once."),
            Metric(id="on_plan", label="Spent as planned", value=round(100 * on_plan / seen_planned) if seen_planned else None, unit="percent",
                   explain="Of the planned time on days with screen data: work, study or a meeting app on screen."),
            Metric(id="events", label="Events", value=len(blocks) if observed else None, unit="events",
                   explain="Calendar events in the range (not all-day ones)."),
            Metric(id="busiest_day", label="Busiest day", value=busiest[1].isoformat() if busiest and busiest[0].get("totals_seconds", {}).get("planned") else None,
                   unit="date", explain="The day with the most planned time."),
        ]
        lines = [Line(name=label, key=key, values=[minutes(plan["totals_seconds"][key]) if not plan.get("missing") else None for plan in plans])
                 for key, label in PLAN_PARTS]
        ranked = sorted(blocks, key=lambda pair: (-pair[0]["planned_seconds"], pair[0]["start"]))[:10]
        series = {
            "plan_by_day": Series(kind="stacked", title="Planned time and where it went", unit="minutes", x=self.labels, lines=lines,
                                  explain="Each day's calendar time: on plan (work, study, meetings), off plan (social, video, games), other screen time, or no screen."),
            "blocks": Series(kind="bars", title="Longest events", unit="minutes",
                             explain="The longest calendar events, with the share spent as planned.",
                             items=[Item(name=block["title"] or "(no title)", key=day.isoformat(), value=minutes(block["planned_seconds"]), share=block["on_plan_pct"])
                                    for block, day in ranked]),
            "hours": Series(kind="heatmap", title="When your calendar is busy", unit="minutes", x=HOURS, y=WEEKDAYS,
                            explain="Planned time by weekday and hour of the day, the whole range added up (overlaps counted once).",
                            cells=[Cell(x=hour, y=weekday, value=minutes(seconds)) for (hour, weekday), seconds in sorted(grid.items())]),
        }
        return metrics, series


# The tabs' answers, kept per database, tab, range and time zone until the data changes (or, for a range that is
# not over, for a minute): a page of charts is worked out once.
_cache_guard = threading.Lock()
_cache: OrderedDict[tuple[str, str, date, date, str], tuple[int, bool, float, BaseModel]] = OrderedDict()


def build_tab(stats: Stats, conn: sqlite3.Connection, tab: str, span: RangeInfo, tz_name: str) -> InsightsTab:
    builder = TabBuilder(stats, conn, span)
    metrics, series = getattr(builder, tab)()
    windows = [stats.day(day) for day in builder.days]
    estimated = any(item.estimated for item in metrics) or any(item.estimated for item in series.values())
    return InsightsTab(tab=tab, tz=tz_name, range=span, in_progress=any(w.until > w.start and not w.over for w in windows),
                       metrics=metrics, series=series, changes=builder.compare(conn) if tab == "overview" else [],
                       meta=Meta(**stats.meta(span.first, span.last, estimated)))


def with_last_seen(result: InsightsTab, conn: sqlite3.Connection) -> InsightsTab:
    """The devices tab's last_seen metrics as they are now: a device's last contact moves without its data changing
    (so without the cache noticing), and a cached answer must not show an old time."""
    if result.tab != "devices":
        return result
    seen = {row["device_id"]: row["last_seen"] for row in conn.execute("SELECT device_id, last_seen FROM devices")}
    metrics = [metric.model_copy(update={"value": seen.get(metric.id.removeprefix("last_seen:"))}) if metric.id.startswith("last_seen:")
               else metric for metric in result.metrics]
    return result.model_copy(update={"metrics": metrics})


def _cached(key: tuple[str, str, date, date, str], version: int) -> Any:
    """The kept answer, if nothing behind it changed: for a range not over yet (today in it, or days still to
    come), only while under a minute old, since the numbers move with the clock even when no data arrives."""
    with _cache_guard:  # one hold: an entry can't be evicted between finding it and marking it used
        hit = _cache.get(key)
        if hit is None or hit[0] != version or (hit[1] and monotonic() - hit[2] >= LIVE_CACHE_SECONDS):
            return None
        _cache.move_to_end(key)
        return hit[3]


def _keep(key: tuple[str, str, date, date, str], version: int, live: bool, result: BaseModel) -> None:
    with _cache_guard:
        _cache[key] = (version, live, monotonic(), result)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


@router.get("/insights/{tab}", response_model=InsightsTab, summary="One Insights tab: metrics and chart-ready series for a range")
def insights_tab(
    tab: Tab,
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    span: Annotated[str, Query(alias="range", description="today, 7d (1 to 92 days, today included) or YYYY-MM-DD..YYYY-MM-DD")] = "7d",
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> InsightsTab:
    zone, zone_name = resolve_tz(tz)
    now = current_time()
    chosen = parse_range(span, now.astimezone(zone).date())
    key = (str(database.path), tab, chosen.first, chosen.last, zone_name)
    with database.connect() as conn:
        version = data_version(conn)
        hit = _cached(key, version)
        if hit is not None:
            return with_last_seen(hit, conn).model_copy(update={"cached": True})
        stats = Stats(conn, zone, zone_name, now)
        result = build_tab(stats, conn, tab, chosen, zone_name)
        live = not stats.day(chosen.last).over
        fresh = with_last_seen(result, conn)
    _keep(key, version, live, result)
    return fresh


# --- one app, for the Apps and Devices tab's detail (DT-55) ---------------------------------------------------------


class LongestSession(BaseModel):
    start: datetime
    end: datetime
    minutes: float
    device_id: str
    device: str = Field(description="The device's name.")


class AppDetail(BaseModel):
    app: str = Field(description="The app or site, as the tabs name it (Stats.totals by app).")
    category: str | None = Field(description="The category holding most of its time; null when it had none in the range.")
    tz: str
    range: RangeInfo
    in_progress: bool
    metrics: list[Metric]
    series: dict[str, Series]
    longest: LongestSession | None = Field(description="Its longest stretch on one device (pieces under a minute apart joined).")
    meta: Meta
    cached: bool = False


def build_app(stats: Stats, conn: sqlite3.Connection, app: str, span: RangeInfo, tz_name: str) -> AppDetail:
    """One app's range: minutes each day (null on a day no device sent screen data), when in the day it is used,
    on which devices, and its longest stretch. From the same pieces as the tabs, so its total is the treemap's."""
    builder = TabBuilder(stats, conn, span)
    daily: list[float | None] = []
    by_hour: dict[str, int] = defaultdict(int)
    by_device: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    per_device: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    estimated = False
    windows = [stats.day(day) for day in builder.days]
    for window in windows:
        if window.until <= window.start or not window.counted_devices_with_data:
            daily.append(None)
            continue
        pieces = [piece for piece in window.pieces if app_key(piece) == app]
        daily.append(minutes(sum(piece.seconds for piece in pieces)))
        for piece in pieces:
            estimated = estimated or piece.estimated
            by_device[piece.device_id] += piece.seconds
            by_category[piece.category or "other"] += piece.seconds
            per_device[piece.device_id].append((piece.start, piece.end))
            for hour, seconds in stats.split_by_hour(piece.start, piece.end):
                by_hour[hour] += seconds
    # Across the whole range, so a stretch over midnight (cut into two days' pieces) is one stretch.
    longest: tuple[int, datetime, datetime, str] | None = None
    for device, stretches in per_device.items():
        for start, end in merge(stretches, join=FOCUS_JOIN):
            seconds = round((end - start).total_seconds())
            if longest is None or seconds > longest[0]:
                longest = (seconds, start, end, device)
    known = any(value is not None for value in daily)
    total = sum(by_device.values())
    used = [value for value in daily if value]
    category = max(by_category.items(), key=lambda kv: (kv[1], kv[0]))[0] if by_category else None
    devices = sorted(by_device.items(), key=lambda kv: (-kv[1], kv[0]))
    metrics = [
        Metric(id="total", label="In the range", value=minutes(total) if any(value is not None for value in daily) else None,
               unit="minutes", explain="Its screen time in the range, every device added up.", estimated=estimated),
        Metric(id="days_used", label="Days used", value=len(used) if any(value is not None for value in daily) else None,
               unit="days", explain="Days with any time in it."),
        Metric(id="a_day_used", label="A day, when used", value=_rounded(sum(used) / len(used), 2) if used else None,
               unit="minutes", explain="Its time divided by the days it was used."),
        Metric(id="longest", label="Longest stretch", value=minutes(longest[0]) if longest else None, unit="minutes",
               explain="Its longest stretch on one device, with breaks under a minute joined."),
    ]
    series = {
        "daily": Series(kind="bars", title="Each day", unit="minutes", x=builder.labels, lines=[Line(name=app, values=daily)],
                        explain="Its minutes each day; a gap is a day no device sent screen data.", estimated=estimated),
        "hours": Series(kind="bars", title="When in the day", unit="minutes", x=HOURS,
                        lines=[Line(name=app, values=[minutes(by_hour.get(hour, 0)) if known else None for hour in HOURS])],
                        explain="Its minutes by hour of the day, the whole range added up; unknown when no day had data.", estimated=estimated),
        "devices": Series(kind="donut", title="On which devices", unit="minutes", estimated=estimated,
                          explain="Its time on each device.",
                          items=[Item(name=builder.device(device), key=device, value=minutes(seconds)) for device, seconds in devices]),
    }
    return AppDetail(
        app=app, category=category, tz=tz_name, range=span, in_progress=any(w.until > w.start and not w.over for w in windows),
        metrics=metrics, series=series, meta=Meta(**stats.meta(span.first, span.last, estimated)),
        longest=LongestSession(start=longest[1], end=longest[2], minutes=minutes(longest[0]), device_id=longest[3],
                               device=builder.device(longest[3])) if longest else None,
    )


@router.get("/insights/apps/detail", response_model=AppDetail, summary="One app or site over a range: each day, the hours, devices, longest stretch")
def app_detail(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    app: Annotated[str, Query(min_length=1, max_length=300, description="The app or site as the Apps tab names it, e.g. YouTube or youtube.com")],
    span: Annotated[str, Query(alias="range", description="today, 7d (1 to 92 days, today included) or YYYY-MM-DD..YYYY-MM-DD")] = "7d",
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> AppDetail:
    zone, zone_name = resolve_tz(tz)
    now = current_time()
    chosen = parse_range(span, now.astimezone(zone).date())
    key = (str(database.path), f"app:{app}", chosen.first, chosen.last, zone_name)
    with database.connect() as conn:
        version = data_version(conn)
        hit = _cached(key, version)
        if hit is not None:
            return hit.model_copy(update={"cached": True})
        stats = Stats(conn, zone, zone_name, now)
        result = build_app(stats, conn, app, chosen, zone_name)
        live = not stats.day(chosen.last).over
    _keep(key, version, live, result)
    return result


# --- the week's Wrapped (DT-41) -----------------------------------------------------------------------------------


class Wrapped(BaseModel):
    week: str = Field(description="ISO week, e.g. 2026-W39 (Monday to Sunday).")
    first: date
    last: date
    tz: str
    in_progress: bool = Field(description="True while the week is not over: the numbers and lines so far.")
    metrics: list[Metric]
    top_apps: list[AppMinutes]
    lines: list[str] = Field(description="Three highlight lines by the local model (every number checked against the facts), or plain ones.")
    facts_used: list[FactOut]
    model: str | None = Field(description="The local model that wrote the lines; null for the plain ones.")
    cached: bool
    fallback: bool
    reason: str | None
    streaks: list[WeekStreakOut] = Field(description="Each streak in the week (DT-53): the days met and the longest run.")
    meta: Meta


def parse_week(text: str | None, today: date) -> date:
    """The Monday of ISO week `YYYY-Www`; by default the last whole week (the Monday before this week's)."""
    if not text:
        return today - timedelta(days=today.weekday() + 7)
    match = re.fullmatch(r"(\d{4})-W(\d{1,2})", text.strip().upper())
    if not match:
        raise ApiError(400, "bad_request", "week must be an ISO week like 2026-W39")
    try:
        first = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        raise ApiError(400, "bad_request", "that week doesn't exist") from None
    if not (EARLIEST <= first and first + timedelta(days=6) <= LATEST):
        raise ApiError(400, "bad_request", f"the week must be between {EARLIEST} and {LATEST}")
    return first


@router.get("/wrapped", response_model=Wrapped, summary="The week in review: top stats and three checked lines")
def wrapped(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    llm: Annotated[LLM, Depends(get_llm)],
    week: Annotated[str | None, Query(description="ISO week, e.g. 2026-W39; default: last week")] = None,
    tz: Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")] = None,
) -> Wrapped:
    zone, zone_name = resolve_tz(tz)
    now = current_time()
    first = parse_week(week, now.astimezone(zone).date())
    last = first + timedelta(days=6)
    span = RangeInfo(first=first, last=last, days=7, label=f"Week of {first.isoformat()}")
    with database.connect() as conn:  # one Stats for the numbers and the facts: each day is read once
        stats = Stats(conn, zone, zone_name, now)
        builder = TabBuilder(stats, conn, span)
        overview = builder.overview_metrics()
        top = builder.top_apps(5)
        facts = week_facts(stats, first, last)
        meta = stats.meta(first, last, any(metric.estimated for metric in overview))
    written = week_wrapped(database, llm, first, zone, zone_name, now, facts=facts)
    year, number, _ = first.isocalendar()
    return Wrapped(
        week=f"{year}-W{number:02d}", first=first, last=last, tz=zone_name, in_progress=written.in_progress,
        metrics=overview, top_apps=[AppMinutes(**app) for app in top], lines=written.lines,
        facts_used=[FactOut(**fact.as_dict()) for fact in written.facts], model=written.model, cached=written.cached,
        fallback=written.fallback, reason=written.reason, meta=Meta(**meta),
        streaks=week_streak_highlights(database, zone, zone_name, now, first, last),
    )
