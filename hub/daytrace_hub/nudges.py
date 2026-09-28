"""DT-43: nudges, because tracking alone rarely changes behaviour.

A nudge is a short prompt sent back to the device that is being used right now (in the ingest response, which the
Android app and the iPhone Shortcuts show), or shown on this computer (a desktop notification, notify.py) when the
hub's own tracker saw the activity. Four rules, checked in this order, one nudge at a time:

- `focus_block`: a distracting app (social, video or games) during a calendar event whose title says it is for
  focus ("Study", "Deep work", "Exam revision"...).
- `late_scroll`: a social or video app after the bedtime goal (DT-53; 23:30 unless changed) and before 04:00, with
  the first event of the coming day.
- `streak_at_risk`: from 20:00, a streak to reach (focus, meals, syncing) not met yet today, with the real amount
  left to keep it ("20 more focused minutes keeps your 6-day Focus flame streak going").
- `social_cap`: a social app once today's social time is over the social goal (DT-53).

Only what is happening now counts: an event that ended more than 10 minutes ago (a phone catching up on a day of
data) nudges no one. Each rule rests 20 minutes after it fires, and no nudge follows another within 5 minutes, across
every device, so a phone and the computer never nag twice; the checks and the log entry are one transaction. Every
nudge is logged (`nudge_log`; applying redaction rules to stored data covers it too), and each rule can be switched
off (settings key "nudges", with the desktop notifications). The streak and goal numbers are the Streaks page's own
(the streaks engine), worked out at most once a minute here for the goals in force: today's alone first, and the
whole history only when a streak is at risk and could still be kept.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from . import streaks
from .categories import Categorizer
from .db import Database, transaction, utc_text
from .models import Event, Nudge
from .redaction import REDACTED
from .sessions import parse_utc
from .stats import DISTRACTING
from .story import duration

logger = logging.getLogger(__name__)

SETTINGS_KEY = "nudges"
COOLDOWN = timedelta(minutes=20)  # a rule rests this long after it fires
GAP = timedelta(minutes=5)  # and no nudge follows another (of any rule) sooner: one at a time
FRESH = timedelta(minutes=10)  # older activity is a device catching up, not what is happening now
ACTIVITY_KINDS = frozenset({"app_session", "app_open", "window", "web"})
LATE_UNTIL = time(4)  # late-night scrolling lasts until 04:00
EVENING = time(20)  # a streak at risk is nudged from 20:00 to midnight
FOCUS_WORDS = re.compile(
    r"\b(study|studying|focus|focused|deep work|work|exam|exams|revision|revise|revising|homework|assignment|"
    r"lecture|thesis|essay|deadline)\b",
    re.IGNORECASE,
)
# What a streak to reach needs more of, in words (singular, plural).
MORE: dict[str, tuple[str, str]] = {
    "focused_minutes": ("focused minute", "focused minutes"),
    "meals": ("meal logged", "meals logged"),
    "devices_synced": ("device sending data", "devices sending data"),
}


@dataclass(frozen=True)
class NudgeRule:
    id: str
    name: str
    description: str


RULES: tuple[NudgeRule, ...] = (
    NudgeRule("focus_block", "Focus time",
              "A social, video or game app during a calendar event whose title is about focus: study, work, an exam, "
              "homework, a deadline."),
    NudgeRule("late_scroll", "Late night",
              "A social or video app after your bedtime goal and before 04:00, with the first event of your next day."),
    NudgeRule("streak_at_risk", "Streak at risk",
              "From 20:00, a streak not kept yet today (focus, a meal logged, every device synced), with what is left."),
    NudgeRule("social_cap", "Social limit", "A social app once today's social time is over your daily social goal."),
)
RULE_IDS = tuple(rule.id for rule in RULES)


# --- choices -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Choices:
    disabled: tuple[str, ...] = ()
    desktop: bool = True  # desktop notifications for this computer's own activity

    def as_json(self) -> str:
        return json.dumps({"disabled": list(self.disabled), "desktop": self.desktop})


def check_choices(disabled: Sequence[str], desktop: bool) -> Choices:
    """Choices from the dashboard, checked strictly: ValueError names an unknown rule."""
    unknown = sorted(set(disabled) - set(RULE_IDS))
    if unknown:
        raise ValueError(f"no nudge rule {unknown[0]!r}; the rules are {', '.join(RULE_IDS)}")
    return Choices(tuple(rule for rule in RULE_IDS if rule in disabled), bool(desktop))


def load_choices(conn: sqlite3.Connection) -> Choices:
    """The stored choices, read leniently: a rule that no longer exists is skipped, and a row that can't be read at
    all counts as none (every rule on)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    try:
        raw = json.loads(row[0]) if row else {}
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    disabled = raw.get("disabled") if isinstance(raw.get("disabled"), list) else []
    return Choices(tuple(rule for rule in RULE_IDS if rule in disabled), raw.get("desktop") is not False)


def save_choices(conn: sqlite3.Connection, choices: Choices) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (SETTINGS_KEY, choices.as_json()))


# --- what is happening now -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """An app or site in use now, with its category."""

    name: str
    category: str


@dataclass
class Moment:
    database: Database
    conn: sqlite3.Connection
    now: datetime  # UTC
    tz: tzinfo
    tz_name: str
    activities: list[Activity]

    @property
    def local(self) -> datetime:
        return self.now.astimezone(self.tz)

    def clock(self, moment: datetime) -> str:
        return moment.astimezone(self.tz).strftime("%H:%M")

    def using(self, categories: frozenset[str] | set[str]) -> Activity | None:
        return next((activity for activity in self.activities if activity.category in categories), None)


def activities(conn: sqlite3.Connection, events: Sequence[Event], now: datetime) -> list[Activity]:
    """The apps and sites in use now among these events (at most 10 minutes old), each with its category."""
    categorizer: Categorizer | None = None
    found: list[Activity] = []
    for event in events:
        if event.kind.value not in ACTIVITY_KINDS:
            continue
        last = event.end or event.start
        if last < now - FRESH or event.start > now + timedelta(minutes=1):
            continue
        is_web = event.kind.value == "web"
        name = str(event.data.get("domain")) if is_web and event.data.get("domain") else event.app or event.app_id
        if not name or REDACTED in (name, event.app):
            continue
        categorizer = categorizer or Categorizer.from_db(conn)
        category = categorizer.category(name if is_web else event.app, event.app_id, "web" if is_web else "app", event.category)
        found.append(Activity(name, category))
    return found


def calendar_events(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """Timed calendar events overlapping [start, end), from every device, each once: (title, start, end)."""
    rows = conn.execute(
        "SELECT title, start_utc, end_utc, data FROM events WHERE kind = 'calendar_event' AND end_utc > ? AND start_utc < ?"
        " ORDER BY start_utc",
        (utc_text(start), utc_text(end)),
    )
    seen: set[tuple[str, datetime, datetime]] = set()
    found = []
    for row in rows:
        try:
            all_day = json.loads(row["data"] or "{}").get("all_day") is True
        except (ValueError, AttributeError):
            all_day = False
        if all_day or not row["title"]:
            continue
        key = (str(row["title"]), parse_utc(row["start_utc"]), parse_utc(row["end_utc"]))
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def _clip(text: str, limit: int) -> str:
    """Text cut to `limit` characters at a word, with "..." when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,.;:") + "..."


def _quoted(title: str, limit: int = 60) -> str:
    return f'"{_clip(title, limit)}"'


def _named(activity: Activity) -> str:
    return _clip(activity.name, 40)


# The Streaks page's numbers, worked out at most once a minute for nudges, for the goals in force: the tracker writes
# every 2 seconds, and each write would otherwise mean working them out again. Today alone is enough for the goals;
# the whole history is only needed for the length of a run.
_evaluations: OrderedDict[tuple[object, ...], streaks.Evaluation] = OrderedDict()
_evaluations_guard = threading.Lock()


def _evaluation(moment: Moment, history: bool) -> streaks.Evaluation:
    targets = tuple(sorted(streaks.load_targets(moment.conn).items()))
    minute = moment.now.replace(second=0, microsecond=0)
    key = (str(moment.database.path), moment.tz_name, targets, minute, history)
    with _evaluations_guard:
        if key in _evaluations:
            return _evaluations[key]
    if history:
        found = streaks.evaluation(moment.database, moment.tz, moment.tz_name, moment.now)
    else:
        found = streaks.evaluate_days(moment.database, moment.tz, moment.tz_name, moment.now, [moment.local.date()])
    with _evaluations_guard:
        _evaluations[key] = found
        while len(_evaluations) > 8:
            _evaluations.popitem(last=False)
    return found


def forget() -> None:
    """Let go of every number worked out (DT-46: after delete-all nothing may be quoted from before)."""
    with _evaluations_guard:
        _evaluations.clear()


# --- the rules -------------------------------------------------------------------------------------------------------


Draft = tuple[str, str]  # title, body


def focus_block(moment: Moment) -> Draft | None:
    distracting = moment.using(DISTRACTING)
    if distracting is None:
        return None
    for title, start, end in calendar_events(moment.conn, moment.now, moment.now + timedelta(seconds=1)):
        if start <= moment.now < end and FOCUS_WORDS.search(title):
            return "Time to focus", f"{_named(distracting)} during {_quoted(title)}, which runs until {moment.clock(end)}."
    return None


def late_scroll(moment: Moment) -> Draft | None:
    scrolling = moment.using({"social", "video"})
    if scrolling is None:
        return None
    local = moment.local
    bedtime = streaks.load_targets(moment.conn)["bedtime"]  # minutes after 18:00
    after_six = (local.hour * 60 + local.minute - streaks.EVENING.hour * 60) % 1440
    until = (LATE_UNTIL.hour * 60 - streaks.EVENING.hour * 60) % 1440
    if not bedtime <= after_six < until:
        return None
    # The day after this night: tomorrow before midnight, today after it.
    from .api.timeline import day_window

    morning: date = local.date() + timedelta(days=1) if local.hour >= 12 else local.date()
    start, end = day_window(morning, moment.tz)  # 23 or 25 hours on a clock-change day
    upcoming = [event for event in calendar_events(moment.conn, max(start, moment.now), end) if start <= event[1] < end and event[1] >= moment.now]
    lead = f"It's {local:%H:%M}, past your {streaks.clock_text(bedtime)} bedtime, and {_named(scrolling)} is open."
    if upcoming:
        title, begins, _ = upcoming[0]
        return "Past your bedtime", f"{lead} Your day starts with {_quoted(title, 50)} at {moment.clock(begins)}."
    return "Past your bedtime", f"{lead} Nothing is on your calendar in the morning, but sleep still counts."


def _can_keep(moment: Moment, track: streaks.Track) -> bool:
    """Whether what is left of a streak to reach can still be done today (streaks.still_to_go)."""
    need = streaks.still_to_go(track, moment.now, moment.tz)
    return need is not None and need[1] and track.measure in MORE


def streak_at_risk(moment: Moment) -> Draft | None:
    if not moment.activities or moment.local.time() < EVENING:
        return None
    # Today alone says which streaks could still be kept; only then is the whole history read for their runs.
    if not any(_can_keep(moment, track) for track in _evaluation(moment, history=False).streaks):
        return None
    at_risk = [track for track in _evaluation(moment, history=True).streaks if track.current and _can_keep(moment, track)]
    if not at_risk:
        return None
    track = max(at_risk, key=lambda item: len(item.current))  # the longest run has the most to lose
    need = streaks.still_to_go(track, moment.now, moment.tz)
    assert need is not None  # _can_keep said so
    left = need[0]
    one, many = MORE[track.measure]
    days = len(track.current)
    return (f"Keep your {track.name}",
            f"{left} more {one if left == 1 else many} keeps your {days}-day {track.name} streak going.")


def social_cap(moment: Moment) -> Draft | None:
    social = moment.using({"social"})
    if social is None:
        return None
    goal = _evaluation(moment, history=False).goals.get("social_cap")
    # Over the goal in whole minutes, as it is said: 60.4 minutes is "1 hour", not over a 1 hour goal.
    if goal is None or goal.target is None or goal.today.value is None or round(goal.today.value) <= round(goal.target):
        return None
    return ("Over your social limit",
            f"{duration(goal.today.value)} in social apps today, over your goal of {duration(goal.target)}. {_named(social)} can wait.")


CHECKS: dict[str, Callable[[Moment], Draft | None]] = {
    "focus_block": focus_block, "late_scroll": late_scroll, "streak_at_risk": streak_at_risk, "social_cap": social_cap,
}


# --- firing ------------------------------------------------------------------------------------------------------


def _resting(conn: sqlite3.Connection, rule: str, now: datetime) -> bool:
    """Whether the rule fired less than 20 minutes ago (from any device)."""
    row = conn.execute("SELECT 1 FROM nudge_log WHERE rule = ? AND created_at > ? LIMIT 1",
                       (rule, utc_text(now - COOLDOWN))).fetchone()
    return row is not None


def _quiet(conn: sqlite3.Connection, now: datetime) -> bool:
    """Whether any nudge was sent less than 5 minutes ago (one at a time, whatever the rule)."""
    return conn.execute("SELECT 1 FROM nudge_log WHERE created_at > ? LIMIT 1", (utc_text(now - GAP),)).fetchone() is not None


def _fire(conn: sqlite3.Connection, rule: str, device_id: str, draft: Draft, now: datetime, again: bool = False) -> Nudge | None:
    """Log the nudge, unless another request sent one meanwhile (checked in the same transaction; `again` doesn't wait)."""
    title, body = _clip(draft[0], 80), _clip(draft[1], 240)
    with transaction(conn):
        if not again and (_resting(conn, rule, now) or _quiet(conn, now)):
            return None
        conn.execute("INSERT INTO nudge_log (rule, device_id, title, body, created_at) VALUES (?, ?, ?, ?, ?)",
                     (rule, device_id, title, body, utc_text(now)))
    return Nudge(rule=rule, title=title, body=body, created_at=now)


def withdraw(database: Database, device_id: str, nudge: Nudge) -> None:
    """Take back a logged nudge nobody saw (a desktop notification that couldn't be shown): its rule isn't resting
    for the phones, and `recent` doesn't list it."""
    with database.connect() as conn, transaction(conn):
        conn.execute("DELETE FROM nudge_log WHERE rule = ? AND device_id = ? AND created_at = ?",
                     (nudge.rule, device_id, utc_text(nudge.created_at)))


def hub_zone() -> tuple[tzinfo, str]:
    """The hub computer's zone: nudges speak in its clock (the devices send times, not zones)."""
    from .api.timeline import resolve_tz

    return resolve_tz(None)


def pick_nudge(database: Database, device_id: str, events: Sequence[Event], now: datetime | None = None,
               desktop: bool = False, again: bool = False) -> Nudge | None:
    """The first rule that fires for these just-stored events, logged, or None; for the desktop tracker (`desktop`),
    only while desktop notifications are on. `again` (the demo) doesn't wait for a rule's rest or the gap between
    nudges. Never raises: a stored event must never fail because of a nudge."""
    from .api.timeline import current_time

    now = (now or current_time()).astimezone(UTC)
    try:
        with database.connect() as conn:
            choices = load_choices(conn)
            if (desktop and not choices.desktop) or (not again and _quiet(conn, now)):
                return None
            used = activities(conn, events, now)
            if not used:
                return None
            tz, tz_name = hub_zone()
            moment = Moment(database, conn, now, tz, tz_name, used)
            for rule in RULE_IDS:
                if rule in choices.disabled or (not again and _resting(conn, rule, now)):
                    continue
                draft = CHECKS[rule](moment)
                if draft is not None:
                    fired = _fire(conn, rule, device_id, draft, now, again)
                    if fired is not None:
                        return fired
    except Exception:  # a nudge is never worth a failed request
        logger.exception("no nudge: the rules failed")
    return None


def silence(database: Database, rule: str, now: datetime) -> str:
    """Why a rule said nothing just now, in words (for the demo's messages)."""
    with database.connect() as conn:
        if rule in load_choices(conn).disabled:
            return f"the {rule} nudge is switched off in the nudge settings"
        if _resting(conn, rule, now):
            return f"the {rule} nudge rests for {COOLDOWN.seconds // 60} minutes after it fires"
        if _quiet(conn, now):
            return f"a nudge went out less than {GAP.seconds // 60} minutes ago (the hub sends one at a time)"
    return "nothing it watches for is happening now"


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT rule, device_id, title, body, created_at FROM nudge_log ORDER BY created_at DESC, id DESC LIMIT ?",
                        (limit,)).fetchall()
