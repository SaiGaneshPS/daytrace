"""DT-39: the day story, with a number check.

The local model (llm.py) writes a short, friendly story of a day from facts the stats engine (stats.py)
computed, and is told to use only those facts. Every number in its answer is then checked against them:

- digits ("125", "2.1", "34%"), number words ("twenty-five", "two"; not "one", which is too often not a number)
  and clock times ("11:40 pm") are all read;
- a number passes when it matches a fact, allowing for rounding (plus or minus 1 minute, point, or percent) and
  for the ways people say durations (125 minutes = 2 hours 5 minutes = about 2 hours = 2.1 hours);
- numbers in the date and in the facts' own labels ("after 11 pm") pass too.

A story with any other number gets one retry, told which numbers were wrong; after that, or when the model is not
available, a plain template story built from the same facts is used instead (`fallback`). Stories the model
wrote are cached per day and time zone until the facts change.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from .db import Database, transaction, utc_text
from .llm import LLM, LLMError
from .stats import Stats

MIN_SENTENCES, MAX_SENTENCES = 3, 7  # the model is asked for 4 to 6; a sentence either way is fine
MAX_STORY_CHARS = 1500
NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "half": 0.5, "hundred": 100,
}
UNITS_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "one": 1}
SYSTEM_PROMPT = (
    "You write a short, warm story of someone's day from facts about their screen time, for them to read. "
    "Rules: 4 to 6 sentences, in the second person (\"you\"). Use only the facts given: never add a number, app, "
    "time or event that is not in them, and never guess. Minutes may be written as they are or as hours and "
    "minutes (125 minutes = 2 hours 5 minutes). Gentle and specific, no advice lists, no headings, no emoji."
)


@dataclass(frozen=True)
class Fact:
    """One number the story may use. `unit` is minutes, times, score, percent, per hour, or time (HH:MM)."""

    label: str
    value: float | int | str
    unit: str

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "unit": self.unit}


# --- facts ---------------------------------------------------------------------------------------------------------


def day_facts(stats: Stats, day: date) -> list[Fact]:
    """The facts for one day, from the stats engine. Empty when nothing was recorded that day."""
    totals = stats.totals(day, group_by="app")
    if day.isoformat() in totals["missing_days"] or not totals["total_seconds"]:
        return []
    facts = [Fact("screen time", round(totals["total_minutes"]), "minutes")]
    facts += [Fact(f"time in {item['key']}", round(item["minutes"]), "minutes") for item in totals["items"][:3]]
    categories = stats.totals(day, group_by="category")["items"]
    facts += [Fact(f"time on {item['key']}", round(item["minutes"]), "minutes") for item in categories[:3]]
    window = stats.day(day)
    if window.counted:
        facts.append(Fact("first screen use", _clock(min(s.start for s in window.counted), stats.tz), "time"))
        facts.append(Fact("last screen use", _clock(max(s.end for s in window.counted), stats.tz), "time"))
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


def _clock(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def day_title(day: date) -> str:
    return f"{day:%A}, {day.day} {day:%B} {day.year}"


# --- the number check ------------------------------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
_WORD = re.compile(r"[a-z]+(?:-[a-z]+)?")
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b|\b(\d{1,2})\s*(?:am|pm|a\.m\.|p\.m\.)", re.IGNORECASE)
# What the word after a number says it counts. A number with a unit matches only facts of that kind, so "40
# minutes" cannot pass on the 40 of 23:40, and "81%" cannot pass on 81 minutes.
_UNIT_AFTER = [
    (re.compile(r"\s*(?:minutes?|mins?)\b", re.IGNORECASE), "minutes"),
    (re.compile(r"\s*(?:hours?|hrs?)\b", re.IGNORECASE), "hours"),
    (re.compile(r"\s*(?:%|percent\b|per cent\b)", re.IGNORECASE), "percent"),
    (re.compile(r"\s*times\b", re.IGNORECASE), "count"),
]


def _kind_after(text: str, end: int) -> str:
    for pattern, kind in _UNIT_AFTER:
        if pattern.match(text, end):
            return kind
    return "any"


def numbers_in(text: str) -> list[tuple[str, float, str]]:
    """Every number in the text: as written, its value, and what it counts (minutes, hours, percent, count,
    clock, or any when nothing says). Digits first, then number words ("one" excepted: it is too often not a
    number); "half an hour" is 30 minutes."""
    clock_spans = [(m.start(), m.end()) for m in _CLOCK.finditer(text)]
    found: list[tuple[str, float, str]] = []
    for match in _NUMBER.finditer(text):
        in_clock = any(start <= match.start() < end for start, end in clock_spans)
        kind = "clock" if in_clock else _kind_after(text, match.end())
        found.append((match.group(0), float(match.group(0).replace(",", "")), kind))
    lowered = text.lower()
    for match in re.finditer(r"half an hour", lowered):
        found.append((match.group(0), 30.0, "minutes"))
    lowered = re.sub(r"half an hour", " ", lowered)
    for match in _WORD.finditer(lowered):
        word = match.group(0)
        tens, _, unit = word.partition("-")
        if unit and tens in NUMBER_WORDS and unit in UNITS_WORDS and NUMBER_WORDS[tens] >= 20:
            found.append((word, NUMBER_WORDS[tens] + UNITS_WORDS[unit], _kind_after(lowered, match.end())))  # twenty-five
        elif not unit and word in NUMBER_WORDS:
            found.append((word, NUMBER_WORDS[word], _kind_after(lowered, match.end())))
    return found


def allowed_values(facts: Iterable[Fact], day: date) -> list[tuple[float, float, str]]:
    """(value, tolerance, kind) triples a number may match. `any` ones (the date, numbers in the labels) match
    a number of any kind."""
    allowed: list[tuple[float, float, str]] = [(day.day, 0, "any"), (day.month, 0, "any"), (day.year, 0, "any")]
    for fact in facts:
        allowed += [(value, 0, "any") for _, value, _ in numbers_in(fact.label)]
        if fact.unit == "time":
            hours, minutes = (int(part) for part in str(fact.value).split(":"))
            allowed += [(hours, 0, "clock"), (hours % 12 or 12, 0, "clock"), (minutes, 0, "clock")]
            continue
        value = float(fact.value)
        if fact.unit == "minutes":
            hours = value / 60
            whole = math.floor(hours)
            allowed += [(value, 1, "minutes"), (value - 60 * whole, 1, "minutes"),
                        (round(hours, 1), 0.1, "hours"), (whole, 0, "hours"), (round(hours), 0, "hours")]
        elif fact.unit in ("score", "percent"):
            allowed.append((value, 1, "percent"))
        elif fact.unit == "per hour":
            allowed += [(value, 0.1, "count"), (round(value), 0, "count")]
        else:  # counts are exact
            allowed.append((value, 0, "count"))
    return allowed


def unsupported_numbers(text: str, facts: Sequence[Fact], day: date) -> list[str]:
    """The numbers in `text` that match no fact (see the module docstring for what matches). A number with no
    unit after it may match a fact of any kind; one with a unit only facts of that kind."""
    allowed = allowed_values(facts, day)

    def fits(value: float, kind: str) -> bool:
        return any(abs(value - candidate) <= tolerance + 1e-9 and (kind == "any" or fact_kind in (kind, "any"))
                   for candidate, tolerance, fact_kind in allowed)

    return [written for written, value, kind in numbers_in(text) if not fits(value, kind)]


def sentences(text: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if re.search(r"[A-Za-z]", part)])


# --- writing -------------------------------------------------------------------------------------------------------


def duration(minutes: float) -> str:
    minutes = round(minutes)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{text} {rest} minute{'s' if rest != 1 else ''}" if rest else text


def template_story(facts: Sequence[Fact], day: date) -> str:
    """A plain story from the facts, used when the model is not available or keeps getting numbers wrong."""
    if not facts:
        return f"Nothing was recorded on {day_title(day)}. When your devices send data, your story appears here."
    by_label = {fact.label: fact for fact in facts}
    lines = [f"On {day_title(day)} you spent {duration(float(by_label['screen time'].value))} on screens."]
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
    """The story without a reasoning model's thinking, extra space or wrapping quotes."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    return re.sub(r"\s+", " ", text).strip().strip('"').strip()


@dataclass
class StoryResult:
    story: str
    facts: list[Fact]
    model: str | None
    cached: bool
    fallback: bool
    reason: str | None = None


def write_story(llm: LLM, facts: Sequence[Fact], day: date) -> StoryResult:
    """Ask the model for the story, check its numbers, retry once, and fall back to the template."""
    facts = list(facts)
    try:
        model = llm.current_model()
    except LLMError as error:
        return StoryResult(template_story(facts, day), facts, None, False, True, str(error))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Facts for {day_title(day)}:\n"
                                     f"{json.dumps([fact.as_dict() for fact in facts], ensure_ascii=False)}\n"
                                     "Write the story."},
    ]
    problem = ""
    for _ in range(2):
        try:
            reply = llm.chat(messages, temperature=0.4, max_tokens=800, model=model)
        except LLMError as error:
            return StoryResult(template_story(facts, day), facts, None, False, True, str(error))
        story = clean_reply(getattr(reply, "content", None))
        wrong = unsupported_numbers(story, facts, day)
        count = sentences(story)
        if story and not wrong and MIN_SENTENCES <= count <= MAX_SENTENCES and len(story) <= MAX_STORY_CHARS:
            return StoryResult(story, facts, model, False, False)
        problem = (f"it used numbers that are not in the facts: {', '.join(wrong)}" if wrong
                   else f"it had {count} sentences instead of 4 to 6" if story else "it was empty")
        messages += [
            {"role": "assistant", "content": story},
            {"role": "user", "content": f"That story cannot be used: {problem}. Rewrite it in 4 to 6 sentences, "
                                        "using only numbers that appear in the facts."},
        ]
    return StoryResult(template_story(facts, day), facts, model, False, True, f"the model's story {problem}")


def facts_hash(facts: Sequence[Fact], day: date, tz_name: str) -> str:
    payload = json.dumps([day.isoformat(), tz_name, [fact.as_dict() for fact in facts]], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def day_story(database: Database, llm: LLM, day: date, tz: tzinfo, tz_name: str, now: datetime | None = None) -> StoryResult:
    """The story for `day` in `tz`: from the cache when the facts have not changed, otherwise written now (the
    database is not held while the model writes)."""
    with database.connect() as conn:
        facts = day_facts(Stats(conn, tz, tz_name, now), day)
        digest = facts_hash(facts, day, tz_name)
        row = conn.execute("SELECT facts_hash, story, model FROM story_cache WHERE day = ? AND tz = ?",
                           (day.isoformat(), tz_name)).fetchone()
    if row is not None and row["facts_hash"] == digest:
        return StoryResult(row["story"], facts, row["model"], True, False)
    if not facts:
        return StoryResult(template_story(facts, day), facts, None, False, True, "nothing was recorded that day")
    result = write_story(llm, facts, day)
    if not result.fallback and result.model is not None:
        with database.connect() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO story_cache (day, tz, facts_hash, story, facts, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (day, tz) DO UPDATE SET facts_hash = excluded.facts_hash, story = excluded.story,"
                " facts = excluded.facts, model = excluded.model, created_at = excluded.created_at",
                (day.isoformat(), tz_name, digest, result.story, json.dumps([f.as_dict() for f in facts]), result.model,
                 utc_text(datetime.now(UTC))),
            )
    return result
