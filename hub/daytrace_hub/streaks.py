"""DT-53: daily goals, streaks and one-time achievements, worked out from the stats engine.

The rules are in data/streak_rules.json; every number behind them comes from stats.py, so a streak always agrees
with Today and Insights, and each one can show its rule and the days that counted.

- A goal is a daily target the user sets (the goals table; the rule's default until they change it): focused time
  of at least N minutes, social apps for at most N minutes, asleep by a time. Every day is judged against it.
- A streak counts the days in a row that met a rule: a goal (Focus flame is the focus target, Balanced the social
  cap) or a fixed target of its own (Screens down: 15 minutes or less on the phone after 11 pm).
- Each day is met, missed or no_data. No data means what the rule needs sent nothing for that day (a computer
  for focus, a phone for meals and for the night before): such a day neither extends nor breaks a streak.
- Today is met as soon as it qualifies, but a limit ("at most") only once what it measures is over (the day, the
  night, or the sleep window). Until then today is at_risk, with what is left (the minutes still to go, or the
  room left under the limit), and the streak shows its days up to yesterday. Once today can no longer qualify
  (minutes past the limit), it is missed and the streak is 0.
- best is the longest run in the hub's history (up to a year).
- Goals apply to the whole history: a new focus target judges the past days again, so a streak always means what
  its rule says now. Achievements are different: once earned, a badge is kept (the achievements table).

A day's readings that are final (what they measure is over) are kept per database, time zone and data version,
so each new minute only reads the days that can still change; a year is read a month at a time, so the windows
the stats engine loads never pile up.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import cached_property, lru_cache
from importlib import resources
from typing import Any, Literal

from .db import Database, data_version, transaction, utc_text
from .sessions import parse_utc
from .stats import (
    COUNTED_TYPES,
    DESK_TYPES,
    LATE_FROM,
    LATE_UNTIL,
    PHONE_TYPES,
    SCREEN_KINDS,
    SLEEP_UNTIL,
    Stats,
)

Kind = Literal["at_least", "at_most"]
KINDS = ("at_least", "at_most")
DayStatus = Literal["met", "missed", "at_risk", "no_data"]
HISTORY_DAYS = 366  # a year of days judged; best streaks older than that are forgotten
EVENING = time(18)  # bedtimes are minutes after 18:00 the evening before, so 23:30 and 00:30 compare as they should
DEVICE_TYPES = ("windows", "macos", "android", "ios")  # the full set of devices a badge asks for
READ_CHUNK = 31  # days read with one Stats before its loaded windows are let go
PERFECT_WEEK_MET_DAYS = 5  # a perfect week: no goal missed, and each goal met on at least this many days
CACHE_SIZE = 16


# --- the rules --------------------------------------------------------------------------------------------------


def clock_minutes(text: str) -> int:
    """"HH:MM" as minutes after 18:00 the evening before (23:30 is 330, 00:30 is 390)."""
    hours, minutes = (int(part) for part in text.split(":"))
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"not a time of day: {text!r}")
    return (hours * 60 + minutes - EVENING.hour * 60) % 1440


def clock_text(value: float) -> str:
    """Minutes after 18:00 the evening before as "HH:MM"."""
    total = (EVENING.hour * 60 + int(value)) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


@dataclass(frozen=True)
class GoalRule:
    id: str
    label: str
    measure: str
    kind: Kind
    unit: str  # "minutes", or "time" for a clock time (kept as minutes after 18:00)
    default: float | str
    low: float
    high: float
    rule: str
    explain: str

    def parse(self, target: Any) -> float:
        """A target as the user gives it (a number of minutes, or "HH:MM"), on the measure's scale. TypeError when it
        is the wrong type, ValueError when it is out of range."""
        if self.unit == "time":
            if not isinstance(target, str):
                raise TypeError(f"{self.label} is a time like \"23:30\"")
            try:
                value = float(clock_minutes(target))
            except ValueError:
                raise ValueError(f"{self.label} is a time like \"23:30\"") from None
            if not self.low <= value <= self.high:
                raise ValueError(f"{self.label} must be between {clock_text(self.low)} and {clock_text(self.high)}")
            return value
        if isinstance(target, bool) or not isinstance(target, int | float):
            raise TypeError(f"{self.label} is a number of {self.unit}")
        if not self.low <= target <= self.high:
            raise ValueError(f"{self.label} must be between {_number(self.low)} and {_number(self.high)} {self.unit}")
        return float(target)

    def show(self, value: float) -> float | str:
        """A target or reading as the API shows it: minutes as a number, a time as "HH:MM"."""
        return clock_text(value) if self.unit == "time" else value

    def rule_text(self, target: float) -> str:
        shown = self.show(target)
        return self.rule.format(target=shown if isinstance(shown, str) else _number(shown))


@dataclass(frozen=True)
class StreakRule:
    id: str
    name: str
    measure: str
    kind: Kind
    unit: str
    rule: str
    needs: str
    goal: str | None = None  # the goal whose target it uses, or None for its own `target`
    target: float | None = None


@dataclass(frozen=True)
class AchievementRule:
    id: str
    name: str
    kind: str
    rule: str
    amount: int | None = None


@dataclass(frozen=True)
class Rules:
    goals: dict[str, GoalRule]
    streaks: list[StreakRule]
    achievements: list[AchievementRule]

    @property
    def measures(self) -> list[str]:
        return sorted({goal.measure for goal in self.goals.values()} | {streak.measure for streak in self.streaks})


def _check(condition: bool, problem: str) -> None:
    if not condition:
        raise ValueError(f"streak_rules.json: {problem}")


def parse_rules(raw: dict[str, Any]) -> Rules:
    """The rules, checked so a typo fails at startup instead of quietly changing a rule: ids are unique, every
    measure, kind and achievement is one this module knows, every goal a streak names exists, and a streak with its
    own measure has a target (unless the measure brings one, as Synced's paired devices)."""
    goals: dict[str, GoalRule] = {}
    for item in raw["goals"]:
        _check(item["id"] not in goals, f"goal {item['id']} is there twice")
        _check(item["measure"] in MEASURES, f"goal {item['id']} has an unknown measure {item['measure']!r}")
        _check(item["kind"] in KINDS, f"goal {item['id']} has an unknown kind {item['kind']!r}")
        _check(item["unit"] in ("minutes", "time"), f"goal {item['id']} has an unknown unit {item['unit']!r}")
        low, high = item["min"], item["max"]
        if item["unit"] == "time":
            low, high = clock_minutes(low), clock_minutes(high)
        goal = GoalRule(item["id"], item["label"], item["measure"], item["kind"], item["unit"], item["default"],
                        float(low), float(high), item["rule"], item["explain"])
        goal.parse(goal.default)  # the default must be a valid target itself
        goals[goal.id] = goal
    streaks: list[StreakRule] = []
    for item in raw["streaks"]:
        _check(item["id"] not in {streak.id for streak in streaks}, f"streak {item['id']} is there twice")
        _check(bool(item.get("needs")), f"streak {item['id']} doesn't say what it needs")
        if "goal" in item:
            _check(item["goal"] in goals, f"streak {item['id']} names an unknown goal {item['goal']!r}")
            goal = goals[item["goal"]]
            streaks.append(StreakRule(item["id"], item["name"], goal.measure, goal.kind, goal.unit, goal.rule,
                                      item["needs"], goal=goal.id))
            continue
        _check(item["measure"] in MEASURES, f"streak {item['id']} has an unknown measure {item['measure']!r}")
        _check(item["kind"] in KINDS, f"streak {item['id']} has an unknown kind {item['kind']!r}")
        target = item.get("target")
        _check(target is not None or item["measure"] in OWN_TARGETS, f"streak {item['id']} has no target")
        streaks.append(StreakRule(item["id"], item["name"], item["measure"], item["kind"], item["unit"], item["rule"],
                                  item["needs"], target=float(target) if target is not None else None))
    achievements: list[AchievementRule] = []
    for item in raw["achievements"]:
        _check(item["id"] not in {badge.id for badge in achievements}, f"achievement {item['id']} is there twice")
        _check(item["kind"] in ACHIEVEMENT_KINDS, f"achievement {item['id']} has an unknown kind {item['kind']!r}")
        achievements.append(AchievementRule(item["id"], item["name"], item["kind"], item["rule"], item.get("amount")))
    return Rules(goals, streaks, achievements)


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    return parse_rules(json.loads(resources.files("daytrace_hub").joinpath("data", "streak_rules.json").read_text(encoding="utf-8")))


# --- goals the user chose ---------------------------------------------------------------------------------------


def load_targets(conn: sqlite3.Connection, rules: Rules | None = None) -> dict[str, float]:
    """Each goal's target on its measure's scale: the user's choice, else the default. A stored target the rules no
    longer allow (a rule changed) falls back to the default rather than breaking the page."""
    rules = rules or load_rules()
    stored = {row["goal_id"]: row["target"] for row in conn.execute("SELECT goal_id, target FROM goals")}
    targets = {}
    for goal in rules.goals.values():
        try:
            targets[goal.id] = goal.parse(json.loads(stored[goal.id])) if goal.id in stored else goal.parse(goal.default)
        except (ValueError, TypeError, json.JSONDecodeError):
            targets[goal.id] = goal.parse(goal.default)
    return targets


def save_target(conn: sqlite3.Connection, goal: GoalRule, target: Any, now: datetime | None = None) -> float:
    """Store a goal's new target (checked first: TypeError or ValueError when it isn't allowed). Returns it on the
    measure's scale."""
    value = goal.parse(target)
    conn.execute(
        "INSERT INTO goals (goal_id, target, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (goal_id) DO UPDATE SET target = excluded.target, updated_at = excluded.updated_at",
        (goal.id, json.dumps(target), utc_text(now or datetime.now(UTC))),
    )
    return value


# --- reading a day ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    value: float | None  # None: no data from what the rule needs
    final: bool  # what it measures is over: only new data can change it now
    target: float | None = None  # a target of its own that day (the devices paired then, for Synced)
    grows: bool = True  # it can only go up until final (minutes, meals), so past a limit is past it for good


def _phones_with_data(stats: Stats, start: datetime, end: datetime) -> set[str]:
    window = stats.window(start, end)
    return {device for device in window.counted_devices_with_data if window.device_types.get(device) in PHONE_TYPES}


def _focused_minutes(stats: Stats, day: date) -> Reading:
    window = stats.day(day)
    if not any(window.device_types.get(device) in DESK_TYPES for device in window.counted_devices_with_data):
        return Reading(None, window.over)  # no computer: nothing to focus on, which says nothing either way
    return Reading(stats.focused_minutes(day)["value"] or 0.0, window.over)


def _social_minutes(stats: Stats, day: date) -> Reading:
    window = stats.day(day)
    totals = stats.totals(day, category="social")
    if window.until <= window.start or day.isoformat() in totals["missing_days"]:
        return Reading(None, window.over)
    return Reading(totals["total_minutes"], window.over)


def _late_phone_minutes(stats: Stats, day: date) -> Reading:
    """Phone time from 23:00 the evening before `day` to 03:00 on it. A night is only judged between two signs of the
    phone: it sent data that evening (from 18:00) and again on `day`. A phone that fell silent at noon, or hasn't
    been picked up yet this morning, has told nothing about the night."""
    night = day - timedelta(days=1)
    if stats.now <= stats.at(night, LATE_FROM):
        return Reading(None, False)
    evening = _phones_with_data(stats, stats.at(night, EVENING), stats.at(day, LATE_UNTIL))
    window = stats.day(day)
    after = {device for device in window.counted_devices_with_data if window.device_types.get(device) in PHONE_TYPES}
    if not evening & after:
        return Reading(None, window.over)
    totals = stats.totals(night, between=(LATE_FROM, LATE_UNTIL), device_types=PHONE_TYPES)
    return Reading(totals["total_minutes"], stats.now >= stats.at(day, LATE_UNTIL))


def _meals(stats: Stats, day: date) -> Reading:
    """Meals logged on `day`, when a phone (the Log Meal shortcut, the health app) sent anything that day: a day the
    phone was off is no data, not 0 meals."""
    window = stats.day(day)
    phones = {event.device_id for event in window.events
              if window.device_types.get(event.device_id) in PHONE_TYPES and window.overlaps(event, window.start, window.until)}
    if not phones:
        return Reading(None, window.over)
    return Reading(float(len(stats.meals(day))), window.over)


def _devices_synced(stats: Stats, day: date) -> Reading:
    window = stats.day(day)
    if window.until <= window.start:
        return Reading(None, False)
    totals = stats.totals(day, group_by="device")
    synced = {item["key"] for item in totals["items"]}
    expected = synced | set(totals["missing"])  # paired that day, or sent data anyway
    if not expected:
        return Reading(None, window.over)
    return Reading(float(len(synced)), window.over, float(len(expected)))


def _bedtime(stats: Stats, day: date) -> Reading:
    """When sleep started the night before `day`, by the wall clock, as minutes after 18:00 that evening. Final once
    the health app sent the night, or the sleep window (until 12:00) is over: an estimate from the phone can still
    move to a later, longer quiet stretch until then."""
    night = stats.sleep_estimate(day)
    if night["value"] is None or not night.get("start"):
        return Reading(None, stats.now >= stats.at(day, SLEEP_UNTIL))
    start = datetime.fromisoformat(night["start"])  # local, so its clock reading is the wall clock (DST included)
    value = float(clock_minutes(f"{start.hour:02d}:{start.minute:02d}"))
    return Reading(value, night.get("method") == "health" or stats.now >= stats.at(day, SLEEP_UNTIL), grows=False)


MEASURES: dict[str, Callable[[Stats, date], Reading]] = {
    "focused_minutes": _focused_minutes,
    "social_minutes": _social_minutes,
    "late_phone_minutes": _late_phone_minutes,
    "meals": _meals,
    "devices_synced": _devices_synced,
    "bedtime": _bedtime,
}
OWN_TARGETS = frozenset({"devices_synced"})  # measures whose reading carries the day's target


def judge(kind: Kind, reading: Reading, target: float | None) -> tuple[DayStatus, float | None]:
    """A day's status against a target, and what is left while it is at_risk (to go, or room under a limit)."""
    goal = reading.target if reading.target is not None else target
    if reading.value is None or goal is None:
        return "no_data", None
    if kind == "at_least":
        if reading.value >= goal:
            return "met", None
        return ("missed", None) if reading.final else ("at_risk", round(goal - reading.value, 2))
    if reading.value > goal and (reading.final or reading.grows):
        return "missed", None
    if not reading.final:
        return "at_risk", round(goal - reading.value, 2) if reading.value <= goal else None
    return "met", None


# --- tracks: one rule over the history --------------------------------------------------------------------------


@dataclass(frozen=True)
class DayResult:
    day: date
    status: DayStatus
    value: float | None
    target: float | None
    remaining: float | None


@dataclass
class Track:
    """One goal or streak judged over a run of days, oldest first; for the history, the last day is today."""

    id: str
    name: str
    rule: str
    measure: str
    kind: Kind
    unit: str
    target: float | None
    needs: str
    days: list[DayResult]

    @cached_property
    def _runs(self) -> tuple[list[list[date]], list[date]]:
        """Runs of met days (a no-data day or today at_risk neither extends nor breaks one), and the run still open
        at the end: the current streak."""
        runs: list[list[date]] = []
        run: list[date] = []
        for day in self.days:
            if day.status == "met":
                run.append(day.day)
            elif day.status == "missed":
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        return runs, run

    @property
    def runs(self) -> list[list[date]]:
        return self._runs[0]

    @property
    def current(self) -> list[date]:
        return self._runs[1]

    @property
    def best(self) -> list[date]:
        return max(self.runs, key=lambda run: (len(run), run[-1]), default=[])  # a tie: the later one

    @property
    def today(self) -> DayResult:
        return self.days[-1]

    def between(self, first: date, last: date) -> Track:
        """The same rule over the days from `first` to `last` only (runs inside them)."""
        return replace(self, days=[day for day in self.days if first <= day.day <= last])


@dataclass
class Evaluation:
    """Every goal and streak over a run of days (the history: the first day with data to today), for one database,
    time zone and moment."""

    today: date
    first: date  # the first day judged
    first_data: date | None  # the first day any screen data was recorded
    tz_name: str
    goals: dict[str, Track]
    streaks: list[Track]
    device_type_days: dict[str, date] = field(default_factory=dict)  # each device type's first day with data


def history_start(conn: sqlite3.Connection, tz: tzinfo, today: date) -> tuple[date, date | None]:
    """The first day to judge and the first day with data: the first day with screen data (a night's sleep that
    ended on it is not a day recorded), at most a year back. One index lookup per kind of screen event."""
    kinds = sorted(SCREEN_KINDS)
    first = conn.execute("SELECT MIN(first) FROM (" + " UNION ALL ".join("SELECT MIN(start_utc) AS first FROM events WHERE kind = ?" for _ in kinds) + ")",
                         kinds).fetchone()[0]
    if first is None:
        return today, None
    first_day = parse_utc(first).astimezone(tz).date()
    return max(min(first_day, today), today - timedelta(days=HISTORY_DAYS - 1)), first_day


def device_type_days(conn: sqlite3.Connection, tz: tzinfo) -> dict[str, date]:
    """The first day each kind of phone or computer sent data (each device's first event, by its index)."""
    found: dict[str, date] = {}
    rows = conn.execute("SELECT d.device_type, (SELECT MIN(e.start_utc) FROM events e WHERE e.device_id = d.device_id) AS first"
                        " FROM devices d WHERE d.device_type IN ({})".format(", ".join("?" for _ in COUNTED_TYPES)),
                        sorted(COUNTED_TYPES)).fetchall()
    for row in rows:
        if row["first"] is not None:
            day = parse_utc(row["first"]).astimezone(tz).date()
            found[row["device_type"]] = min(found.get(row["device_type"], day), day)
    return found


Readings = dict[tuple[str, date], Reading]


def evaluate(conn: sqlite3.Connection, tz: tzinfo, tz_name: str, now: datetime, rules: Rules | None = None,
             targets: dict[str, float] | None = None, days: Sequence[date] | None = None, known: Readings | None = None) -> Evaluation:
    """Judge every goal and streak on `days` (by default the history: the first day with data to today).

    Each measure is read once per day, a month of days per Stats (then its windows are let go). `known` holds final
    readings from before for this data version: they are used as they are, and new final ones are added to it."""
    rules = rules or load_rules()
    targets = targets if targets is not None else load_targets(conn, rules)
    now = now.astimezone(UTC)
    today = now.astimezone(tz).date()
    first, first_data = history_start(conn, tz, today)
    days = list(days) if days is not None else [first + timedelta(days=i) for i in range((today - first).days + 1)]
    known = known if known is not None else {}
    readings: Readings = {}
    for start in range(0, len(days), READ_CHUNK):
        needed = [(measure, day) for day in days[start:start + READ_CHUNK] for measure in rules.measures
                  if (measure, day) not in known or not known[(measure, day)].final]
        if needed:
            stats = Stats(conn, tz, tz_name, now)
            for measure, day in needed:
                readings[(measure, day)] = MEASURES[measure](stats, day)
    for key, reading in readings.items():
        if reading.final:
            known[key] = reading

    def results(measure: str, kind: Kind, target: float | None) -> list[DayResult]:
        out = []
        for day in days:
            reading = readings.get((measure, day)) or known[(measure, day)]
            status, remaining = judge(kind, reading, target)
            out.append(DayResult(day, status, reading.value, reading.target if reading.target is not None else target, remaining))
        return out

    goals = {
        goal.id: Track(goal.id, goal.label, goal.rule_text(targets[goal.id]), goal.measure, goal.kind, goal.unit, targets[goal.id],
                       goal.explain, results(goal.measure, goal.kind, targets[goal.id]))
        for goal in rules.goals.values()
    }
    streaks = []
    for rule in rules.streaks:
        if rule.goal is not None:  # the goal's days, judged once
            track = goals[rule.goal]
            streaks.append(Track(rule.id, rule.name, track.rule, track.measure, track.kind, track.unit, track.target, rule.needs, track.days))
        else:
            text = rule.rule.format(target=_number(rule.target)) if rule.target is not None else rule.rule
            streaks.append(Track(rule.id, rule.name, text, rule.measure, rule.kind, rule.unit, rule.target, rule.needs,
                                 results(rule.measure, rule.kind, rule.target)))
    return Evaluation(today, days[0] if days else first, first_data, tz_name, goals, streaks, device_type_days(conn, tz))


# Evaluations, kept per database, time zone, data version, goals and minute (today changes with the clock even when
# no data arrives), and final readings per database, time zone and data version. A request that finds another one
# working out the same evaluation waits for it instead of doing it again (the dashboard asks for streaks, goals and
# badges together).
_cache_guard = threading.Lock()
_cache: OrderedDict[tuple[Any, ...], Evaluation] = OrderedDict()
_building: dict[tuple[Any, ...], threading.Lock] = {}
_readings: OrderedDict[tuple[str, str, int], Readings] = OrderedDict()


def _known(key: tuple[str, str, int]) -> Readings:
    with _cache_guard:
        if key not in _readings:
            _readings[key] = {}
        _readings.move_to_end(key)
        while len(_readings) > 4:
            _readings.popitem(last=False)
        return _readings[key]


def evaluation(database: Database, tz: tzinfo, tz_name: str, now: datetime | None = None) -> Evaluation:
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    rules = load_rules()
    with database.connect() as conn:
        version, targets = data_version(conn), load_targets(conn, rules)
        key = (str(database.path), tz_name, version, tuple(sorted(targets.items())), now.replace(second=0, microsecond=0))
        with _cache_guard:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
            building = _building.setdefault(key, threading.Lock())
        try:
            with building:
                with _cache_guard:
                    if key in _cache:  # worked out while this one waited
                        return _cache[key]
                result = evaluate(conn, tz, tz_name, now, rules, targets, known=_known((str(database.path), tz_name, version)))
                with _cache_guard:
                    _cache[key] = result
                    _cache.move_to_end(key)
                    while len(_cache) > CACHE_SIZE:
                        _cache.popitem(last=False)
        finally:
            with _cache_guard:
                _building.pop(key, None)
    return result


def evaluate_days(database: Database, tz: tzinfo, tz_name: str, now: datetime, days: Sequence[date]) -> Evaluation:
    """The goals and streaks over some other days (a week older than the history), with final readings shared."""
    now = now.astimezone(UTC)
    with database.connect() as conn:
        known = _known((str(database.path), tz_name, data_version(conn)))
        return evaluate(conn, tz, tz_name, now, days=days, known=known)


# --- achievements -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    value: float
    target: float
    unit: str


@dataclass(frozen=True)
class Earned:
    earned_on: date
    dates: list[date]


def _first_data(rule: AchievementRule, found: Evaluation) -> tuple[Earned | None, Progress | None]:
    return (Earned(found.first_data, [found.first_data]) if found.first_data else None), None


def _every_device_type(rule: AchievementRule, found: Evaluation) -> tuple[Earned | None, Progress | None]:
    days = found.device_type_days
    progress = Progress(len([kind for kind in DEVICE_TYPES if kind in days]), len(DEVICE_TYPES), "device types")
    if all(kind in days for kind in DEVICE_TYPES):
        return Earned(max(days[kind] for kind in DEVICE_TYPES), sorted({days[kind] for kind in DEVICE_TYPES})), progress
    return None, progress


def _streak_days(rule: AchievementRule, found: Evaluation) -> tuple[Earned | None, Progress | None]:
    amount = rule.amount or 1
    reached = [run[:amount] for track in found.streaks for run in track.runs if len(run) >= amount]
    best = max((len(track.best) for track in found.streaks), default=0)
    progress = Progress(min(best, amount), amount, "days")
    if not reached:
        return None, progress
    first = min(reached, key=lambda run: run[-1])  # the earliest day any streak got there
    return Earned(first[-1], first), progress


def _focused_total(rule: AchievementRule, found: Evaluation) -> tuple[Earned | None, Progress | None]:
    amount = rule.amount or 1
    total, counted = 0.0, []
    focus = next((track for track in found.goals.values() if track.measure == "focused_minutes"), None)
    for day in focus.days if focus else []:
        if day.value:
            total += day.value
            counted.append(day.day)
            if total >= amount:
                return Earned(day.day, counted), Progress(amount, amount, "minutes")
    return None, Progress(round(total, 2), amount, "minutes")


def _perfect_week(rule: AchievementRule, found: Evaluation) -> tuple[Earned | None, Progress | None]:
    """A whole week, Monday to Sunday and over, with no goal missed and each goal met on at least
    PERFECT_WEEK_MET_DAYS days: a day without data for a goal (the laptop off on Sunday) doesn't count against it.
    Progress is the most days in one week with every goal met."""
    goals = list(found.goals.values())
    if not goals:
        return None, None
    best = 0
    monday = found.first + timedelta(days=(7 - found.first.weekday()) % 7)  # the first whole week in the history
    while monday + timedelta(days=6) < found.today:  # only weeks that are over
        start = (monday - found.first).days
        week = [[track.days[i].status for track in goals] for i in range(start, start + 7)]
        best = max(best, sum(all(status == "met" for status in day) for day in week))
        no_miss = all(status != "missed" for day in week for status in day)
        if no_miss and all(sum(day[g] == "met" for day in week) >= PERFECT_WEEK_MET_DAYS for g in range(len(goals))):
            return Earned(monday + timedelta(days=6), [monday + timedelta(days=i) for i in range(7)]), Progress(7, 7, "days")
        monday += timedelta(days=7)
    return None, Progress(best, 7, "days")


ACHIEVEMENT_KINDS: dict[str, Callable[[AchievementRule, Evaluation], tuple[Earned | None, Progress | None]]] = {
    "first_data": _first_data,
    "every_device_type": _every_device_type,
    "streak_days": _streak_days,
    "focused_total": _focused_total,
    "perfect_week": _perfect_week,
}


@dataclass(frozen=True)
class AchievementResult:
    rule: AchievementRule
    earned: Earned | None
    unlocked_at: datetime | None
    progress: Progress | None


def _stored(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["achievement_id"]: row for row in conn.execute("SELECT * FROM achievements")}


def achievements(database: Database, found: Evaluation, now: datetime | None = None, rules: Rules | None = None) -> list[AchievementResult]:
    """Every badge: earned ones as first recorded (a badge is never taken back), newly earned ones recorded now,
    the rest with their progress. Stored badges are read first, and the write lock is taken only for a new one;
    when the database is too busy to record it, the badge still shows as earned."""
    rules = rules or load_rules()
    now = (now or datetime.now(UTC)).astimezone(UTC)
    checked = [(rule, *ACHIEVEMENT_KINDS[rule.kind](rule, found)) for rule in rules.achievements]
    with database.connect() as conn:
        stored = _stored(conn)
        new = [(rule, earned) for rule, earned, _ in checked if earned is not None and rule.id not in stored]
        if new:
            try:
                with transaction(conn):
                    for rule, earned in new:
                        conn.execute(
                            "INSERT INTO achievements (achievement_id, earned_on, tz, dates, unlocked_at) VALUES (?, ?, ?, ?, ?)"
                            " ON CONFLICT (achievement_id) DO NOTHING",
                            (rule.id, earned.earned_on.isoformat(), found.tz_name, json.dumps([d.isoformat() for d in earned.dates]), utc_text(now)),
                        )
                stored = _stored(conn)  # another request may have recorded one first: its row wins
            except sqlite3.OperationalError:
                pass  # too busy: shown as earned now, recorded next time
    results = []
    for rule, earned, progress in checked:
        row = stored.get(rule.id)
        if row is not None:  # earned: complete, whatever today's data says
            kept = Earned(date.fromisoformat(row["earned_on"]), [date.fromisoformat(d) for d in json.loads(row["dates"])])
            done = Progress(progress.target, progress.target, progress.unit) if progress else None
            results.append(AchievementResult(rule, kept, parse_utc(row["unlocked_at"]), done))
        else:
            results.append(AchievementResult(rule, earned, now if earned else None, progress))
    return results


# --- a week's highlights (Wrapped) ------------------------------------------------------------------------------


@dataclass(frozen=True)
class WeekStreak:
    id: str
    name: str
    rule: str
    met: int
    days_with_data: int
    longest: int
    dates: list[date]


def week_streaks(database: Database, tz: tzinfo, tz_name: str, now: datetime, first: date, last: date) -> list[WeekStreak]:
    """Each streak in the week from `first` to `last`: the days met, the days with data, and the longest run inside
    the week. A week inside the history is a slice of it; an older one is judged on its own."""
    found = evaluation(database, tz, tz_name, now)
    if first < found.first:
        found = evaluate_days(database, tz, tz_name, now, [first + timedelta(days=i) for i in range((last - first).days + 1)])
    out = []
    for track in (item.between(first, last) for item in found.streaks):
        out.append(WeekStreak(track.id, track.name, track.rule, sum(day.status == "met" for day in track.days),
                              sum(day.status != "no_data" for day in track.days), len(track.best),
                              [day.day for day in track.days if day.status == "met"]))
    return out
