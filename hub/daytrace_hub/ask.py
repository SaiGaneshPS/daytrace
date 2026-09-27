"""DT-40: ask your day: tool calling over the stats engine.

A question ("How much YouTube after 11 pm last week?") goes to the local model (llm.py) with seven tools. Each tool
calls the stats engine (stats.py) or the streaks engine (streaks.py) and returns facts, so every number the model sees
comes from plain code:

- get_totals: screen time over a range, grouped by app, category, device, hour or day, optionally only for an app
  or site, a category, phones or computers, and a time of day (23:00 to 03:00 runs into the next morning);
- get_sessions: the sessions themselves, with their times;
- get_focus: focused time, focus score, phone pickups and app switches per day;
- get_sleep: sleep per night, and screen time after 11 pm the night before;
- get_calendar: calendar events (not all-day ones), upcoming ones included;
- compare_plan: how calendar time was spent (as planned, off plan), up to now;
- get_streaks: each streak's days in a row up to today, its longest run and what today still needs, and each goal's
  target and today's reading (the Streaks page's numbers, DT-53).

The model may call at most 4 tools per question, each over at most 31 days (get_streaks takes no range: it judges
the runs up to today) and returning at most 40 facts (the summary first), then answers in a few sentences. The answer goes through the day story's number check (story.py):
every amount in it must match one of the facts the tools returned (`facts_used`), and a date must be one the tools
looked at. An answer that fails gets one retry, told what was wrong; after that the facts themselves are shown
(`fallback`). Questions that are not about the person's own day are declined politely: the model answers
OFF_TOPIC and the hub words the reply.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from time import monotonic
from typing import Any

from .api.timeline import EARLIEST, LATEST, day_window
from .categories import CATEGORIES
from .db import Database
from .llm import LLM, LLMError
from .sessions import Session, snap
from .stats import GROUPINGS, Stats
from .story import Fact, clean_reply, duration, sentences, unsupported_numbers

MAX_TOOL_CALLS = 4
MAX_RANGE_DAYS = 31
MAX_FACTS = 40  # per tool call: enough for a month by day, few enough for a small model's context
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 1000
MAX_SENTENCES = 6  # the model is asked for 1 to 4
MAX_TOKENS = 2048  # room for a reasoning model to think before it answers
MAX_ITEMS = 10  # apps, categories or devices listed by name (hours and days are all listed)
# No new model call starts after this; the facts found so far answer instead. One call can still take up to 4
# minutes (llm.TIMEOUT plus a retry), so a question ends within about 8, and the dashboard waits 10.
BUDGET_SECONDS = 240
MAX_SESSIONS = 25
SESSION_JOIN = timedelta(seconds=60)  # pieces of the same app this close are one session in a list
OFF_TOPIC = "OFF_TOPIC"
DECLINED = ("I can only answer questions about your own day: screen time, apps and sites, focus, phone pickups, "
            "sleep, your calendar, and your streaks and goals. Try \"How much YouTube did I watch last week?\"")
NO_ANSWER = ("Sorry, I couldn't answer that from your data. Try asking about your screen time, apps, focus, sleep, "
             "calendar, or your streaks and goals.")
DEVICES = {"phone": frozenset({"android", "ios"}), "computer": frozenset({"windows", "macos"})}
NOUNS = {"app": "apps", "category": "categories", "device": "devices"}  # a count's unit, per grouping


class ToolError(ValueError):
    """Arguments a tool can't use. The model is told why and may try again (within its 4 calls); `facts` hold any
    number the message gives (a limit), so an answer may repeat it."""

    def __init__(self, message: str, facts: Sequence[Fact] = ()) -> None:
        super().__init__(message)
        self.facts = list(facts)


@dataclass
class ToolOutput:
    """What a tool found: facts (the only numbers the answer may use), notes, the days it looked at, and a small
    series the dashboard can draw."""

    facts: list[Fact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    days: set[date] = field(default_factory=set)
    chart: dict[str, Any] | None = None

    def content(self) -> dict[str, Any]:
        body: dict[str, Any] = {"facts": [fact.as_dict() for fact in self.facts]}
        if self.notes:
            body["notes"] = self.notes
        return body


# --- arguments -----------------------------------------------------------------------------------------------------


def _text(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} must be text")
    return value.strip() or None


def _day(args: dict[str, Any], name: str) -> date:
    try:
        day = date.fromisoformat(_text(args, name) or "")
    except (ToolError, ValueError):
        raise ToolError(f"{name} must be a date like 2026-09-25") from None
    if not EARLIEST <= day <= LATEST:
        raise ToolError(f"{name} is out of range")
    return day


def _range(args: dict[str, Any]) -> list[date]:
    first = _day(args, "first_day")
    last = _day(args, "last_day") if args.get("last_day") not in (None, "") else first
    if last < first:
        raise ToolError("last_day must not be before first_day")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise ToolError(f"at most {MAX_RANGE_DAYS} days at a time: ask for a shorter range",
                        [Fact("the most days one question can look at", MAX_RANGE_DAYS, "days")])
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _clock(args: dict[str, Any], name: str) -> time | None:
    value = _text(args, name)
    if value is None:
        return None
    if re.fullmatch(r"\d:\d\d", value):
        value = "0" + value
    if value in ("24:00", "24:00:00"):
        return time(0)
    try:
        clock = time.fromisoformat(value)
    except ValueError:
        clock = None
    if clock is None or clock.tzinfo is not None:  # "23:00Z": local times only
        raise ToolError(f"{name} must be a 24-hour local time like 23:00")
    return clock


def _between(args: dict[str, Any]) -> tuple[time, time] | None:
    start, until = _clock(args, "from_time"), _clock(args, "until_time")
    if start is None and until is None:
        return None
    return start or time(0), until or time(0)


@dataclass(frozen=True)
class _Filters:
    app: str | None
    category: str | None
    device: str | None
    between: tuple[time, time] | None

    @classmethod
    def read(cls, args: dict[str, Any]) -> _Filters:
        app = (_text(args, "app") or "")[:80] or None
        category = _text(args, "category")
        if category is not None and category not in CATEGORIES:
            raise ToolError(f"category must be one of {', '.join(CATEGORIES)}")
        device = _text(args, "device")
        if device is not None and device not in DEVICES:
            raise ToolError("device must be phone or computer")
        return cls(app, category, device, _between(args))

    @property
    def device_types(self) -> frozenset[str] | None:
        return DEVICES[self.device] if self.device else None

    def subject(self) -> str:
        """What is counted, in words: "time in apps matching YouTube on phones between 23:00 and 03:00". An app
        filter matches words of names, so the total is never labelled as if it were one app."""
        text = f"time in apps matching {self.app}" if self.app else f"time on {self.category}" if self.category else "screen time"
        if self.app and self.category:
            text += f" ({self.category})"
        return text + self.scope(app=False, category=False)

    def scope(self, app: bool = True, category: bool = True) -> str:
        text = ""
        if category and self.category:
            text += f" ({self.category})"
        if app and self.app:
            text += f" in apps matching {self.app}"
        if self.device:
            text += f" on {self.device}s"
        if self.between:
            text += f" between {self.between[0]:%H:%M} and {self.between[1]:%H:%M}"
        return text


def _on(day: date) -> str:
    return f"{day:%A} {day.isoformat()}"


def _when(days: Sequence[date]) -> str:
    return f"on {_on(days[0])}" if len(days) == 1 else f"from {_on(days[0])} to {_on(days[-1])}"


def _hhmm(moment: datetime | str, tz: tzinfo) -> str:
    moment = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    return moment.astimezone(tz).strftime("%H:%M")


def _chart(title: str, unit: str, points: Iterable[tuple[str, float]]) -> dict[str, Any] | None:
    points = [{"label": label, "value": value} for label, value in points]
    return {"kind": "bar", "title": title, "unit": unit, "points": points} if len(points) >= 2 else None


def _in_progress_note(stats: Stats, days: Sequence[date], out: ToolOutput) -> None:
    today = stats.now.astimezone(stats.tz).date()
    if days[0] <= today <= days[-1]:
        out.notes.append(f"today ({today.isoformat()}) is not over yet: its numbers are the day so far")
    if days[-1] > today:
        out.notes.append("days after today have no data yet")


# --- tools ---------------------------------------------------------------------------------------------------------


def get_totals(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    days, filters = _range(args), _Filters.read(args)
    group_by = _text(args, "group_by") or "app"
    if group_by not in GROUPINGS:
        raise ToolError(f"group_by must be one of {', '.join(GROUPINGS)}")
    result = stats.totals(days[0], days[-1], group_by, between=filters.between, app=filters.app,
                          category=filters.category, device_types=filters.device_types)
    subject, when = filters.subject(), _when(days)
    out = ToolOutput(days=set(days))
    _in_progress_note(stats, days, out)
    for device, missing in sorted(result["missing"].items()):
        out.notes.append(f"{device} sent nothing on {', '.join(missing)}: its time then is unknown, not zero")
    if result["missing_days"]:
        out.notes.append(f"no screen data at all on {', '.join(result['missing_days'])}")
    counted = [d for d in days if d.isoformat() not in result["missing_days"] and stats.part(d, filters.between).until
               > stats.part(d, filters.between).start]
    if not counted:  # missing is not zero: no total to give
        out.notes.append("no screen data on any of those days, so there is nothing to count")
        return out
    used = [item for item in result["items"] if item["seconds"]]
    # An app filter can match several apps and sites (YouTube, youtube.com). Whatever the grouping, the total and all
    # that is cut from it are then of all of them together ("the whole"), and each app's own time says whose part it
    # is: an answer can't pair one app's time with the average of them all (both are facts, so the number check
    # can't tell).
    matched = (used if group_by == "app" else _matched(stats, days, filters)) if filters.app else []
    many = len(matched) > 1
    whole = subject + (f" (all {len(matched)} together)" if many else "")
    if many:
        out.notes.append(f"the total counts all {len(matched)} apps and sites matching {filters.app} at once, and the daily "
                         "average is of that total; each line starting \"of that total\" is one of them")
    total = round(result["total_minutes"])
    named = f"{subject} (all {len(matched)} together: {_names([item['key'] for item in matched])})" if many else subject
    out.facts.append(Fact(f"{named}, {when}", total, "minutes"))
    if len(days) > 1:
        out.facts.append(Fact(f"daily average of {whole}, over the {len(counted)} days with data, {when}",
                              round(result["total_minutes"] / len(counted)), "minutes"))
    if group_by in NOUNS and not (group_by == "app" and many):  # how many match is in the total's own label then
        what = f"{NOUNS[group_by]} matching {filters.app}" if group_by == "app" and filters.app else NOUNS[group_by]
        out.facts.append(Fact(f"number of {what} used{filters.scope(app=group_by != 'app')}, {when}", len(used), NOUNS[group_by]))
    items = used[:MAX_ITEMS] if group_by in NOUNS else result["items"]  # every hour and day, in order
    values = [round(item["minutes"]) for item in items]
    if group_by == "app" and many and len(used) <= MAX_ITEMS:
        values = _whole_parts([item["minutes"] for item in items], total)  # parts that add up to the total said
    points = []
    for item, value in zip(items, values, strict=True):
        key = item["key"]
        if group_by == "app":
            one_of = f" (one of the {len(matched)} matching {filters.app})" if many else ""
            label, point = f"{'of that total, ' if many else ''}time in {key}{filters.scope(app=False)}{one_of}, {when}", key
        elif group_by in ("category", "device"):
            label, point = f"time on {key}{filters.scope(category=group_by != 'category')}, {when}", key
        elif group_by == "hour":
            label, point = f"{whole} between {key}:00 and {(int(key) + 1) % 24:02d}:00 (all days together), {when}", f"{key}:00"
        else:
            label, point = f"{whole} on {_on(date.fromisoformat(key))}", key
        out.facts.append(Fact(label, value, "minutes"))
        points.append((point, value))
    if group_by in NOUNS and len(used) > MAX_ITEMS:
        out.notes.append(f"only the {NOUNS[group_by]} with the most time are listed by name")
    if filters.between and filters.between[1] <= filters.between[0]:
        out.days |= {d + timedelta(days=1) for d in days}  # the time after midnight belongs to the night before
        out.notes.append("time after midnight counts for the evening it started on")
    out.chart = _chart(f"{subject}, {when}", "minutes", points)
    return out


@dataclass
class _Run:
    """Pieces of one app on one device, less than a minute apart: one session in a list. `seconds` is the time in
    use (the gaps are not), so it adds up to what get_totals counts."""

    first: Session
    end: datetime
    seconds: int


def _runs(pieces: Iterable[Session]) -> list[_Run]:
    runs: list[_Run] = []
    for piece in sorted(pieces, key=lambda s: (s.device_id, s.start)):
        last = runs[-1] if runs else None
        if (last is not None and last.first.device_id == piece.device_id and piece.start - last.end <= SESSION_JOIN
                and (last.first.app, last.first.app_id, last.first.kind) == (piece.app, piece.app_id, piece.kind)):
            last.end, last.seconds = max(last.end, piece.end), last.seconds + piece.seconds
        else:
            runs.append(_Run(piece, piece.end, piece.seconds))
    return sorted(runs, key=lambda run: run.first.start)


def _matched(stats: Stats, days: Sequence[date], filters: _Filters) -> list[dict[str, Any]]:
    """The apps and sites an app filter matches with time on these days, whatever a total is grouped by."""
    found = stats.totals(days[0], days[-1], "app", between=filters.between, app=filters.app, category=filters.category,
                         device_types=filters.device_types)
    return [item for item in found["items"] if item["seconds"]]


def _names(names: Sequence[str]) -> str:
    """ "YouTube and youtube.com", "A, B and C", "A, B, C and others" (no count: a bare number would lend itself to an
    answer's check)."""
    if len(names) > 3:
        return f"{', '.join(names[:3])} and others"
    return " and ".join(names) if len(names) < 3 else f"{names[0]}, {names[1]} and {names[2]}"


def _whole_parts(minutes: Sequence[float], total: int) -> list[int]:
    """Whole minutes for the parts of a total that add up to the total as said (the largest remainders round up)."""
    parts = [math.floor(value) for value in minutes]
    by_remainder = sorted(range(len(parts)), key=lambda i: minutes[i] - parts[i], reverse=True)
    for i in by_remainder[: max(0, total - sum(parts))]:
        parts[i] += 1
    return parts


def get_sessions(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    days, filters = _range(args), _Filters.read(args)
    pieces: list[Session] = []
    for day in days:
        window = stats.part(day, filters.between)
        pieces += stats.matching(window, app=filters.app, category=filters.category, device_types=filters.device_types)
    runs = _runs(pieces)  # across days too: a session running past midnight is one session
    when = _when(days)
    out = ToolOutput(days=set(days))
    _in_progress_note(stats, days, out)
    names = {run.first.app or run.first.app_id or "unknown" for run in runs}
    together = f" (all {len(names)} apps and sites together)" if filters.app and len(names) > 1 else ""
    if together:
        out.notes.append(f"the time in those sessions counts all {len(names)} apps and sites matching {filters.app} at once; "
                         "each session listed is one of them")
    out.facts.append(Fact(f"number of sessions{filters.scope()}, {when}", len(runs), "sessions"))
    out.facts.append(Fact(f"time in those sessions{together}, {when}", round(sum(run.seconds for run in runs) / 60), "minutes"))
    for run in runs[:MAX_SESSIONS]:
        session, local = run.first, run.first.start.astimezone(stats.tz)
        out.days.add(local.date())
        name = session.app or session.app_id or "unknown"
        out.facts.append(Fact(
            f"{name} ({session.category or 'other'}) on {session.device_id}, {_on(local.date())} "
            f"{_hhmm(session.start, stats.tz)} to {_hhmm(run.end, stats.tz)}", round(run.seconds / 60), "minutes"))
    if len(runs) > MAX_SESSIONS:
        out.notes.append("only the first sessions are listed: ask for fewer days or a filter to see the rest")
    return out


def get_focus(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    days = _range(args)
    out = ToolOutput(days=set(days))
    _in_progress_note(stats, days, out)
    per_day: list[Fact] = []
    focused: list[float] = []
    scores: list[tuple[str, float]] = []
    for day in days:
        on = f"on {_on(day)}"
        focus, score = stats.focused_minutes(day), stats.focus_score(day)
        pickups, switches = stats.pickups(day), stats.switches_per_hour(day)
        if focus["value"] is None and pickups["value"] is None:
            out.notes.append(f"no screen data on {day.isoformat()}")
            continue
        if focus["value"] is not None:
            focused.append(focus["value"])
            per_day.append(Fact(f"focused time (work or study blocks of 10+ minutes) {on}", round(focus["value"]), "minutes"))
        if score["value"] is not None:
            scores.append((day.isoformat(), score["value"]))
            per_day.append(Fact(f"focus score (0 to 100) {on}", score["value"], "score"))
        if pickups["value"] is not None:
            per_day.append(Fact(f"phone pickups {on}", pickups["value"], "times"))
        if switches["value"] is not None:
            per_day.append(Fact(f"app switches per hour of screen time {on}", switches["value"], "per hour"))
    if len(days) > 1 and focused:
        out.facts.append(Fact(f"average focused time per day over the {len(focused)} days with data, {_when(days)}",
                              round(sum(focused) / len(focused)), "minutes"))
    if len(days) > 1 and scores:
        out.facts.append(Fact(f"average focus score over the {len(scores)} days with a score, {_when(days)}",
                              round(sum(score for _, score in scores) / len(scores)), "score"))
    out.facts += per_day
    out.chart = _chart(f"focus score, {_when(days)}", "score", scores)
    return out


def get_sleep(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    days = _range(args)
    out = ToolOutput(days=set(days) | {d - timedelta(days=1) for d in days})
    per_night: list[Fact] = []
    slept: list[tuple[str, float]] = []
    for day in days:
        night = f"the night before {_on(day)}"
        sleep = stats.sleep_estimate(day)
        if sleep["value"] is None:
            out.notes.append(f"no sleep found for the night before {day.isoformat()}: {sleep.get('reason') or 'no data'}")
        else:
            how = ("" if sleep.get("measured") else " (estimated from when the phone was not used)"
                   if sleep["method"] == "idle_gap" else " (estimated)")
            slept.append((day.isoformat(), round(sleep["value"])))
            per_night.append(Fact(f"sleep {night}{how}", round(sleep["value"]), "minutes"))
            if sleep.get("start") and sleep.get("end"):
                per_night.append(Fact(f"fell asleep, {night}", _hhmm(sleep["start"], stats.tz), "time"))
                per_night.append(Fact(f"woke up on {_on(day)}", _hhmm(sleep["end"], stats.tz), "time"))
        late = stats.late_night_minutes(day - timedelta(days=1))
        if late["value"] is not None:
            per_night.append(Fact(f"screen time after 11 pm {night}", round(late["value"]), "minutes"))
    if len(days) > 1 and slept:
        out.facts.append(Fact(f"average sleep over the {len(slept)} nights with data, {_when(days)}",
                              round(sum(minutes for _, minutes in slept) / len(slept)), "minutes"))
    out.facts += per_night
    out.chart = _chart(f"sleep, {_when(days)}", "minutes", slept)
    return out


def get_calendar(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    """Every timed calendar event of those days as planned, upcoming ones included (compare_plan says how the
    time went, up to now). The same event from two phones is listed once."""
    days = _range(args)
    out = ToolOutput(days=set(days))
    events: list[Fact] = []
    for day in days:
        start, end = day_window(day, stats.tz)
        seen: set[tuple[str | None, datetime, datetime]] = set()
        for event in sorted(stats.day(day).events, key=lambda e: e.start):
            if event.kind != "calendar_event" or event.end is None or event.data.get("all_day") is True:
                continue
            begins, ends = max(snap(event.start), start), min(snap(event.end), end)
            key = (event.title, begins, ends)
            if ends <= begins or key in seen:
                continue
            seen.add(key)
            title = " ".join(str(event.title or "untitled").split())[:80]
            events.append(Fact(f"calendar: {title}, {_on(day)} {_hhmm(begins, stats.tz)} to {_hhmm(ends, stats.tz)}",
                               round((ends - begins).total_seconds() / 60), "minutes"))
    out.facts.append(Fact(f"number of calendar events (not all-day ones), {_when(days)}", len(events), "events"))
    out.facts += events
    return out


def compare_plan(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    days = _range(args)
    out = ToolOutput(days=set(days))
    _in_progress_note(stats, days, out)
    totals = dict.fromkeys(("planned", "on_plan", "off_plan"), 0)
    per_day: list[Fact] = []
    points = []
    for day in days:
        plan = stats.planned_vs_actual(day)
        if not plan["blocks"]:
            continue
        on = f"on {_on(day)}"
        if plan["value"] is None:
            out.notes.append(f"no screen data on {day.isoformat()}, so how its calendar time went is unknown")
            continue
        seconds = plan["totals_seconds"]
        for name in totals:
            totals[name] += seconds[name]
        per_day.append(Fact(f"planned calendar time up to now {on}", round(seconds["planned"] / 60), "minutes"))
        per_day.append(Fact(f"share of planned time spent as planned {on}", plan["value"], "percent"))
        points.append((day.isoformat(), plan["value"]))
    if not totals["planned"]:
        if not out.notes:
            out.notes.append("no calendar events up to now then (all-day events are not counted)")
        return out
    when = _when(days)
    if len(days) > 1:
        out.facts.append(Fact(f"planned calendar time up to now, {when}", round(totals["planned"] / 60), "minutes"))
        out.facts.append(Fact(f"share of planned time spent as planned, {when}",
                              round(100 * totals["on_plan"] / totals["planned"]), "percent"))
    out.facts.append(Fact(f"time spent as planned (work, study or meetings) during calendar blocks, {when}",
                          round(totals["on_plan"] / 60), "minutes"))
    out.facts.append(Fact(f"time off plan (social, video or games) during calendar blocks, {when}",
                          round(totals["off_plan"] / 60), "minutes"))
    out.facts += per_day
    out.chart = _chart(f"share of planned time spent as planned, {when}", "percent", points)
    return out


# --- the tool list the model sees ------------------------------------------------------------------------------------

_RANGE = {
    "first_day": {"type": "string", "description": "first day, YYYY-MM-DD"},
    "last_day": {"type": "string", "description": "last day, YYYY-MM-DD (the same as first_day for one day; at "
                                                  f"most {MAX_RANGE_DAYS} days in all)"},
}
_FILTERS = {
    "app": {"type": "string", "description": "only apps and sites with this in their name, e.g. YouTube"},
    "category": {"type": "string", "enum": list(CATEGORIES)},
    "device": {"type": "string", "enum": list(DEVICES)},
    "from_time": {"type": "string", "description": "only from this local time each day, HH:MM (24-hour)"},
    "until_time": {"type": "string", "description": "only until this local time, HH:MM; earlier than from_time "
                                                    "means the next morning (23:00 to 03:00)"},
}


def _schema(name: str, description: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {**_RANGE, **(extra or {})}, "required": ["first_day", "last_day"]},
    }}


def get_streaks(stats: Stats, args: dict[str, Any]) -> ToolOutput:
    """The streaks and goals as the Streaks page shows them, judged now (its own evaluation when the question's
    database is known): each streak's days in a row up to today and its longest run, what today still needs to keep
    it (rounded up, and only when it can still be done today) or the room left under a limit (rounded down), and each
    goal's target, today's reading and verdict. A reading and what is left always add up to the target: a reading
    toward a target is rounded down, one against a limit up. Any range asked for is ignored: a streak is the run up to
    today, and only today, the evening before (a night's reading) and a run's start within 31 days may be named."""
    from . import streaks

    rules = streaks.load_rules()
    if stats.database is not None:
        found = streaks.evaluation(stats.database, stats.tz, stats.tz_name, stats.now)
    else:
        found = streaks.evaluate(stats.conn, stats.tz, stats.tz_name, stats.now, rules)
    today = found.today
    out = ToolOutput(days={today, today - timedelta(days=1)})
    status_words = {"met": "done for today", "missed": "missed today", "no_data": "nothing yet today (no data)",
                    "at_risk": "not kept yet today"}
    for track in found.streaks:
        run, best = track.current, track.best
        inferred = " (partly estimated)" if any(day.estimated for day in track.days) else ""
        out.facts.append(Fact(f"{track.name} streak ({track.rule}): days in a row up to today{inferred}", len(run), "days"))
        out.facts.append(Fact(f"{track.name} streak: its longest run{inferred}", len(best), "days"))
        said = f"{track.name} streak: {status_words[track.today.status]}"
        need = streaks.still_to_go(track, stats.now, stats.tz)
        room = streaks.room_left(track)
        if need is not None:
            amount, doable = need
            if doable:
                aim = "to keep it" if run else "to start a new run"
                out.facts.append(Fact(f"{track.name} streak: still needed today {aim}", amount, track.unit))
            else:
                said += "; it can't be kept today any more: there isn't enough of the day left"
        elif room is not None:
            out.facts.append(Fact(f"{track.name} streak: room left under its limit today", room, track.unit))
        if run and (today - run[0]).days < MAX_RANGE_DAYS:
            out.days.add(run[0])
            said += f"; this run started on {_on(run[0])}"
        out.notes.append(said)
    for goal in found.goals.values():
        rule = rules.goals[goal.id]
        estimated = " (estimated)" if goal.today.estimated else ""
        if goal.unit == "time":  # a bedtime, as minutes after 18:00
            out.facts.append(Fact(f"{goal.name} goal: {goal.rule}", str(rule.show(goal.target or 0)), "time"))
            if goal.today.value is not None:
                out.facts.append(Fact(f"{goal.name}: fell asleep last night{estimated}", str(rule.show(goal.today.value)), "time"))
        else:
            out.facts.append(Fact(f"{goal.name} goal: {goal.rule}", round(goal.target or 0), goal.unit))
            if goal.today.value is not None:
                toward = math.floor(goal.today.value + 1e-9) if goal.kind == "at_least" else math.ceil(goal.today.value - 1e-9)
                out.facts.append(Fact(f"{goal.name}: today so far{estimated}", toward, goal.unit))
        out.notes.append(f"{goal.name} goal: {status_words[goal.today.status].replace('today', 'for today')}"
                         .replace("for for today", "for today"))
    return out


Tool = Callable[[Stats, dict[str, Any]], ToolOutput]
TOOLS: dict[str, tuple[Tool, dict[str, Any]]] = {
    "get_totals": (get_totals, _schema(
        "get_totals", "Screen time over a range of days, grouped by app, category, device, hour of the day or day. "
        "Filters: an app or site, a category, phones or computers, a time of day.",
        {"group_by": {"type": "string", "enum": list(GROUPINGS)}, **_FILTERS})),
    "get_sessions": (get_sessions, _schema(
        "get_sessions", "The sessions themselves (app, device, start and end time), with the same filters as "
        "get_totals. Use it for when something happened.", _FILTERS)),
    "get_focus": (get_focus, _schema(
        "get_focus", "Per day: focused time (work or study blocks of 10+ minutes), focus score (0 to 100), phone "
        "pickups and app switches per hour.")),
    "get_sleep": (get_sleep, _schema(
        "get_sleep", "Per day: sleep the night before (with bedtime and wake time) and screen time after 11 pm that "
        "night.")),
    "get_calendar": (get_calendar, _schema(
        "get_calendar", "Calendar events per day with their times, upcoming ones included (not all-day events).")),
    "compare_plan": (compare_plan, _schema(
        "compare_plan", "How calendar time was spent, up to now: planned time, the share spent as planned (work, "
        "study or meetings) and time off plan (social, video or games).")),
    "get_streaks": (get_streaks, {"type": "function", "function": {
        "name": "get_streaks", "description": "The streaks and daily goals now: each streak's days in a row up to today, "
        "its longest run and what today still needs; each goal's target and today's reading. No range needed.",
        "parameters": {"type": "object", "properties": {}, "required": []}}}),
}
TOOL_SCHEMAS = [schema for _, schema in TOOLS.values()]


def run_tool(stats: Stats, name: str, arguments: str | None) -> ToolOutput:
    """One tool call from the model. Raises ToolError for a tool or arguments it can't use."""
    if name not in TOOLS:
        raise ToolError(f"there is no tool called {name}; use one of {', '.join(TOOLS)}")
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        raise ToolError("the arguments were not valid JSON") from None
    if not isinstance(args, dict):
        raise ToolError("the arguments must be a JSON object")
    output = TOOLS[name][0](stats, args)
    if len(output.facts) > MAX_FACTS:  # the summary comes first, so it is kept
        output.facts = output.facts[:MAX_FACTS]
        output.notes.append("only the first facts are listed: ask for fewer days to see the rest")
    return output


# --- asking --------------------------------------------------------------------------------------------------------


def _span(first: date, last: date) -> str:
    return f"just {_on(first)}" if first == last else f"{_on(first)} to {_on(last)}"


@dataclass(frozen=True)
class DateGuide:
    text: str
    days: frozenset[date]  # every day the text names, which an answer may repeat


def date_guide(today: date) -> DateGuide:
    """The dates behind the relative phrases people use, for the prompt. Small models get "last week" wrong when left
    to work it out from today's date, so the model looks dates up here instead of counting."""
    monday = today - timedelta(days=today.weekday())
    this_month = today.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    week_back = [today - timedelta(days=n) for n in range(1, 8)]  # newest first: each weekday named once
    phrases = [
        ("yesterday", _on(today - timedelta(days=1))),
        ("tomorrow", _on(today + timedelta(days=1))),
        ("last night", f"get_sleep's entry for today, {_on(today)} (each night is listed under the morning it ends)"),
        ("this week", _span(monday, today)),
        ("last week", _span(monday - timedelta(days=7), monday - timedelta(days=1))),
        ("this weekend", _span(monday + timedelta(days=5), monday + timedelta(days=6))),
        ("last weekend", _span(monday - timedelta(days=2), monday - timedelta(days=1))),
        ("the last 7 days", f"the 7 full days {_span(week_back[-1], week_back[0])}"),
        ("this month", _span(this_month, today)),
        ("last month", _span(last_month, this_month - timedelta(days=1))),
    ]
    text = (
        "Weeks run Monday to Sunday. When the question says one of these, use these dates in the tool calls: "
        + "; ".join(f"{phrase}: {dates}" for phrase, dates in phrases)
        + ". A day named without a date (\"on Tuesday\", \"last Friday\") is the one in the past week: "
        + ", ".join(_on(day) for day in week_back) + ". For any other day or period, use the dates the question gives."
    )
    # Last month's first day is the earliest day named, tomorrow the latest.
    days = frozenset(last_month + timedelta(days=n) for n in range((today - last_month).days + 2))
    return DateGuide(text, days)


def system_prompt(today: date, tz_name: str) -> str:
    return (
        "You answer questions about the person's own day from their Daytrace data (screen time per app, site, "
        "category and device; sessions; focus; phone pickups; sleep; calendar; streaks and daily goals), speaking to "
        f"them as \"you\". Today is {_on(today)} ({tz_name}). {date_guide(today).text} Call the tools to get facts; "
        f"each covers at most {MAX_RANGE_DAYS} days (get_streaks needs no range: it gives the runs up to today), and you "
        f"may call at most {MAX_TOOL_CALLS}. Use only numbers from "
        "the tool results: never estimate, add up or work out a number yourself (minutes may be written as hours and "
        "minutes, 125 minutes = 2 hours 5 minutes). If the tools found no data, say so. Answer in 1 to 4 short "
        "sentences, with no lists or headings. If the question is not about the person's own day, screen time, apps, "
        f"focus, sleep, calendar, streaks, goals or habits, reply with just {OFF_TOPIC}."
    )


def answer_problems(answer: str, facts: Sequence[Fact], days: Iterable[date], cut_off: bool = False) -> list[str]:
    """Why an answer can't be used, in words the model can act on; empty when it is fine. Counts of apps and
    categories come from facts here, not from how many are listed (unlike the story's top 3)."""
    if cut_off:
        return ["it was cut off before it finished"]
    if not answer:
        return ["it was empty"]
    problems = []
    wrong = unsupported_numbers(answer, facts, days, count_listed=False)
    if wrong:
        hint = "; call a tool to get facts first" if not facts else ""
        problems.append(f"it used numbers that are not in the tool results ({', '.join(wrong)}){hint}")
    count = sentences(answer)
    if count > MAX_SENTENCES:
        problems.append(f"it had {count} sentences instead of 1 to 4")
    if len(answer) > MAX_ANSWER_CHARS:
        problems.append(f"it was {len(answer)} characters long, over the limit of {MAX_ANSWER_CHARS}")
    return problems


def _value_text(fact: Fact) -> str:
    if fact.unit == "minutes":
        return duration(float(fact.value))
    if fact.unit == "score":
        return f"{fact.value} out of 100"
    if fact.unit == "percent":
        return f"{fact.value}%"
    if fact.unit == "per hour":
        return f"{fact.value} per hour"
    if fact.unit in ("times", "time"):
        return str(fact.value)
    unit = fact.unit[:-1] if str(fact.value) == "1" and fact.unit.endswith("s") else fact.unit
    return f"{fact.value} {unit}"  # a count of something: "4 sessions", "1 day"


def facts_answer(facts: Sequence[Fact]) -> str:
    """The plain answer used when the model's answer keeps failing the number check: the first facts found."""
    if not facts:
        return NO_ANSWER
    return "Here is what I found: " + "; ".join(f"{fact.label}: {_value_text(fact)}" for fact in facts[:6]) + "."


@dataclass
class AskResult:
    answer: str
    facts: list[Fact]
    tools_called: list[str]
    chart: dict[str, Any] | None
    model: str | None
    fallback: bool = False
    declined: bool = False
    reason: str | None = None
    meta: dict[str, Any] | None = None  # the days the tools read: their range, source and whether any was inferred


def _unique(facts: Iterable[Fact]) -> list[Fact]:
    return list(dict.fromkeys(facts))


def ask(database: Database, llm: LLM, question: str, tz: tzinfo, tz_name: str, now: datetime | None = None) -> AskResult:
    """Answer one question from the data. Raises LLMError when the model can't be used before any facts are in.
    One read connection serves the whole question, so the tools share what the stats engine has loaded."""
    question = " ".join(question.split())[:MAX_QUESTION_CHARS]
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    model = llm.current_model()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(today, tz_name)},
        {"role": "user", "content": question},
    ]
    facts: list[Fact] = []
    days: set[date] = {today, *date_guide(today).days}  # an answer may repeat the dates the prompt gave
    called: list[str] = []
    chart: dict[str, Any] | None = None
    retried = False
    read: set[date] = set()  # the days the tools looked at: what the answer's numbers are about

    def meta() -> dict[str, Any] | None:
        known = sorted(day for day in read if day <= today)
        if not known:
            return None
        return stats.meta(known[0], known[-1], any(stats.day_estimated(day) for day in known), unit=None)

    def fallback(reason: str) -> AskResult:
        return AskResult(facts_answer(_unique(facts)), _unique(facts), called, chart, None, True, False, reason, meta())

    started = monotonic()
    with database.connect() as conn:
        stats = Stats(conn, tz, tz_name, now, database=database)
        while True:
            offer_tools = len(called) < MAX_TOOL_CALLS
            if called and monotonic() - started > BUDGET_SECONDS:
                return fallback(f"{model} took more than {BUDGET_SECONDS // 60} minutes, so the answer is the facts "
                                "found so far")
            try:
                choice = llm.complete(messages, tools=TOOL_SCHEMAS if offer_tools else None, temperature=0.2,
                                      max_tokens=MAX_TOKENS, model=model)
            except LLMError as error:
                if not facts:
                    raise
                return fallback(str(error))
            message = choice.message
            calls = [call for call in (getattr(message, "tool_calls", None) or []) if getattr(call, "function", None)]
            if calls and offer_tools:
                ids = [call.id or f"call_{len(called)}_{i}" for i, call in enumerate(calls)]
                messages.append({"role": "assistant", "content": message.content, "tool_calls": [
                    {"id": call_id, "type": "function",
                     "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"}}
                    for call_id, call in zip(ids, calls, strict=True)
                ]})
                for call_id, call in zip(ids, calls, strict=True):
                    if len(called) >= MAX_TOOL_CALLS:
                        content: dict[str, Any] = {"error": "no more tool calls for this question: answer with the "
                                                            "facts you have"}
                    else:
                        called.append(call.function.name)
                        try:
                            output = run_tool(stats, call.function.name, call.function.arguments)
                        except ToolError as error:
                            facts += error.facts
                            content = {"error": str(error)}
                        except (TypeError, ValueError) as error:  # arguments of a shape no check foresaw
                            content = {"error": f"those arguments could not be used ({error})"}
                        else:
                            facts += output.facts
                            days |= output.days
                            read |= output.days
                            chart = output.chart or chart
                            content = output.content()
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps(content, ensure_ascii=False)})
                continue
            answer = clean_reply(message.content)
            if OFF_TOPIC in answer.upper():
                return AskResult(DECLINED, [], called, None, model, False, True)
            problems = answer_problems(answer, facts, days, cut_off=getattr(choice, "finish_reason", None) == "length")
            if not problems:
                return AskResult(answer, _unique(facts), called, chart, model, meta=meta())
            if retried:
                return fallback(f"the answer from {model} could not be used, even after a retry: {'; '.join(problems)}")
            retried = True
            messages += [
                {"role": "assistant", "content": answer or "(no answer)"},
                {"role": "user", "content": f"That answer cannot be used: {'; '.join(problems)}. Answer again in 1 to 4 "
                                            "sentences, using only numbers from the tool results."},
            ]
