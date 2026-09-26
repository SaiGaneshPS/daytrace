"""DT-39: the day story, with a number check.

The local model (llm.py) writes a short, friendly story of a day from facts the stats engine (stats.py)
computed, and is told to use only those facts. Every amount in its answer is then read whole and checked against
one fact of the same kind:

- Durations are read however they are written ("2 hours 35 minutes", "155 minutes", "2h35m", "two and a half
  hours", "an hour and a half", "a three-hour block"). So are clock times ("11:40 pm", "9 am", "8.15 am", "half
  past eight"), percents, scores ("81 out of 100"), counts ("12 times", "twice"), rates ("4 switches an hour"),
  dates and plain numbers. Number words count too ("twenty two", "a dozen"), except a lone "one" or "once", which
  are too often not numbers.
- A duration matches a minutes fact to the minute (plus or minus 1), or at the precision it was said in: "about 2
  hours" fits 125 or 155 minutes (whole hours, rounded down or to the nearest), "two and a half hours" the half
  hour, "2.6 hours" a tenth of an hour. A clock time matches a time fact to the minute, "9 am" to the hour, and
  without am or pm either half of the day. Scores and percents match within 1, counts exactly.
- A plain number with no unit may match a fact of any kind. A number with a unit no fact has ("155 seconds",
  "81 apps") matches only an amount written in a fact's own label ("10+ minutes", "after 11 pm") or, for apps and
  categories, how many are listed. A date must be the story's day (for DT-40's answers, a day the tools
  looked at).

A story with any other number, the wrong length, or cut off gets one retry, told what was wrong. After that, or
when the model is not available, a plain template story built from the same facts is used instead (`fallback`).
Stories the model wrote are cached per day and time zone until the facts, the prompt, the checker or the
configured model change. Today's story is of the day so far, and is written again at most every 15 minutes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .db import Database, transaction, utc_text
from .llm import LLM, LLMError
from .sessions import parse_utc
from .stats import Stats, Window

MIN_SENTENCES, MAX_SENTENCES = 3, 7  # the model is asked for 4 to 6; a sentence either way is fine
MAX_STORY_CHARS = 1500
MAX_TOKENS = 2048  # a story is about 200 tokens: the rest leaves a reasoning model room to think first
TODAY_REWRITE = timedelta(minutes=15)  # today's facts change with every sync; a new story at most this often
CHECK_VERSION = "2"  # change it when the number check changes, so cached stories are written again
SYSTEM_PROMPT = (
    "You write a short, warm story of someone's day from facts about their screen time, for them to read. "
    "Rules: 4 to 6 sentences, in the second person (\"you\"). Use only the facts given: never add a number, app, "
    "time or event that is not in them, and never guess. Minutes may be written as they are or as hours and "
    "minutes (125 minutes = 2 hours 5 minutes). Times of day are 24-hour and may be written with am or pm "
    "(23:40 = 11:40 pm). Gentle and specific, no advice lists, no headings, no emoji."
)
STORY_VERSION = f"{CHECK_VERSION}:{hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class Fact:
    """One number the story may use. `unit` is minutes, times, score, percent, per hour, or time (HH:MM); any other
    unit is a plural noun counted ("sessions", "apps"), which a number followed by that noun may match."""

    label: str
    value: float | int | str
    unit: str

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "unit": self.unit}


# --- facts ---------------------------------------------------------------------------------------------------------


def day_facts(stats: Stats, day: date) -> list[Fact]:
    """The facts for one day, from the stats engine. Empty when nothing was recorded that day. While the day is
    still going (`day_in_progress`) they are the day so far, without a last screen use (that would be now)."""
    totals = stats.totals(day, group_by="app")
    if day.isoformat() in totals["missing_days"] or not totals["total_seconds"]:
        return []
    facts = [Fact("screen time", round(totals["total_minutes"]), "minutes")]
    facts += [Fact(f"time in {item['key']}", round(item["minutes"]), "minutes") for item in totals["items"][:3]]
    categories = stats.totals(day, group_by="category")["items"]
    facts += [Fact(f"time on {item['key']}", round(item["minutes"]), "minutes") for item in categories[:3]]
    window = stats.day(day)
    first, last = screen_use_bounds(window)
    if first is not None:
        facts.append(Fact("first screen use", _clock(first, stats.tz), "time"))
    if last is not None and window.over:
        facts.append(Fact("last screen use", _clock(last, stats.tz), "time"))
    focus = stats.focused_minutes(day)
    if focus["value"] is not None:
        facts.append(Fact("focused time (work or study blocks of 10+ minutes)", round(focus["value"]), "minutes"))
    score = stats.focus_score(day)
    if score["value"] is not None:
        facts.append(Fact("focus score (0 to 100)", score["value"], "score"))
    pickups = stats.pickups(day)
    if pickups["value"] is not None:
        facts.append(Fact("phone pickups", pickups["value"], "times"))
    switches = stats.switches_per_hour(day)
    if switches["value"] is not None:
        facts.append(Fact("app switches per hour of screen time", switches["value"], "per hour"))
    late = stats.late_night_minutes(day - timedelta(days=1))
    if late["value"] is not None:
        facts.append(Fact("screen time after 11 pm the night before", round(late["value"]), "minutes"))
    sleep = stats.sleep_estimate(day)
    if sleep["value"] is not None:
        facts.append(Fact("sleep last night" + ("" if sleep.get("measured") else " (estimated)"), round(sleep["value"]), "minutes"))
    plan = stats.planned_vs_actual(day)
    if plan.get("value") is not None:
        facts.append(Fact("planned calendar time", round(plan["totals_minutes"]["planned"]), "minutes"))
        facts.append(Fact("share of planned time spent as planned", plan["value"], "percent"))
    return facts


def screen_use_bounds(window: Window) -> tuple[datetime | None, datetime | None]:
    """When screen use began and ended that day. Sessions are cut at local midnight: one still going from the
    night before starts exactly at midnight and is not the day's first use, and one still going at the next
    midnight means the day's use did not end that day (so there is no last use to tell)."""
    starts = [s.start for s in window.counted if s.start > window.start]
    ends = [s.end for s in window.counted]
    last = max(ends) if ends else None
    return (min(starts) if starts else None), (last if last is not None and last < window.end else None)


def day_in_progress(stats: Stats, day: date) -> bool:
    """Whether `day` is not over yet in the stats' time zone, so its facts can still change."""
    return not stats.day(day).over


def _clock(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def day_title(day: date) -> str:
    return f"{day:%A}, {day.day} {day:%B} {day.year}"


# --- reading amounts -------------------------------------------------------------------------------------------------

_SMALL = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_MONTHS = {
    **{name: number for number, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
         "november", "december"], start=1)},
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_HOURS = {"hours", "hour", "hrs", "hr"}
_MINUTES = {"minutes", "minute", "mins", "min"}
_ORDINALS = {"st", "nd", "rd", "th"}
_COUNTS = {"times", "pickups", "unlocks", "checks", "switches", "opens"}
_ITEMS = {"apps", "app", "sites", "site", "websites", "website", "categories", "category"}
_OTHER_UNITS = {  # units no fact is in: a number with one of these matches only a label
    "seconds", "second", "secs", "sec", "days", "day", "weeks", "week", "months", "month", "years", "year",
    "nights", "steps", "calories", "kcal", "notifications", "messages", "emails", "texts", "calls", "sessions",
    "videos", "episodes", "songs", "tabs", "screens", "people", "meals", "breaks", "blocks", "pages", "posts",
    "reels", "games",
}
_NOT_SKIPPED = _HOURS | _MINUTES | _OTHER_UNITS | set(_SMALL) | set(_TENS) | {"and", "or", "but", "than", "in"}
_TOKENS = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2})(?!\d)"
    r"|(?P<clock>\d{1,2}:\d{2})(?!\d)"
    r"|(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|\d+(?:\.\d+)?)"
    r"|(?P<ampm>\b[ap]\.\s?m\b\.?)"
    r"|(?P<word>[^\W\d_]+(?:'[^\W\d_]+)*)"
    r"|(?P<stop>[^\w\s\-])",  # % + / , . and the like; hyphens only separate ("three-hour" is "three hour")
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Amount:
    """One amount read from text. `kind` says what it counts:

    - duration: `value` in minutes; `precision` exact, tenth (of an hour), half (an hour) or hour
    - clock: `times`, the minutes after midnight it may mean (both halves of the day when it doesn't say am or pm);
      `precision` exact or hour
    - percent, score, count, rate (per hour), items (apps or categories), ordinal ("25th")
    - date: `calendar_day` is (day, month, year or None)
    - other: a unit no fact has ("seconds", "days"); bare: no unit at all

    `unit` is the word that said what a count, items or other amount counts ("times", "apps", "sessions").
    """

    written: str
    kind: str
    value: float = 0
    precision: str = "exact"
    times: tuple[int, ...] = ()
    calendar_day: tuple[int, int, int | None] | None = None
    unit: str = ""


@dataclass(frozen=True)
class _Token:
    text: str  # lower case; a.m. and p.m. are "am" and "pm"
    kind: str  # iso, clock, num, word, stop
    start: int
    end: int


def _tokens(text: str) -> list[_Token]:
    text = text.replace(chr(0x2019), "'")  # a curly apostrophe (o'clock) is one character, so positions stay
    found = []
    for match in _TOKENS.finditer(text):
        kind, token = match.lastgroup or "stop", match.group(0).lower()
        if kind == "ampm":
            kind, token = "word", token[0] + "m"
        found.append(_Token(token, kind, match.start(), match.end()))
    return found


def _clock_times(hours: int, minutes: int, half: str | None) -> tuple[int, ...]:
    """The minutes after midnight a time of day may mean: one with am or pm (or "at night"), and both halves of the
    day without, unless it is 24-hour (23:40, 0:15). Nothing for an impossible time."""
    if not 0 <= minutes < 60 or not 0 <= hours <= 24:
        return ()
    if half == "night":
        half = "pm" if 6 <= hours <= 11 else "am"
    if half in ("am", "pm"):
        if not 1 <= hours <= 12:
            return ()
        return ((hours % 12 + (12 if half == "pm" else 0)) * 60 + minutes,)
    if hours == 0 or hours >= 13:
        return ((hours % 24) * 60 + minutes,)
    return ((hours % 12) * 60 + minutes, (hours % 12 + 12) * 60 + minutes)


class _Reader:
    """Reads the amounts in one text, token by token."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)

    def word(self, i: int) -> str:
        """The token at i unless it is a number, clock or date ("" past the end)."""
        if 0 <= i < len(self.tokens) and self.tokens[i].kind in ("word", "stop"):
            return self.tokens[i].text
        return ""

    def kind(self, i: int) -> str:
        return self.tokens[i].kind if 0 <= i < len(self.tokens) else ""

    def attached(self, i: int) -> bool:
        """Whether token i follows the one before it with no space ("2h", "25th")."""
        return 0 < i < len(self.tokens) and self.tokens[i].start == self.tokens[i - 1].end

    def amount(self, start: int, end: int, kind: str, **fields: Any) -> tuple[Amount, int]:
        written = self.text[self.tokens[start].start:self.tokens[end - 1].end]
        return Amount(written, kind, **fields), end

    def all(self) -> list[Amount]:
        found: list[Amount] = []
        i = 0
        while i < len(self.tokens):
            read = self.read(i)
            if read is None:
                i += 1
            else:
                found.append(read[0])
                i = read[1]
        return found

    def number(self, i: int) -> tuple[float, int, bool] | None:
        """The number at i: its value, the index after it, and whether it is a lone "one"."""
        if self.kind(i) == "num":
            return float(self.tokens[i].text.replace(",", "")), i + 1, False
        word = self.word(i)
        if word in ("a", "an") and self.word(i + 1) == "hundred":
            return (*self.after_hundred(100, i + 2), False)
        if word in ("a", "an") and self.word(i + 1) == "dozen":
            return 12, i + 2, False
        if word == "dozen":
            return 12, i + 1, False
        if word in _TENS:
            value, j = float(_TENS[word]), i + 1
            if 1 <= _SMALL.get(self.word(j), 0) <= 9:  # "twenty two", "twenty-two"
                value, j = value + _SMALL[self.word(j)], j + 1
            return value, j, False
        if word in _SMALL:
            value, j = float(_SMALL[word]), i + 1
            if self.word(j) == "hundred" and 1 <= value <= 9:
                value, j = self.after_hundred(value * 100, j + 1)
            return value, j, word == "one" and j == i + 1
        return None

    def after_hundred(self, value: float, j: int) -> tuple[float, int]:
        """After "two hundred": "and forty-five", "forty-five" or nothing."""
        k = j + 1 if self.word(j) == "and" else j
        rest = self.number(k) if self.word(k) in _TENS or self.word(k) in _SMALL else None
        if rest is not None and rest[0] < 100:
            return value + rest[0], rest[1]
        return value, j

    def read(self, i: int) -> tuple[Amount, int] | None:
        """The amount starting at token i and the index after it, or None when no amount starts there."""
        token, word = self.tokens[i], self.word(i)
        if token.kind == "iso":
            year, month, day = (int(part) for part in token.text.split("-"))
            return self.amount(i, i + 1, "date", calendar_day=(day, month, year))
        if token.kind == "clock":
            hours, minutes = (int(part) for part in token.text.split(":"))
            half, j = self.half_of_day(i + 1)
            return self.amount(i, j, "clock", times=_clock_times(hours, minutes, half))
        if (word == "half" and self.word(i + 1) == "past") or (word == "quarter" and self.word(i + 1) in ("past", "to")):
            hour = self.number(i + 2)
            if hour is not None and hour[0] in range(1, 13):
                hours, minutes = int(hour[0]), 30 if word == "half" else 15
                if self.word(i + 1) == "to":
                    hours, minutes = hours - 1 or 12, 45
                half, j = self.half_of_day(hour[1])
                return self.amount(i, j, "clock", times=_clock_times(hours, minutes, half))
        if word == "half" and self.word(i + 1) in _HOURS:  # "half hour"
            return self.amount(i, i + 2, "duration", value=30, precision="half")
        if word == "half" and self.word(i + 1) in ("a", "an") and self.word(i + 2) in _HOURS:  # "half an hour"
            return self.amount(i, i + 3, "duration", value=30, precision="half")
        if word in ("a", "an") and self.word(i + 1) == "half" and self.word(i + 2) in _HOURS:  # "a half hour"
            return self.amount(i, i + 3, "duration", value=30, precision="half")
        if word in ("a", "an") and self.word(i + 1) in _HOURS:  # "an hour", "an hour and a half"
            return self.after_hours(i, 1, i + 2, half=False)
        if word in ("twice", "thrice"):
            return self.amount(i, i + 1, "count", value=2 if word == "twice" else 3)
        if word in _MONTHS and self.kind(i + 1) == "num" and self.tokens[i + 1].text.isdigit():  # "September 25"
            day = int(self.tokens[i + 1].text)
            if 1 <= day <= 31:
                j = i + 3 if self.word(i + 2) in _ORDINALS and self.attached(i + 2) else i + 2
                return self.date_rest(i, j, day, _MONTHS[word])
        number = self.number(i)
        if number is None:
            return None
        return self.after_number(i, *number)

    def after_number(self, i: int, value: float, j: int, lone_one: bool) -> tuple[Amount, int] | None:
        """What a number at i (ending before j) is, from what follows it."""
        whole = value == int(value)
        if self.word(j) in _ORDINALS and self.attached(j) and whole:  # "25th", "25th of September"
            k = j + 2 if self.word(j + 1) == "of" else j + 1
            if self.word(k) in _MONTHS:
                return self.date_rest(i, k + 1, int(value), _MONTHS[self.word(k)])
            return self.amount(i, j + 1, "ordinal", value=value)
        if self.word(j) in _MONTHS and whole and 1 <= value <= 31:  # "25 September 2026"
            return self.date_rest(i, j + 1, int(value), _MONTHS[self.word(j)])
        half = False
        if (self.word(j), self.word(j + 1), self.word(j + 2)) == ("and", "a", "half"):
            value, j, half = value + 0.5, j + 3, True
        if self.word(j) == "+":  # "10+ minutes"
            j += 1
        unit = self.word(j)
        if unit in _HOURS or (unit == "h" and self.attached(j)):
            return self.after_hours(i, value, j + 1, half)
        if unit in _MINUTES or (unit == "m" and self.attached(j)):
            return self.amount(i, j + 1, "duration", value=value)
        rate_end = self.rate_end(j)
        if rate_end is not None:
            return self.amount(i, rate_end, "rate", value=value)
        if unit in ("%", "percent"):
            return self.amount(i, j + 1, "percent", value=value)
        if unit == "per" and self.word(j + 1) == "cent":
            return self.amount(i, j + 2, "percent", value=value)
        scale_end = self.out_of_100(j)
        if scale_end is not None:
            return self.amount(i, scale_end, "score", value=value)
        half_of_day, end = self.half_of_day(j)
        if end > j:  # "9 am", "8.15 pm", "nine o'clock", "11 at night"
            if self.kind(i) == "num" and "." in self.tokens[i].text:
                hours, minutes = self.tokens[i].text.split(".")
                times = _clock_times(int(hours), int(minutes), half_of_day) if len(minutes) == 2 else ()
                return self.amount(i, end, "clock", times=times)
            times = _clock_times(int(value), 0, half_of_day) if whole else ()
            return self.amount(i, end, "clock", times=times, precision="hour")
        if lone_one:
            return None  # "one of your apps", "at one point": too often not a number
        if unit in ("points", "point", "pts"):
            return self.amount(i, j + 1, "score", value=value)
        if unit in _COUNTS or (unit == "pick" and self.word(j + 1) in ("ups", "up")):
            return self.amount(i, j + (2 if unit == "pick" else 1), "count", value=value,
                               unit="pickups" if unit == "pick" else unit)
        if unit in _ITEMS:
            return self.amount(i, j + 1, "items", value=value, unit=unit)
        if unit in _OTHER_UNITS or (unit == "s" and self.attached(j)):
            return self.amount(i, j + 1, "other", value=value, unit=unit)
        return self.amount(i, j, "bare", value=value)

    def after_hours(self, start: int, hours: float, j: int, half: bool) -> tuple[Amount, int]:
        """After "2 hours": "and a half", "(and) 35 minutes" or nothing. The precision is what was said."""
        whole = hours == int(hours) and not half
        if whole and (self.word(j), self.word(j + 1), self.word(j + 2)) == ("and", "a", "half"):
            return self.amount(start, j + 3, "duration", value=hours * 60 + 30, precision="half")
        if whole:
            k = j + 1 if self.word(j) in ("and", ",") else j
            minutes = self.number(k)
            if minutes is not None:
                unit = self.word(minutes[1])
                if unit in _MINUTES or (unit == "m" and self.attached(minutes[1])):
                    return self.amount(start, minutes[1] + 1, "duration", value=hours * 60 + minutes[0])
        precision = "half" if half else "hour" if whole else "tenth"
        return self.amount(start, j, "duration", value=hours * 60, precision=precision)

    def rate_end(self, j: int) -> int | None:
        """Where "per hour" ends, when it follows a number within two words ("4 switches an hour", "4.2 app
        switches per hour")."""
        for k in range(j, j + 3):
            word = self.word(k)
            if word in ("per", "an", "a", "each", "every") and self.word(k + 1) in _HOURS:
                return k + 2
            if word == "/" and self.word(k + 1) in (*_HOURS, "h"):
                return k + 2
            if word == "hourly":
                return k + 1
            if self.kind(k) != "word" or word in _NOT_SKIPPED:
                return None
        return None

    def out_of_100(self, j: int) -> int | None:
        """Where "out of 100" or "/100" ends, after a score."""
        if (self.word(j), self.word(j + 1)) == ("out", "of"):
            k = j + 2
        elif self.word(j) == "/":
            k = j + 1
        else:
            return None
        scale = self.number(k)
        return scale[1] if scale is not None and scale[0] == 100 else None

    def half_of_day(self, j: int) -> tuple[str | None, int]:
        """am or pm after a time ("pm", "p.m.", "in the evening", "at night", or "o'clock" for either), and the index
        after it; (None, j) when nothing says."""
        word = self.word(j)
        if word in ("am", "pm"):
            return word, j + 1
        if word == "in" and self.word(j + 1) == "the" and self.word(j + 2) in ("morning", "afternoon", "evening"):
            return ("am" if self.word(j + 2) == "morning" else "pm"), j + 3
        if word == "at" and self.word(j + 1) == "night":
            return "night", j + 2
        if word == "o'clock":
            half, k = self.half_of_day(j + 1)
            return (half, k) if half is not None else ("either", j + 1)
        return None, j

    def date_rest(self, start: int, j: int, day: int, month: int) -> tuple[Amount, int]:
        """After "25 September" or "September 25": an optional year."""
        k = j + 1 if self.word(j) == "," else j
        year = int(self.tokens[k].text) if self.kind(k) == "num" and re.fullmatch(r"\d{4}", self.tokens[k].text) else None
        return self.amount(start, k + 1 if year is not None else j, "date", calendar_day=(day, month, year))


def numbers_in(text: str) -> list[Amount]:
    """Every amount in the text, read whole (see the module docstring)."""
    return _Reader(text).all()


# --- the number check ------------------------------------------------------------------------------------------------

_FACT_KINDS = {"minutes": "duration", "time": "clock", "score": "score", "percent": "percent", "times": "count",
               "per hour": "rate"}


@dataclass(frozen=True)
class Allowed:
    """What a story's amounts may match: each fact's (kind, value), the amounts written in the facts' labels, how
    many apps and categories are listed, counts of a named noun ("sessions"), and the days it may name (the story's
    day; for an answer, every day the tools looked at)."""

    values: list[tuple[str, float]]
    labels: list[Amount]
    items: set[int]
    days: frozenset[date]
    nouns: list[tuple[str, float]]


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def allowed_values(facts: Iterable[Fact], days: date | Iterable[date], count_listed: bool = True) -> Allowed:
    """`count_listed`: how many "time in ..." and "time on ..." facts there are is how many apps and categories
    the story may say it lists (the story's top 3). Answers (DT-40) give those counts as facts instead."""
    facts = list(facts)
    values: list[tuple[str, float]] = []
    labels: list[Amount] = []
    nouns: list[tuple[str, float]] = []
    for fact in facts:
        labels += numbers_in(fact.label)
        kind = _FACT_KINDS.get(fact.unit, "count")
        if kind == "clock":
            hours, minutes = (int(part) for part in str(fact.value).split(":"))
            values.append((kind, hours * 60 + minutes))
        else:
            values.append((kind, float(fact.value)))
        if fact.unit not in _FACT_KINDS:
            nouns.append((_singular(fact.unit.lower()), float(fact.value)))
    listed = {sum(f.label.startswith("time in ") for f in facts), sum(f.label.startswith("time on ") for f in facts)}
    return Allowed(values, labels, (listed - {0}) if count_listed else set(),
                   frozenset([days] if isinstance(days, date) else days), nouns)


def _duration_fits(minutes: float, precision: str, fact: float) -> bool:
    if precision == "exact":
        return abs(minutes - fact) <= 1 + 1e-9
    if precision == "tenth":
        return abs(minutes - fact) <= 6 + 1e-9
    step = 30 if precision == "half" else 60
    return minutes > 0 and minutes in (math.floor(fact / step) * step, math.floor(fact / step + 0.5) * step)


def _clock_fits(time: int, precision: str, fact: float) -> bool:
    if precision == "exact":
        return abs(time - fact) <= 1 or abs(time - fact) >= 1439  # 23:59 is a minute from 00:00
    return time % 1440 in (math.floor(fact / 60) * 60 % 1440, math.floor(fact / 60 + 0.5) * 60 % 1440)


def _rate_fits(value: float, fact: float) -> bool:
    return abs(value - fact) <= 0.1 + 1e-9 or value == math.floor(fact + 0.5)


def _bare_hour_times(value: float) -> tuple[int, ...]:
    """The times of day a plain number may mean ("you started at 9"): that hour, in either half of the day."""
    return _clock_times(int(value), 0, None) if value == int(value) and 0 <= value <= 24 else ()


def _same_as_label(amount: Amount, label: Amount) -> bool:
    """Whether a story's amount is one written in a fact's label ("10+ minutes", "after 11 pm")."""
    if label.kind == "clock":
        times = amount.times if amount.kind == "clock" else _bare_hour_times(amount.value) if amount.kind == "bare" else ()
        return bool(set(times) & set(label.times))
    if label.kind == "date" or amount.kind not in (label.kind, "bare"):
        return False
    return abs(amount.value - label.value) < 1e-9


def _fits(amount: Amount, kind: str, value: float) -> bool:
    """Whether a story's amount matches one fact's value."""
    if amount.kind == "bare":
        if kind == "clock":
            return any(_clock_fits(time, "hour", value) for time in _bare_hour_times(amount.value))
        if kind == "count":
            return amount.value == value
        if kind == "rate":
            return _rate_fits(amount.value, value)
        return abs(amount.value - value) <= 1 + 1e-9  # minutes, score, percent
    if amount.kind == "duration":
        return kind == "duration" and _duration_fits(amount.value, amount.precision, value)
    if amount.kind == "clock":
        return kind == "clock" and any(_clock_fits(time, amount.precision, value) for time in amount.times)
    if amount.kind == "percent":  # a score out of 100 may be said as a percent, not the other way round
        return kind in ("percent", "score") and abs(amount.value - value) <= 1 + 1e-9
    if amount.kind == "score":
        return kind == "score" and abs(amount.value - value) <= 1 + 1e-9
    if amount.kind == "count":
        return kind == "count" and amount.value == value
    if amount.kind == "rate":
        return kind == "rate" and _rate_fits(amount.value, value)
    return False  # items and other units: only what the labels say


def supported(amount: Amount, allowed: Allowed) -> bool:
    """Whether an amount read from a story matches a fact (see the module docstring)."""
    days = allowed.days
    if amount.kind == "date" and amount.calendar_day is not None:
        on, month, year = amount.calendar_day
        return any((on, month) == (day.day, day.month) and year in (None, day.year) for day in days)
    if amount.kind == "ordinal":
        return any(amount.value == day.day for day in days) or 1 <= amount.value <= max(allowed.items, default=0)
    if any(_same_as_label(amount, label) for label in allowed.labels):
        return True
    if amount.kind in ("items", "bare") and amount.value in allowed.items:
        return True
    if amount.kind in ("count", "items", "other") and any(
            (noun, value) == (_singular(amount.unit), amount.value) for noun, value in allowed.nouns):
        return True  # "4 sessions" for a count of sessions
    if amount.kind == "bare" and any(amount.value == day.year for day in days):
        return True  # a plain day of the month is not: "the 25th" and "25 September" are dates
    return any(_fits(amount, kind, value) for kind, value in allowed.values)


def unsupported_numbers(text: str, facts: Sequence[Fact], days: date | Iterable[date], count_listed: bool = True) -> list[str]:
    """The amounts in `text`, as written, that match no fact. `days` are the days it may name."""
    allowed = allowed_values(facts, days, count_listed)
    return [amount.written for amount in numbers_in(text) if not supported(amount, allowed)]


_ABBREVIATION = re.compile(r"\b(?:[ap]\.\s?m|e\.g|i\.e|etc|vs|approx|mr|mrs|ms|dr)\.", re.IGNORECASE)


def sentences(text: str) -> int:
    """How many sentences: a split after . ! or ?, except in abbreviations (a.m., p.m., e.g.)."""
    text = _ABBREVIATION.sub(lambda match: match.group(0).replace(".", ""), text)
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if re.search(r"[A-Za-z]", part)])


def story_problems(story: str, facts: Sequence[Fact], day: date, cut_off: bool = False) -> list[str]:
    """Why a story can't be used, in words the model can act on; empty when it is fine."""
    if cut_off:
        return ["it was cut off before it finished"]
    if not story:
        return ["it was empty"]
    problems = []
    wrong = unsupported_numbers(story, facts, day)
    if wrong:
        problems.append(f"it used numbers that are not in the facts ({', '.join(wrong)})")
    count = sentences(story)
    if not MIN_SENTENCES <= count <= MAX_SENTENCES:
        problems.append(f"it had {count} sentence{'s' if count != 1 else ''} instead of 4 to 6")
    if len(story) > MAX_STORY_CHARS:
        problems.append(f"it was {len(story)} characters long, over the limit of {MAX_STORY_CHARS}")
    return problems


# --- writing -------------------------------------------------------------------------------------------------------


def duration(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{text} {rest} minute{'s' if rest != 1 else ''}" if rest else text


def template_story(facts: Sequence[Fact], day: date, in_progress: bool = False) -> str:
    """A plain story from the facts, used when the model is not available or keeps getting numbers wrong."""
    if not facts:
        recorded = "has been recorded yet" if in_progress else "was recorded"
        return f"Nothing {recorded} on {day_title(day)}. When your devices send data, your story appears here."
    by_label = {fact.label: fact for fact in facts}
    screen = duration(float(by_label["screen time"].value))
    lines = [f"So far on {day_title(day)}, you have spent {screen} on screens." if in_progress
             else f"On {day_title(day)} you spent {screen} on screens."]
    apps = [fact for fact in facts if fact.label.startswith("time in ")]
    if apps:
        named = ", ".join(f"{fact.label[8:]} ({duration(float(fact.value))})" for fact in apps)
        lines.append(f"The apps you used most were {named}.")
    focus = next((fact for fact in facts if fact.label.startswith("focused time")), None)
    score = by_label.get("focus score (0 to 100)")
    if focus is not None:
        lines.append(f"You focused for {duration(float(focus.value))} in longer blocks of work or study"
                     + (f", for a focus score of {score.value}." if score else "."))
    if "phone pickups" in by_label:
        count = by_label["phone pickups"].value
        lines.append(f"You picked up your phone {count} time{'s' if count != 1 else ''}.")
    sleep = next((fact for fact in facts if fact.label.startswith("sleep last night")), None)
    if sleep is not None:
        lines.append(f"The night before, you slept about {duration(float(sleep.value))}.")
    return " ".join(lines)


def clean_reply(text: str | None) -> str:
    """The story without a reasoning model's thinking, extra space or wrapping quotes. Some servers leave out the
    opening <think>, so everything up to the last </think> goes; thinking that never closed (the reply was cut off)
    leaves nothing."""
    text = text or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", text).strip().strip('"').strip()


@dataclass
class StoryResult:
    story: str
    facts: list[Fact]
    model: str | None
    cached: bool
    fallback: bool
    reason: str | None = None
    in_progress: bool = False


def write_story(llm: LLM, facts: Sequence[Fact], day: date, in_progress: bool = False) -> StoryResult:
    """Ask the model for the story, check it, retry once, and fall back to the template."""
    facts = list(facts)

    def fallback(reason: str) -> StoryResult:
        return StoryResult(template_story(facts, day, in_progress), facts, None, False, True, reason, in_progress)

    try:
        model = llm.current_model()
    except LLMError as error:
        return fallback(str(error))
    so_far = " The day is not over yet: these are the facts so far, so tell the day so far, not how it ended." if in_progress else ""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Facts for {day_title(day)}.{so_far}\n"
                                     f"{json.dumps([fact.as_dict() for fact in facts], ensure_ascii=False)}\n"
                                     "Write the story."},
    ]
    problems: list[str] = []
    for _ in range(2):
        try:
            choice = llm.complete(messages, temperature=0.4, max_tokens=MAX_TOKENS, model=model)
        except LLMError as error:
            return fallback(str(error))
        story = clean_reply(getattr(choice.message, "content", None))
        problems = story_problems(story, facts, day, cut_off=getattr(choice, "finish_reason", None) == "length")
        if not problems:
            return StoryResult(story, facts, model, False, False, None, in_progress)
        messages += [
            {"role": "assistant", "content": story or "(no story)"},
            {"role": "user", "content": f"That story cannot be used: {'; '.join(problems)}. Write it again in 4 to 6 "
                                        f"sentences (under {MAX_STORY_CHARS} characters), using only numbers that "
                                        "appear in the facts. Reply with the story only."},
        ]
    return fallback(f"the story from {model} could not be used, even after a retry: {'; '.join(problems)}")


# --- the cache -----------------------------------------------------------------------------------------------------

_locks_guard = threading.Lock()
_locks: dict[tuple[str, str, str], threading.Lock] = {}


def _story_lock(database: Database, day: date, tz_name: str) -> threading.Lock:
    """One lock per database, day and time zone: a second request for the same story waits for the first and then
    reads it from the cache, instead of asking the model again."""
    with _locks_guard:
        return _locks.setdefault((str(database.path), day.isoformat(), tz_name), threading.Lock())


def facts_hash(facts: Sequence[Fact], day: date, tz_name: str) -> str:
    payload = json.dumps([day.isoformat(), tz_name, [fact.as_dict() for fact in facts]], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def story_writer(llm: LLM, in_progress: bool) -> str:
    """What a cached story must have been written with to be served: the prompt and checker, the configured model
    ("auto" when the server's loaded model is used), and whether the day was over."""
    return f"{STORY_VERSION}|{llm.settings.model or 'auto'}|{'so far' if in_progress else 'whole day'}"


def cache_story(database: Database, day: date, tz_name: str, writer: str, digest: str, result: StoryResult,
                facts_at: datetime) -> bool:
    """Keep a story the model wrote. `facts_at` is when its facts were read: a story from older facts never
    replaces one from newer. False when the database was too busy (the story is still good, just not kept)."""
    try:
        with database.connect() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO story_cache (day, tz, facts_hash, writer, story, facts, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (day, tz) DO UPDATE SET facts_hash = excluded.facts_hash, writer = excluded.writer,"
                " story = excluded.story, facts = excluded.facts, model = excluded.model, created_at = excluded.created_at"
                " WHERE excluded.created_at >= story_cache.created_at",
                (day.isoformat(), tz_name, digest, writer, result.story, json.dumps([f.as_dict() for f in result.facts]),
                 result.model, utc_text(facts_at)),
            )
    except sqlite3.OperationalError:
        return False
    return True


def day_story(database: Database, llm: LLM, day: date, tz: tzinfo, tz_name: str, now: datetime | None = None) -> StoryResult:
    """The story for `day` in `tz`: from the cache when nothing behind it changed (for today, when it is under 15
    minutes old), otherwise written now. The database is not held while the model writes."""
    with _story_lock(database, day, tz_name):
        with database.connect() as conn:
            stats = Stats(conn, tz, tz_name, now)
            facts, in_progress = day_facts(stats, day), day_in_progress(stats, day)
            row = conn.execute("SELECT facts_hash, writer, story, facts, model, created_at FROM story_cache"
                               " WHERE day = ? AND tz = ?", (day.isoformat(), tz_name)).fetchone()
        writer, digest = story_writer(llm, in_progress), facts_hash(facts, day, tz_name)
        if row is not None and row["writer"] == writer:
            recent = in_progress and parse_utc(row["created_at"]) > stats.now - TODAY_REWRITE
            if row["facts_hash"] == digest or recent:
                written_from = [Fact(**fact) for fact in json.loads(row["facts"])]
                return StoryResult(row["story"], written_from, row["model"], True, False, None, in_progress)
        if not facts:
            return StoryResult(template_story(facts, day, in_progress), facts, None, False, True,
                               "nothing was recorded that day", in_progress)
        result = write_story(llm, facts, day, in_progress)
        if not result.fallback and result.model is not None:
            cache_story(database, day, tz_name, writer, digest, result, stats.now)
        return result
