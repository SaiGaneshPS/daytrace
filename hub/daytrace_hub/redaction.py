"""DT-44: sensitive window and calendar titles are never stored.

A title that matches a redaction rule is stored as "[redacted]": banking, health portals and appointments, password
managers, private and incognito windows, or words the user added. The app name and the times stay, so the time
still counts on the timeline and in every total; only the words are gone. A redacted calendar event keeps whether
it is all-day, and nothing else a collector sent with it (a location, notes).

Redaction happens twice: in the desktop tracker (tracker/base.py), so a sensitive title is never even held in a
span, and at ingest (api/events.store_events), so every collector and the seed are covered. A resent event keeps
its identity: an event stored before a rule existed, sent again redacted, is the same event (its stored title is
redacted then too), never a conflict or a second copy. A content key (collectors that send neither seq nor
external_id) is worked out from the redacted event, so not even a hash of the title is kept.

The rules are the built-in ones (data/redaction_rules.json, patterns checked here) and the user's choices (the
settings table, key "redaction"): built-in rules switched off, and rules of their own as plain words or phrases,
matched whole and ignoring case. Never regular expressions from the user, so no rule can hang the hub. A change
applies to the next event, whichever collector sends it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import resources
from time import monotonic
from typing import Any

from .db import Database
from .models import Event, Kind, has_hidden_characters

REDACTED = "[redacted]"
SETTINGS_KEY = "redaction"
MAX_CUSTOM_RULES = 20
MAX_WORDS = 50
WORD_LENGTH = (2, 100)
NAME_LENGTH = (1, 60)
TRACKER_REFRESH_SECONDS = 5.0  # how often the desktop tracker looks for changed rules
KEPT_CALENDAR_DATA = ("all_day",)


class RulesError(ValueError):
    """The user's rules can't be saved: the message says why."""


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    description: str
    builtin: bool
    words: tuple[str, ...] = ()  # the user's own rules: words or phrases
    titles: re.Pattern[str] | None = None
    apps: tuple[str, ...] = ()  # built in: pieces of an app name or id, lowercase
    names_too: bool = False  # the user's words also match an app's name or id (as whole words)

    def matches(self, title: str, app: str | None, app_id: str | None) -> bool:
        if self.titles is not None and self.titles.search(title):
            return True
        if self.names_too and self.titles is not None and any(self.titles.search(part) for part in (app, app_id) if part):
            return True
        named = " ".join(part for part in (app, app_id) if part).casefold()
        return bool(named) and any(piece in named for piece in self.apps)


def _word_pattern(words: Sequence[str]) -> re.Pattern[str]:
    """Whole words or phrases, ignoring case: "Acme" matches "Acme Corp" and "acme's plan", not "Acmeville"."""
    return re.compile("|".join(rf"(?<!\w){re.escape(word)}(?!\w)" for word in words), re.IGNORECASE)


@lru_cache(maxsize=1)
def builtin_rules() -> tuple[Rule, ...]:
    raw = json.loads(resources.files("daytrace_hub").joinpath("data", "redaction_rules.json").read_text(encoding="utf-8"))
    rules = []
    for item in raw["rules"]:
        titles = re.compile("|".join(f"(?:{pattern})" for pattern in item["titles"]), re.IGNORECASE) if item["titles"] else None
        apps = tuple(piece.casefold() for piece in item["apps"])
        rules.append(Rule(item["id"], item["name"], item["description"], True, titles=titles, apps=apps))
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("redaction_rules.json has a rule id twice")
    return tuple(rules)


@dataclass(frozen=True)
class Choices:
    """What the user changed: built-in rules switched off, and their own rules (name, words)."""

    disabled: tuple[str, ...] = ()
    custom: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def as_json(self) -> str:
        return json.dumps({"disabled": list(self.disabled), "custom": [{"name": name, "words": list(words)} for name, words in self.custom]},
                          sort_keys=True)


def check_choices(disabled: Sequence[str], custom: Sequence[tuple[str, Sequence[str]]]) -> Choices:
    """The user's rules, cleaned (spaces trimmed, repeated words dropped) and checked. RulesError says what is wrong."""
    known = {rule.id for rule in builtin_rules()}
    unknown = sorted(set(disabled) - known)
    if unknown:
        raise RulesError(f"no built-in rule {', '.join(unknown)}; the rules are {', '.join(sorted(known))}")
    if len(custom) > MAX_CUSTOM_RULES:
        raise RulesError(f"at most {MAX_CUSTOM_RULES} rules of your own")
    cleaned: list[tuple[str, tuple[str, ...]]] = []
    for name, words in custom:
        name = " ".join(name.split())
        if not NAME_LENGTH[0] <= len(name) <= NAME_LENGTH[1] or has_hidden_characters(name):
            raise RulesError(f"a rule's name is {NAME_LENGTH[0]} to {NAME_LENGTH[1]} plain characters")
        kept: list[str] = []
        for word in words:
            word = " ".join(word.split())
            if not WORD_LENGTH[0] <= len(word) <= WORD_LENGTH[1] or has_hidden_characters(word):
                raise RulesError(f"{name}: each word or phrase is {WORD_LENGTH[0]} to {WORD_LENGTH[1]} plain characters")
            if word.casefold() not in {kept_word.casefold() for kept_word in kept}:
                kept.append(word)
        if not kept:
            raise RulesError(f"{name}: add at least one word or phrase")
        if len(kept) > MAX_WORDS:
            raise RulesError(f"{name}: at most {MAX_WORDS} words or phrases")
        cleaned.append((name, tuple(kept)))
    return Choices(tuple(sorted(set(disabled))), tuple(cleaned))


def load_choices(conn: sqlite3.Connection) -> Choices:
    """The user's stored choices. A row that can't be read counts as none: every built-in rule on, the safe way."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    return _parse_choices(row[0] if row else None)


@lru_cache(maxsize=32)
def _parse_choices(text: str | None) -> Choices:
    if not text:
        return Choices()
    try:
        raw = json.loads(text)
        return check_choices(raw.get("disabled", []), [(item["name"], item["words"]) for item in raw.get("custom", [])])
    except (ValueError, TypeError, KeyError, AttributeError):
        return Choices()


def save_choices(conn: sqlite3.Connection, choices: Choices) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (SETTINGS_KEY, choices.as_json()))


class Redactor:
    """The rules in force: the built-in ones not switched off, then the user's."""

    def __init__(self, choices: Choices | None = None) -> None:
        choices = choices or Choices()
        self.choices = choices
        builtin = [rule for rule in builtin_rules() if rule.id not in choices.disabled]
        custom = [Rule(f"custom-{number}", name, f"Your words: {', '.join(words)}", False, words=words, titles=_word_pattern(words),
                       names_too=True)
                  for number, (name, words) in enumerate(choices.custom, start=1)]
        self.rules: tuple[Rule, ...] = (*builtin, *custom)

    def match(self, title: str | None, app: str | None = None, app_id: str | None = None) -> Rule | None:
        """The first rule a title matches. None for no title (nothing to hide) or one already redacted."""
        if not title or title == REDACTED:
            return None
        return next((rule for rule in self.rules if rule.matches(title, app, app_id)), None)

    def event(self, event: Event) -> Event:
        """The event as it may be stored: its title replaced when a rule matches (and a calendar event's extra data
        dropped), otherwise the event itself."""
        if self.match(event.title, event.app, event.app_id) is None:
            return event
        data = event.data
        if event.kind == Kind.CALENDAR_EVENT:
            data = {key: value for key, value in event.data.items() if key in KEPT_CALENDAR_DATA}
        return event.model_copy(update={"title": REDACTED, "data": data})

    def reading(self, reading: Any) -> Any:
        """A tracker reading (app, app_id, title) with its title redacted when a rule matches."""
        if self.match(reading.title, reading.app, reading.app_id) is None:
            return reading
        return replace(reading, title=REDACTED)


@lru_cache(maxsize=32)
def _redactor(choices: Choices) -> Redactor:
    return Redactor(choices)


def redactor_for(conn: sqlite3.Connection) -> Redactor:
    """The rules in force now (the choices are read each time; the compiled rules are reused while they stay)."""
    return _redactor(load_choices(conn))


class LiveRedactor:
    """The desktop tracker's redaction hook: the rules in force, looked up again every few seconds, so a rule the
    user adds applies within seconds without restarting the tracker (ingest applies it at once as well)."""

    def __init__(self, database: Database, refresh: float = TRACKER_REFRESH_SECONDS) -> None:
        self.database = database
        self.refresh = refresh
        self._lock = threading.Lock()
        self._redactor: Redactor = Redactor()
        self._checked: float | None = None

    def current(self) -> Redactor:
        with self._lock:
            if self._checked is None or monotonic() - self._checked >= self.refresh:
                try:
                    with self.database.connect() as conn:
                        self._redactor = redactor_for(conn)
                except sqlite3.Error:
                    pass  # busy or not there yet: keep the rules it has (every built-in rule, at the start)
                self._checked = monotonic()
            return self._redactor

    def __call__(self, reading: Any) -> Any:
        return self.current().reading(reading)
