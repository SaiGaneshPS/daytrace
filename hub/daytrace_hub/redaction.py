"""DT-44: sensitive window and calendar titles are never stored.

A title that matches a redaction rule is stored as "[redacted]": banking, health portals and appointments, password
managers, private and incognito windows, or words the user added. The app name and the times stay, so the time
still counts on the timeline and in every total; only the words are gone. A redacted calendar event keeps whether
it is all-day, and nothing else a collector sent with it (a location, notes). A browser extension's site (web
events carry no title) that matches a rule is stored as "[redacted]" too. The user's own words are hidden wherever
they appear: in a title, an app's name or id, or a site.

Redaction happens twice: in the desktop tracker (tracker/base.py), so a sensitive title is never even held in a
span, and at ingest (api/events.store_events), so every collector and the seed are covered. A resent event keeps
its identity (store_events): an event stored before a rule existed, sent again redacted, is the same event and its
stored copy is redacted then too, never a conflict or a second copy. A content key (collectors that send neither
seq nor external_id) is worked out from the redacted event, so not even a hash of the title is kept; two redacted
events that match in everything else (kind, times, app) then count as one.

Events stored before a rule was added keep their words until the user applies the rules to them
(stored_matches(apply=True), behind a confirmation on the Privacy page): hiding history can't be undone.

The rules are the built-in ones (data/redaction_rules.json, vetted patterns) and the user's choices (the settings
table, key "redaction"): built-in rules switched off, and rules of their own as plain words or phrases. The user's
words are never regular expressions: titles, names and sites are split into runs of letters and of digits, and a
word matches the same run (or runs, for a phrase) ignoring case, so "Acme" hides "acme_notes.docx" and "Acme2026"
but not "Acmeville", and "Project Falcon" hides "Project_Falcon". A change applies to the next event.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from time import monotonic
from typing import Any

from .db import Database, transaction, utc_text
from .models import Event, content_key, has_hidden_characters
from .sessions import parse_utc

REDACTED = "[redacted]"
SETTINGS_KEY = "redaction"
MAX_CUSTOM_RULES = 20
MAX_WORDS = 50
WORD_LENGTH = (2, 100)
NAME_LENGTH = (1, 60)
TRACKER_REFRESH_SECONDS = 5.0  # how often the desktop tracker looks for changed rules
KEPT_CALENDAR_DATA = ("all_day",)
APPLY_BATCH = 500  # stored events redacted per transaction, so the write lock is never held for long
# The runs a title, name or site is split into: letters, or digits. A word is a run (or runs, for a phrase).
TOKEN = re.compile(r"[^\W\d_]+|\d+")


class RulesError(ValueError):
    """The user's rules can't be saved: the message says why."""


def tokens(text: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in TOKEN.findall(text))


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    description: str
    builtin: bool
    words: tuple[str, ...] = ()  # the user's own rules: words or phrases
    titles: re.Pattern[str] | None = None  # built in: matched in titles and sites
    apps: tuple[str, ...] = ()  # built in: pieces of an app name or id, or of a site, lowercase

    @property
    def phrases(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tokens(word) for word in self.words)


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


# --- the user's choices -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Choices:
    """What the user changed: built-in rules switched off, and their own rules (name, words)."""

    disabled: tuple[str, ...] = ()
    custom: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def as_json(self) -> str:
        return json.dumps({"disabled": list(self.disabled), "custom": [{"name": name, "words": list(words)} for name, words in self.custom]},
                          sort_keys=True)


def _clean_words(words: Iterable[str]) -> list[str]:
    """Spaces tidied, and a word already there (ignoring case, as matching does) dropped."""
    kept: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for word in words:
        word = " ".join(word.split())
        if tokens(word) not in seen:
            seen.add(tokens(word))
            kept.append(word)
    return kept


def check_choices(disabled: Sequence[str], custom: Sequence[tuple[str, Sequence[str]]]) -> Choices:
    """The user's new rules, cleaned and checked (sizes first, so a huge request is refused at once). RulesError
    says what is wrong."""
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
        if len(words) > MAX_WORDS:
            raise RulesError(f"{name}: at most {MAX_WORDS} words or phrases")
        for word in words:
            word = " ".join(word.split())
            if not WORD_LENGTH[0] <= len(word) <= WORD_LENGTH[1] or has_hidden_characters(word):
                raise RulesError(f"{name}: each word or phrase is {WORD_LENGTH[0]} to {WORD_LENGTH[1]} plain characters")
            if not tokens(word):
                raise RulesError(f"{name}: {word!r} has no letters or digits to look for")
        kept = _clean_words(words)
        if not kept:
            raise RulesError(f"{name}: add at least one word or phrase")
        cleaned.append((name, tuple(kept)))
    return Choices(tuple(sorted(set(disabled))), tuple(cleaned))


def load_choices(conn: sqlite3.Connection) -> Choices:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    return _parse_choices(row[0] if row else None)


@lru_cache(maxsize=32)
def _parse_choices(text: str | None) -> Choices:
    """The stored choices, read leniently: a built-in rule that no longer exists is skipped, and the user's words
    are kept even if today's limits are smaller than when they were saved. Only a row that can't be read at all
    counts as no choices (every built-in rule on)."""
    if not text:
        return Choices()
    try:
        raw = json.loads(text)
    except ValueError:
        return Choices()
    if not isinstance(raw, dict):
        return Choices()
    known = {rule.id for rule in builtin_rules()}
    disabled = raw.get("disabled") if isinstance(raw.get("disabled"), list) else []
    custom: list[tuple[str, tuple[str, ...]]] = []
    for item in raw.get("custom") if isinstance(raw.get("custom"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("words"), list):
            continue
        words = _clean_words(word for word in item["words"] if isinstance(word, str) and tokens(word))
        name = item.get("name") if isinstance(item.get("name"), str) and item["name"].strip() else "Your words"
        if words:
            custom.append((" ".join(name.split()), tuple(words)))
    return Choices(tuple(sorted({rule for rule in disabled if isinstance(rule, str) and rule in known})), tuple(custom))


def save_choices(conn: sqlite3.Connection, choices: Choices) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (SETTINGS_KEY, choices.as_json()))


# --- matching -----------------------------------------------------------------------------------------------------


Redacted = tuple[str | None, str | None, str | None, dict[str, Any]]  # title, app, app_id, data


class Redactor:
    """The rules in force: the built-in ones not switched off, then the user's. Checking a title is one regex search
    (every built-in pattern at once) and one pass over its word runs (every one of the user's words at once)."""

    def __init__(self, choices: Choices | None = None) -> None:
        choices = choices or Choices()
        self.choices = choices
        self.builtin = tuple(rule for rule in builtin_rules() if rule.id not in choices.disabled)
        self.custom = tuple(Rule(f"custom-{number}", name, f"Your words: {', '.join(words)}", False, words=words)
                            for number, (name, words) in enumerate(choices.custom, start=1))
        self.rules = (*self.builtin, *self.custom)
        patterns = [rule.titles.pattern for rule in self.builtin if rule.titles is not None]
        self._titles = re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE) if patterns else None
        self._pieces = tuple(piece for rule in self.builtin for piece in rule.apps)
        self._phrases: dict[str, list[tuple[tuple[str, ...], Rule]]] = defaultdict(list)
        for rule in self.custom:
            for phrase in rule.phrases:
                self._phrases[phrase[0]].append((phrase, rule))

    # -- which rule, if any -------------------------------------------------------------------------------------

    def _builtin_text(self, text: str) -> Rule | None:
        if self._titles is None or not self._titles.search(text):
            return None
        return next(rule for rule in self.builtin if rule.titles is not None and rule.titles.search(text))

    def _builtin_names(self, *names: str | None) -> Rule | None:
        named = " ".join(name for name in names if name).casefold()
        if not named or not any(piece in named for piece in self._pieces):
            return None
        return next(rule for rule in self.builtin if any(piece in named for piece in rule.apps))

    def _custom(self, *texts: str | None) -> Rule | None:
        for text in texts:
            if not text:
                continue
            runs = tokens(text)
            for index, token in enumerate(runs):
                for phrase, rule in self._phrases.get(token, ()):
                    if runs[index:index + len(phrase)] == phrase:
                        return rule
        return None

    def match(self, title: str | None, app: str | None = None, app_id: str | None = None, domain: str | None = None) -> Rule | None:
        """The rule that hides this title (or site). None when nothing matches, or there is nothing left to hide."""
        if title and title != REDACTED:
            found = self._builtin_text(title) or self._builtin_names(app, app_id) or self._custom(title, app, app_id)
            if found:
                return found
        if domain and domain != REDACTED:
            return self._builtin_text(domain) or self._builtin_names(domain) or self._custom(domain)
        return None

    # -- redacting ----------------------------------------------------------------------------------------------

    def redact(self, kind: str, title: str | None, app: str | None, app_id: str | None, data: dict[str, Any]) -> Redacted | None:
        """What to store instead, or None when nothing changes.

        - A title that matches (or belongs to a matching app) becomes [redacted].
        - The user's words in an app's name or id hide that name and id too: the word is what they asked to hide.
        - A web event's site that matches becomes [redacted].
        - A redacted calendar event (even one sent already redacted) keeps only whether it is all-day."""
        hide_title = bool(title) and title != REDACTED and self.match(title, app, app_id) is not None
        hide_names = hide_title and self._custom(app, app_id) is not None
        domain = data.get("domain") if kind == "web" else None
        hide_domain = isinstance(domain, str) and self.match(None, domain=domain) is not None
        calendar = kind == "calendar_event" and (hide_title or title == REDACTED)
        trim = calendar and any(key not in KEPT_CALENDAR_DATA for key in data)
        if not (hide_title or hide_domain or trim):
            return None
        new_data = dict(data)
        if calendar:
            new_data = {key: value for key, value in data.items() if key in KEPT_CALENDAR_DATA}
        if hide_domain:
            new_data["domain"] = REDACTED
        return (REDACTED if hide_title else title,
                REDACTED if hide_names and app else app,
                REDACTED if hide_names and app_id else app_id,
                new_data)

    def event(self, event: Event) -> Event:
        """The event as it may be stored: the event itself when nothing matches."""
        changed = self.redact(event.kind.value, event.title, event.app, event.app_id, event.data)
        if changed is None:
            return event
        title, app, app_id, data = changed
        return event.model_copy(update={"title": title, "app": app, "app_id": app_id, "data": data})

    def reading(self, reading: Any) -> Any:
        """A tracker reading (app, app_id, title) as it may be held: title, and the user's words in names, hidden."""
        changed = self.redact("window", reading.title, reading.app, reading.app_id, {})
        if changed is None:
            return reading
        title, app, app_id, _ = changed
        return replace(reading, title=title, app=app, app_id=app_id)


@lru_cache(maxsize=32)
def _redactor(choices: Choices) -> Redactor:
    return Redactor(choices)


def redactor_for(conn: sqlite3.Connection) -> Redactor:
    """The rules in force now (the choices are read each time; the compiled rules are reused while they stay)."""
    return _redactor(load_choices(conn))


def redacted_forms(event: Event) -> list[str]:
    """The keys an event from a stateless collector could have been stored under while a rule, since switched off,
    hid it: its title alone, its title and names, or its site. store_events checks them so a resend after the
    change is still one event."""
    forms = []
    if event.title and event.title != REDACTED:
        data = {key: value for key, value in event.data.items() if key in KEPT_CALENDAR_DATA} if event.kind.value == "calendar_event" else event.data
        forms.append((REDACTED, event.app, event.app_id, data))
        if event.app or event.app_id:
            forms.append((REDACTED, REDACTED if event.app else None, REDACTED if event.app_id else None, data))
    if event.kind.value == "web" and event.data.get("domain") not in (None, REDACTED):
        forms.append((event.title, event.app, event.app_id, {**event.data, "domain": REDACTED}))
    return [content_key(event.kind.value, event.start, event.end, app, app_id, title, data) for title, app, app_id, data in forms]


# --- stored events ------------------------------------------------------------------------------------------------


def stored_matches(database: Database, redactor: Redactor | None = None, apply: bool = False, now: datetime | None = None) -> int:
    """How many stored events the rules in force would change; with `apply`, change them (that can't be undone).
    A content key is worked out again from the redacted event; an event that then matches another stored one in
    everything is the same event, and only one copy is kept. Done a batch at a time, each in its own transaction.
    Applying also hides the words of logged nudges (DT-43) that the rules would hide, as they quote app names and
    calendar titles."""
    count, last = 0, 0
    now_text = utc_text(now or datetime.now(UTC))
    while True:
        with database.connect() as conn:
            rules = redactor or redactor_for(conn)
            rows = conn.execute("SELECT id, device_id, dedup_key, kind, start_utc, end_utc, app, app_id, title, data FROM events"
                                " WHERE id > ? ORDER BY id LIMIT ?", (last, APPLY_BATCH)).fetchall()
            if not rows:
                if apply:
                    _redact_nudges(conn, rules)
                return count
            last = rows[-1]["id"]
            changes = []
            for row in rows:
                data = json.loads(row["data"])
                changed = rules.redact(row["kind"], row["title"], row["app"], row["app_id"], data if isinstance(data, dict) else {})
                if changed is not None:
                    changes.append((row, changed))
            count += len(changes)
            if not apply or not changes:
                continue
            with transaction(conn):
                for row, (title, app, app_id, data) in changes:
                    key = row["dedup_key"]
                    if key.startswith("content:"):
                        key = content_key(row["kind"], parse_utc(row["start_utc"]), parse_utc(row["end_utc"]) if row["end_utc"] else None,
                                          app, app_id, title, data)
                    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    try:
                        conn.execute("UPDATE events SET title = ?, app = ?, app_id = ?, data = ?, dedup_key = ?, updated_at = ? WHERE id = ?",
                                     (title, app, app_id, text, key, now_text, row["id"]))
                    except sqlite3.IntegrityError:  # redacted, it is an event already stored: one copy is kept
                        conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))


def _redact_nudges(conn: sqlite3.Connection, rules: Redactor) -> None:
    """A logged nudge whose words a rule would hide (its title or body read as a title) keeps only its rule."""
    hidden = [row["id"] for row in conn.execute("SELECT id, title, body FROM nudge_log")
              if any(rules.redact("window", text, None, None, {}) is not None for text in (row["title"], row["body"]))]
    if hidden:
        with transaction(conn):
            conn.executemany("UPDATE nudge_log SET title = ?, body = ? WHERE id = ?", [(REDACTED, REDACTED, id_) for id_ in hidden])


# --- the desktop tracker ------------------------------------------------------------------------------------------


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
