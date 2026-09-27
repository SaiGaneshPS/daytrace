"""DT-41: insights and Wrapped endpoints. DT-31 starts it with GET /insights/day, the numbers behind the Today tab;
DT-41 adds GET /insights/{tab} for the Insights tabs and GET /wrapped for the week in review.

Every number comes from the stats engine (stats.py), so the dashboard never works one out itself and every page
agrees: the timeline's lanes, the hero cards, the story and the charts all count the same pieces.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import OrderedDict, defaultdict
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import Reader, get_database
from ..db import Database
from ..llm import LLM
from ..stats import DESK_TYPES, PHONE_TYPES, Stats
from ..story import week_wrapped
from . import API_PREFIX, ApiError
from .ai import FactOut, get_llm
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
SeriesKind = Literal["trend", "stacked", "bars", "donut", "treemap", "heatmap", "sankey", "scatter", "gauge"]
MAX_RANGE_DAYS = 92
LIVE_CACHE_SECONDS = 60.0  # a range with today in it changes as time passes: worked out again this often
CACHE_SIZE = 64
TREEMAP_APPS = 8  # apps named inside each category; the rest are one "Other apps" box
TOP_APP_BARS = 15
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
    values: list[float | None] = Field(description="One per x label; null where there is no data (not zero).")


class Item(BaseModel):
    name: str
    value: float
    key: str | None = None
    category: str | None = None
    share: float | None = Field(default=None, description="A percent that goes with the item (a block's on-plan share).")
    children: list[Item] | None = None


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
    y: list[str] | None = Field(default=None, description="heatmap: the row labels.")
    lines: list[Line] | None = Field(default=None, description="trend and stacked: one per line; bars: one.")
    items: list[Item] | None = Field(default=None, description="donut, treemap and bars by name.")
    cells: list[Cell] | None = Field(default=None, description="heatmap.")
    nodes: list[str] | None = Field(default=None, description="sankey.")
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


class Meta(BaseModel):
    unit: str
    range: dict[str, str]
    source: str
    estimated: bool


class InsightsTab(BaseModel):
    tab: Tab
    tz: str
    range: RangeInfo
    in_progress: bool = Field(description="True when the range includes today, which is not over: numbers so far.")
    metrics: list[Metric]
    series: dict[str, Series]
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


def _category_name(key: str) -> str:
    return key.capitalize()


class TabBuilder:
    """Builds one tab for one range from one Stats (which loads each day once for all the tab's charts)."""

    def __init__(self, stats: Stats, conn: sqlite3.Connection, span: RangeInfo) -> None:
        self.stats = stats
        self.span = span
        self.days = [span.first + timedelta(days=i) for i in range(span.days)]
        self.labels = [day.isoformat() for day in self.days]
        self.names = {row["device_id"]: row["name"] for row in conn.execute("SELECT device_id, name FROM devices")}

    # -- shared pieces -------------------------------------------------------------------------------------------

    def device(self, device_id: str) -> str:
        return self.names.get(device_id, device_id)

    @property
    def screen_days(self) -> set[str]:
        """The days in the range with screen data (not a day that hasn't begun, nor one nobody sent anything for)."""
        return {item["key"] for item in self.stats.totals(self.span.first, self.span.last, group_by="day")["items"]}

    def per_day(self, row: str) -> tuple[dict[str, dict[str, int]], set[str], bool]:
        """Seconds per (day, row key), the days with data, and whether any was estimated."""
        table = self.stats.crosstab(self.span.first, self.span.last, "day", row)  # type: ignore[arg-type]
        return table["cells"], self.screen_days, table["estimated"]

    def stacked(self, row: str, title: str, explain: str, name=None, category_of=None) -> Series:
        cells, with_data, estimated = self.per_day(row)
        keys = sorted({key for day in cells.values() for key in day}, key=lambda key: -sum(day.get(key, 0) for day in cells.values()))
        lines = [
            Line(name=name(key) if name else key, key=key, category=category_of(key) if category_of else None,
                 values=[minutes(cells.get(day, {}).get(key, 0)) if day in with_data else None for day in self.labels])
            for key in keys
        ]
        return Series(kind="stacked", title=title, unit="minutes", explain=explain, estimated=estimated, x=self.labels, lines=lines)

    def weekday_hours(self) -> Series:
        table = self.stats.crosstab(self.span.first, self.span.last, "day", "hour")
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

    def overview(self) -> tuple[list[Metric], dict[str, Series]]:
        stats, first, last = self.stats, self.span.first, self.span.last
        totals = stats.totals(first, last, group_by="category")
        by_day = stats.totals(first, last, group_by="day")
        days_with = len(by_day["items"])
        focused = [stats.focused_minutes(day)["value"] for day in self.days]
        scores = [stats.focus_score(day)["value"] for day in self.days]
        pickups = [stats.pickups(day)["value"] for day in self.days]
        sleep = [stats.sleep_estimate(day) for day in self.days]
        late = [stats.late_night_minutes(day)["value"] for day in self.days]
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
        }
        return metrics, series

    def apps(self) -> tuple[list[Metric], dict[str, Series]]:
        stats, first, last = self.stats, self.span.first, self.span.last
        totals = stats.totals(first, last, group_by="app")
        top = stats.top_apps(first, last, limit=TOP_APP_BARS)
        used = [item for item in totals["items"] if item["seconds"]]
        switches = [stats.switches_per_hour(day)["value"] for day in self.days]
        table = stats.crosstab(first, last, "category", "app")
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
        }
        return metrics, series

    def devices(self) -> tuple[list[Metric], dict[str, Series]]:
        stats, first, last = self.stats, self.span.first, self.span.last
        totals = stats.totals(first, last, group_by="device")
        phones = stats.totals(first, last, device_types=PHONE_TYPES)["total_seconds"]
        desks = stats.totals(first, last, device_types=DESK_TYPES)["total_seconds"]
        hours = stats.crosstab(first, last, "device", "hour")
        flow = stats.crosstab(first, last, "device", "category")
        devices = totals["items"]
        metrics = [
            Metric(id=f"device:{item['key']}", label=self.device(item["key"]), value=item["minutes"], unit="minutes",
                   explain="Screen time on this device in the range.", estimated=totals["estimated"])
            for item in devices
        ]
        metrics.append(Metric(id="phone_share", label="On the phone", value=round(100 * phones / (phones + desks)) if phones + desks else None,
                              unit="percent", explain="The phones' share of phone and computer screen time together."))
        device_rows = [item["key"] for item in devices]
        category_names = sorted({category for row in flow["cells"].values() for category in row})
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
                           nodes=[self.device(device) for device in device_rows] + [_category_name(category) for category in category_names],
                           links=[Link(source=self.device(device), target=_category_name(category), value=minutes(seconds))
                                  for device in device_rows for category, seconds in sorted(flow["cells"].get(device, {}).items()) if seconds]),
        }
        return metrics, series

    def focus(self) -> tuple[list[Metric], dict[str, Series]]:
        stats, first, last = self.stats, self.span.first, self.span.last
        scores = [stats.focus_score(day) for day in self.days]
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
            Metric(id="nights", label="Nights with sleep", value=len([v for v in values if v is not None]), unit="nights",
                   explain="Nights in the range with sleep measured or estimated."),
            Metric(id="estimated_nights", label="Estimated nights", value=len(estimated_nights), unit="nights",
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
        metrics = [
            Metric(id="meals", label="Meals logged", value=len(meals), unit="meals", explain="Meals sent from your phone's health app or the Log Meal shortcut."),
            Metric(id="days_with_meals", label="Days with a meal logged", value=len(by_day), unit="days", explain="Days in the range with at least one meal."),
            Metric(id="top_item", label="Most logged", value=ranked[0][0] if ranked else None, unit="item", explain="The food logged most often."),
        ]
        series = {
            "meals_by_day": Series(kind="stacked", title="Meals by day", unit="meals", x=self.labels,
                                   explain="Meals logged each day, by type.",
                                   lines=[Line(name=kind.capitalize(), key=kind, values=[float(by_day.get(day, {}).get(kind, 0)) for day in self.labels])
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
        grid: dict[tuple[int, int], int] = defaultdict(int)
        for block, day in blocks:
            start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
            for hour, seconds in stats.split_by_hour(start, end):
                grid[(int(hour), day.weekday())] += seconds
        metrics = [
            Metric(id="planned", label="Planned time", value=minutes(planned), unit="minutes", explain="Time in calendar events (not all-day ones), overlaps counted once."),
            Metric(id="on_plan", label="Spent as planned", value=round(100 * on_plan / seen_planned) if seen_planned else None, unit="percent",
                   explain="Of the planned time on days with screen data: work, study or a meeting app on screen."),
            Metric(id="events", label="Events", value=len(blocks), unit="events", explain="Calendar events in the range (not all-day ones)."),
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
                            explain="Planned time by weekday and hour of the day, the whole range added up.",
                            cells=[Cell(x=hour, y=weekday, value=minutes(seconds)) for (hour, weekday), seconds in sorted(grid.items())]),
        }
        return metrics, series


# The tabs' answers, kept per database, tab, range and time zone until the data changes (or, with today in the
# range, for a minute): a page of charts is worked out once.
_cache_guard = threading.Lock()
_cache: OrderedDict[tuple[str, str, date, date, str], tuple[tuple[Any, ...], float, InsightsTab]] = OrderedDict()


def data_version(conn: sqlite3.Connection) -> tuple[Any, ...]:
    """What the insights depend on, cheaply: new or replaced events, category choices, devices paired or revoked."""
    events = tuple(conn.execute("SELECT COUNT(*), MAX(id), MAX(updated_at) FROM events").fetchone())
    overrides = tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM category_overrides").fetchone())
    devices = tuple(conn.execute("SELECT COUNT(*), MAX(paired_at), MAX(revoked_at) FROM devices").fetchone())
    return events + overrides + devices


def build_tab(stats: Stats, conn: sqlite3.Connection, tab: str, span: RangeInfo, tz_name: str) -> InsightsTab:
    builder = TabBuilder(stats, conn, span)
    metrics, series = getattr(builder, tab)()
    windows = [stats.day(day) for day in builder.days]
    estimated = any(item.estimated for item in metrics) or any(item.estimated for item in series.values())
    return InsightsTab(tab=tab, tz=tz_name, range=span, in_progress=any(w.until > w.start and not w.over for w in windows),
                       metrics=metrics, series=series, meta=Meta(**stats.meta(span.first, span.last, estimated)))


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
        with _cache_guard:
            hit = _cache.get(key)
        if hit is not None and hit[0] == version and (not hit[2].in_progress or monotonic() - hit[1] < LIVE_CACHE_SECONDS):
            with _cache_guard:
                _cache.move_to_end(key)
            return hit[2].model_copy(update={"cached": True})
        result = build_tab(Stats(conn, zone, zone_name, now), conn, tab, chosen, zone_name)
    with _cache_guard:
        _cache[key] = (version, monotonic(), result)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
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
    streaks: list[dict[str, Any]] = Field(default_factory=list, description="The week's streak highlights (DT-53); empty until then.")
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
    with database.connect() as conn:
        stats = Stats(conn, zone, zone_name, now)
        overview, _ = TabBuilder(stats, conn, span).overview()
        top = stats.top_apps(first, last, limit=5)
        meta = stats.meta(first, last, any(metric.estimated for metric in overview))
    written = week_wrapped(database, llm, first, zone, zone_name, now)
    year, number, _ = first.isocalendar()
    return Wrapped(
        week=f"{year}-W{number:02d}", first=first, last=last, tz=zone_name, in_progress=written.in_progress,
        metrics=overview, top_apps=[AppMinutes(**app) for app in top], lines=written.lines,
        facts_used=[FactOut(**fact.as_dict()) for fact in written.facts], model=written.model, cached=written.cached,
        fallback=written.fallback, reason=written.reason, meta=Meta(**meta),
    )
