"""DT-53: GET /streaks, GET /goals, PUT /goals/{goal_id} and GET /achievements.

Every streak, goal and badge comes with its rule in words and the days that counted, so the dashboard can always
say why a flame is lit (streaks.py has the rules). Reading needs a paired device or the local dashboard; changing
a goal needs the dashboard (a viewer token, or the dashboard on the hub computer), as categories do.
"""
from __future__ import annotations

import datetime as dt
from datetime import tzinfo
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Query
from pydantic import BaseModel, Field

from ..auth import Editor, Reader, get_database
from ..db import Database, transaction
from ..streaks import (
    HISTORY_DAYS,
    Evaluation,
    GoalRule,
    Track,
    achievements,
    evaluation,
    load_rules,
    save_target,
    week_streaks,
)
from . import API_PREFIX, ApiError
from .timeline import Meta, current_time, range_meta, resolve_tz

router = APIRouter(prefix=API_PREFIX, tags=["streaks"])
Status = Literal["met", "missed", "at_risk", "no_data"]
TzQuery = Annotated[str | None, Query(description="IANA time zone, e.g. America/Toronto; default: the hub's")]


class Amount(BaseModel):
    value: float
    unit: str


class StreakDay(BaseModel):
    date: dt.date
    status: Status
    value: float | None = Field(description="The day's reading (minutes, meals, devices); null without data.")
    estimated: bool = Field(default=False, description="The reading was partly inferred (a night guessed from the phone).")


class Streak(BaseModel):
    id: str
    name: str
    rule: str = Field(description="The rule in words, with the current target.")
    needs: str = Field(description="What must send data for a day to count; without it the day is no_data.")
    unit: str
    target: float | None = Field(description="The target on the rule's scale; null when it changes by day (Synced).")
    current: int = Field(description="Met days in a row, up to today (up to yesterday while today is at_risk).")
    best: int = Field(description="The longest run in the history (up to a year).")
    today: Status = Field(description="met, at_risk (not yet: see remaining), missed (can't be any more) or no_data.")
    value: float | None = Field(description="Today's reading so far.")
    remaining: Amount | None = Field(description="While at_risk: still to go, or the room left under a limit.")
    counted: list[dt.date] = Field(description="The days in the current streak.")
    best_dates: list[dt.date] = Field(description="The days in the best run.")
    days: list[StreakDay] = Field(description="The last days, oldest first, today last.")
    estimated: bool = Field(default=False, description="Some of the days behind its numbers (the current run, the best, the days listed) were inferred.")


class StreakList(BaseModel):
    tz: str
    date: dt.date = Field(description="Today, in tz.")
    since: dt.date = Field(description="The first day judged: the first day with data (at most a year back).")
    streaks: list[Streak]
    meta: Meta


class GoalToday(BaseModel):
    value: float | str | None = Field(description="Today's reading so far (a time as HH:MM); null without data.")
    status: Status
    progress: int | None = Field(description="0 to 100: toward a target, or how much of a limit is used.")
    estimated: bool = Field(default=False, description="Today's reading was partly inferred (last night guessed from the phone).")


class Goal(BaseModel):
    id: str
    label: str
    rule: str
    explain: str
    kind: Literal["at_least", "at_most"]
    unit: str = Field(description="minutes, or time (a clock time, HH:MM).")
    target: float | str
    default: float | str
    min: float | str
    max: float | str
    today: GoalToday


class GoalList(BaseModel):
    tz: str
    date: dt.date
    goals: list[Goal]
    meta: Meta


class GoalTarget(BaseModel):
    target: float | str = Field(description="Minutes for focused time and social apps, HH:MM for bedtime.")


class Progress(BaseModel):
    value: float
    target: float
    unit: str


class Achievement(BaseModel):
    id: str
    name: str
    rule: str
    unlocked: bool
    earned_on: dt.date | None = Field(description="The day it was earned, in the time zone of that moment.")
    unlocked_at: dt.datetime | None = Field(description="When the hub first saw it earned.")
    dates: list[dt.date] = Field(description="The days that counted.")
    progress: Progress | None


class AchievementList(BaseModel):
    tz: str
    unlocked: int
    achievements: list[Achievement]
    meta: Meta


class WeekStreakOut(BaseModel):
    id: str
    name: str
    rule: str
    met: int = Field(description="Days in the week that met the rule.")
    days_with_data: int
    longest: int = Field(description="The longest run of met days inside the week.")
    dates: list[dt.date]


class Found:
    """One evaluation, with the zone and the moment it was made for, so its meta describes the same moment."""

    def __init__(self, database: Database, tz: str | None) -> None:
        self.database = database
        self.zone, self.zone_name = resolve_tz(tz)
        self.now = current_time()
        self.evaluation = evaluation(database, self.zone, self.zone_name, self.now)

    def meta(self, first: dt.date, unit: str | None, estimated: bool) -> Meta:
        """The meta for `first` to today. A night is read from the evening before its day (bedtime, Screens down), so
        callers start a day earlier than the first day they judge."""
        with self.database.connect() as conn:
            return range_meta(conn, self.zone, self.zone_name, first, self.evaluation.today, self.now, unit=unit, estimated=estimated)


def _found(database: Database, tz: str | None) -> tuple[Evaluation, str]:
    found = Found(database, tz)
    return found.evaluation, found.zone_name


def _streak(track: Track, history: int) -> Streak:
    today = track.today
    listed = track.days[-history:]
    behind = {*track.current, *track.best}  # the days behind current and best, listed or not
    estimated = any(day.estimated for day in listed) or any(day.estimated for day in track.days if day.day in behind)
    return Streak(
        id=track.id, name=track.name, rule=track.rule, needs=track.needs, unit=track.unit, target=track.target,
        current=len(track.current), best=len(track.best), today=today.status, value=today.value,
        remaining=Amount(value=today.remaining, unit=track.unit) if today.remaining is not None else None,
        counted=track.current, best_dates=track.best,
        days=[StreakDay(date=day.day, status=day.status, value=day.value, estimated=day.estimated) for day in listed],
        estimated=estimated,
    )


@router.get("/streaks", response_model=StreakList, summary="Every streak: today, the current run, the best, and the days")
def get_streaks(
    _: Reader,
    database: Annotated[Database, Depends(get_database)],
    tz: TzQuery = None,
    days: Annotated[int, Query(ge=1, le=HISTORY_DAYS, description="How many recent days to list for each streak")] = 30,
) -> StreakList:
    found = Found(database, tz)
    judged = found.evaluation
    streaks = [_streak(track, days) for track in judged.streaks]
    # Current and best run over the whole history, from the evening before its first day (a night's streak).
    return StreakList(tz=found.zone_name, date=judged.today, since=judged.first, streaks=streaks,
                      meta=found.meta(judged.first - dt.timedelta(days=1), "days", any(streak.estimated for streak in streaks)))


def _goal(rule: GoalRule, track: Track) -> Goal:
    today = track.today
    value = rule.show(today.value) if today.value is not None else None
    if today.value is None or track.target is None:
        progress = None
    elif rule.unit == "time":  # a bedtime is met or missed; while the night can still change, no progress yet
        progress = {"met": 100, "missed": 0}.get(today.status)
    else:
        progress = min(100, round(100 * today.value / track.target)) if track.target else 100
    return Goal(id=rule.id, label=rule.label, rule=track.rule, explain=rule.explain, kind=rule.kind, unit=rule.unit,
                target=rule.show(track.target), default=rule.default, min=rule.show(rule.low), max=rule.show(rule.high),
                today=GoalToday(value=value, status=today.status, progress=progress, estimated=today.estimated))


@router.get("/goals", response_model=GoalList, summary="The daily goals, with today's progress")
def get_goals(_: Reader, database: Annotated[Database, Depends(get_database)], tz: TzQuery = None) -> GoalList:
    found = Found(database, tz)
    judged = found.evaluation
    rules = load_rules()
    goals = [_goal(rules.goals[key], track) for key, track in judged.goals.items()]
    # Today, and yesterday evening, where last night's bedtime comes from. Each goal has its own unit.
    return GoalList(tz=found.zone_name, date=judged.today, goals=goals,
                    meta=found.meta(judged.today - dt.timedelta(days=1), None, any(goal.today.estimated for goal in goals)))


@router.put("/goals/{goal_id}", response_model=Goal, summary="Set a goal's target")
def put_goal(
    goal_id: Annotated[str, Path(description="focus_target, social_cap or bedtime")],
    body: Annotated[GoalTarget, Body()],
    _: Editor,
    database: Annotated[Database, Depends(get_database)],
    tz: TzQuery = None,
) -> Goal:
    rule = load_rules().goals.get(goal_id)
    if rule is None:
        raise ApiError(404, "not_found", f"no goal {goal_id!r}")
    target: Any = body.target
    if isinstance(target, float) and target.is_integer():
        target = int(target)
    try:
        with database.connect() as conn, transaction(conn):
            save_target(conn, rule, target)
    except (TypeError, ValueError) as error:
        raise ApiError(400, "bad_request", str(error)) from None
    found, _ = _found(database, tz)
    return _goal(rule, found.goals[goal_id])


@router.get("/achievements", response_model=AchievementList, summary="Every badge, earned or not, with its rule")
def get_achievements(_: Reader, database: Annotated[Database, Depends(get_database)], tz: TzQuery = None) -> AchievementList:
    context = Found(database, tz)
    found, zone_name = context.evaluation, context.zone_name
    items = [
        Achievement(
            id=result.rule.id, name=result.rule.name, rule=result.rule.rule, unlocked=result.earned is not None,
            earned_on=result.earned.earned_on if result.earned else None, unlocked_at=result.unlocked_at,
            dates=result.earned.dates if result.earned else [],
            progress=Progress(value=result.progress.value, target=result.progress.target, unit=result.progress.unit) if result.progress else None,
        )
        for result in achievements(database, found, context.now)
    ]
    # Badges are earned from the streaks' and goals' days: estimated when any day behind an earned one was.
    earned = {day for item in items for day in item.dates}
    estimated = any(day.estimated for track in [*found.streaks, *found.goals.values()] for day in track.days if day.day in earned)
    return AchievementList(tz=zone_name, unlocked=sum(item.unlocked for item in items), achievements=items,
                           meta=context.meta(found.first - dt.timedelta(days=1), "badges", estimated))


def week_streak_highlights(database: Database, zone: tzinfo, zone_name: str, now: dt.datetime, first: dt.date, last: dt.date) -> list[WeekStreakOut]:
    """Wrapped's streaks: each streak's days met in the week."""
    return [WeekStreakOut(id=item.id, name=item.name, rule=item.rule, met=item.met, days_with_data=item.days_with_data,
                          longest=item.longest, dates=item.dates) for item in week_streaks(database, zone, zone_name, now, first, last)]
