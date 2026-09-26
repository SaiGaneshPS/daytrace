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
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Literal

from .db import utc_text
from .models import Category

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
