"""DT-14: app and website categories (AI fill-in: DT-42).

Every session gets exactly one category. For apps:
1. The user's override on the app id or name.
2. The category the collector sent (docs/event-schema.json: the hub's mapping is used when it is null).
3. The built-in list in data/categories.json, by app id (Android package, iOS/macOS bundle id, Windows exe),
   then by app name.
4. An override DT-42's local AI saved.
5. "other".

For websites the most specific domain wins: after a user override on the exact domain and the collector's
category, each suffix is tried from longest to shortest, and at each level a user override beats the built-in
list, which beats an AI guess. So a user setting google.com to study leaves mail.google.com as comms.

DT-42: apps and sites nothing above knows are sent in batches to the local model with the fixed category list,
and its answer is saved as an override with source "ai", so each is asked about once (an answer of "other"
counts). The user's own choice always wins, and an AI guess never replaces one. `AiCategorizer` does this in the
background: a while after new events arrive, and every 10 minutes.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal

import httpx

from .db import Database, transaction, utc_text
from .models import Category

if TYPE_CHECKING:
    from .llm import LLM

log = logging.getLogger("daytrace.categories")

CATEGORIES: tuple[str, ...] = tuple(category.value for category in Category)
CategorySource = Literal["user", "ai", "builtin", "collector", "default"]
_PORT = re.compile(r"^(?P<host>[^:]+):\d+$")


def canonical_key(text: str) -> str:
    """How app names, ids and domains are compared everywhere (lookups, overrides, the API).

    Unicode-normalized and case-folded, trimmed, without a trailing dot, `.exe`, `www.` or a port.
    """
    key = unicodedata.normalize("NFKC", text).strip().casefold().rstrip(".")
    key = key.removesuffix(".exe").removeprefix("www.")
    match = _PORT.match(key)
    return match["host"] if match else key


def domain_suffixes(domain: str) -> list[str]:
    """m.youtube.com -> [m.youtube.com, youtube.com] (never the bare top-level domain)."""
    parts = canonical_key(domain).split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] or [".".join(parts)]


@dataclass(frozen=True)
class Builtin:
    names: dict[str, str]
    ids: dict[str, str]
    domains: dict[str, str]

    @property
    def size(self) -> int:
        return len(self.names) + len(self.ids) + len(self.domains)


def parse_builtin(data: dict[str, object]) -> Builtin:
    """Read the built-in list; a key listed under two categories is an error, not a silent coin flip."""
    tables: dict[str, dict[str, str]] = {"apps": {}, "ids": {}, "domains": {}}
    for category, groups in data.items():
        if category.startswith("$"):
            continue
        if category not in CATEGORIES:
            raise ValueError(f"categories.json: unknown category {category!r}")
        assert isinstance(groups, dict)
        for group, entries in groups.items():
            if group not in tables:
                raise ValueError(f"categories.json: {category}.{group} must be apps, ids or domains")
            for entry in entries:
                key = canonical_key(entry)
                if tables[group].get(key, category) != category:
                    raise ValueError(f"categories.json: {entry!r} is listed as {tables[group][key]} and {category}")
                tables[group][key] = category
    return Builtin(names=tables["apps"], ids=tables["ids"], domains=tables["domains"])


@lru_cache(maxsize=1)
def load_builtin() -> Builtin:
    text = resources.files("daytrace_hub").joinpath("data", "categories.json").read_text(encoding="utf-8")
    return parse_builtin(json.loads(text))


@dataclass(frozen=True)
class Override:
    category: str
    source: Literal["user", "ai"]
    updated_at: str


@dataclass(frozen=True)
class Lookup:
    category: str
    source: CategorySource
    override_key: str | None = None  # the override that decided, so the dashboard can remove it


def load_overrides(conn: sqlite3.Connection) -> dict[str, Override]:
    rows = conn.execute("SELECT app_key, category, source, updated_at FROM category_overrides")
    return {row["app_key"]: Override(row["category"], row["source"], row["updated_at"]) for row in rows}


class Categorizer:
    """Looks up categories with one set of overrides (load it once per request)."""

    def __init__(self, overrides: dict[str, Override] | None = None, builtin: Builtin | None = None) -> None:
        overrides = overrides or {}
        self.user = {key: o for key, o in overrides.items() if o.source == "user"}
        self.ai = {key: o for key, o in overrides.items() if o.source == "ai"}
        self.builtin = builtin or load_builtin()

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> Categorizer:
        return cls(load_overrides(conn))

    def lookup(
        self, app: str | None, app_id: str | None = None, kind: str = "app", collector: str | None = None
    ) -> Lookup:
        hint = collector if collector in CATEGORIES else None
        if kind == "web":
            return self._lookup_web(app, hint)
        keys = [key for key in (canonical_key(app_id) if app_id else None, canonical_key(app) if app else None) if key]
        for key in keys:
            if key in self.user:
                return Lookup(self.user[key].category, "user", key)
        if hint is not None:
            return Lookup(hint, "collector")
        for key in keys:  # Windows exe names arrive as app_id but read like names, so check both tables
            for table in (self.builtin.ids, self.builtin.names):
                if key in table:
                    return Lookup(table[key], "builtin")
        for key in keys:
            if key in self.ai:
                return Lookup(self.ai[key].category, "ai", key)
        return Lookup("other", "default")

    def _lookup_web(self, domain: str | None, hint: str | None) -> Lookup:
        suffixes = domain_suffixes(domain) if domain else []
        if suffixes and suffixes[0] in self.user:
            return Lookup(self.user[suffixes[0]].category, "user", suffixes[0])
        if hint is not None:
            return Lookup(hint, "collector")
        for key in suffixes:  # the most specific domain wins
            if key in self.user:
                return Lookup(self.user[key].category, "user", key)
            if key in self.builtin.domains:
                return Lookup(self.builtin.domains[key], "builtin")
            if key in self.ai:
                return Lookup(self.ai[key].category, "ai", key)
        return Lookup("other", "default")

    def category(self, app: str | None, app_id: str | None = None, kind: str = "app", collector: str | None = None) -> str:
        return self.lookup(app, app_id, kind, collector).category


def override_key(app: str | None, app_id: str | None, kind: str) -> str | None:
    """The key a new override for this app is saved under: the domain for web, else the id, else the name."""
    if kind == "web":
        return canonical_key(app) if app else None
    for text in (app_id, app):
        if text and canonical_key(text):
            return canonical_key(text)
    return None


def save_override(conn: sqlite3.Connection, key: str, category: str, source: Literal["user", "ai"]) -> Override:
    """Store an override. An AI guess never replaces a choice the user made."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    now = utc_text(datetime.now(UTC))
    conn.execute(
        "INSERT INTO category_overrides (app_key, category, source, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (app_key) DO UPDATE SET category = excluded.category, source = excluded.source,"
        " updated_at = excluded.updated_at WHERE excluded.source = 'user' OR category_overrides.source = 'ai'",
        (key, category, source, now),
    )
    row = conn.execute(
        "SELECT category, source, updated_at FROM category_overrides WHERE app_key = ?", (key,)
    ).fetchone()
    return Override(row["category"], row["source"], row["updated_at"])


def delete_override(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("DELETE FROM category_overrides WHERE app_key = ?", (key,)).rowcount > 0


# --- DT-42: the local model fills in unknown apps -----------------------------------------------------------------

AI_BATCH = 10  # apps per question: a few seconds for a small model, so a meal or a question never waits long behind it
AI_BATCHES_PER_RUN = 8
AI_WINDOW_DAYS = 92  # the longest Insights range: an app not used since doesn't matter to any chart
AI_SKIP_SECONDS = 6 * 3600.0  # apps the model answered badly about are asked about again after this
AI_MAX_TOKENS = 2048
AI_TIMEOUT = httpx.Timeout(connect=3.0, read=120.0, write=10.0, pool=5.0)
DECLINED_KEY = "ai_categories"  # settings row: {"declined": [keys whose guess the user removed]}
MAX_DECLINED = 5000
# What each category means, as the built-in list uses it (data/categories.json).
AI_MEANINGS = {
    "social": "social media and feeds (Instagram, Reddit, X, LinkedIn)",
    "video": "watching video or streams, and video players (YouTube, Netflix, Twitch, VLC)",
    "work": "work tools: code editors, office documents, spreadsheets, design, project tools",
    "study": "learning: courses, flashcards, school apps, language apps (Duolingo, Anki, Canvas)",
    "comms": "chat, messages, calls and email (WhatsApp, Discord, Gmail, Outlook, Slack, Zoom)",
    "games": "games, game launchers and game stores (Minecraft, Steam)",
    "health": "fitness, sleep, meditation and health tracking (Strava, Calm)",
    "other": ("anything else: web browsers, music, shopping, news, maps, files, settings and system tools, "
              "and anything you don't recognise"),
}
AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "apps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "category": {"type": "string", "enum": list(CATEGORIES)}},
                "required": ["n", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["apps"],
    "additionalProperties": False,
}
_AI_SYSTEM = (
    "You sort apps and websites into categories for a screen-time tracker. The categories:\n"
    + "\n".join(f"- {name}: {meaning}" for name, meaning in AI_MEANINGS.items())
    + '\nAnswer with JSON only: {"apps": [{"n": <its number>, "category": <one category>}]}, one entry for every item,'
    " using only the category names above. Browsers are other, whatever they show (the website is sorted on its own)."
    " If you don't know an item, answer other rather than guessing."
)
# App and site events of the last AI_WINDOW_DAYS with neither a category from their collector nor one of their own
# yet (events_by_start keeps it to that window, however long the history).
_UNKNOWN_SQL = (
    "SELECT kind = 'web' AS web, CASE WHEN kind = 'web' THEN json_extract(data, '$.domain') ELSE app END AS name,"
    " app_id, SUM(COALESCE(julianday(end_utc) - julianday(start_utc), 0)) AS used, COUNT(*) AS seen"
    " FROM events WHERE start_utc >= ? AND category IS NULL"
    " AND kind IN ('app_session', 'app_open', 'app_close', 'window', 'web')"
    " GROUP BY web, name, app_id ORDER BY used DESC, seen DESC"
)


@dataclass(frozen=True)
class UnknownApp:
    key: str  # the override its category is saved under
    web: bool
    name: str | None
    app_id: str | None


def load_declined(conn: sqlite3.Connection) -> set[str]:
    """Keys whose AI guess the user removed: never guessed again (a choice of their own still can be made)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (DECLINED_KEY,)).fetchone()
    try:
        keys = json.loads(row[0]).get("declined", []) if row else []
    except (ValueError, AttributeError):
        return set()
    return {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else set()


def decline(conn: sqlite3.Connection, key: str) -> None:
    """Remember that the user took the model's guess for `key` off, so it isn't guessed again."""
    keys = sorted(load_declined(conn) | {key})[-MAX_DECLINED:]
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (DECLINED_KEY, json.dumps({"declined": keys}, ensure_ascii=False)))


def unknown_apps(conn: sqlite3.Connection, limit: int | None = None, now: datetime | None = None) -> list[UnknownApp]:
    """Apps and sites of the last AI_WINDOW_DAYS nothing knows yet (neither the user, the collector, the built-in
    list nor the model), the most used first. Never one the privacy rules hide (DT-44): its name stays out of the
    question and out of the table. Nor one whose guess the user removed."""
    from .redaction import REDACTED, redactor_for  # redaction imports sessions, which imports this module

    categorizer = Categorizer.from_db(conn)
    rules = redactor_for(conn)
    declined = load_declined(conn)
    since = utc_text((now or datetime.now(UTC)) - timedelta(days=AI_WINDOW_DAYS))
    found: dict[str, UnknownApp] = {}
    for row in conn.execute(_UNKNOWN_SQL, (since,)):
        web, name, app_id = bool(row["web"]), row["name"], row["app_id"]
        name = name if isinstance(name, str) and name.strip() else None
        if REDACTED in (name, app_id) or (web and name is None) or rules.hides_name(name, app_id, web=web):
            continue
        kind = "web" if web else "app"
        if categorizer.lookup(name, None if web else app_id, kind).source != "default":
            continue
        key = override_key(name, None if web else app_id, kind)
        if key is None or key in found or key in declined:
            continue
        found[key] = UnknownApp(key=key, web=web, name=name, app_id=None if web else app_id)
        if limit is not None and len(found) >= limit:
            break
    return list(found.values())


def _ai_items(apps: list[UnknownApp]) -> str:
    items: list[dict[str, Any]] = []
    for n, app in enumerate(apps, start=1):
        item: dict[str, Any] = {"n": n}
        if app.web:
            item["website"] = app.name
        else:
            if app.name:
                item["app"] = app.name
            if app.app_id and canonical_key(app.app_id) != canonical_key(app.name or ""):
                item["id"] = app.app_id
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def ask_categories(llm: LLM, apps: list[UnknownApp]) -> dict[str, str]:
    """The local model's category for each app, by override key. An app it skipped is "other" (asked about once);
    a reply that answers fewer than half of them is no answer at all (LLMBadAnswer), so nothing is saved from it."""
    from .llm import LLMBadAnswer

    messages = [{"role": "system", "content": _AI_SYSTEM}, {"role": "user", "content": _ai_items(apps)}]
    reply = llm.json_reply(messages, AI_SCHEMA, "app_categories", max_tokens=AI_MAX_TOKENS, timeout=AI_TIMEOUT)
    entries = reply.get("apps") if isinstance(reply, dict) else None
    answers: dict[str, str] = {}
    for entry in entries if isinstance(entries, list) else []:
        n = entry.get("n") if isinstance(entry, dict) else None
        category = entry.get("category") if isinstance(entry, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(apps) and category in CATEGORIES:
            answers.setdefault(apps[n - 1].key, category)
    if 2 * len(answers) < len(apps):
        raise LLMBadAnswer(f"the model answered for {len(answers)} of {len(apps)} apps")
    return {app.key: answers.get(app.key, "other") for app in apps}


def categorize_unknown(
    database: Database,
    llm: LLM,
    batches: int = AI_BATCHES_PER_RUN,
    batch: int = AI_BATCH,
    skip: dict[str, float] | None = None,
    stopped: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Ask the local model about apps nothing knows, a batch at a time, and save its answers (source "ai"). Returns
    how many were saved.

    - Before saving, each app is checked again in the same transaction: still seen, still unknown, still visible
      under the privacy rules. So a delete-all, a new rule or the user's own choice made while the model thought
      wins, and nothing it read before is written back.
    - A batch the model answers badly (cut off, too little) is put in `skip` for AI_SKIP_SECONDS, and the next
      batch is asked; it never blocks the others.
    - `stopped` is asked between batches and before saving (the hub is shutting down). A run also ends, to be tried
      later, when the model is busy answering someone else (a question, a meal).
    Raises LLMError when the model can't be used; what was saved before stays."""
    from .llm import LLMBadAnswer

    skip = {} if skip is None else skip
    saved = 0
    for key, until in list(skip.items()):
        if until <= clock():
            del skip[key]
    with database.connect() as conn:
        waiting = [app for app in unknown_apps(conn, limit=batch * batches + len(skip)) if app.key not in skip]
    for first in range(0, min(len(waiting), batch * batches), batch):
        if stopped() or llm.busy():
            break
        apps = waiting[first:first + batch]
        try:
            answers = ask_categories(llm, apps)
        except LLMBadAnswer as exc:
            log.info("The model answered badly about %d apps (%s); they are asked about again later", len(apps), exc)
            skip.update({app.key: clock() + AI_SKIP_SECONDS for app in apps})
            continue
        if stopped():
            break
        with database.connect() as conn, transaction(conn):
            still = {app.key for app in unknown_apps(conn)}
            for key, category in answers.items():
                if key in still:
                    save_override(conn, key, category, "ai")
                    saved += 1
    return saved


class AiCategorizer:
    """Runs categorize_unknown() in a background thread: `delay` seconds after wake() (new events arrived, so a burst
    of them is one run), and every `interval` seconds (the desktop tracker writes without waking it). A run is
    skipped when nothing changed since the last one found no more work. A model that isn't there is tried again at
    the next run; the hub never waits for this."""

    def __init__(self, database: Database, llm: LLM, interval: float = 600.0, delay: float = 30.0) -> None:
        self.database = database
        self.llm = llm
        self.interval = interval
        self.delay = delay
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._skip: dict[str, float] = {}
        self._done_at: int | None = None  # the data version when a run last found nothing left to ask
        self.runs = 0  # finished runs, for tests

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="daytrace-ai-categories", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def forget(self) -> None:
        """After delete-all (DT-46): nothing about the old apps stays in memory, and the next run looks afresh."""
        self._skip.clear()
        self._done_at = None

    def run_once(self) -> int:
        from .db import data_version
        from .llm import LLMError

        try:
            with self.database.connect() as conn:
                version = data_version(conn)
            if version == self._done_at:
                return 0
            saved = categorize_unknown(self.database, self.llm, skip=self._skip, stopped=self._stop.is_set)
            with self.database.connect() as conn:
                if not [app for app in unknown_apps(conn, limit=len(self._skip) + 1) if app.key not in self._skip]:
                    self._done_at = data_version(conn)  # nothing left to ask until something changes
            return saved
        except LLMError as exc:
            if not self._stop.is_set():
                log.info("Apps not sorted into categories yet: %s", exc)
        except Exception:  # never let the thread die: the next run tries again
            if not self._stop.is_set():  # a client closed at shutdown is expected
                log.exception("Sorting apps into categories failed")
        return 0

    def _run(self) -> None:
        wait = self.delay  # a first look soon after the start: what arrived while the hub was off
        while not self._stop.is_set():
            woken = self._wake.wait(timeout=wait)
            if self._stop.is_set():
                break
            if woken:
                self._wake.clear()
                if self._stop.wait(self.delay):
                    break
            self.run_once()
            self.runs += 1
            wait = self.interval
